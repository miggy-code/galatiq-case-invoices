"""Reading a text or PDF invoice with the model, and every check code makes before trusting that reading:
each value must quote the document, the regex reading must agree, and the arithmetic must close (POLICY 2)."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import InvalidOperation
from pathlib import Path

from openai import APIError, APITimeoutError

from .documents import PARSE_ERRORS, from_mapping, invoice_key, item_key, load_invoice, number, parsed_date
from .evidence import locate
from .validation import MATH_REASONS, invoice_findings


MODEL_ERRORS = (TimeoutError, ConnectionError, APIError, APITimeoutError)


class ModelStepFailed(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


NUMBER_WORDS = dict(zip('one two three four five six seven eight nine ten'.split(), range(1, 11), strict=True))


ANSWER_ERRORS = (ValueError, TypeError, KeyError, AttributeError, InvalidOperation)


def _total_quote(text, quote, value):
    """Accept a labeled total only when exactly one numeric token matches its value."""
    tokens = re.findall(r'\$?[\dOo][\dOo,]*(?:\.\d+)?', quote)
    matching = []
    for token in tokens:
        try:
            if number(token, []) == value:
                matching.append(token)
        except (ValueError, InvalidOperation):
            pass
    if len(matching) != 1:
        return quote, []
    quoted = locate(text, quote)
    return matching[0], [(c['start'], c['end']) for c in quoted['candidates']]


def _field_evidence(text, name, value, quote, contexts=None, line_span=None, numeric=False):
    """Match the quoted value before locating its tokens inside the allowed source rows."""
    quote = quote if isinstance(quote, str) else ''
    matches = False
    if value is not None and str(value).strip() and quote:
        if name in ('date', 'due_date'):
            matches = parsed_date(quote, []) == str(value)
        elif name == 'invoice_number':
            matches = invoice_key(quote) == invoice_key(str(value))
        elif numeric:
            try:
                matches = number(quote, []) == value
            except (ValueError, InvalidOperation):
                matches = NUMBER_WORDS.get(quote.strip().lower()) == value
                if not matches and name == 'total':
                    quote, contexts = _total_quote(text, quote, value)
                    matches = bool(contexts)
        else:
            matches = item_key(quote) == item_key(str(value))
    result = locate(text, quote, contexts=contexts, token=True) if matches and contexts != [] else locate('', '')
    record = {'field': name, 'value': str(value) if value is not None else None, 'quote': quote, **result}
    if line_span is not None:
        record['line_start'], record['line_end'] = line_span
    return record


def _row_contexts(text, line_quote):
    """Accept whole source rows so a quote cannot borrow values from adjacent items."""
    row = locate(text, line_quote)
    contexts = []
    for candidate in row['candidates']:
        start, end = candidate['start'], candidate['end']
        line_start = text.rfind('\n', 0, start) + 1
        line_end = text.find('\n', end)
        line_end = len(text) if line_end < 0 else line_end
        if '\n' not in text[start:end] and not text[line_start:start].strip() and not text[end:line_end].strip():
            contexts.append((line_start, line_end))
    return contexts


def _resolve_row_evidence(line, row_fields, evidence, text, line_span):
    """Resolve repeated price/amount tokens, then mark reused source spans as ambiguous."""
    price = next((e for e in row_fields if e['field'].endswith('.unit_price')), None)
    amount = next((e for e in row_fields if e['field'].endswith('.amount')), None)
    qty = next((e for e in row_fields if e['field'].endswith('.quantity')), None)
    if price and amount and qty and line_span[0] is not None:
        candidates = price['candidates']
        header = text[: line_span[0]]
        if (
            len(candidates) == 2
            and candidates == amount['candidates']
            and qty['start'] is not None
            and qty['end'] <= candidates[0]['start']
            and re.search(r'(?im)^.*\bunit\s+price\b.*\bamount\b.*$', header)
        ):
            for entry, chosen in ((price, candidates[0]), (amount, candidates[1])):
                entry['start'], entry['end'] = chosen['start'], chosen['end']
                entry['ambiguous'] = False
    if (
        price
        and amount
        and price['found']
        and len(amount['candidates']) == 1
        and amount['candidates'] == price['candidates']
    ):
        # The row prints this number once, as the unit price, so no line amount was printed
        # (typically an Amt:/Total row read as the line amount). Code computes quantity x price.
        line.amount = None
        evidence.remove(amount)
        row_fields.remove(amount)
    overlapping = []
    for left in row_fields:
        for right in row_fields:
            if left is right or left['start'] is None or right['start'] is None:
                continue
            if right['start'] <= left['start'] and right['end'] >= left['end']:
                overlapping.append(left)
                break
    for entry in overlapping:
        entry['start'] = entry['end'] = None
        entry['ambiguous'] = True


def extraction_evidence(invoice, supplied, text):
    """Ground header quotes and line-item values within their quoted source row."""
    supplied = supplied if isinstance(supplied, dict) else {}
    lines = supplied.get('line_items')
    lines = lines if isinstance(lines, list) else []
    evidence = []

    for name, value in (
        ('vendor', invoice.vendor),
        ('invoice_number', invoice.number),
        ('date', invoice.date),
        ('due_date', invoice.due_date),
        ('total', invoice.total),
    ):
        evidence.append(_field_evidence(text, name, value, supplied.get(name), numeric=name == 'total'))
    for i, line in enumerate(invoice.lines):
        quotes = lines[i] if i < len(lines) and isinstance(lines[i], dict) else {}
        line_quote = quotes.get('line_quote')
        contexts = _row_contexts(text, line_quote)
        line_span = contexts[0] if len(contexts) == 1 else (None, None)
        for name in ('item', 'quantity', 'unit_price', 'amount'):
            value = getattr(line, name)
            if name == 'amount' and value is None:
                continue
            evidence.append(
                _field_evidence(
                    text,
                    f'line_items[{i}].{name}',
                    value,
                    quotes.get(name),
                    contexts,
                    line_span,
                    numeric=name != 'item',
                )
            )
        row_fields = [e for e in evidence if e['field'].startswith(f'line_items[{i}].')]
        _resolve_row_evidence(line, row_fields, evidence, text, line_span)
    return evidence


def parse_extraction(raw, source, text):
    """Turn the model's JSON answer into an Invoice plus its evidence. Raises on a malformed answer."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object')
    required = ('invoice_number', 'vendor', 'date', 'due_date', 'line_items', 'total')
    if any(key not in data for key in required) or not isinstance(data['line_items'], list):
        raise ValueError('JSON must contain invoice_number, vendor, date, due_date, line_items and total')
    rows = (data.get('evidence') or {}).get('line_items') if isinstance(data.get('evidence'), dict) else None
    if isinstance(rows, list) and any(isinstance(r, dict) and '\n' in str(r.get('line_quote') or '') for r in rows):
        raise ValueError('Each line_quote must contain exactly one source row; do not infer an amount from another row')
    for key in ('tax_amount', 'shipping', 'currency'):
        if data.get(key) is None:
            data.pop(key, None)
    for line in data['line_items']:
        if isinstance(line, dict) and isinstance(line.get('quantity'), str):
            line['quantity'] = NUMBER_WORDS.get(line['quantity'].strip().lower(), line['quantity'])
    invoice = from_mapping(data, source)
    return invoice, extraction_evidence(invoice, data.get('evidence'), text)


def extract_invoice(text, source, model):
    """Ask the model for the invoice; if its answer is malformed, ask once more with the error.
    Returns (invoice, evidence), or (None, []) after two bad answers. Model errors propagate."""
    issue = None
    for _ in range(2):
        try:
            return parse_extraction(model(text, issue), source, text)
        except ANSWER_ERRORS as exc:
            issue = f'Invalid JSON or invoice fields: {exc}'
    return None, []


MONEY_FIELD = re.compile(r'total|line_items\[\d+\]\.(?:quantity|unit_price|amount)')


PRINTED_TOTAL = re.compile(
    r'(?im)^\s*(?:grand\s+total|total(?:\s+(?:amount|due))?|amount\s+due|balance\s+due|amt)\s*:?\s*(\$?[\dOo][\dOo,]*(?:\.[\dOo]+)?)\s*$'
)


def printed_totals(text):
    """Distinct amounts printed on total lines; more than one means we cannot tell which to pay."""
    return {number(m[1], []) for m in PRINTED_TOTAL.finditer(text)}


def cross_check(inv, baseline, text):
    """Money fields where the regex reading disagrees with the model's, or the document with itself."""
    disagree = set()
    printed = printed_totals(text)
    if (
        len(printed) > 1
        or (printed and inv.total not in printed)
        or (baseline is not None and baseline.total is not None and baseline.total != inv.total)
    ):
        # A total that differs from the printed total fails the source check.
        disagree.add('total')
    if baseline is not None and baseline.lines:
        _, regex_quantities = invoice_findings(baseline)
        _, model_quantities = invoice_findings(inv)
        prices = defaultdict(set)
        for line in baseline.lines:
            prices[item_key(line.item)].add(line.unit_price)
        for index, line in enumerate(inv.lines):
            key = item_key(line.item)
            if key in regex_quantities and regex_quantities[key] != model_quantities[key]:
                disagree.add(f'line_items[{index}].quantity')
            if key in prices and line.unit_price not in prices[key]:
                disagree.add(f'line_items[{index}].unit_price')
    return disagree


def confidence_gate(inv, baseline, evidence, text):
    """POLICY decision 2: a money or quantity value is confident when it is grounded, the
    regex cross-check agrees and the arithmetic closes. Returns True when any is not."""
    disagree = cross_check(inv, baseline, text)
    closes = not MATH_REASONS & set(invoice_findings(inv)[0])
    uncertain = 'total' in disagree
    for entry in evidence:
        if MONEY_FIELD.fullmatch(entry['field']):
            trusted = entry['found'] and entry['field'] not in disagree
            entry['confident'] = trusted and closes
            uncertain = uncertain or not trusted
    return uncertain


def extract_document(source, text, model, offline):
    """LLM extraction checked by code: grounding, regex cross-check and the confidence gate."""
    try:
        baseline = load_invoice(source)
    except PARSE_ERRORS:
        baseline = None
    try:
        inv, evidence = extract_invoice(text, Path(source).name, model)
    except FileNotFoundError:
        if not offline:
            raise
        if baseline is None:
            raise ModelStepFailed('recording_missing') from None
        return baseline, [], ['recording_missing']
    except MODEL_ERRORS as exc:
        raise ModelStepFailed('extraction_failed', type(exc).__name__) from exc
    if inv is None:
        raise ModelStepFailed('extraction_failed')
    inv.notes = text
    extra = []
    evidence = _restore_printed_summary(inv, baseline, evidence, text)
    if any(
        not e['found'] or (e['ambiguous'] and e['field'].startswith('line_items[') and not e['field'].endswith('.item'))
        for e in evidence
    ):
        extra.append('ungrounded_extraction')
    # Two comparisons with the regex reading, for two kinds of misread:
    # - extraction_disagreement compares the whole reading: the total and which items, in what quantities.
    #   It catches a model that drops, adds or renames a line, even when every number it quotes is real.
    # - extraction_uncertain (confidence_gate) checks each money value the model read: quoted in the
    #   document, agreeing with the regex where the regex read it, and summing to the total.
    # Either one means a failed sum may be our misread, so the invoice holds for a clerk (graph.validate_stage).
    if baseline is not None:
        errors, baseline_quantities = invoice_findings(baseline)
        missing = any(e.startswith('missing_') for e in errors)
        candidate_errors, extracted_quantities = invoice_findings(inv)
        candidate_missing = any(e.startswith('missing_') for e in candidate_errors)
        if (
            not missing
            and not candidate_missing
            and (inv.total != baseline.total or extracted_quantities != baseline_quantities)
        ):
            extra.append('extraction_disagreement')
    if confidence_gate(inv, baseline, evidence, text):
        extra.append('extraction_uncertain')
    return inv, evidence, extra


def _restore_printed_summary(inv, baseline, evidence, text):
    """Recover omitted totals and tax rates from the regex reading, retaining source offsets."""
    if baseline is not None and inv.total is None and baseline.total is not None:
        for match in re.finditer(
            r'(?im)^\s*(?:grand total|total amount|total|amt)\s*:\s*(\$?[\dOo,]+(?:\.[\dOo]+)?)', text
        ):
            quote = match.group(1)
            if number(quote, []) == baseline.total:
                inv.total = baseline.total
                evidence = [e for e in evidence if e['field'] != 'total']
                evidence.append(
                    {
                        'field': 'total',
                        'value': str(inv.total),
                        'quote': quote,
                        'start': match.start(1),
                        'end': match.end(1),
                        'found': True,
                        'ambiguous': False,
                        'candidates': [{'start': match.start(1), 'end': match.end(1)}],
                    }
                )
                break
    if baseline is not None and inv.tax_rate is None and baseline.tax_rate is not None:
        # The model returns the tax amount but often drops the printed rate; keep the regex's rate
        # so the rate check (POLICY decision 1) still runs on the model path.
        inv.tax_rate = baseline.tax_rate
    return evidence
