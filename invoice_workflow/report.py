"""Static review report: one self-contained HTML file built only from the run's results and case packets."""

import re
from html import escape
from pathlib import Path

from .packet import OWNERS, needs_packet

AUDIT = {'suspicious_instructions', 'urgency_pressure', 'payment_detail_change'}
MATH = {'line_mismatch', 'subtotal_mismatch', 'tax_mismatch', 'total_mismatch'}
STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--muted:#5f6368;--line:#d9d9de;--card:#f7f7f9;
--grounded:#c8ecd0;--ambiguous:#fde9a8;--ungrounded:#f6c4c4;--fraud:#e9c8f5;--math:#fdd3a8}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--muted:#a0a0aa;--line:#34343a;--card:#1d1d21;
--grounded:#1f5130;--ambiguous:#5c4b12;--ungrounded:#6b2323;--fraud:#52265f;--math:#6a3d12}}
body{margin:0 auto;max-width:960px;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:16px;margin:0 0 6px}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 ui-monospace,monospace;margin:8px 0 0}
mark{color:inherit;border-radius:3px;padding:0 1px}.grounded{background:var(--grounded)}.ambiguous{background:var(--ambiguous)}
.ungrounded{background:var(--ungrounded)}.fraud{background:var(--fraud)}.math{background:var(--math)}
.legend mark{margin-right:8px}ul{margin:4px 0;padding-left:20px}.step{font-weight:600}
"""


def _spans(entry, text):
    """Offsets for one evidence record. Repeated values prefer the occurrence on the field's own label line."""
    if entry.get('start') is not None:
        return [(entry['start'], entry['end'])]
    spans = [(c['start'], c['end']) for c in entry.get('candidates', [])]
    if entry.get('field') == 'total' and len(spans) > 1:
        labelled = [
            s
            for s in spans
            if re.search(r'(?i)(?<!sub)total|amount due|amt', text[text.rfind('\n', 0, s[0]) + 1 : s[0]])
        ]
        spans = labelled if len(labelled) == 1 else spans
    return spans


def _status(entry, result):
    if entry.get('field') in AUDIT:
        return 'fraud'
    if entry.get('field') == 'total' and MATH & set(result['reasons']):
        return 'math'
    if entry.get('confident') is False:
        return 'ungrounded'
    if entry.get('start') is None:
        return 'ambiguous'
    return 'grounded'


def highlighted(text, result):
    marks = []
    evidence = list(result.get('evidence', [])) + [
        {**j, 'field': j['trigger']} for j in result.get('vp', {}).get('justifications', [])
    ]
    for entry in evidence:
        for start, end in _spans(entry, text):
            if start is not None and 0 <= start < end <= len(text):
                marks.append((start, end, _status(entry, result), entry.get('field', '')))
    out, at = [], 0
    for start, end, status, field in sorted(marks):
        if start < at:
            continue  # overlapping evidence keeps the first span
        out.append(escape(text[at:start]))
        out.append(f'<mark class="{status}" title="{escape(field)}">{escape(text[start:end])}</mark>')
        at = end
    out.append(escape(text[at:]))
    return ''.join(out)


def _numbers(numbers):
    if not numbers:
        return ''
    rows = ''.join(
        f"<tr><td>{escape(line['item'])}</td><td>{escape(line['quantity'] or '')}</td><td>{escape(line['unit_price'] or '')}</td>"
        f"<td>{escape(line['printed_amount'] or '')}</td><td>{escape(line['computed_amount'])}</td></tr>"
        for line in numbers['lines']
    )
    total, subtotal = numbers['total'], numbers['subtotal']
    return (
        '<table><tr><th>Item</th><th>Qty</th><th>Unit price</th><th>Printed</th><th>Computed</th></tr>'
        + rows
        + f"<tr><td colspan=3>Subtotal</td><td>{escape(subtotal['printed'] or '')}</td><td>{escape(subtotal['computed'])}</td></tr>"
        f"<tr><td colspan=3>Tax + shipping</td><td colspan=2>{escape(numbers['tax'])} + {escape(numbers['shipping'])}</td></tr>"
        f"<tr><td colspan=3>Total</td><td>{escape(total['printed'] or '')}</td><td>{escape(total['computed'])}</td></tr></table>"
    )


def _vp(vp):
    if not vp:
        return ''
    parts = [
        f"<p><b>VP agent:</b> {escape(vp.get('decision', 'no decision'))} &middot; outcome {escape(vp.get('outcome', ''))}"
        f" &middot; {vp.get('rounds', 0)} round(s)</p>"
    ]
    if vp.get('rationale'):
        parts.append(f"<p class=muted>{escape(vp['rationale'])}</p>")
    for j in vp.get('justifications', []):
        parts.append(
            f"<p>Stated reason for {escape(j['trigger'])}: <mark class=grounded>{escape(j['quote'])}</mark></p>"
        )
    for i, critique in enumerate(vp.get('critic', []), 1):
        issues = ''.join(
            f"<li>{escape(x['description'])} <span class=muted>({escape(x['path'])})</span></li>"
            for x in critique['issues']
        )
        parts.append(
            f"<p><b>Critic, round {i}:</b> {escape(critique['status'])}</p>" + (f'<ul>{issues}</ul>' if issues else '')
        )
    if vp.get('beyond_authority'):
        parts.append(f"<p>Beyond the agent's authority: {escape(', '.join(vp['beyond_authority']))}</p>")
    return ''.join(parts)


def _card(result):
    packet = result['packet']
    reasons = ''.join(
        f"<li><b>{escape(r['text'])}</b> <span class=muted>({escape(r['code'])}"
        f"{', POLICY decision ' + str(r['policy_decision']) if r['policy_decision'] else ''})</span>"
        f"{' &middot; ' + escape(r['detail']) if r.get('detail') else ''}"
        + ''.join(f" &middot; <q>{escape(e['quote'])}</q>" for e in r['evidence'][:3])
        + '</li>'
        for r in packet['reasons']
    )
    notes = ''.join(f'<li>{escape(n)}</li>' for n in packet.get('notes', []))
    doc = packet.get('document')
    source = (
        f'<details><summary>Source document, evidence highlighted</summary><pre>{highlighted(doc, result)}</pre></details>'
        if doc
        else ''
    )
    return (
        f"<div class=card><h3>{escape(result.get('invoice_number') or '')} <span class=muted>{escape(result['source'])}"
        f" &middot; {escape(result.get('vendor') or '')} &middot; ${escape(result.get('total') or '?')}</span></h3>"
        f"<p>Decision: <b>{escape(packet['decision'])}</b> &middot; <span class=step>Next: {escape(packet['next_step'] or '')}</span></p>"
        f"<ul>{reasons}</ul>"
        + (f'<p>Notes:</p><ul>{notes}</ul>' if notes else '')
        + _vp(packet.get('vp'))
        + f"<details><summary>Numbers checked</summary>{_numbers(packet.get('numbers'))}</details>{source}</div>"
    )


def render(results):
    packets = [r for r in results if needs_packet(r) and r.get('packet')]
    touchless = [r for r in results if not needs_packet(r)]
    counts = {d: sum(r['decision'] == d for r in results) for d in ('hold', 'reject', 'duplicate', 'superseded')}
    vp_paid = [r for r in packets if r['decision'] == 'approve']
    body = [
        f"<h1>Invoice run report</h1><p>{len(results)} documents: <b>{len(touchless)}</b> paid automatically, "
        f"<b>{len(vp_paid)}</b> approved through the VP agent, <b>{counts['hold']}</b> held, <b>{counts['reject']}</b> rejected, "
        f"<b>{counts['duplicate'] + counts['superseded']}</b> duplicate or superseded.</p>",
        '<p class=legend>Evidence: <mark class=grounded>grounded</mark><mark class=ambiguous>repeated value</mark>'
        '<mark class=ungrounded>not confident</mark><mark class=fraud>fraud signal</mark><mark class=math>math mismatch</mark></p>',
    ]
    for owner, label in OWNERS.items():
        group = [r for r in packets if r.get('owner') == owner]
        if group:
            title = 'Approved through the VP agent (audit record)' if owner == 'vp_agent' else label
            body.append(f'<h2>{escape(title)} ({len(group)})</h2>' + ''.join(_card(r) for r in group))
    rows = ''.join(
        f"<tr><td>{escape(r.get('invoice_number') or '')}</td><td>{escape(r['source'])}</td><td>{escape(r.get('vendor') or '')}</td>"
        f"<td>${escape(r.get('total') or '')}</td><td>{escape('; '.join(r.get('notes', [])))}</td></tr>"
        for r in touchless
    )
    body.append(
        f'<h2>Paid automatically ({len(touchless)})</h2><table><tr><th>Invoice</th><th>File</th><th>Vendor</th>'
        f'<th>Amount</th><th>Notes</th></tr>{rows}</table>'
    )
    return (
        '<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>Invoice run report</title><style>{STYLE}</style></head><body>{"".join(body)}</body></html>\n'
    )


def write_report(results, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(results))
    return path
