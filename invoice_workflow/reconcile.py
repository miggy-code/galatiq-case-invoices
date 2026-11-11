"""Batch reconciliation (POLICY 7, 8 and 12): copies become duplicates, a revision replaces its original
only when the hard checks pass, and anything already in the ledger is not paid again."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal

from . import database
from .documents import amount_key, invoice_key, item_key, vendor_key
from .routing import add_reasons, severity


REVISION_SUFFIX = re.compile(r'[-\s_]*(?:R|REV|REVISION)[-\s_]*\d+$', re.I)
REVISION_OF = re.compile(r'\brevision\s+of\s+(?:invoice\s+)?(?:no\.?\s*|#\s*)?([A-Z0-9][A-Z0-9-]*)', re.I)


def revised_numbers(inv):
    """Invoice numbers this invoice says it revises: its own number minus a suffix such as -R1 or Rev 2,
    and any number named in its text as "revision of ...". Its own number is left out."""
    own = invoice_key(inv.number)
    named = [REVISION_SUFFIX.sub('', inv.number)] + REVISION_OF.findall(inv.notes or '')
    return {invoice_key(n) for n in named if invoice_key(n) and invoice_key(n) != own}


def economic_content(inv):
    # Compare computed economics so absent summary fields in a PDF are harmless.
    lines = sorted(
        (
            item_key(line.item),
            str(line.quantity.normalize()),
            str(line.unit_price.normalize()),
            str((line.amount if line.amount is not None else line.quantity * line.unit_price).normalize()),
        )
        for line in inv.lines
    )
    return (inv.date, inv.due_date, inv.currency, inv.revision, inv.total, inv.tax, inv.shipping, lines)


def line_signature(line):
    return (item_key(line.item), line.quantity.normalize(), line.unit_price.normalize())


def revision_checks(heads, conn):
    """POLICY decision 7 hard checks on an original and its revision. Returns the failed checks."""
    marked = [e for e in heads if e['inv'].revision]
    if len(heads) != 2 or len(marked) != 1:
        return ['versions are not one original and one revision']
    revision = marked[0]
    original = heads[0] if heads[1] is revision else heads[1]
    failed = []
    if original['inv'].payment_details != revision['inv'].payment_details or any(
        severity(r) == 'audit' for e in heads for r in e['result']['reasons']
    ):
        failed.append('payment details changed, or a fraud signal')
    if Counter(map(line_signature, original['inv'].lines)) - Counter(map(line_signature, revision['inv'].lines)):
        failed.append('an original line was changed or removed')
    if any(severity(r) != 'vp' for r in revision['result']['reasons']):
        failed.append('the revision fails other checks')
    if original['inv'].total is None or revision['inv'].total is None:
        failed.append('a total is missing')
    paid = database.ledger_row(conn, revision['identity'])
    if paid and revision['inv'].total is not None and Decimal(paid[1]) != revision['inv'].total:
        failed.append('another version is already paid')
    return failed


def conflict(entries, failed):
    for entry in entries:
        entry['result']['revision_checks_failed'] = failed
        add_reasons(entry['result'], 'revision_conflict')


def check_ledger(entry, conn):
    """Payment happens at most once (decision 12); a changed amount under a paid number holds (decision 7)."""
    inv, result = entry['inv'], entry['result']
    row = database.ledger_row(conn, entry['identity'])
    if row is None:
        return
    status, amount = row
    if status == 'pending':
        add_reasons(result, 'payment_pending')
    elif inv.total is not None and Decimal(amount) == inv.total:
        add_reasons(result, 'already_recorded')
    elif inv.total is not None:
        add_reasons(result, 'revision_after_payment' if inv.revision else 'revision_conflict')


def reconcile(entries, conn):
    """Batch reconciliation (POLICY decisions 7, 8 and 12). Returns one entry per invoice that may
    go on to review and payment; copies become duplicates and conflicting versions hold."""
    groups = defaultdict(list)
    for entry in entries:
        inv = entry.get('inv')
        if inv is not None and inv.vendor.strip() and invoice_key(inv.number):
            entry['identity'] = (vendor_key(inv.vendor), invoice_key(inv.number))
            groups[entry['identity']].append(entry)
    _flag_cross_identity_matches(groups, conn)
    primaries = []
    for group in groups.values():
        primary = _reconcile_group(group, conn)
        if primary is not None:
            primaries.append(primary)
    return primaries


def _flag_cross_identity_matches(groups, conn):
    """Hold suspicious matches across invoice numbers or vendors before choosing any primary."""
    # A matching vendor, date and amount under another number needs human review.
    seen = set()
    for identity, group in groups.items():
        inv, result = group[0]['inv'], group[0]['result']
        key = (identity[0], inv.date, amount_key(inv.total))
        if key in seen or database.paid_under_another_number(conn, *key, identity[1]):
            add_reasons(result, 'possible_duplicate')
        seen.add(key)
    # Named originals and their renumbered revisions hold together, including across payment runs.
    for (vendor, _), group in groups.items():
        if database.related_payment(conn, group[0]['identity']):
            for entry in group:
                add_reasons(entry['result'], 'possible_revision')
        for original in revised_numbers(group[0]['inv']):
            known = groups.get((vendor, original), [])
            if known or database.ledger_row(conn, (vendor, original)):
                for entry in group + known:
                    add_reasons(entry['result'], 'possible_revision')
    # A revision under another vendor's name is never matched to an original: hold it.
    vendors = defaultdict(set)
    for vendor, number_key in groups:
        vendors[number_key].add(vendor)
    for (vendor, number_key), group in groups.items():
        if any(e['inv'].revision for e in group) and (
            len(vendors[number_key]) > 1 or database.number_paid_to_another_vendor(conn, number_key, vendor)
        ):
            conflict(group, ['the vendor differs from the original'])


def _reconcile_group(group, conn):
    """Choose one payable candidate from copies and versions of the same invoice identity."""
    if any('revision_conflict' in e['result']['reasons'] for e in group):
        return None
    # Stable source order chooses the same primary copy on every run.
    group.sort(key=lambda e: e['inv'].source)
    clusters = []
    for entry in group:
        same = next((c for c in clusters if economic_content(c[0]['inv']) == economic_content(entry['inv'])), None)
        if same:
            same.append(entry)
        else:
            clusters.append([entry])
    # Equal economics do not erase a fraud signal or a failed extraction on another copy.
    if any(e['result']['reasons'] != c[0]['result']['reasons'] for c in clusters for e in c[1:]):
        conflict(group, ['copies of the same content were assessed differently'])
        return None
    for cluster in clusters:
        for entry in cluster[1:]:
            add_reasons(entry['result'], 'duplicate_document')
    heads = [c[0] for c in clusters]
    if len(heads) > 1:
        failed = revision_checks(heads, conn)
        if failed:
            conflict(group, failed)
            return None
        heads = [_link_revision(heads)]
    check_ledger(heads[0], conn)
    return heads[0]


def _link_revision(heads):
    """Link checked versions; payment marks the original superseded only after success."""
    original, revision = sorted(heads, key=lambda e: bool(e['inv'].revision))
    old, new = original['inv'], revision['inv']
    added = Counter(map(line_signature, new.lines)) - Counter(map(line_signature, old.lines))
    revision['result']['revision_of'] = {
        'original_source': old.source,
        'revision': new.revision,
        'original_total': str(old.total),
        'revised_total': str(new.total),
        'increase': str(new.total - old.total),
        'added_lines': [
            {'item': i, 'quantity': str(q), 'unit_price': str(p), 'count': n} for (i, q, p), n in added.items()
        ],
    }
    revision['original'] = original
    original['result']['superseded_by'] = new.source
    add_reasons(revision['result'], 'revision_change')
    add_reasons(original['result'], 'revision_change')
    return revision
