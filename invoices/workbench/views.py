"""Server-rendered HTML. Every dynamic value goes through `e()`; there is no script."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from urllib.parse import quote, urlencode

from .policy import (ACTORS, CORRECTION_PARTIES, PERMISSIONS, ROLE_LABELS, TEAMS, reason_label)
from .service import (HEADER_FIELDS, LINE_FIELDS, STATUS_LABELS, field_value)

MAX_DISPLAY_BYTES = 200_000

CSS = """
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d2227;--muted:#5d6670;--line:#dcd9d2;--accent:#1f5f8b;--accent-ink:#fff;
--ok:#1d6b43;--ok-bg:#e1f1e8;--warn:#8a5a00;--warn-bg:#fbefd2;--bad:#a12626;--bad-bg:#f8e1e1;--info:#1f5f8b;--info-bg:#e0edf6;
--neutral-bg:#ebeae6;--radius:8px}
@media (prefers-color-scheme:dark){:root{--bg:#15181b;--panel:#1d2126;--ink:#e8e6e1;--muted:#a2a9b0;--line:#363c43;
--accent:#6db0dd;--accent-ink:#0d1a24;--ok:#7fd0a1;--ok-bg:#183425;--warn:#f0c46b;--warn-bg:#3a2c0e;--bad:#f09a9a;--bad-bg:#3d1c1c;
--info:#8ec6ec;--info-bg:#162b3b;--neutral-bg:#2a2f35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
header.top{background:var(--panel);border-bottom:1px solid var(--line);padding:10px 24px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
header.top h1{font-size:17px;margin:0}
header.top h1 a{color:inherit;text-decoration:none}
header.top .grow{flex:1}
.demo{background:var(--warn-bg);color:var(--warn);padding:6px 24px;font-size:13px;border-bottom:1px solid var(--line)}
main{max-width:1500px;margin:0 auto;padding:20px 24px 60px}
h2{font-size:16px;margin:0 0 10px}
h3{font-size:14px;margin:16px 0 6px}
.muted{color:var(--muted)}
.small{font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px;margin-bottom:16px}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:10px 14px;min-width:130px;text-decoration:none;color:var(--ink)}
.tile b{display:block;font-size:24px;line-height:1.1}
.tile.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.row:hover{background:var(--neutral-bg)}
.chip{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12.5px;font-weight:600;margin:0 4px 4px 0;background:var(--neutral-bg);white-space:nowrap}
.chip.ok{background:var(--ok-bg);color:var(--ok)}.chip.warn{background:var(--warn-bg);color:var(--warn)}
.chip.bad{background:var(--bad-bg);color:var(--bad)}.chip.info{background:var(--info-bg);color:var(--info)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:14px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:3px}
input[type=text],select,textarea{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);width:100%}
textarea{min-height:64px}
.filters input,.filters select{width:auto;min-width:150px}
button,.btn{font:inherit;font-weight:600;padding:7px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:transparent;color:var(--accent)}
button.danger{background:var(--bad);border-color:var(--bad);color:#fff}
button[disabled]{opacity:.5;cursor:not-allowed}
.split{display:grid;grid-template-columns:minmax(380px,5fr) 7fr;gap:16px;align-items:start}
.split .sticky{position:sticky;top:12px}
@media (max-width:1000px){.split{grid-template-columns:1fr}.split .sticky{position:static}}
.gates{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:16px}
.gate{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:10px 12px}
.gate h3{margin:0 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.gate .state{font-weight:700}
.doc{background:var(--neutral-bg);border-radius:6px;padding:12px;overflow:auto;max-height:560px;white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
iframe.pdf{width:100%;height:600px;border:1px solid var(--line);border-radius:6px;background:#fff}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tabs a{padding:5px 10px;border:1px solid var(--line);border-radius:6px;text-decoration:none;font-size:13px;color:var(--ink)}
.tabs a.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.diff{background:var(--warn-bg)}
.bad-cell{color:var(--bad);font-weight:700}
.reason{border-left:4px solid var(--bad);padding:6px 10px;margin:6px 0;background:var(--bad-bg);border-radius:0 6px 6px 0}
.reason.gate{border-color:var(--warn);background:var(--warn-bg)}
details.action{border:1px solid var(--line);border-radius:var(--radius);margin-bottom:10px;background:var(--panel)}
details.action>summary{padding:10px 14px;cursor:pointer;font-weight:600}
details.action>div{padding:4px 14px 14px}
.notice{padding:10px 14px;border-radius:var(--radius);margin-bottom:14px;font-weight:600}
.notice.ok{background:var(--ok-bg);color:var(--ok)}.notice.err{background:var(--bad-bg);color:var(--bad)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.edit-table input{padding:3px 6px;min-width:90px}
.hist td{font-size:13.5px}
.hist .before{color:var(--bad);text-decoration:line-through}.hist .after{color:var(--ok);font-weight:600}
.tw{overflow-x:auto}
"""


def e(value):
    return escape('' if value is None else str(value), quote=True)


def money(value, currency=''):
    if value is None:
        return '-'
    text = f'{value:,.2f}' if isinstance(value, Decimal) else str(value)
    return f'{text} {e(currency)}'.strip()


def qty(value):
    return format(value.normalize(), 'f') if isinstance(value, Decimal) else str(value)


def parse_ts(ts):
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def age(ts, now):
    secs = max(0, int((now - parse_ts(ts)).total_seconds()))
    if secs < 90:
        return f'{secs}s'
    if secs < 5400:
        return f'{round(secs / 60)}m'
    if secs < 172800:
        return f'{round(secs / 3600)}h'
    return f'{secs // 86400}d'


STATUS_TONE = {'paid': 'ok', 'rejected': 'bad', 'needs_version': 'warn', 'blocked': 'bad',
               'awaiting_approval': 'info', 'ready_to_pay': 'ok'}


def status_chip(st):
    label = STATUS_LABELS[st.status]
    if st.status in ('blocked', 'needs_version') and st.row['workflow'] == 'correction_requested':
        label += ' · correction requested'
    elif st.status in ('blocked', 'needs_version') and st.row['workflow'] == 'escalated':
        label += ' · escalated'
    return f'<span class="chip {STATUS_TONE[st.status]}">{e(label)}</span>'


def reason_chips(st):
    out = [f'<span class="chip bad" title="Blocker: only corrected data or a selected version clears it">{e(reason_label(r))}</span>'
           for r in st.blockers]
    out += [f'<span class="chip warn" title="Approval gate">{e(reason_label(r))}</span>' for r in st.gates]
    return ''.join(out) or '<span class="muted">none</span>'


def page(title, body, actor, path='/', now=None):
    options = ''.join(
        f'<option value="{e(a.id)}"{" selected" if a.id == actor.id else ""}>{e(a.name)} - {e(ROLE_LABELS[a.role])}</option>'
        for a in ACTORS.values())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body>
<header class="top"><h1><a href="/">Invoice Review Workbench</a></h1><span class="muted small">local prototype</span>
<span class="grow"></span>
<form method="post" action="/actor" style="display:flex;gap:8px;align-items:end">
<input type="hidden" name="next" value="{e(path)}">
<div><label for="actor">Demo actor</label><select id="actor" name="actor">{options}</select></div>
<button class="secondary" type="submit">Switch</button></form></header>
<div class="demo">Local demo. The actor selector is <b>not authentication</b>: any viewer can pick any role. Payments are <b>mock ledger rows only</b>; no external call is made.</div>
<main>{body}</main></body></html>"""


# ---------------------------------------------------------------- inbox

FILTERS = [('all', 'All cases'), ('blocked', 'Blocked'), ('needs_version', 'Needs version'),
           ('awaiting_approval', 'Awaiting sign-off'), ('ready_to_pay', 'Ready for mock payment'),
           ('paid', 'Paid (mock)'), ('rejected', 'Rejected')]


def inbox(states, query, actor, now):
    status, team, text = query.get('status', 'all'), query.get('team', ''), query.get('q', '').strip()
    counts = {k: sum(1 for s in states if s.status == k) for k, _ in FILTERS[1:]}
    counts['all'] = len(states)
    shown = [s for s in states
             if (status == 'all' or s.status == status)
             and (not team or s.responsible.split(' (')[0].replace('Waiting on ', '') == team)
             and (not text or text.lower() in f'{s.number} {s.vendor} {" ".join(d.filename for d in s.docs)}'.lower())]
    shown.sort(key=lambda s: (s.row['waiting_since'], s.case_id))
    tiles = ''.join(
        f'<a class="tile{" on" if status == k else ""}" href="/?{e(urlencode({"status": k}))}"><b>{counts[k]}</b>{e(label)}</a>'
        for k, label in FILTERS)
    teams = sorted({s.responsible.split(' (')[0].replace('Waiting on ', '') for s in states})
    team_opts = '<option value="">Anyone</option>' + ''.join(
        f'<option{" selected" if t == team else ""}>{e(t)}</option>' for t in teams)
    status_opts = ''.join(f'<option value="{k}"{" selected" if k == status else ""}>{e(l)}</option>' for k, l in FILTERS)
    rows = []
    for s in shown:
        docs = ', '.join(f'{d.fmt.upper()}' + (' (dup)' if d.role == 'duplicate' else '') for d in s.docs)
        amount = ' / '.join(money(t, c) for t, c in s.amounts) or '-'
        rows.append(f"""<tr class="row"><td><a href="/case/{e(quote(s.case_id))}"><b>{e(s.number)}</b></a>
<div class="muted small">{len(s.docs)} document{'s' if len(s.docs) != 1 else ''}: {e(docs)}</div></td>
<td>{e(s.vendor) or '<span class="muted">(missing)</span>'}</td><td class="num">{amount}</td>
<td>{status_chip(s)}</td><td>{reason_chips(s)}</td><td>{e(s.responsible)}</td>
<td class="num" title="Since {e(s.row['waiting_since'])}">{e(age(s.row['waiting_since'], now))}</td></tr>""")
    table = ('<div class="tw"><table><thead><tr><th>Invoice</th><th>Vendor</th><th class="num">Amount</th><th>Status</th>'
             '<th>Blockers and gates</th><th>Responsible</th><th class="num">Waiting</th></tr></thead><tbody>'
             + ''.join(rows) + '</tbody></table></div>') if rows else '<p class="muted">No cases match these filters.</p>'
    return page('Exception inbox', f"""
<h2>Exception inbox</h2>
<p class="muted small">One row per invoice. Duplicate formats and revisions of the same invoice are grouped into a single case and are never separately payable.
Waiting counts from the last status or ownership change (first load for untouched cases). Red chips are blockers; amber is an approval gate.</p>
<div class="tiles">{tiles}</div>
<form class="filters" method="get" action="/"><div><label for="fs">Status</label><select id="fs" name="status">{status_opts}</select></div>
<div><label for="ft">Responsible</label><select id="ft" name="team">{team_opts}</select></div>
<div><label for="fq">Search</label><input id="fq" type="text" name="q" value="{e(text)}" placeholder="invoice, vendor, file"></div>
<button type="submit">Filter</button> <a class="btn secondary" style="border-color:var(--line)" href="/">Clear</a></form>
<div class="panel">{table}</div>""", actor, '/', now)


# ---------------------------------------------------------------- case page

def _gate_cards(st, actor):
    def card(title, state, tone, detail=''):
        return f'<div class="gate"><h3>{e(title)}</h3><div class="state"><span class="chip {tone}">{e(state)}</span></div><div class="small muted">{detail}</div></div>'
    cards = []
    if st.conflict:
        cards.append(card('1 · Core validation', 'Hold: competing versions', 'warn', 'Select the authoritative version; validation then runs on it.'))
    elif st.effective is None:
        cards.append(card('1 · Core validation', 'Reject: unreadable', 'bad', ''))
    else:
        tone = {'approve': 'ok', 'hold': 'warn', 'reject': 'bad'}[st.decision]
        word = {'approve': 'Eligible', 'hold': 'Hold', 'reject': 'Reject'}[st.decision]
        if st.decision == 'hold' and not st.blockers:
            word, tone = 'Eligible, needs VP gate', 'info'
        cards.append(card('1 · Core validation', word, tone,
                          'Eligibility only. It is not a sign-off.' if not st.blockers else
                          f'{len(st.blockers)} blocker(s) unresolved.'))
    for gate, title, who in (('ap', '2 · AP Manager sign-off', 'AP Manager'), ('vp', '3 · VP Finance sign-off', 'VP Finance')):
        if gate not in st.required:
            if gate == 'vp':
                cards.append(card(title, 'Not required', '', 'Required only above 10,000.00 USD.'))
            continue
        ap = st.approvals.get(gate)
        stale = [s for s in st.stale_approvals if s['gate'] == gate]
        if ap:
            cards.append(card(title, 'Signed', 'ok', f"{e(ap['actor_name'])} ({e(ROLE_LABELS.get(ap['role'], ap['role']))})<br>{e(ap['ts'])}"))
        elif stale:
            cards.append(card(title, 'Stale: data changed', 'warn', 'Earlier sign-off no longer applies. Sign again after review.'))
        else:
            cards.append(card(title, 'Pending', 'info', f'Owner: {who}.'))
    if st.payment:
        p = st.payment
        cards.append(card('4 · Mock payment', 'Recorded', 'ok', f"{e(p['currency'])} {e(p['amount'])} · {e(p['ts'])}"))
    elif st.payable:
        cards.append(card('4 · Mock payment', 'Eligible', 'ok', 'All gates satisfied for the current data.'))
    else:
        cards.append(card('4 · Mock payment', 'Not eligible', 'bad', e(st.problems[0]) if st.problems else ''))
    return '<div class="gates">' + ''.join(cards) + '</div>'


def _viewer(wb, st, doc):
    base = f'/case/{quote(st.case_id)}/doc/{quote(doc.filename)}/raw'
    head = (f'<p class="small muted">{e(doc.filename)} · {doc.size:,} bytes · sha256 <code>{e(doc.sha256[:16])}…</code> · '
            f'<a href="{base}" target="_blank" rel="noopener">open raw</a>. Original bytes are never modified.</p>')
    if doc.fmt == 'pdf':
        return head + f'<iframe class="pdf" title="Original PDF {e(doc.filename)}" src="{base}"></iframe>'
    try:
        path, _ = wb.document_path(st.case_id, doc.filename)
        raw = path.read_bytes()[:MAX_DISPLAY_BYTES]
        text = raw.decode('utf-8', errors='replace')
        if path.stat().st_size > MAX_DISPLAY_BYTES:
            text += '\n… (truncated for display)'
    except Exception as exc:    # noqa: BLE001 - show why evidence is unavailable instead of failing the page
        return head + f'<div class="notice err">{e(exc)}</div>'
    return head + f'<pre class="doc">{e(text)}</pre>'


def _field_table(st, doc):
    inv, orig = doc.effective, doc.original
    rows = []
    for fid, label, _ in HEADER_FIELDS:
        cur, was = field_value(inv, fid), field_value(orig, fid)
        mark = ' class="diff"' if cur != was else ''
        rows.append(f'<tr{mark}><td>{e(label)}</td><td>{e(was) or "<span class=muted>-</span>"}</td><td>{e(cur) or "<span class=muted>-</span>"}</td></tr>')
    return ('<div class="tw"><table><thead><tr><th>Field</th><th>As parsed from file</th><th>Current (after corrections)</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>')


def _calc_tables(st):
    b, inv = st.breakdown, st.invoice
    lines = ''.join(f"""<tr><td>{x['index'] + 1}</td><td>{e(x['item'])}</td><td class="num">{qty(x['quantity'])}</td>
<td class="num">{money(x['unit_price'])}</td><td class="num">{money(x['stated'])}</td>
<td class="num {'bad-cell' if not x['ok'] else ''}">{money(x['computed'])}{'' if x['ok'] else ' ≠ stated'}</td></tr>""" for x in b['lines'])
    sums = f"""<tr><td>Lines add up to</td><td class="num">{money(b['computed_subtotal'])}</td><td class="muted small">stated subtotal {money(inv.subtotal)}</td><td>{'' if b['subtotal_ok'] else '<span class="bad-cell">mismatch</span>'}</td></tr>
<tr><td>Tax (rate {e(inv.tax_rate) if inv.tax_rate is not None else 'n/a'})</td><td class="num">{money(inv.tax)}</td><td class="muted small">{'expected ' + money(b['expected_tax']) if b['expected_tax'] is not None else 'no rate to check'}</td><td>{'' if b['tax_ok'] else '<span class="bad-cell">mismatch</span>'}</td></tr>
<tr><td>Shipping</td><td class="num">{money(inv.shipping)}</td><td></td><td></td></tr>
<tr><td><b>Expected total</b></td><td class="num"><b>{money(b['expected_total'])}</b></td><td class="muted small">stated total {money(inv.total)}</td><td>{'' if b['total_ok'] else '<span class="bad-cell">mismatch</span>'}</td></tr>"""
    stock_label = {'ok': ('In stock', 'ok'), 'unknown': ('Unknown item', 'bad'), 'out_of_stock': ('Out of stock', 'bad'),
                   'exceeded': ('Exceeds stock', 'bad')}
    inventory = ''.join(f"""<tr><td>{e(i['name'])}</td><td class="num">{qty(i['requested'])}</td>
<td class="num">{'-' if i['stock'] is None else i['stock']}</td>
<td><span class="chip {stock_label[i['status']][1]}">{stock_label[i['status']][0]}</span></td></tr>""" for i in b['items'])
    return f"""<h3>Line calculations</h3><div class="tw"><table><thead><tr><th>#</th><th>Item</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Stated amount</th><th class="num">Qty × price</th></tr></thead><tbody>{lines}</tbody></table></div>
<div class="tw"><table><thead><tr><th>Summary</th><th class="num">Value</th><th>Check</th><th></th></tr></thead><tbody>{sums}</tbody></table></div>
<h3>Inventory (aggregated per item, SQLite snapshot)</h3><div class="tw"><table><thead><tr><th>Item</th><th class="num">Requested</th><th class="num">Stock</th><th>Result</th></tr></thead><tbody>{inventory}</tbody></table></div>"""


def _compare(st):
    docs = [d for d in st.docs if d.effective]
    if len(docs) < 2:
        return ''
    rows = []

    def row(label, values):
        diff = len({v for v in values}) > 1
        rows.append(f'<tr{" class=diff" if diff else ""}><td>{e(label)}</td>' + ''.join(f'<td>{e(v) or "-"}</td>' for v in values) + '</tr>')
    for fid, label, _ in HEADER_FIELDS:
        row(label, [field_value(d.effective, fid) for d in docs])
    items = sorted({l.item for d in docs for l in d.effective.lines})
    for item in items:
        row(f'Line: {item} (qty × price)', [
            ' + '.join(f'{qty(l.quantity)} × {l.unit_price}' for l in d.effective.lines if l.item == item) or 'absent'
            for d in docs])
    row('Notes', [d.effective.notes[:160] if '\n' not in d.effective.notes else '' for d in docs])
    row('Core result (alone)', [f"{d.result['decision']}: {', '.join(d.result['reasons']) or 'ok'}" for d in docs])
    head = ''.join(f'<th>{e(d.filename)}<div class="chip {"ok" if d.role == "effective" else "warn" if d.role == "competing" else ""}">{e(d.role)}</div></th>' for d in docs)
    if st.conflict:
        note = '<p class="small">These documents disagree (highlighted rows). Choose the authoritative one below; the other becomes superseded and unpayable.</p>'
    elif st.row['selected_filename']:
        note = f'<p class="small">{e(st.row["selected_filename"])} was selected as authoritative; the others are superseded.</p>'
    else:
        note = '<p class="muted small">Core treats these as the same invoice (duplicate formats). Highlighted rows are layout or extraction differences only; the extra copies are never payable.</p>'
    return f'<div class="panel"><h2>Document versions</h2>{note}<div class="tw"><table><thead><tr><th>Field</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div></div>'


def _hidden(st, extra=''):
    return (f'<input type="hidden" name="rev" value="{st.rev}"><input type="hidden" name="evidence" value="{e(st.evidence)}">' + extra)


def _forms(st, actor, doc):
    perms = PERMISSIONS.get(actor.role, set())
    cid = quote(st.case_id)
    out = []
    closed = st.payment or st.rejection

    def block(name, perm, title, inner, open_=False):
        if perm not in perms:
            return
        out.append(f'<details class="action"{" open" if open_ else ""}><summary>{e(title)}</summary><div>{inner}</div></details>')

    def form(action, inner, button, cls='', hidden=''):
        return (f'<form method="post" action="/case/{cid}/{action}">{_hidden(st, hidden)}{inner}'
                f'<p><button class="{cls}" type="submit">{e(button)}</button></p></form>')

    counter = [0]

    def reason(name='reason', label='Reason (required, saved with your name and role)'):
        counter[0] += 1
        uid = f'{name}-{counter[0]}'
        return (f'<label for="{uid}">{e(label)}</label>'
                f'<textarea id="{uid}" name="{name}" required minlength="3" maxlength="2000"></textarea>')

    if closed:
        out.append(f'<div class="notice err">{"A mock payment is recorded" if st.payment else "This case is rejected"}; corrections and assignments are locked'
                   f'{"" if st.payment else ". Reopen to continue"}.</div>')
    # --- compare / select authoritative version
    readable = [d for d in st.docs if d.effective]
    if len(readable) > 1 and not closed:
        choices = ''.join(
            f'<label style="font-weight:400"><input type="radio" name="filename" value="{e(d.filename)}" required'
            f'{" checked" if st.row["selected_filename"] == d.filename else ""}> {e(d.filename)} '
            f'<span class="muted small">({e(d.effective.revision or "no revision")}, total {money(d.effective.total, d.effective.currency)})</span></label>'
            for d in readable)
        block('select', 'select_version', 'Select authoritative version',
              form('select-version', f'<p class="small muted">The chosen document is validated; the others are marked superseded. This invalidates earlier sign-offs.</p>{choices}{reason()}', 'Mark as authoritative'),
              open_=st.conflict)
    # --- corrections
    if doc.effective and not closed:
        rows = []
        for fid, label, _ in HEADER_FIELDS:
            rows.append(f'<tr><td><label for="f_{fid}" style="font-weight:400">{e(label)}</label></td><td class="muted small">{e(field_value(doc.original, fid)) or "-"}</td>'
                        f'<td><input type="text" id="f_{fid}" name="f:{e(fid)}" value="{e(field_value(doc.effective, fid))}"></td></tr>')
        for i, l in enumerate(doc.effective.lines):
            for name, label, _ in LINE_FIELDS:
                fid = f'line.{i}.{name}'
                rows.append(f'<tr><td><label for="f_{i}_{name}" style="font-weight:400">Line {i + 1} {e(label.lower())}</label></td>'
                            f'<td class="muted small">{e(field_value(doc.original, fid)) or "-"}</td>'
                            f'<td><input type="text" id="f_{i}_{name}" name="f:{e(fid)}" value="{e(field_value(doc.effective, fid))}"></td></tr>')
        inner = (f'<p class="small muted">Correct what the extraction got wrong compared with the original document at left. '
                 f'The file stays untouched; before/after is saved. Saving revalidates immediately and voids earlier sign-offs. '
                 f'Notes are not editable, so a vendor-change flag cannot be edited away.</p>'
                 f'<p class="small">Editing: <b>{e(doc.filename)}</b></p>'
                 f'<div class="tw"><table class="edit-table"><thead><tr><th>Field</th><th>As parsed</th><th>Corrected value</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
                 f'{reason("reason", "Why this correction is right (required)")}')
        block('edit', 'edit', 'Correct extracted fields and revalidate',
              form('edit', inner, 'Save corrections and revalidate', hidden=f'<input type="hidden" name="doc" value="{e(doc.filename)}">'))
    # --- request correction / assign / escalate
    if not closed:
        parties = ''.join(f'<option>{e(p)}</option>' for p in CORRECTION_PARTIES)
        block('request', 'request_correction', 'Request correction (math, unknown item, stock, vendor change)',
              form('request-correction', f'<label for="party">Ask</label><select id="party" name="party">{parties}</select>{reason("message", "What needs correcting (required)")}', 'Record correction request')
              + '<p class="small muted">Records the request and who the case waits on. It does not clear any blocker.</p>')
        if st.row['workflow'] == 'correction_requested':
            block('resolve-request', 'resolve_correction', 'Resolve correction request',
                  form('resolve-correction', '<p>Resolve only after checking the correction. Remaining validation blockers must be cleared first. Fresh sign-offs will be required.</p>' + reason(), 'Resolve request'))
        people = ''.join(f'<option>{e(a.name)}</option>' for a in ACTORS.values() if a.role != 'observer')
        teams = ''.join(f'<option>{e(t)}</option>' for t in TEAMS)
        target = f'<label for="assignee">To</label><select id="assignee" name="assignee"><optgroup label="Teams">{teams}</optgroup><optgroup label="Demo people">{people}</optgroup></select>'
        block('assign', 'assign', 'Assign', form('assign', target + reason(), 'Assign'))
        block('escalate', 'escalate', 'Escalate',
              form('escalate', f'<label for="target">Escalate to</label><select id="target" name="target">{teams}</select>{reason()}', 'Escalate'))
        block('reval', 'revalidate', 'Revalidate now',
              form('revalidate', '<p class="small muted">Re-runs validation against the current inventory and policy, and logs the result.</p>', 'Revalidate', 'secondary'))
    block('comment', 'comment', 'Add a note',
          f'<form method="post" action="/case/{cid}/comment"><input type="hidden" name="rev" value="{st.rev}">{reason("text", "Note")}<p><button class="secondary" type="submit">Save note</button></p></form>')
    # --- decisions
    if not closed:
        for gate, perm, who in (('ap', 'approve_ap', 'AP Manager'), ('vp', 'approve_vp', 'VP Finance')):
            if gate in st.required and gate not in st.approvals:
                reasons_left = []
                if st.conflict:
                    reasons_left.append('select the authoritative version')
                if st.blockers:
                    reasons_left.append('clear blockers')
                dis = ' disabled' if reasons_left else ''
                note = (f'<p class="small bad-cell">Not yet: {e(" and ".join(reasons_left))}.</p>' if reasons_left else
                        f'<p class="small muted">Signs off the exact data shown on this page. Any later change voids it.</p>')
                block(f'approve-{gate}', perm, f'Record {who} sign-off',
                      note + f'<form method="post" action="/case/{cid}/approve">{_hidden(st, f"<input type=hidden name=gate value={gate}>")}'
                      f'{reason("reason", "Approval reason (required)")}<p><button type="submit"{dis}>Sign off as {who}</button></p></form>',
                      open_=not reasons_left)
        block('reject', 'reject', 'Reject invoice', form('reject', reason(), 'Reject invoice', 'danger'))
    if st.rejection:
        block('reopen', 'reopen', 'Reopen', form('reopen', reason(), 'Reopen case'))
    if 'pay' in perms and not st.payment:
        dis = '' if st.payable else ' disabled'
        why = '' if st.payable else '<ul class="small">' + ''.join(f'<li>{e(p)}</li>' for p in st.problems) + '</ul>'
        out.append(f'<div class="panel"><h2>Mock payment</h2>{why}<form method="post" action="/case/{cid}/pay">{_hidden(st)}'
                   f'<button type="submit"{dis}>Record mock payment</button> <span class="small muted">Local ledger row only. The server rechecks every gate; a repeat is refused.</span></form></div>')
    missing = sorted(p.replace('_', ' ') for p in ('edit', 'assign', 'approve_ap', 'approve_vp', 'pay') if p not in perms)
    if missing:
        out.append(f'<p class="small muted">Your demo role ({e(ROLE_LABELS.get(actor.role, actor.role))}) cannot: {e(", ".join(missing))}. Switch actor at top right.</p>')
    return ''.join(out)


def _history(events):
    rows = []
    for ev in events:
        detail = ''
        d = ev['detail']
        if ev['kind'] == 'edit' and d:
            detail = '<ul class="small">' + ''.join(
                f"<li>{e(c['label'])}: <span class=before>{e(c['before']) or '(empty)'}</span> → <span class=after>{e(c['after']) or '(empty)'}</span>"
                f"{' <span class=muted>(file said ' + e(c['original']) + ')</span>' if c['original'] != c['before'] else ''}</li>" for c in d['changes']) + '</ul>'
        rows.append(f"<tr><td class='small'>{e(ev['ts'])}</td><td>{e(ev['actor_name'])}<div class='muted small'>{e(ROLE_LABELS.get(ev['role'], ev['role']))}</div></td>"
                    f"<td><span class='chip'>{e(ev['kind'].replace('_', ' '))}</span></td><td>{e(ev['summary'])}{detail}</td><td class='num small'>r{ev['rev_after']}</td></tr>")
    return ('<div class="panel hist" id="history"><h2>Approval and change history</h2><div class="tw"><table><thead><tr><th>When (UTC)</th><th>Who</th><th>Event</th><th>What and why</th><th class="num">Rev</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div></div>')


NOTICES = {
    'edit': 'Corrections saved and the invoice was revalidated.', 'select': 'Authoritative version recorded; validation re-ran on it.',
    'assign': 'Assignment saved.', 'request': 'Correction request recorded. Blockers stay until the data is corrected.',
    'resolve-correction': 'Correction request resolved. Obtain fresh sign-offs.',
    'escalate': 'Escalation recorded.', 'comment': 'Note saved.', 'revalidate': 'Validation re-run and logged.',
    'approve': 'Sign-off recorded for the exact data shown.', 'reject': 'Invoice rejected; it cannot be paid.',
    'reopen': 'Case reopened.', 'pay': 'Mock payment recorded in the local ledger. No external call was made.',
}


def case_page(wb, st, history, actor, query, now, notice=None, error=None):
    doc_name = query.get('doc', '')
    doc = next((d for d in st.docs if d.filename == doc_name), None) or st.effective or \
        next((d for d in st.docs if d.effective), None) or st.docs[0]
    tabs = ''.join(
        f'<a class="{"on" if d is doc else ""}" href="/case/{e(quote(st.case_id))}?{e(urlencode({"doc": d.filename}))}">{e(d.filename)} · {e(d.role)}</a>'
        for d in st.docs)
    viewer = f'<div class="panel sticky"><h2>Original document</h2><div class="tabs">{tabs}</div>{_viewer(wb, st, doc)}</div>'
    reasons_html = ''
    for code in st.reasons:
        gate = code in st.gates
        reasons_html += (f'<div class="reason{" gate" if gate else ""}"><b>{e(reason_label(code))}</b>'
                         f'{" (approval gate)" if gate else ""}<div class="small">{e(st.explanations.get(code, ""))}</div></div>')
    if not st.reasons:
        reasons_html = '<p class="small">No validation findings: eligible on the current data.</p>'
    detail = ''
    if st.invoice and doc is st.effective:
        detail = f'<div class="panel"><h2>Parsed invoice (selected document)</h2>{_field_table(st, doc)}{_calc_tables(st) if st.breakdown else ''}</div>'
    elif doc.effective:
        detail = (f'<div class="panel"><h2>Parsed fields of {e(doc.filename)} ({e(doc.role)})</h2>'
                  f'<p class="small muted">This document is not the one being validated for payment.</p>{_field_table(st, doc)}</div>')
    elif doc.parse_error:
        detail = f'<div class="panel"><h2>Not readable</h2><p>{e(doc.parse_error)}</p></div>'
    head = f"""<p><a href="/">← Inbox</a></p>
<div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap"><h2 style="font-size:22px;margin:0">{e(st.number)} · {e(st.vendor) or '(vendor missing)'}</h2>
{status_chip(st)}<span style="font-size:18px;font-weight:700">{' / '.join(money(t, c) for t, c in st.amounts) or '-'}</span>
<span class="muted small">revision {st.rev} · responsible: {e(st.responsible)} · waiting {e(age(st.row['waiting_since'], now))}</span></div>"""
    banner = ''
    if notice:
        banner = f'<div class="notice ok" role="status">{e(notice)}</div>'
    if error:
        banner = f'<div class="notice err" role="alert">{e(error)}</div>'
    body = f"""{head}{banner}
{_gate_cards(st, actor)}
<div class="split"><div>{viewer}</div><div>
<div class="panel"><h2>Why it is {e(STATUS_LABELS[st.status].lower())}</h2>{reasons_html}</div>
{_compare(st)}{detail}
<div class="panel"><h2>Resolve</h2>{_forms(st, actor, doc)}</div></div></div>
{_history(history)}"""
    return page(f'{st.number} · review', body, actor, f'/case/{quote(st.case_id)}', now)


def error_page(title, message, actor, back='/', now=None):
    return page(title, f'<div class="panel"><h2>{e(title)}</h2><p>{e(message)}</p><p><a class="btn" href="{e(back)}">Reload the case</a></p></div>', actor, back, now)
