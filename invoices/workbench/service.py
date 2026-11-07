"""Review workbench state and rules, persisted in SQLite.

Source documents are never modified. Corrections, version selection,
assignments, sign-offs and mock payments are review state layered on top of the
parsed originals, and the existing deterministic `evaluate` is re-run on the
current (selected + corrected) invoice every time a case is read or changed.

Concurrency: every mutation runs in one `BEGIN IMMEDIATE` transaction and must
present the case revision (and evidence hash) the browser rendered. A stale
submission is refused, so two tabs cannot silently overwrite or approve stale
data. Sign-offs are bound to an evidence hash covering policy version, the
documents, the corrected invoice, the validation result and inventory levels.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from pypdf.errors import PdfReadError

from ..core import (Invoice, Line, economic_content, evaluate, invoice_key, item_key,
                    load_invoice, open_database, vendor_key)
from .policy import (ACTORS, CORRECTION_PARTIES, GATE_REASONS, PERMISSIONS, POLICY_VERSION,
                     SYSTEM, TEAMS, order_reasons, reason_label, reason_team)

SUPPORTED = {'.json', '.csv', '.xml', '.txt', '.pdf'}
PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, InvalidOperation,
                ET.ParseError, PdfReadError)
CENT = Decimal('.01')
MAX_TEXT = 2000


class WorkbenchError(Exception):
    status = 400


class NotFound(WorkbenchError):
    status = 404


class Forbidden(WorkbenchError):
    status = 403


class Stale(WorkbenchError):
    status = 409


class Invalid(WorkbenchError):
    status = 422


class Refused(WorkbenchError):
    """A business rule blocks the action; nothing was changed."""
    status = 422


# ---------------------------------------------------------------- invoice <-> JSON

def _dec(value):
    return None if value is None else format(value, 'f')


def inv_to_dict(inv):
    return {'source': inv.source, 'number': inv.number, 'vendor': inv.vendor, 'date': inv.date,
            'due_date': inv.due_date, 'currency': inv.currency, 'revision': inv.revision,
            'notes': inv.notes, 'subtotal': _dec(inv.subtotal), 'tax': _dec(inv.tax),
            'tax_rate': _dec(inv.tax_rate), 'shipping': _dec(inv.shipping), 'total': _dec(inv.total),
            'corrections': list(inv.corrections),
            'lines': [{'item': l.item, 'quantity': _dec(l.quantity),
                       'unit_price': _dec(l.unit_price), 'amount': _dec(l.amount)}
                      for l in inv.lines]}


def inv_from_dict(x):
    def D(v):
        return None if v is None else Decimal(v)
    lines = [Line(l['item'], D(l['quantity']), D(l['unit_price']), D(l['amount'])) for l in x['lines']]
    return Invoice(x['source'], x['number'], x['vendor'], x['date'], x['due_date'], lines,
                   D(x['total']), D(x['subtotal']), D(x['tax']), D(x['tax_rate']), D(x['shipping']),
                   x['currency'], x['revision'], x['notes'], list(x['corrections']))


# ---------------------------------------------------------------- editable fields

HEADER_FIELDS = [('vendor', 'Vendor', 'text'), ('number', 'Invoice number', 'text'),
                 ('revision', 'Revision', 'text'), ('date', 'Invoice date', 'date'),
                 ('due_date', 'Due date', 'date'), ('currency', 'Currency', 'currency'),
                 ('subtotal', 'Subtotal', 'optional'), ('tax_rate', 'Tax rate (0-1)', 'optional'),
                 ('tax', 'Tax amount', 'zero'), ('shipping', 'Shipping', 'zero'),
                 ('total', 'Total', 'optional')]
LINE_FIELDS = [('item', 'Item', 'text'), ('quantity', 'Qty', 'required'),
               ('unit_price', 'Unit price', 'required'), ('amount', 'Stated amount', 'optional')]
HEADER_KIND = {f: k for f, _, k in HEADER_FIELDS}
HEADER_LABEL = {f: l for f, l, _ in HEADER_FIELDS}
LINE_KIND = {f: k for f, _, k in LINE_FIELDS}
LINE_LABEL = {f: l for f, l, _ in LINE_FIELDS}
NUMERIC_KINDS = {'optional', 'zero', 'required'}
# `notes` is intentionally not editable: it carries the vendor-change evidence,
# and no vendor registry exists to justify clearing that flag.


def field_ids(inv):
    ids = [f for f, _, _ in HEADER_FIELDS]
    for i in range(len(inv.lines)):
        ids += [f'line.{i}.{name}' for name, _, _ in LINE_FIELDS]
    return ids


def field_label(fid):
    if fid.startswith('line.'):
        _, i, name = fid.split('.')
        return f'Line {int(i) + 1} {LINE_LABEL[name].lower()}'
    return HEADER_LABEL[fid]


def _field_meta(inv, fid):
    if fid.startswith('line.'):
        parts = fid.split('.')
        if len(parts) != 3 or parts[2] not in LINE_KIND or not parts[1].isdigit() \
                or int(parts[1]) >= len(inv.lines):
            raise Invalid(f'Unknown field {fid!r}')
        return LINE_KIND[parts[2]], int(parts[1]), parts[2]
    if fid not in HEADER_KIND:
        raise Invalid(f'Unknown field {fid!r}')
    return HEADER_KIND[fid], None, fid


def field_value(inv, fid):
    kind, index, name = _field_meta(inv, fid)
    value = getattr(inv.lines[index] if index is not None else inv, name)
    if value is None:
        return ''
    return format(value, 'f') if isinstance(value, Decimal) else str(value)


def set_field(inv, fid, text):
    kind, index, name = _field_meta(inv, fid)
    text = (text or '').strip()
    if len(text) > MAX_TEXT:
        raise Invalid(f'{field_label(fid)} is too long')
    if kind in NUMERIC_KINDS:
        if text == '':
            if kind == 'optional':
                value = None
            elif kind == 'zero':
                value = Decimal(0)
            else:
                raise Invalid(f'{field_label(fid)} is required')
        else:
            try:
                value = Decimal(text.replace(',', '').replace('$', ''))
            except InvalidOperation:
                raise Invalid(f'{field_label(fid)}: {text!r} is not a number') from None
            if not value.is_finite():
                raise Invalid(f'{field_label(fid)}: {text!r} is not a finite number')
            if abs(value) > Decimal('1e12') or value.as_tuple().exponent < -12:
                raise Invalid(f'{field_label(fid)}: use at most 12 fractional digits and magnitude <= 1 trillion')
    elif kind == 'currency':
        if not text:
            raise Invalid('Currency is required')
        value = text.upper()
    elif kind == 'date':
        value = text or None
    else:
        value = text
    if index is None:
        return replace(inv, **{name: value})
    lines = list(inv.lines)
    lines[index] = replace(lines[index], **{name: value})
    return replace(inv, lines=lines)


def same_value(inv, fid, text):
    """True when `text` would not change the field (numeric compare is by value)."""
    new = set_field(inv, fid, text)
    a, b = field_value(inv, fid), field_value(new, fid)
    kind = _field_meta(inv, fid)[0]
    if kind in NUMERIC_KINDS and a and b:
        return Decimal(a) == Decimal(b)
    return a == b


# ---------------------------------------------------------------- arithmetic breakdown

def breakdown(inv, conn):
    """Show-your-work view of what `evaluate` checks (display only; reasons come from core)."""
    close = lambda a, b: abs(a - b) <= CENT
    lines, subtotal = [], Decimal(0)
    for i, l in enumerate(inv.lines):
        computed = (l.quantity * l.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotal += computed
        lines.append({'index': i, 'item': l.item, 'quantity': l.quantity, 'unit_price': l.unit_price,
                      'stated': l.amount, 'computed': computed,
                      'ok': l.amount is None or close(l.amount, computed)})
    base = inv.subtotal if inv.subtotal is not None else subtotal
    expected_tax = None
    if inv.tax_rate is not None:
        expected_tax = (subtotal * inv.tax_rate).quantize(CENT, rounding=ROUND_HALF_UP)
    expected_total = base + inv.tax + inv.shipping
    stocks = {item_key(n): (n, s) for n, s in conn.execute('SELECT item, stock FROM inventory')}
    agg = {}
    for l in inv.lines:
        key = item_key(l.item)
        entry = agg.setdefault(key, {'name': stocks.get(key, (l.item, None))[0],
                                     'requested': Decimal(0), 'stock': stocks.get(key, (None, None))[1]})
        entry['requested'] += l.quantity
    items = []
    for entry in agg.values():
        stock, req = entry['stock'], entry['requested']
        status = 'unknown' if stock is None else 'out_of_stock' if stock <= 0 else \
            'exceeded' if req > stock else 'ok'
        items.append({**entry, 'status': status})
    return {'lines': lines, 'computed_subtotal': subtotal, 'subtotal_ok': inv.subtotal is None or close(inv.subtotal, subtotal),
            'expected_tax': expected_tax,
            'tax_ok': expected_tax is None or close(inv.tax, expected_tax),
            'expected_total': expected_total,
            'total_ok': inv.total is not None and close(inv.total, expected_total),
            'total_diff': None if inv.total is None else inv.total - expected_total,
            'items': items}


def explain(codes, inv, b, detail=''):
    """Plain-language sentence per reason code, from the same numbers core used."""
    m = lambda v: '-' if v is None else format(v, 'f')
    out = {}
    for code in codes:
        if code == 'line_mismatch':
            bad = [f"line {x['index'] + 1} {x['item']}: stated {m(x['stated'])}, qty x price = {m(x['computed'])}"
                   for x in b['lines'] if not x['ok']]
            out[code] = 'Line amounts disagree: ' + '; '.join(bad)
        elif code == 'subtotal_mismatch':
            out[code] = f"Stated subtotal {m(inv.subtotal)} but the lines add up to {m(b['computed_subtotal'])}."
        elif code == 'tax_mismatch':
            out[code] = f"Stated tax {m(inv.tax)} but rate {m(inv.tax_rate)} x subtotal = {m(b['expected_tax'])}."
        elif code == 'total_mismatch':
            out[code] = (f"Stated total {m(inv.total)} but subtotal + tax + shipping = {m(b['expected_total'])} "
                         f"(difference {m(b['total_diff'])}; tolerance 0.01).")
        elif code == 'high_value':
            out[code] = f'USD total {m(inv.total)} is above 10,000.00: VP Finance sign-off is required.'
        elif code == 'unknown_item':
            names = ', '.join(i['name'] for i in b['items'] if i['status'] == 'unknown')
            out[code] = f'Not in the inventory database: {names}.'
        elif code == 'out_of_stock':
            names = ', '.join(i['name'] for i in b['items'] if i['status'] == 'out_of_stock')
            out[code] = f'Zero stock on hand: {names}.'
        elif code == 'stock_exceeded':
            names = ', '.join(f"{i['name']} (requested {m(i['requested'])}, stock {i['stock']})"
                              for i in b['items'] if i['status'] == 'exceeded')
            out[code] = f'Quantity exceeds stock: {names}.'
        elif code == 'unsupported_currency':
            out[code] = f'Currency {inv.currency} cannot be validated: no FX rates exist in this prototype.'
        elif code == 'vendor_change':
            out[code] = ('The document mentions a former or merged vendor. There is no vendor registry '
                         'to verify it, so it stays held until a human confirms outside this tool and the '
                         'source is corrected.')
        elif code == 'revision_conflict':
            out[code] = 'Documents for this invoice disagree. Select the authoritative version first.'
        elif code == 'parse_error':
            out[code] = detail or 'The document could not be read.'
        elif code.startswith('missing_') or code.startswith('invalid_'):
            out[code] = reason_label(code) + '.'
        else:
            out[code] = reason_label(code) + '.'
    return out


# ---------------------------------------------------------------- derived state

@dataclass
class DocView:
    filename: str
    fmt: str
    sha256: str
    size: int
    original: Invoice | None
    effective: Invoice | None
    parse_error: str | None
    result: dict | None
    role: str = 'effective'   # effective | duplicate | superseded | competing | unparsed
    edit_count: int = 0
    source_error: str | None = None


@dataclass
class CaseState:
    case_id: str
    row: dict
    docs: list
    effective: DocView | None
    conflict: bool
    decision: str
    reasons: list
    blockers: list
    gates: list
    required: list
    explanations: dict
    evidence: str
    approvals: dict            # gate -> decision row, valid for the current evidence
    stale_approvals: list      # active but bound to older evidence
    rejection: dict | None
    payment: dict | None
    status: str
    pending: list
    problems: list             # reasons mock payment is not allowed right now
    responsible: str
    breakdown: dict | None = None

    @property
    def rev(self):
        return self.row['rev']

    @property
    def number(self):
        for d in ([self.effective] if self.effective else []) + self.docs:
            inv = d.effective
            if inv and inv.number:
                return inv.number
        return self.docs[0].filename if self.docs else self.case_id

    @property
    def vendor(self):
        for d in ([self.effective] if self.effective else []) + self.docs:
            if d.effective and d.effective.vendor:
                return d.effective.vendor
        return ''

    @property
    def invoice(self):
        return self.effective.effective if self.effective else None

    @property
    def amounts(self):
        if self.effective:
            return [(self.effective.effective.total, self.effective.effective.currency)]
        seen, out = set(), []
        for d in self.docs:
            if d.effective and (d.effective.total, d.effective.currency) not in seen:
                seen.add((d.effective.total, d.effective.currency))
                out.append((d.effective.total, d.effective.currency))
        return out

    @property
    def eligible_core(self):
        return self.effective is not None and not self.blockers

    @property
    def payable(self):
        return not self.problems


STATUS_LABELS = {
    'paid': 'Paid (mock)', 'rejected': 'Rejected', 'needs_version': 'Needs version selection',
    'blocked': 'Blocked', 'awaiting_approval': 'Awaiting sign-off', 'ready_to_pay': 'Ready for mock payment',
}


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _require(actor, permission):
    if permission not in PERMISSIONS.get(actor.role, ()):
        raise Forbidden(f"The demo role '{actor.role}' may not do '{permission}'.")


def _reason_text(text, label='A reason', minimum=3):
    text = (text or '').strip()
    if len(text) < minimum:
        raise Invalid(f'{label} is required')
    if len(text) > MAX_TEXT:
        raise Invalid(f'{label} is too long')
    return text


class Workbench:
    def __init__(self, db_path, data_dir, clock=utcnow):
        if str(db_path) == ':memory:':
            raise ValueError('The workbench needs a file database: each request opens its own connection')
        self.db_path = str(db_path)
        self.data_dir = Path(data_dir).resolve()
        self.clock = clock
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------ connections

    def connect(self):
        conn = open_database(self.db_path)      # core tables: inventory, mock_payments
        conn.isolation_level = None             # explicit transactions below
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 10000')
        return conn

    @contextmanager
    def tx(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN')
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            conn.close()

    def now(self):
        return iso(self.clock())

    # ------------------------------------------------------------ ingestion

    def ingest(self):
        """Load fixtures without resetting existing work. Idempotent."""
        report = {'added': [], 'unchanged': [], 'changed_on_disk': []}
        paths = sorted(p for p in self.data_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith('.'))
        with self.tx() as c:
            touched = defaultdict(list)
            for p in paths:
                sha = hashlib.sha256(p.read_bytes()).hexdigest()
                known = c.execute('SELECT sha256 FROM documents WHERE filename = ?', (p.name,)).fetchone()
                if known:
                    report['unchanged' if known['sha256'] == sha else 'changed_on_disk'].append(p.name)
                    continue
                try:
                    inv = load_invoice(p)
                    parsed, error = json.dumps(inv_to_dict(inv)), None
                    key = f'{vendor_key(inv.vendor)}|{invoice_key(inv.number)}' \
                        if inv.vendor.strip() and invoice_key(inv.number) else None
                except PARSE_ERRORS as exc:
                    parsed, error, key = None, str(exc) or exc.__class__.__name__, None
                key = key or f'doc:{p.name}'
                case = c.execute('SELECT case_id FROM cases WHERE case_key = ?', (key,)).fetchone()
                now = self.now()
                created = case is None
                if created:
                    case_id = self._new_case_id(c, p)
                    c.execute('INSERT INTO cases(case_id, case_key, created_at, waiting_since) VALUES (?,?,?,?)',
                              (case_id, key, now, now))
                else:
                    case_id = case['case_id']
                c.execute('INSERT INTO documents(filename, sha256, fmt, size, case_id, parsed, parse_error, ingested_at) '
                          'VALUES (?,?,?,?,?,?,?,?)',
                          (p.name, sha, p.suffix.lower().lstrip('.'), p.stat().st_size, case_id, parsed, error, now))
                touched[case_id].append((p.name, created))
                report['added'].append(p.name)
            for case_id, items in touched.items():
                names = ', '.join(n for n, _ in items)
                if items[0][1]:
                    summary = f'Ingested {len(items)} document(s): {names}'
                    kind = 'ingested'
                else:
                    c.execute('UPDATE cases SET rev = rev + 1 WHERE case_id = ?', (case_id,))
                    summary = f'New document(s) joined this case: {names}'
                    kind = 'document_added'
                self._event(c, case_id, SYSTEM, kind, summary, {'files': [n for n, _ in items]})
                if kind == 'document_added':
                    self._invalidate_stale(c, self._state(c, case_id), SYSTEM, 'a document was added')
        return report

    def _new_case_id(self, c, path):
        base = re.sub(r'[^A-Za-z0-9_-]', '_', path.name.split('.')[0]) or 'case'
        candidate, n = base, 1
        while c.execute('SELECT 1 FROM cases WHERE case_id = ?', (candidate,)).fetchone():
            n += 1
            candidate = f'{base}-{n}'
        return candidate

    # ------------------------------------------------------------ reads

    def list_cases(self):
        with self.read() as c:
            ids = [r['case_id'] for r in c.execute('SELECT case_id FROM cases ORDER BY case_id')]
            return [self._state(c, i) for i in ids]

    def case(self, case_id):
        with self.read() as c:
            return self._state(c, case_id, detail=True)

    def history(self, case_id):
        with self.read() as c:
            self._state(c, case_id)
            rows = c.execute('SELECT * FROM events WHERE case_id = ? ORDER BY id DESC', (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['detail'] = json.loads(d['detail'] or 'null')
            out.append(d)
        return out

    def edits(self, case_id):
        with self.read() as c:
            return [dict(r) for r in c.execute(
                'SELECT * FROM field_edits WHERE case_id = ? ORDER BY id', (case_id,))]

    def document_path(self, case_id, filename):
        """Resolve a case document to its file, verifying the bytes still match ingestion."""
        with self.read() as c:
            row = c.execute('SELECT * FROM documents WHERE case_id = ? AND filename = ?',
                            (case_id, filename)).fetchone()
        if row is None or Path(row['filename']).name != row['filename']:
            raise NotFound('Unknown document')
        path = (self.data_dir / row['filename']).resolve()
        if path.parent != self.data_dir or not path.is_file():
            raise NotFound('Document file is missing')
        if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise Stale('The file on disk no longer matches the bytes ingested; refusing to display it as evidence.')
        return path, row['fmt']

    def ledger(self):
        with self.read() as c:
            return [dict(r) for r in c.execute('SELECT * FROM mock_payments ORDER BY vendor, invoice_number')]

    def _source_error(self, filename, sha256):
        try:
            path = (self.data_dir / filename).resolve()
            if path.parent != self.data_dir or not path.is_file():
                return f'{filename}: original document is missing or outside the data directory'
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
                return f'{filename}: original document changed since ingestion'
        except OSError:
            return f'{filename}: original document cannot be read'
        return None

    # ------------------------------------------------------------ state derivation

    def _state(self, c, case_id, detail=False):
        row = c.execute('SELECT * FROM cases WHERE case_id = ?', (case_id,)).fetchone()
        if row is None:
            raise NotFound(f'No case {case_id!r}')
        row = dict(row)
        edit_rows = c.execute('SELECT * FROM field_edits WHERE case_id = ? ORDER BY id', (case_id,)).fetchall()
        docs = []
        for d in c.execute('SELECT * FROM documents WHERE case_id = ? ORDER BY filename', (case_id,)):
            original = inv_from_dict(json.loads(d['parsed'])) if d['parsed'] else None
            current, count = original, 0
            for e in edit_rows:
                if e['filename'] == d['filename'] and current is not None:
                    current = set_field(current, e['field'], e['after'])
                    count += 1
            result = None
            if current is not None:
                try:
                    result = evaluate(current, c)
                except Exception as exc:   # a malformed value must reject, never crash the queue
                    result = {'source': d['filename'], 'decision': 'reject', 'reasons': ['parse_error'],
                              'detail': f'{exc.__class__.__name__}: {exc}'}
            docs.append(DocView(d['filename'], d['fmt'], d['sha256'], d['size'], original, current,
                                d['parse_error'], result, 'unparsed' if current is None else 'effective', count,
                                self._source_error(d['filename'], d['sha256'])))
        parsable = [d for d in docs if d.effective is not None]
        selected = row['selected_filename']
        conflict, effective = False, None
        if not parsable:
            decision, reasons = 'reject', ['parse_error']
        elif selected and any(d.filename == selected for d in parsable):
            effective = next(d for d in parsable if d.filename == selected)
            for d in parsable:
                if d is not effective:
                    d.role = 'superseded'
        elif len(parsable) > 1:
            first = parsable[0]
            conflict = any(economic_content(d.effective) != economic_content(first.effective)
                           or d.result['reasons'] != first.result['reasons'] for d in parsable[1:])
            if conflict:
                for d in parsable:
                    d.role = 'competing'
            else:
                effective = first
                for d in parsable[1:]:
                    d.role = 'duplicate'
        else:
            effective = parsable[0]
        if conflict:
            decision, reasons = 'hold', ['revision_conflict']
        elif effective:
            decision, reasons = effective.result['decision'], order_reasons(effective.result['reasons'])
        reasons = list(reasons)
        if any(d.source_error for d in docs):
            reasons.append('source_unavailable')
        if row['workflow'] == 'correction_requested':
            reasons.append('correction_requested')
        if any(r in reasons for r in ('source_unavailable', 'correction_requested')) and decision == 'approve':
            decision = 'hold'
        blockers = [r for r in reasons if r not in GATE_REASONS]
        gates = [r for r in reasons if r in GATE_REASONS]
        required = ['ap'] + (['vp'] if 'high_value' in reasons else [])
        inventory = self._inventory_snapshot(c, docs)
        evidence = self._evidence_hash(docs, effective, decision, reasons, inventory)

        decisions = [dict(r) for r in c.execute(
            'SELECT * FROM decisions WHERE case_id = ? AND invalidated_at IS NULL ORDER BY id', (case_id,))]
        approvals = {d['gate']: d for d in decisions if d['gate'] in ('ap', 'vp') and d['evidence_hash'] == evidence}
        stale = [d for d in decisions if d['gate'] in ('ap', 'vp') and d['evidence_hash'] != evidence]
        rejection = next((d for d in reversed(decisions) if d['gate'] == 'reject'), None)
        payment = c.execute('SELECT * FROM case_payments WHERE case_id = ?', (case_id,)).fetchone()
        payment = dict(payment) if payment else None
        pending = [g for g in required if g not in approvals]

        problems = []
        if payment:
            problems.append('A mock payment is already recorded for this case.')
        else:
            if rejection:
                problems.append('A reviewer rejected this case; reopen it first.')
            if conflict:
                problems.append('Competing versions: select the authoritative one first.')
            if effective is None and not conflict:
                problems.append('No readable document.')
            if blockers and not conflict:
                problems.append('Unresolved validation blockers: ' + ', '.join(reason_label(b) for b in blockers) + '.')
            if 'ap' in pending:
                problems.append('AP Manager sign-off is missing' + (' (stale after changes)' if any(
                    s['gate'] == 'ap' for s in stale) else '') + '.')
            if 'vp' in pending:
                problems.append('VP Finance sign-off is missing (high-value invoice)' + (' (stale after changes)' if any(
                    s['gate'] == 'vp' for s in stale) else '') + '.')

        if payment:
            status = 'paid'
        elif rejection:
            status = 'rejected'
        elif conflict:
            status = 'needs_version'
        elif blockers or effective is None:
            status = 'blocked'
        elif pending:
            status = 'awaiting_approval'
        else:
            status = 'ready_to_pay'

        explanations, bd = {}, None
        if effective and 'parse_error' not in effective.result['reasons']:
            bd = breakdown(effective.effective, c)
            explanations = explain(reasons, effective.effective, bd, effective.result.get('detail', ''))
        elif conflict:
            explanations = explain(reasons, None, None)
        elif docs:
            explanations = explain(reasons, None, None, docs[0].parse_error or '')
        if 'source_unavailable' in reasons:
            explanations['source_unavailable'] = '; '.join(d.source_error for d in docs if d.source_error)
        if 'correction_requested' in reasons:
            explanations['correction_requested'] = 'A correction was requested. Resolve it explicitly before obtaining fresh sign-offs.'
        state = CaseState(case_id, row, docs, effective, conflict, decision, reasons, blockers, gates,
                          required, explanations, evidence, approvals, stale, rejection, payment, status,
                          pending, problems, '', bd)
        state.responsible = self._responsible(state)
        return state

    def _inventory_snapshot(self, c, docs):
        keys = {item_key(l.item) for d in docs if d.effective for l in d.effective.lines}
        return sorted((n, s) for n, s in c.execute('SELECT item, stock FROM inventory') if item_key(n) in keys)

    def _evidence_hash(self, docs, effective, decision, reasons, inventory):
        payload = {'policy': POLICY_VERSION,
                   'documents': [(d.filename, d.sha256, d.role, d.source_error) for d in docs],
                   'effective': effective.filename if effective else None,
                   'invoices': [inv_to_dict(d.effective) for d in docs if d.effective],
                   'validation': {'decision': decision, 'reasons': reasons},
                   'inventory': [list(x) for x in inventory]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _responsible(self, st):
        r = st.row
        if st.status in ('paid', 'rejected'):
            return 'Closed'
        if r['workflow'] == 'correction_requested' and st.status in ('blocked', 'needs_version'):
            return f"Waiting on {r['waiting_on']}"
        if r['workflow'] == 'escalated' and r['assignee']:
            return f"{r['assignee']} (escalated)"
        if r['assignee']:
            return r['assignee']
        if st.status == 'awaiting_approval':
            return 'AP Manager' if 'ap' in st.pending else 'VP Finance'
        if st.status == 'ready_to_pay':
            return 'AP Manager'
        for code in order_reasons(st.blockers):
            return reason_team(code)
        return 'AP Operations'

    # ------------------------------------------------------------ mutation framework

    def _event(self, c, case_id, actor, kind, summary, detail=None, rev=None):
        if rev is None:
            rev = c.execute('SELECT rev FROM cases WHERE case_id = ?', (case_id,)).fetchone()['rev']
        c.execute('INSERT INTO events(case_id, ts, actor_id, actor_name, role, kind, summary, detail, rev_after) '
                  'VALUES (?,?,?,?,?,?,?,?,?)',
                  (case_id, self.now(), actor.id, actor.name, actor.role, kind, summary,
                   json.dumps(detail) if detail is not None else None, rev))

    def _invalidate_stale(self, c, st, actor, because):
        for d in st.stale_approvals:
            c.execute('UPDATE decisions SET invalidated_at = ?, invalidated_reason = ? WHERE id = ?',
                      (self.now(), because, d['id']))
            self._event(c, st.case_id, actor, 'approval_invalidated',
                        f"{'AP' if d['gate'] == 'ap' else 'VP'} sign-off by {d['actor_name']} invalidated: {because}",
                        {'decision_id': d['id'], 'gate': d['gate']})

    def _act(self, case_id, actor, permission, rev, evidence, fn, *, check_rev=True):
        _require(actor, permission)
        refusal = None
        with self.tx() as c:
            before = self._state(c, case_id)
            if check_rev:
                if rev is None or str(rev) != str(before.rev):
                    raise Stale('This case changed since you opened it. Reload and review before acting.')
                if evidence is not None and evidence != before.evidence:
                    raise Stale('The evidence behind this case changed (data, policy or inventory). '
                                'Reload and review before acting.')
            out = fn(c, before)
            if isinstance(out, Refused):
                refusal = out
            elif out is not False:                  # False = no state change (comment)
                c.execute('UPDATE cases SET rev = rev + 1 WHERE case_id = ?', (case_id,))
                after = self._state(c, case_id)
                self._invalidate_stale(c, after, actor, out or permission.replace('_', ' '))
                after = self._state(c, case_id)
                if (before.status, before.responsible) != (after.status, after.responsible):
                    c.execute('UPDATE cases SET waiting_since = ? WHERE case_id = ?', (self.now(), case_id))
        if refusal:
            raise refusal
        return self.case(case_id)

    def _editable(self, st):
        if st.payment:
            raise Refused('A mock payment is recorded; this case is closed.')
        if st.rejection:
            raise Refused('This case was rejected; reopen it before changing it.')

    # ------------------------------------------------------------ actions

    def edit_fields(self, case_id, actor, rev, doc, values, reason, evidence=None):
        _require(actor, 'edit')
        reason = _reason_text(reason, 'A correction reason')

        def fn(c, st):
            self._editable(st)
            d = next((x for x in st.docs if x.filename == doc), None)
            if d is None or d.effective is None:
                raise Invalid('Choose a readable document of this case to correct')
            inv, changes = d.effective, []
            for fid, text in values.items():
                if same_value(inv, fid, text):
                    continue
                before = field_value(inv, fid)
                inv = set_field(inv, fid, text)
                changes.append({'field': fid, 'label': field_label(fid), 'before': before,
                                'after': field_value(inv, fid), 'original': field_value(d.original, fid)})
            if not changes:
                raise Invalid('No field changed')
            now = self.now()
            for ch in changes:
                c.execute('INSERT INTO field_edits(case_id, filename, field, before, after, actor_id, actor_name, role, reason, ts) '
                          'VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (case_id, doc, ch['field'], ch['before'], ch['after'], actor.id, actor.name, actor.role, reason, now))
            new = self._state(c, case_id)
            self._event(c, case_id, actor, 'edit',
                        f"Corrected {len(changes)} field(s) on {doc}; revalidated: {st.decision} -> {new.decision}"
                        f" ({', '.join(new.reasons) or 'no reasons'}). Reason: {reason}",
                        {'doc': doc, 'changes': changes, 'reason': reason, 'before': st.decision, 'after': new.decision,
                         'reasons_after': new.reasons}, rev=st.rev + 1)
            return 'invoice data was corrected'
        return self._act(case_id, actor, 'edit', rev, evidence, fn)

    def select_version(self, case_id, actor, rev, filename, reason, evidence=None):
        _require(actor, 'select_version')
        reason = _reason_text(reason, 'A reason for choosing this version')

        def fn(c, st):
            self._editable(st)
            readable = [d for d in st.docs if d.effective is not None]
            if len(readable) < 2:
                raise Refused('Only one readable document: nothing to choose between.')
            chosen = next((d for d in readable if d.filename == filename), None)
            if chosen is None:
                raise Invalid('Unknown document')
            c.execute('UPDATE cases SET selected_filename = ? WHERE case_id = ?', (filename, case_id))
            superseded = [d.filename for d in readable if d.filename != filename]
            new = self._state(c, case_id)
            self._event(c, case_id, actor, 'select_version',
                        f"Selected {filename} as authoritative; superseded {', '.join(superseded)}. "
                        f"Revalidated: {new.decision} ({', '.join(new.reasons) or 'no reasons'}). Reason: {reason}",
                        {'selected': filename, 'superseded': superseded, 'reason': reason,
                         'previous': st.row['selected_filename']}, rev=st.rev + 1)
            return 'the authoritative version changed'
        return self._act(case_id, actor, 'select_version', rev, evidence, fn)

    def assign(self, case_id, actor, rev, assignee, reason, evidence=None):
        _require(actor, 'assign')
        allowed = set(TEAMS) | {a.name for a in ACTORS.values() if a.role != 'observer'}
        if assignee not in allowed:
            raise Invalid('Choose a listed team or demo person')
        reason = _reason_text(reason, 'A reason')

        def fn(c, st):
            self._editable(st)
            c.execute("UPDATE cases SET assignee = ?, workflow = CASE WHEN workflow = 'correction_requested' THEN workflow ELSE 'assigned' END WHERE case_id = ?",
                      (assignee, case_id))
            self._event(c, case_id, actor, 'assign', f'Assigned to {assignee}. Reason: {reason}',
                        {'assignee': assignee, 'reason': reason}, rev=st.rev + 1)
            return 'responsibility changed'
        return self._act(case_id, actor, 'assign', rev, evidence, fn)

    def request_correction(self, case_id, actor, rev, party, message, evidence=None):
        _require(actor, 'request_correction')
        if party not in CORRECTION_PARTIES:
            raise Invalid('Choose who should correct it')
        message = _reason_text(message, 'A message describing the correction needed')

        def fn(c, st):
            self._editable(st)
            c.execute("UPDATE cases SET workflow = 'correction_requested', waiting_on = ? WHERE case_id = ?",
                      (party, case_id))
            self._event(c, case_id, actor, 'request_correction',
                        f'Requested correction from {party}: {message}',
                        {'party': party, 'message': message, 'blockers': st.blockers}, rev=st.rev + 1)
            return 'a correction was requested'
        return self._act(case_id, actor, 'request_correction', rev, evidence, fn)

    def resolve_correction(self, case_id, actor, rev, reason, evidence=None):
        reason = _reason_text(reason, 'A resolution reason')

        def fn(c, st):
            self._editable(st)
            if st.row['workflow'] != 'correction_requested':
                raise Refused('There is no open correction request.')
            if any(r != 'correction_requested' for r in st.blockers):
                raise Refused('Correct the remaining validation blockers before resolving the request.')
            c.execute("UPDATE cases SET workflow = 'open', waiting_on = NULL WHERE case_id = ?", (case_id,))
            self._event(c, case_id, actor, 'correction_resolved', f'Correction request resolved: {reason}',
                        {'reason': reason}, rev=st.rev + 1)
            return 'the correction request was resolved'
        return self._act(case_id, actor, 'resolve_correction', rev, evidence, fn)

    def escalate(self, case_id, actor, rev, target, reason, evidence=None):
        _require(actor, 'escalate')
        if target not in TEAMS:
            raise Invalid('Choose a listed team')
        reason = _reason_text(reason, 'An escalation reason')

        def fn(c, st):
            self._editable(st)
            c.execute("UPDATE cases SET workflow = CASE WHEN workflow = 'correction_requested' THEN workflow ELSE 'escalated' END, assignee = ? WHERE case_id = ?",
                      (target, case_id))
            self._event(c, case_id, actor, 'escalate', f'Escalated to {target}. Reason: {reason}',
                        {'target': target, 'reason': reason, 'blockers': st.blockers}, rev=st.rev + 1)
            return 'the case was escalated'
        return self._act(case_id, actor, 'escalate', rev, evidence, fn)

    def comment(self, case_id, actor, text):
        _require(actor, 'comment')
        text = _reason_text(text, 'A comment')

        def fn(c, st):
            self._event(c, case_id, actor, 'comment', text, None)
            return False
        return self._act(case_id, actor, 'comment', None, None, fn, check_rev=False)

    def revalidate(self, case_id, actor, rev, evidence=None):
        def fn(c, st):
            self._event(c, case_id, actor, 'revalidate',
                        f"Revalidated against current inventory and policy: {st.decision} "
                        f"({', '.join(st.reasons) or 'no reasons'})",
                        {'decision': st.decision, 'reasons': st.reasons}, rev=st.rev + 1)
            return 'validation was re-run'
        return self._act(case_id, actor, 'revalidate', rev, evidence, fn)

    def approve(self, case_id, actor, rev, gate, evidence, reason):
        if gate not in ('ap', 'vp'):
            raise Invalid('Unknown approval gate')
        _require(actor, 'approve_ap' if gate == 'ap' else 'approve_vp')
        reason = _reason_text(reason, 'An approval reason')
        label = 'AP Manager' if gate == 'ap' else 'VP Finance'

        def fn(c, st):
            self._editable(st)
            if st.conflict:
                raise Refused('Select the authoritative version before anyone signs off.')
            if st.blockers or st.effective is None:
                raise Refused('Validation blockers remain: ' + ', '.join(reason_label(b) for b in st.blockers or ['unreadable']) + '.')
            if gate not in st.required:
                raise Refused('VP sign-off is only required for invoices above 10,000 USD.')
            if gate in st.approvals:
                raise Refused(f'{label} sign-off is already recorded for this exact data.')
            cur = c.execute('INSERT INTO decisions(case_id, gate, actor_id, actor_name, role, reason, ts, evidence_hash) '
                            'VALUES (?,?,?,?,?,?,?,?)',
                            (case_id, gate, actor.id, actor.name, actor.role, reason, self.now(), st.evidence))
            self._event(c, case_id, actor, f'approve_{gate}',
                        f'{label} sign-off recorded by {actor.name}. Reason: {reason}',
                        {'decision_id': cur.lastrowid, 'evidence': st.evidence, 'reason': reason}, rev=st.rev + 1)
            return 'a sign-off was recorded'
        return self._act(case_id, actor, 'approve_ap' if gate == 'ap' else 'approve_vp', rev, evidence, fn)

    def reject(self, case_id, actor, rev, reason, evidence=None):
        _require(actor, 'reject')
        reason = _reason_text(reason, 'A rejection reason')

        def fn(c, st):
            self._editable(st)
            c.execute('INSERT INTO decisions(case_id, gate, actor_id, actor_name, role, reason, ts, evidence_hash) '
                      "VALUES (?,'reject',?,?,?,?,?,?)",
                      (case_id, actor.id, actor.name, actor.role, reason, self.now(), st.evidence))
            self._event(c, case_id, actor, 'reject', f'Rejected by {actor.name}. Reason: {reason}',
                        {'reason': reason}, rev=st.rev + 1)
            return 'the case was rejected'
        return self._act(case_id, actor, 'reject', rev, evidence, fn)

    def reopen(self, case_id, actor, rev, reason, evidence=None):
        _require(actor, 'reopen')
        reason = _reason_text(reason, 'A reason to reopen')

        def fn(c, st):
            if st.payment:
                raise Refused('A mock payment is recorded; this case is closed.')
            if not st.rejection:
                raise Refused('The case is not rejected.')
            c.execute('UPDATE decisions SET invalidated_at = ?, invalidated_reason = ? WHERE id = ?',
                      (self.now(), f'reopened: {reason}', st.rejection['id']))
            self._event(c, case_id, actor, 'reopen', f'Reopened by {actor.name}. Reason: {reason}',
                        {'reason': reason}, rev=st.rev + 1)
            return 'the case was reopened'
        return self._act(case_id, actor, 'reopen', rev, evidence, fn)

    def pay(self, case_id, actor, rev, evidence):
        """Record a mock payment. Eligibility is enforced here, never only in the UI."""
        def fn(c, st):
            problems = list(st.problems)
            ledger_key = None
            if not problems:
                inv = st.invoice
                ledger_key = (vendor_key(inv.vendor), invoice_key(inv.number))
                if c.execute('SELECT 1 FROM mock_payments WHERE vendor = ? AND invoice_number = ?',
                             ledger_key).fetchone():
                    problems.append('The mock ledger already holds a payment for this vendor and invoice number.')
            if problems:
                self._event(c, case_id, actor, 'payment_refused',
                            'Mock payment refused: ' + ' '.join(problems), {'problems': problems}, rev=st.rev)
                return Refused('Mock payment refused: ' + ' '.join(problems))
            inv = st.invoice
            cur = c.execute('INSERT OR IGNORE INTO mock_payments VALUES (?,?,?,?,?)',
                            (*ledger_key, inv.currency, str(inv.total), st.effective.filename))
            if cur.rowcount != 1:
                return Refused('Mock payment refused: ledger already holds this invoice.')
            c.execute('INSERT INTO case_payments(case_id, ledger_vendor, ledger_invoice, amount, currency, source, '
                      'evidence_hash, actor_id, actor_name, role, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (case_id, *ledger_key, str(inv.total), inv.currency, st.effective.filename, st.evidence,
                       actor.id, actor.name, actor.role, self.now()))
            self._event(c, case_id, actor, 'payment_recorded',
                        f'Mock payment recorded: {inv.currency} {inv.total} for {inv.number} '
                        f'(no external call was made).',
                        {'amount': str(inv.total), 'currency': inv.currency, 'source': st.effective.filename,
                         'evidence': st.evidence}, rev=st.rev + 1)
            return 'mock payment was recorded'
        return self._act(case_id, actor, 'pay', rev, evidence, fn)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, case_key TEXT UNIQUE NOT NULL, rev INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, waiting_since TEXT NOT NULL,
  workflow TEXT NOT NULL DEFAULT 'open', assignee TEXT, waiting_on TEXT, selected_filename TEXT);
CREATE TABLE IF NOT EXISTS documents (
  filename TEXT PRIMARY KEY, sha256 TEXT NOT NULL, fmt TEXT NOT NULL, size INTEGER NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(case_id), parsed TEXT, parse_error TEXT, ingested_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS field_edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, filename TEXT NOT NULL, field TEXT NOT NULL,
  before TEXT NOT NULL, after TEXT NOT NULL, actor_id TEXT NOT NULL, actor_name TEXT NOT NULL, role TEXT NOT NULL,
  reason TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, gate TEXT NOT NULL CHECK (gate IN ('ap','vp','reject')),
  actor_id TEXT NOT NULL, actor_name TEXT NOT NULL, role TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL,
  evidence_hash TEXT NOT NULL, invalidated_at TEXT, invalidated_reason TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, ts TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_name TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT,
  rev_after INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS case_payments (
  case_id TEXT PRIMARY KEY, ledger_vendor TEXT NOT NULL, ledger_invoice TEXT NOT NULL, amount TEXT NOT NULL,
  currency TEXT NOT NULL, source TEXT NOT NULL, evidence_hash TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_name TEXT NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_case ON events(case_id, id);
CREATE INDEX IF NOT EXISTS edits_case ON field_edits(case_id, id);
'''
# History is append-only at the database level too.
for _t in ('documents', 'field_edits', 'events', 'case_payments'):
    SCHEMA += (f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} "
               f"BEGIN SELECT RAISE(ABORT, '{_t} is append-only'); END;\n"
               f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} "
               f"BEGIN SELECT RAISE(ABORT, '{_t} is append-only'); END;\n")
