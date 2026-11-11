"""The checks code makes on an invoice: required fields, arithmetic to the cent, stock, catalog prices
and currency. validate() returns a routed result; no model is involved."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .database import inventory
from .documents import item_key
from .routing import route


HIGH_VALUE_USD_THRESHOLD = Decimal('10000')


# Checks on a model's reading of a text or PDF invoice (POLICY 2).
MATH_REASONS = {'line_mismatch', 'subtotal_mismatch', 'tax_mismatch', 'total_mismatch'}


def expected_tax(inv, subtotal):
    """Tax at the printed rate on the computed subtotal; without a printed rate, the printed tax."""
    if inv.tax_rate is None:
        return inv.tax
    return (subtotal * inv.tax_rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def invoice_findings(inv):
    errors = []
    quantities = defaultdict(Decimal)

    def close(a, b):
        return abs(a - b) <= Decimal('.01')

    for key, value in [
        ('vendor', inv.vendor),
        ('invoice_number', inv.number),
        ('date', inv.date),
        ('due_date', inv.due_date),
    ]:
        if not value or not str(value).strip():
            errors.append('missing_' + key)
    for key, value in [('date', inv.date), ('due_date', inv.due_date)]:
        if value:
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                errors.append('invalid_' + key)
    if not inv.lines:
        errors.append('missing_items')
    subtotal = Decimal('0')
    for line in inv.lines:
        if not line.item.strip():
            errors.append('missing_item')
        if line.quantity <= 0:
            errors.append('invalid_quantity')
        if line.unit_price <= 0:
            errors.append('invalid_price')
        amount = (line.quantity * line.unit_price).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        subtotal += amount
        quantities[item_key(line.item)] += line.quantity
        if line.amount is not None and not close(line.amount, amount):
            errors.append('line_mismatch')
    if inv.subtotal is not None and not close(inv.subtotal, subtotal):
        errors.append('subtotal_mismatch')
    if inv.tax < 0 or inv.shipping < 0 or (inv.tax_rate is not None and not 0 <= inv.tax_rate <= 1):
        errors.append('invalid_charges')
    if not close(inv.tax, expected_tax(inv, subtotal)):
        errors.append('tax_mismatch')
    if inv.total is None:
        errors.append('missing_total')
    else:
        if inv.total <= 0:
            errors.append('invalid_total')
        # Each step may be a cent off, but not the whole: the total is also checked against one computed
        # from the lines alone, so cent errors cannot stack (POLICY 1).
        if not close(
            inv.total, (inv.subtotal if inv.subtotal is not None else subtotal) + inv.tax + inv.shipping
        ) or not close(inv.total, subtotal + expected_tax(inv, subtotal) + inv.shipping):
            errors.append('total_mismatch')
    return errors, quantities


def numbers(inv):
    """Printed versus computed amounts, for the case packet."""

    def text(value):
        return None if value is None else str(value)

    lines = []
    computed = Decimal('0')
    for line in inv.lines:
        amount = (line.quantity * line.unit_price).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        computed += amount
        lines.append(
            {
                'item': line.item,
                'quantity': text(line.quantity),
                'unit_price': text(line.unit_price),
                'printed_amount': text(line.amount),
                'computed_amount': str(amount),
            }
        )
    return {
        'lines': lines,
        'subtotal': {'printed': text(inv.subtotal), 'computed': str(computed)},
        'tax': str(inv.tax),
        'shipping': str(inv.shipping),
        'total': {'printed': text(inv.total), 'computed': str(computed + expected_tax(inv, computed) + inv.shipping)},
    }


CONTRARY_CURRENCY = re.compile(r'[€£]|\b(?:EUR|GBP|CAD)\b|C\$')


US_ADDRESS = re.compile(
    r'\b(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s+\d{5}(?:-\d{4})?\b'
)


def currency_evidence(inv, catalog):
    """POLICY decision 10: why we believe the currency; 'none' holds as currency_unknown."""
    text = inv.notes
    if inv.currency == 'USD' and CONTRARY_CURRENCY.search(text):
        return 'none'
    document = Path(inv.source).suffix.lower() in ('.txt', '.pdf')
    if inv.currency_stated and (not document or re.search(rf'\b{re.escape(inv.currency)}\b', text)):
        return 'stated'
    if '$' in text:
        return 'dollar_sign'
    if US_ADDRESS.search(text):
        return 'us_address'
    if inv.lines and all(
        item_key(line.item) in catalog and line.unit_price == catalog[item_key(line.item)][2] for line in inv.lines
    ):
        return 'catalog_prices'
    return 'none'


def price_variances(inv, catalog):
    """POLICY decision 5: each line priced away from the catalog is a VP trigger."""
    found = []
    for index, line in enumerate(inv.lines):
        entry = catalog.get(item_key(line.item))
        if entry is None or line.unit_price <= 0 or line.unit_price == entry[2]:
            continue
        percent = ((line.unit_price - entry[2]) / entry[2] * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        found.append(
            {
                'line': index,
                'item': line.item,
                'unit_price': str(line.unit_price),
                'catalog_price': str(entry[2]),
                'variance_pct': str(percent),
                # Direction from the exact prices: $250.01 is 0.004% above, which rounds to 0.00%.
                'reason': 'price_above_catalog' if line.unit_price > entry[2] else 'price_below_catalog',
            }
        )
    return found


def rename_notes(text):
    """POLICY decision 9: a vendor rename alone is a note, not a hold."""
    match = re.search(r'\b(formerly|now part of)\s+([^)\n]+)', text, re.I)
    return [f'vendor renamed ({match[1]} {match[2].strip()}): update the vendor record'] if match else []


def validate(inv, conn):
    """Every code check on one invoice, as a routed result with reasons."""
    errors, quantities = invoice_findings(inv)
    holds = []
    if inv.total is not None and inv.currency == 'USD' and inv.total > HIGH_VALUE_USD_THRESHOLD:
        holds.append('high_value')
    catalog = {item_key(name): (name, stock, Decimal(price)) for name, stock, price in inventory(conn)}
    display = {}
    for item, qty in quantities.items():
        canonical, stock, _ = catalog.get(
            item, (next(line.item for line in inv.lines if item_key(line.item) == item), None, None)
        )
        display[canonical] = int(qty) if qty == int(qty) else str(qty)
        if stock is None:
            holds.append('unknown_item')
        elif stock <= 0:
            holds.append('out_of_stock')
        elif qty > stock:
            holds.append('stock_exceeded')
    evidence = currency_evidence(inv, catalog)
    if evidence == 'none':
        holds.append('currency_unknown')
    if inv.currency != 'USD':
        holds.append('unsupported_currency')
    prices = price_variances(inv, catalog) if inv.currency == 'USD' and evidence != 'none' else []
    holds += [p['reason'] for p in prices]
    result = {
        'source': inv.source,
        'invoice_number': inv.number,
        'vendor': inv.vendor,
        'reasons': sorted(set(errors + holds)),
        'total': str(inv.total) if inv.total is not None else None,
        'currency': inv.currency,
        'currency_evidence': evidence,
        'date': inv.date,
        'due_date': inv.due_date,
        'quantities': display,
        'corrections': inv.corrections,
        'notes': rename_notes(inv.notes),
        'numbers': numbers(inv),
    }
    if prices:
        result['prices'] = prices
    return route(result)
