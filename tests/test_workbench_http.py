"""HTTP-level tests: a real threaded server on an ephemeral loopback port."""
import http.client
import re
import shutil
import sqlite3
import threading
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from invoices.workbench.server import make_server
from invoices.workbench.service import Workbench
from tests.test_workbench import EXTRAS, ROOT
import json


@pytest.fixture
def app(tmp_path):
    data = tmp_path / 'data'
    shutil.copytree(ROOT / 'data/invoices', data)
    for name, body in EXTRAS:
        (data / name).write_text(json.dumps(body))
    wb = Workbench(tmp_path / 'wb.db', data)
    wb.ingest()
    server = make_server(wb, port=0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    wb.port = server.server_address[1]
    yield wb
    server.shutdown()
    server.server_close()


class Client:
    def __init__(self, app, actor='observer'):
        self.app, self.actor = app, actor

    def request(self, method, path, form=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.app.port, timeout=10)
        h = {'Cookie': f'demo_actor={self.actor}', **(headers or {})}
        body = None
        if form is not None:
            body = urlencode(form)
            h.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        if raw_body is not None:
            body = raw_body
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        conn.close()
        return out

    def get(self, path, **kw):
        status, headers, data = self.request('GET', path, **kw)
        return status, headers, data.decode('utf-8', 'replace')

    def post(self, path, form, **kw):
        status, headers, data = self.request('POST', path, form=form, **kw)
        return status, headers, data.decode('utf-8', 'replace')

    def hidden(self, case_id):
        _, _, page = self.get(f'/case/{case_id}')
        rev = re.search(r'name="rev" value="(\d+)"', page)
        ev = re.search(r'name="evidence" value="([0-9a-f]{64})"', page)
        return {'rev': rev[1], 'evidence': ev[1]}


def snapshot(app):
    conn = sqlite3.connect(app.db_path)
    try:
        return [conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                for t in ('events', 'field_edits', 'decisions', 'case_payments', 'mock_payments')] + \
               [conn.execute('SELECT group_concat(rev) FROM cases').fetchone()[0]]
    finally:
        conn.close()


def test_every_page_renders_and_get_never_changes_state(app):
    before = snapshot(app)
    c = Client(app, 'manager')
    status, _, inbox = c.get('/')
    assert status == 200 and 'Exception inbox' in inbox
    links = sorted(set(re.findall(r'href="(/case/[A-Za-z0-9_-]+)"', inbox)))
    assert len(links) == 19            # 16 corpus cases + 3 synthetic
    for link in links:
        assert c.get(link)[0] == 200
    assert c.get('/?status=blocked&team=Procurement&q=1008')[0] == 200
    assert snapshot(app) == before


def test_inbox_filters_and_grouping(app):
    c = Client(app)
    _, _, page = c.get('/?status=needs_version')
    assert 'INV-1004' in page and 'INV-1001' not in page
    _, _, page = c.get('/?status=blocked&team=Procurement')
    assert 'INV-1008' in page and 'INV-1016' in page and 'INV-1002' not in page
    _, _, page = c.get('/?q=summit')
    assert page.count('class="row"') == 1 and 'PDF, TXT (dup)' in page
    assert 'Gadgets Co.' not in page


def test_action_urls_do_not_accept_get(app):
    before = snapshot(app)
    c = Client(app, 'manager')
    for action in ('pay', 'approve', 'edit', 'assign', 'select-version'):
        assert c.get(f'/case/invoice_1001/{action}')[0] == 404
    assert c.get('/actor?actor=vp')[0] == 404
    assert snapshot(app) == before


def test_hostile_document_content_is_escaped_everywhere(app):
    c = Client(app, 'clerk')
    _, _, inbox = c.get('/')
    _, _, case = c.get('/case/invoice_9003')
    for page in (inbox, case):
        assert '<script>alert(1)</script>' not in page
        assert '<img src=x' not in page
        assert '<b>note</b>' not in page
    assert 'Evil &lt;script&gt;alert(1)&lt;/script&gt; &amp; Co' in case
    assert '&lt;img src=x onerror=alert(1)&gt;' in case
    status, headers, raw = c.get('/case/invoice_9003/doc/invoice_9003.json/raw')
    assert status == 200 and headers['content-type'].startswith('text/plain')
    assert headers['x-content-type-options'] == 'nosniff' and 'sandbox' in headers['content-security-policy']
    csp = c.get('/case/invoice_9003')[1]['content-security-policy']
    assert "default-src 'none'" in csp and 'script-src' not in csp         # no script source is ever allowed


def test_each_format_is_viewable_from_its_original_bytes(app):
    c = Client(app)
    cases = {'invoice_1001': ('invoice_1001.txt', 'text/plain'), 'invoice_1006': ('invoice_1006.csv', 'text/plain'),
             'invoice_1014': ('invoice_1014.xml', 'text/plain'), 'invoice_1005': ('invoice_1005.json', 'text/plain')}
    for case_id, (name, ctype) in cases.items():
        status, headers, body = c.get(f'/case/{case_id}/doc/{name}/raw')
        assert status == 200 and headers['content-type'].startswith(ctype)
        assert body.encode() == (ROOT / 'data/invoices' / name).read_bytes()
    status, headers, body = c.request('GET', '/case/invoice_1011/doc/invoice_1011.pdf/raw')
    assert status == 200 and headers['content-type'] == 'application/pdf'
    assert body == (ROOT / 'data/invoices/invoice_1011.pdf').read_bytes() and body.startswith(b'%PDF')
    _, _, page = c.get('/case/invoice_1011')
    assert '<iframe class="pdf"' in page and 'invoice_1011.pdf/raw' in page
    _, _, xml_page = c.get('/case/invoice_1014')
    assert '&lt;invoice&gt;' in xml_page and '<invoice>' not in xml_page      # XML shown escaped inline


@pytest.mark.parametrize('path', [
    '/case/invoice_1011/doc/..%2F..%2FAGENTS.md/raw', '/case/invoice_1011/doc/../../AGENTS.md/raw',
    '/case/invoice_1011/doc/%2Fetc%2Fpasswd/raw', '/case/invoice_1011/doc/invoice_1001.txt/raw',   # other case's file
    '/case/..%2Fx/doc/a.txt/raw', '/case/invoice_1011/doc/nonexistent.pdf/raw', '/case/invoice_1011/doc/invoice_1011.pdf%00/raw',
    '/case/invoice_1011/doc/invoice_1011.pdf/raw/extra'])
def test_document_route_resists_traversal_and_foreign_files(app, path):
    status, _, body = Client(app).get(path)
    assert status in (400, 404) and 'root:' not in body and 'Galatiq' not in body


def test_tampered_source_file_is_refused_over_http(app):
    (app.data_dir / 'invoice_1001.txt').write_text('tampered')
    status, _, body = Client(app).get('/case/invoice_1001/doc/invoice_1001.txt/raw')
    assert status == 409 and 'tampered' not in body
    _, _, page = Client(app).get('/case/invoice_1001')
    assert 'no longer matches' in page


def test_host_origin_and_content_type_guards(app):
    c = Client(app, 'manager')
    assert c.get('/', headers={'Host': 'evil.example'})[0] == 421
    before = snapshot(app)
    form = {**c.hidden('invoice_1001'), 'text': 'a note'}
    assert c.post('/case/invoice_1001/comment', form, headers={'Origin': 'http://evil.example'})[0] == 403
    assert c.post('/case/invoice_1001/comment', form, headers={'Origin': 'null'})[0] == 403
    assert c.post('/case/invoice_1001/comment', form, headers={'Content-Type': 'application/json'})[0] == 415
    assert c.request('POST', '/case/invoice_1001/comment', raw_body='x' * 300_000,
                     headers={'Content-Type': 'application/x-www-form-urlencoded'})[0] == 413
    assert snapshot(app) == before
    ok = c.post('/case/invoice_1001/comment', form, headers={'Origin': f'http://127.0.0.1:{app.port}'})
    assert ok[0] == 303          # a same-origin post works, and the refused ones did not poison the server


def test_role_cookie_is_validated_and_enforced_server_side(app):
    before = snapshot(app)
    for actor in ('observer', 'not-a-role', ''):
        c = Client(app, actor)
        status, _, page = c.post('/case/invoice_1001/assign', {'rev': '1', 'assignee': 'Treasury', 'reason': 'because'})
        assert status == 403 and 'may not do' in page
    status, headers, _ = Client(app).post('/actor', {'actor': 'vp', 'next': '/case/invoice_1001'})
    assert status == 303 and headers['location'] == '/case/invoice_1001' and 'SameSite=Strict' in headers['set-cookie']
    status, headers, _ = Client(app).post('/actor', {'actor': 'vp', 'next': 'https://evil.example/'})
    assert headers['location'] == '/'                       # no open redirect
    assert Client(app).post('/actor', {'actor': 'root'})[0] == 422
    assert snapshot(app) == before


def test_full_review_flow_over_http_including_replay_and_stale_tabs(app):
    clerk, mgr = Client(app, 'clerk'), Client(app, 'manager')
    tab_a = clerk.hidden('invoice_1004')
    tab_b = mgr.hidden('invoice_1004')          # a second tab opened at the same revision

    status, headers, _ = clerk.post('/case/invoice_1004/select-version', {
        **tab_a, 'filename': 'invoice_1004_revised.json', 'reason': 'R1 matches the PO amendment'})
    assert status == 303 and headers['location'].endswith('?done=select#history')
    # the other tab still holds the old revision and cannot sign off or pay
    status, _, page = mgr.post('/case/invoice_1004/approve', {**tab_b, 'gate': 'ap', 'reason': 'signing the stale view'})
    assert status == 409 and 'changed since you opened it' in page
    assert mgr.post('/case/invoice_1004/pay', tab_b)[0] == 409

    fresh = mgr.hidden('invoice_1004')
    assert mgr.post('/case/invoice_1004/pay', fresh)[0] == 422            # eligible for sign-off, not signed
    assert snapshot(app)[4] == 0
    assert clerk.post('/case/invoice_1004/approve', {**fresh, 'gate': 'ap', 'reason': 'clerk sign'})[0] == 403
    assert mgr.post('/case/invoice_1004/approve', {**fresh, 'gate': 'ap', 'reason': 'Verified the amended PO'})[0] == 303
    fresh = mgr.hidden('invoice_1004')
    assert mgr.post('/case/invoice_1004/pay', fresh)[0] == 303
    assert snapshot(app)[3:5] == [1, 1]
    # replay of the exact same form, then a fresh form for the paid case
    assert mgr.post('/case/invoice_1004/pay', fresh)[0] == 409
    paid = app.case('invoice_1004')           # the paid page has no forms, so build a current-looking one
    status, _, msg = mgr.post('/case/invoice_1004/pay', {'rev': str(paid.rev), 'evidence': paid.evidence})
    assert status == 422 and 'already recorded' in msg
    assert snapshot(app)[3:5] == [1, 1]
    _, _, page = mgr.get('/case/invoice_1004')
    assert 'Recorded' in page and 'Record mock payment' not in page and 'Paid (mock)' in page


def test_correction_form_round_trip_over_http(app):
    c = Client(app, 'clerk')
    _, _, page = c.get('/case/invoice_9002')
    assert 'name="f:total"' in unescape(page) and 'value="5000"' in page
    h = c.hidden('invoice_9002')
    form = {**h, 'doc': 'invoice_9002.json', 'reason': 'Total mis-read from the scan'}
    fields = dict(re.findall(r'name="(f:[^"]+)" value="([^"]*)"', page))
    fields['f:total'] = '500.00'
    status, _, _ = c.post('/case/invoice_9002/edit', {**form, **fields})
    assert status == 303
    _, _, after = c.get('/case/invoice_9002')
    assert 'Awaiting sign-off' in after and 'Total mis-read from the scan' in after
    assert '<span class=before>5000</span>' in after and '<span class=after>500.00</span>' in after
    # an edit that changes nothing is rejected rather than silently accepted
    status, _, msg = c.post('/case/invoice_9002/edit', {**c.hidden('invoice_9002'), 'doc': 'invoice_9002.json',
                                                         'reason': 'no change here', **fields, 'f:total': '500'})
    assert status == 422 and 'No field changed' in msg


def test_correction_request_resolution_requires_fresh_approval_over_http(app):
    mgr, clerk = Client(app, 'manager'), Client(app, 'clerk')
    case = 'invoice_1001'
    base = f'/case/{case}'
    assert mgr.post(base+'/approve', {**mgr.hidden(case), 'gate':'ap', 'reason':'Checked original'})[0] == 303
    assert clerk.post(base+'/request-correction', {**clerk.hidden(case), 'party':'Vendor', 'message':'Verify remittance details'})[0] == 303
    status, _, page = clerk.get(base)
    assert status == 200 and 'Resolve correction request' in page and 'Correction request open' in page
    assert mgr.post(base+'/pay', mgr.hidden(case))[0] == 422
    assert clerk.post(base+'/resolve-correction', {**clerk.hidden(case), 'reason':'Vendor confirmed existing details'})[0] == 303
    assert mgr.post(base+'/pay', mgr.hidden(case))[0] == 422
    assert mgr.post(base+'/approve', {**mgr.hidden(case), 'gate':'ap', 'reason':'Reviewed resolution'})[0] == 303
    assert mgr.post(base+'/pay', mgr.hidden(case))[0] == 303
    assert len(app.ledger()) == 1


def test_extreme_correction_is_422_and_overflow_document_still_renders(app):
    c = Client(app, 'clerk')
    case = 'invoice_1001'
    before = snapshot(app)
    assert c.post(f'/case/{case}/edit', {**c.hidden(case), 'doc':'invoice_1001.txt',
                  'f:line.0.unit_price':'1e100', 'reason':'Mistyped price'})[0] == 422
    assert snapshot(app) == before
    (app.data_dir/'overflow.json').write_text(json.dumps({
        'invoice_number':'INV-9999','vendor':'Overflow Ltd','date':'2026-01-01','due_date':'2026-02-01',
        'line_items':[{'item':'WidgetA','quantity':1,'unit_price':'1e100'}],'total':'1e100'}))
    app.ingest()
    assert c.get('/')[0] == 200
    status, _, page = c.get('/case/overflow')
    assert status == 200 and 'Unreadable document' in page
