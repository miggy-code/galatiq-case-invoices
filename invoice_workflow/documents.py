"""Reading invoice files into Invoice records: JSON, CSV and XML by code, and a regex reading of text
and PDF that serves as the cross-check on the model's reading. Also the keys that identify an invoice."""

from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf.errors import PdfReadError


# Structured fields that carry where the money goes; a revision must not change them.
PAYMENT_KEYS = (
    'bank',
    'bank_details',
    'bank_account',
    'payment_details',
    'remit_to',
    'iban',
    'account_number',
    'routing_number',
)


@dataclass
class Line:
    item: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None = None


@dataclass
class Invoice:
    source: str
    number: str
    vendor: str
    date: str | None
    due_date: str | None
    lines: list[Line]
    total: Decimal | None
    subtotal: Decimal | None = None
    tax: Decimal = Decimal('0')
    tax_rate: Decimal | None = None
    shipping: Decimal = Decimal('0')
    currency: str = 'USD'
    revision: str = ''
    notes: str = ''
    corrections: list[str] = field(default_factory=list)
    currency_stated: bool = True
    payment_details: str = ''


def number(value, corrections):
    text = str(value).strip().replace('$', '').replace(',', '')
    if re.fullmatch(r'[+\-]?[\dOo]+(?:\.[\dOo]+)?', text) and re.search(r'\d', text):
        corrected = text.replace('O', '0').replace('o', '0')
        if corrected != text:
            corrections.append(f'numeric OCR: {text} -> {corrected}')
        text = corrected
    result = Decimal(text)
    if not result.is_finite():
        raise ValueError('Non-finite number')
    return result


def item_key(value):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'\([^)]*\)', '', value).lower())


def vendor_key(value):
    words = re.findall(r'[a-z0-9]+', value.lower().replace('&', ' and '))
    aliases = {'incorporated': 'inc', 'company': 'co', 'corporation': 'corp', 'limited': 'ltd'}
    return ' '.join(aliases.get(w, w) for w in words)


def invoice_key(value):
    text = re.sub(r'[^A-Z0-9]', '', value.upper())
    return text[3:] if text.startswith('INV') else text


def amount_key(total):
    """One spelling per amount, so $1000, $1000.0 and $1000.00 compare equal as text (ledger, duplicates)."""
    return None if total is None else str(total.quantize(Decimal('.01')))


def parsed_date(value, corrections):
    if not value:
        return None
    text = str(value).strip()
    fixed = re.sub(r'(?<=\d)[Oo](?=\d)', '0', text)
    if fixed != text:
        corrections.append(f'date OCR: {text} -> {fixed}')
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%b %d %Y', '%B %d, %Y', '%d-%b-%Y'):
        try:
            return datetime.strptime(fixed, fmt).date().isoformat()
        except ValueError:
            pass
    return text  # Preserve unparseable evidence for a reject diagnostic.


def from_mapping(data, source):
    corrections = []

    def dec(key, default=None):
        val = data.get(key)
        val = default if val is None else val  # JSON null means absent, not a crash in validation
        return None if val is None else number(val, corrections)

    vendor = data.get('vendor', '')
    if isinstance(vendor, dict):
        vendor = vendor.get('name', '')
    lines = []
    for row in data.get('line_items', []):
        lines.append(
            Line(
                str(row.get('item', '')),
                number(row['quantity'], corrections),
                number(row['unit_price'], corrections),
                None if row.get('amount') is None else number(row['amount'], corrections),
            )
        )
    payment = {k: data[k] for k in PAYMENT_KEYS if data.get(k) not in (None, '')}
    return Invoice(
        source,
        str(data.get('invoice_number') or ''),
        str(vendor or ''),
        parsed_date(data.get('date'), corrections),
        parsed_date(data.get('due_date'), corrections),
        lines,
        dec('total'),
        dec('subtotal'),
        dec('tax_amount', 0),
        dec('tax_rate'),
        dec('shipping', 0),
        str(data.get('currency') or 'USD').upper(),
        str(data.get('revision', '')),
        str(data.get('notes', '')),
        corrections,
        data.get('currency') not in (None, ''),
        json.dumps(payment, sort_keys=True, default=str) if payment else '',
    )


def csv_mapping(text):
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        raise ValueError('Empty CSV')
    result = {'line_items': []}
    if rows[0] == ['field', 'value']:
        current = None
        for key, value in rows[1:]:
            if key == 'item':
                current = {'item': value}
                result['line_items'].append(current)
            elif key in ('quantity', 'unit_price'):
                if current is None:
                    raise ValueError('CSV quantity/price before item')
                current[key] = value
            else:
                result['tax_amount' if key == 'tax' else key] = value
    else:
        for row in csv.DictReader(text.splitlines()):
            if row.get('Invoice Number'):
                for key, col in [
                    ('invoice_number', 'Invoice Number'),
                    ('vendor', 'Vendor'),
                    ('date', 'Date'),
                    ('due_date', 'Due Date'),
                ]:
                    if key in result and result[key] != row[col]:
                        raise ValueError('Mixed invoice identities in CSV')
                    result[key] = row[col]
                result['line_items'].append(
                    {
                        'item': row['Item'],
                        'quantity': row['Qty'],
                        'unit_price': row['Unit Price'],
                        'amount': row['Line Total'],
                    }
                )
            else:
                label = row.get('Unit Price', '').lower()
                key = (
                    'subtotal'
                    if label.startswith('subtotal')
                    else 'tax_amount'
                    if label.startswith('tax')
                    else 'total'
                    if label.startswith('total')
                    else None
                )
                if key is None:
                    raise ValueError('Unrecognized CSV summary')
                result[key] = row['Line Total']
                rate = re.search(r'([\d.]+)%', label)
                if rate:
                    result['tax_rate'] = str(Decimal(rate[1]) / 100)
    return result


def text_mapping(text):
    result = {'line_items': [], 'notes': text}
    # shortcut: handles the supplied text-based layouts, not arbitrary scans; add
    # a separate extraction stage when new vendor layouts or image PDFs arrive.
    labels = {
        'vendor': r'(?:Vendor|Vndr)',
        'invoice_number': r'(?:Invoice Number|Invoice|Inv #|INV NO)',
        'date': r'(?:Date|Dt)',
        'due_date': r'(?:Due Date|Due Dt|Due|DUE)',
    }
    for key, label in labels.items():
        match = re.search(rf'^\s*{label}\s*:\s*(.+?)(?=\s{{2,}}(?:Date|Due):|$)', text, re.I | re.M)
        if match:
            result[key] = match[1].strip()
    if 'vendor' not in result:
        match = re.search(r'^\s*FROM:\s*(.+)$', text, re.I | re.M)
        if match:
            result['vendor'] = match[1].strip()
    if 'invoice_number' not in result:
        match = re.search(r'\b(?:INV[- ]?\d+)\b', text, re.I)
        if match:
            result['invoice_number'] = match[0]
    # PDF headers can contain Date/Due on the same row as another field.
    for key, label in [('date', 'Date'), ('due_date', 'Due')]:
        if key not in result:
            match = re.search(rf'\b{label}:\s*(\d{{4}}-\d{{2}}-\d{{2}})', text, re.I)
            if match:
                result[key] = match[1]
    numeric = r'[+\-]?[\dOo][\dOo,]*(?:\.[\dOo]+)?'
    money = rf'\$?{numeric}'
    for raw in text.splitlines():
        line = raw.strip()
        # Totals are matched from start of line to avoid subtotal/line confusion.
        match = re.fullmatch(
            rf'(Subtotal|Sales Tax|Tax(?:\s*\([\d.]+%\))?|Shipping|Grand Total|Total Amount|Total|Amt)\s*:\s*({money})',
            line,
            re.I,
        )
        if match:
            label = match[1].lower()
            key = (
                'subtotal'
                if label == 'subtotal'
                else 'tax_amount'
                if 'tax' in label
                else 'shipping'
                if label == 'shipping'
                else 'total'
            )
            result[key] = match[2]
            rate = re.search(r'([\d.]+)%', label)
            if rate:
                result['tax_rate'] = str(Decimal(rate[1]) / 100)
            continue
        # qty / @ layouts, email x12 layouts, and aligned table rows.
        patterns = [
            rf'(.+?)\s+qty:?\s*({numeric})\s+(?:unit price:\s*|@\s*)({money})(?:\s+ea)?',
            rf'-?\s*(.+?)\s+x({numeric})\s+({money})\s+each',
            rf'(.+?)\s{{2,}}({numeric})\s+({money})\s+({money})(?:\s+.*)?',
        ]
        for pattern in patterns:
            match = re.fullmatch(pattern, line, re.I)
            if match:
                item, quantity, price = match.groups()[:3]
                row = {'item': item.strip(), 'quantity': quantity, 'unit_price': price}
                if len(match.groups()) == 4:
                    row['amount'] = match[4]
                result['line_items'].append(row)
                break
    return result


def document_text(path):
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader

        return '\n'.join(p.extract_text(extraction_mode='layout') or '' for p in PdfReader(path).pages)
    return path.read_text()


DOCUMENT_SUFFIXES = ('.json', '.csv', '.xml', '.txt', '.pdf')


TEXT_SUFFIXES = ('.txt', '.pdf')
# Text we could not decode: our problem, not the vendor's (POLICY 2).
UNREADABLE = (UnicodeDecodeError, PdfReadError)
# A file we could not read or parse.
PARSE_ERRORS = (
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    InvalidOperation,
    ET.ParseError,
    PdfReadError,
    FileNotFoundError,
)


def load_invoice(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.json':
        data = json.loads(path.read_text(), parse_float=Decimal)
    elif suffix == '.csv':
        data = csv_mapping(path.read_text())
    elif suffix == '.xml':
        root = ET.fromstring(path.read_text())
        data = {child.tag: child.text for group in ('header', 'totals') for child in root.findall(f'{group}/*')}
        data['line_items'] = [
            {('item' if c.tag == 'name' else c.tag): c.text for c in row} for row in root.findall('line_items/item')
        ]
    elif suffix in ('.txt', '.pdf'):
        text = document_text(path)
        data = text_mapping(text)
    else:
        raise ValueError(f'Unsupported format: {suffix}')
    if not isinstance(data, dict):
        raise ValueError('Invoice must be an object')
    return from_mapping(data, path.name)
