"""Render frozen POLICY-labeled scenarios, without importing the invoice pipeline.

Run: uv run python evals/generate.py
The manifest is the oracle. PDF metadata and image rendering are fixed; the
lockfile pins rendering dependencies. Images deliberately have no text layer.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
from textwrap import wrap
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal('0.01')


def money(value):
    return f'{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}'


def render_text(facts):
    lines = facts['lines']
    subtotal = sum(Decimal(line['quantity']) * Decimal(line['unit_price']) for line in lines)
    tax = Decimal(
        facts.get('printed_tax', money(subtotal * Decimal(facts['tax_rate']) / 100 + Decimal(facts['tax_delta'])))
    )
    # A printed subtotal may differ from the lines, so small errors can stack (POLICY 1's overall cap).
    printed_subtotal = subtotal + Decimal(facts.get('subtotal_delta', '0'))
    total = printed_subtotal + tax + Decimal(facts['total_delta'])
    header = [
        'INVOICE',
        f"Vendor: {facts['vendor']}",
        f"Invoice Number: {facts['invoice_number']}",
        f"Date: {facts['date']}",
        f"Due Date: {facts['due_date']}",
        'Currency: USD',
        '',
    ]
    rows = []
    layout = facts['layout']
    if layout == 'table':
        rows.append('Item | Quantity | Unit Price | Amount')
    for line in lines:
        item, qty, price = line['item'], line['quantity'], money(line['unit_price'])
        amount = money(Decimal(qty) * Decimal(price))
        if layout == 'table':
            rows.append(f'{item} | {qty} | ${price} | ${amount}')
        elif layout == 'prose':
            rows.append(f'{item}: {qty} units at ${price}; line amount ${amount}')
        else:
            rows.append(f'{item} quantity {qty}, unit price ${price}, amount ${amount}')
    total_label = {'key': 'Total', 'table': 'Amount due', 'prose': 'Balance due'}[layout]
    footer = [
        '',
        f'Subtotal: ${money(printed_subtotal)}',
        f"Tax ({facts['tax_rate']}%): ${money(tax)}",
        f'{total_label}: ${money(total)}',
    ]
    if 'alternate_delta' in facts:
        footer.append(f"Total: ${money(total + Decimal(facts['alternate_delta']))}")
    if facts['note']:
        footer += [''] + wrap('Notes: ' + facts['note'], width=80)
    text = '\n'.join(header + rows + footer) + '\n'
    return text, money(printed_subtotal), money(tax), money(total)


def render_pdf(text, image_only=False):
    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 5, 1, tzinfo=timezone.utc))
    pdf.set_title('Invoice')
    pdf.add_page()
    if image_only:
        image = Image.new('RGB', (1400, 1800), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=23)
        draw.multiline_text((80, 80), text, font=font, fill='black', spacing=15)
        data = BytesIO()
        image.save(data, format='PNG')
        pdf.image(data, x=10, y=10, w=190)
    else:
        pdf.set_font('Courier', size=10)
        for line in text.splitlines():
            # Keep each source row intact in the extracted text and on the page.
            assert pdf.get_string_width(line) < 190, line
            pdf.cell(w=0, h=7, text=line, new_x='LMARGIN', new_y='NEXT')
    return bytes(pdf.output())


def main():
    manifest = json.loads((ROOT / 'evals/generated-manifest.json').read_text())
    folder = ROOT / 'evals/generated-invoices'
    folder.mkdir(exist_ok=True)
    cases = []
    identities = set()
    for pattern in manifest['patterns']:
        for facts in pattern['documents']:
            text, subtotal, tax, total = render_text(facts)
            image_only = facts['format'] == 'image'
            content = render_pdf(text, image_only) if facts['format'] != 'txt' else text.encode()
            path = folder / facts['file']
            path.write_bytes(content)
            if path.suffix == '.pdf':
                extracted = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(content)).pages)
                assert bool(extracted.strip()) != image_only
                if not image_only:
                    assert all(row in extracted for row in text.splitlines() if row)
            case = dict(
                file=path.name,
                sha256=sha256(content).hexdigest(),
                **pattern['expected'],
                batch_decision=facts['batch_decision'],
                policy_decisions=pattern['policy_decisions'],
                pattern=pattern['pattern'],
            )
            if not image_only:
                case.update({k: facts[k] for k in ('vendor', 'invoice_number', 'date', 'due_date', 'currency')})
                case.update(
                    subtotal=subtotal,
                    tax=tax,
                    quantities={line['item']: line['quantity'] for line in facts['lines']},
                    unit_prices={line['item']: line['unit_price'] for line in facts['lines']},
                )
                if 'alternate_delta' not in facts:
                    case['total'] = total
                if pattern.get('route'):
                    case['route'] = pattern['route']
                    if facts['batch_decision'] == 'approve':
                        case['batch_route'] = pattern['route']
                identity = (facts['vendor'], facts['invoice_number'])
                assert identity not in identities or 8 in pattern['policy_decisions']
                identities.add(identity)
            cases.append(case)
    assert len(cases) == 46 and sum(c['decision'] == 'approve' for c in cases) >= len(cases) * Decimal('.4')
    assert {p.name for p in folder.iterdir()} == {c['file'] for c in cases}
    (ROOT / 'evals/expected-generated.json').write_text(
        json.dumps(
            dict(
                label_status=manifest['label_status'],
                policy='evals/POLICY.md; per-case policy_decisions cite the oracle',
                cases=cases,
            ),
            indent=2,
        )
        + '\n'
    )
    print(f'Rendered and self-checked {len(cases)} invoices')


if __name__ == '__main__':
    main()
