"""Test harness. The pipeline runs for real: parsers, grounding, routing, the review loop and the
SQLite ledger. Only its two external boundaries are replaced: the model and the bank."""

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from invoice_workflow.database import open_database
from invoice_workflow.graph import run_batch

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Doc:
    """One invoice. It renders as JSON, CSV or free text, and knows what an honest model reads from its text."""

    number: str = 'INV-7'
    vendor: str = 'Widgets Inc.'
    lines: tuple = (('WidgetA', 4, '250.00'),)
    total: str | None = None  # the printed total; by default the lines plus shipping
    shipping: str = '0.00'
    notes: str = ''
    revision: str = ''
    remit_to: str = ''
    sign: str = '$'  # the money sign in the text rendering
    date: str = '2026-01-05'
    due_date: str | None = '2026-02-04'

    def but(self, **changes):
        return replace(self, **changes)

    @property
    def amount(self):
        computed = sum(q * Decimal(p) for _, q, p in self.lines) + Decimal(self.shipping or 0)
        return self.total or f'{computed:.2f}'

    def json(self):
        data = {
            'invoice_number': self.number,
            'vendor': self.vendor,
            'date': self.date,
            'due_date': self.due_date,
            'currency': 'USD',
            'line_items': [{'item': i, 'quantity': q, 'unit_price': p} for i, q, p in self.lines],
            'shipping': self.shipping,
            'total': self.amount,
            'notes': self.notes,
            'revision': self.revision,
        }
        return json.dumps({**data, 'remit_to': self.remit_to} if self.remit_to else data)

    def csv(self):
        rows = [
            'field,value',
            f'invoice_number,{self.number}',
            f'vendor,{self.vendor}',
            f'date,{self.date}',
            f'due_date,{self.due_date or ""}',
            'currency,USD',
            f'shipping,{self.shipping}',
            f'total,{self.amount}',
        ]
        for item, quantity, price in self.lines:
            rows += [f'item,{item}', f'quantity,{quantity}', f'unit_price,{price}']
        return '\n'.join(rows) + '\n'

    def text(self):
        """A layout the regex parser cannot read, so the model's extraction is the only reading."""
        rows = [
            f'BILL FROM: {self.vendor}',
            f'Invoice ID {self.number}',
            f'Issued {self.date}; payable {self.due_date}',
        ]
        rows += [f'{item}: {quantity} units at {self.sign}{_money(price)}' for item, quantity, price in self.lines]
        rows += [f'AMOUNT DUE {self.sign}{_money(self.amount)}'] + ([self.notes] if self.notes else [])
        return '\n'.join(rows) + '\n'

    def extraction(self):
        """What an honest model returns for text(): each value with the exact text it was read from."""
        quotes = [
            {
                'line_quote': f'{item}: {quantity} units at {self.sign}{_money(price)}',
                'item': item,
                'quantity': str(quantity),
                'unit_price': _money(price),
            }
            for item, quantity, price in self.lines
        ]
        return json.dumps(
            {
                'invoice_number': self.number,
                'vendor': self.vendor,
                'date': self.date,
                'due_date': self.due_date,
                'line_items': [{'item': i, 'quantity': str(q), 'unit_price': p} for i, q, p in self.lines],
                'total': self.amount,
                'currency': None,
                'evidence': {
                    'vendor': self.vendor,
                    'invoice_number': self.number,
                    'date': self.date,
                    'due_date': self.due_date or '',
                    'total': _money(self.amount),
                    'line_items': quotes,
                },
            }
        )


def _money(value):
    return f'{Decimal(value):,.2f}'


# A $1,000 invoice; a $12,000 invoice that is a pure VP question; a sibling that must always pay.
A = Doc()
H = Doc(
    number='INV-12',
    vendor='Northwind Supply',
    lines=(('WidgetB', 10, '500.00'), ('GadgetX', 5, '750.00'), ('WidgetA', 13, '250.00')),
)
S = Doc(number='INV-8', vendor='Acme Parts', lines=(('WidgetB', 2, '500.00'),))


class Models:
    """Scripted extraction, screen, VP and critic calls with configurable responses.
    Defaults extract source values, return no screen signals, approve with matching
    facts and pass critique. Every call is recorded."""

    def __init__(self):
        self.calls = {'extract': [], 'screen': [], 'vp': [], 'critic': []}
        self.extractions = {}  # document text -> answer (JSON text, an exception, or a model callable)
        self.screens = {}  # screened text -> answer
        self.vp_says = 'approves'  # or 'approves on a false fact', 'holds'
        self.vp_quote = None  # the stated reason the VP quotes; by default the invoice's own "Reason:" line
        self.critic_says = 'finds nothing'  # or 'objects', 'cites a missing fact'

    def extract(self, text, issue):
        self.calls['extract'].append(text)
        return _answer(self.extractions.get(text, '{}'), text, issue)

    def screen(self, text, issue):
        self.calls['screen'].append(text)
        return _answer(self.screens.get(text, '{"signals": []}'), text, issue)

    def vp(self, payload, issue):
        self.calls['vp'].append(payload)
        facts = json.loads(payload)['facts']
        document = facts.get('document', {}).get('text', '')
        stated = re.search(r'Reason: (.+)', document)
        quote = self.vp_quote or (stated[1].strip() if stated else None)
        total = facts['invoice']['total'] if self.vp_says != 'approves on a false fact' else '1.00'
        return json.dumps(
            {
                'decision': 'hold' if self.vp_says == 'holds' else 'approve',
                'rationale': 'RATIONALE-CANARY: within policy',
                'claims': [{'path': 'invoice.total', 'value': total}],
                'justifications': [{'trigger': t, 'quote': quote} for t in facts['triggers'] if quote],
            }
        )

    def critic(self, payload, issue):
        self.calls['critic'].append(payload)
        facts = json.loads(payload)['facts']
        issues = {
            'finds nothing': [],
            'objects': [
                {
                    'path': 'invoice.total',
                    'value': facts['invoice']['total'],
                    'description': 'The total needs a second look',
                }
            ],
            'cites a missing fact': [
                {'path': 'invoice.discount', 'value': '100.00', 'description': 'A discount was not applied'}
            ],
        }[self.critic_says]
        return json.dumps({'issues': issues})


def _answer(answer, text, issue):
    if isinstance(answer, BaseException):
        raise answer
    return answer(text, issue) if callable(answer) else answer


class Rail:
    """The bank. Records the amount of every payment the pipeline sends."""

    def __init__(self):
        self.sent = []
        self.crash_after_send = False  # the bank takes the money, then the process dies
        self.during_send = None  # runs inside the first payment, before the bank answers

    def __call__(self, vendor, amount):
        self.sent.append(f'{Decimal(amount):.2f}')
        if self.during_send and len(self.sent) == 1:
            self.during_send()
        if self.crash_after_send:
            raise ConnectionError('the process died after the bank accepted the payment')
        return {'status': 'success'}


class AP:
    """An accounts-payable desk on one ledger file: write documents, run batches, inspect the bank."""

    def __init__(self, folder):
        self.dir, self.ledger = folder, folder / 'ap.db'
        self.models, self.rail = Models(), Rail()

    def file(self, name, doc, *, text=None, extraction=None, screen=None):
        path = self.dir / name
        if path.suffix == '.json':
            path.write_text(doc.json())
        elif path.suffix == '.csv':
            path.write_text(doc.csv())
        else:
            path.write_text(doc.text() if text is None else text)
            self.models.extractions[path.read_text()] = doc.extraction() if extraction is None else extraction
        if screen is not None:
            self.models.screens[path.read_text() if path.suffix == '.txt' else doc.notes] = screen
        return path

    def run(self, *paths):
        conn = open_database(self.ledger)
        try:
            results = run_batch(
                paths,
                conn,
                record_payments=True,
                offline=True,
                payment=self.rail,
                model=self.models.extract,
                screen_model=self.models.screen,
                vp_model=self.models.vp,
                critic_model=self.models.critic,
            )
        finally:
            conn.close()
        return {result['source']: result for result in results}

    def stock(self, units):
        conn = open_database(self.ledger)
        try:
            with conn:
                conn.execute('UPDATE inventory SET stock=?', (units,))
        finally:
            conn.close()
