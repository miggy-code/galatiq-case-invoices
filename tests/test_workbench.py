"""Backend tests for the review workbench: real SQLite files, real fixtures, no mocks."""
import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from invoices.core import open_database, process_batch, load_invoice
from invoices.workbench.policy import ACTORS
from invoices.workbench.service import (Forbidden, Invalid, NotFound, Refused, Stale, Workbench)

ROOT = Path(__file__).resolve().parents[1]
CASES = {c['file']: c for c in json.loads((ROOT / 'evals/expected-main.json').read_text())['cases']}
CLERK, MANAGER, VP, OBSERVER = (ACTORS[k] for k in ('clerk', 'manager', 'vp', 'observer'))


def synthetic(name, number, vendor, items, total, notes=None):
    return name, {'invoice_number': number, 'vendor': {'name': vendor}, 'date': '2026-02-01',
                  'due_date': '2026-03-01', 'currency': 'USD',
                  'line_items': items, 'total': total, **({'notes': notes} if notes else {})}


EXTRAS = [
    # over 10K, in stock, arithmetic correct: needs AP and VP sign-off
    synthetic('invoice_9001.json', 'INV-9001', 'Bigco Ltd.', [{'item': 'WidgetA', 'quantity': 12, 'unit_price': 1000}], 12000),
    # extraction typo: stated total 5000 but the lines add up to 500
    synthetic('invoice_9002.json', 'INV-9002', 'Typo Parts Inc.', [{'item': 'WidgetA', 'quantity': 2, 'unit_price': 250}], 5000),
    # hostile strings that must be escaped everywhere
    synthetic('invoice_9003.json', 'INV-9003', 'Evil <script>alert(1)</script> & Co',
              [{'item': '<img src=x onerror=alert(1)>', 'quantity': 1, 'unit_price': 10}], 10, notes='<b>note</b>'),
]


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    data = tmp_path / 'data'
    shutil.copytree(ROOT / 'data/invoices', data)
    for name, body in EXTRAS:
        (data / name).write_text(json.dumps(body))
    clock = Clock()
    wb = Workbench(tmp_path / 'wb.db', data, clock=clock)
    wb.ingest()
    wb.clock_obj = clock
    wb.tmp = tmp_path
    return wb


def st(wb, case_id):
    return wb.case(case_id)


def ledger_count(wb):
    return len(wb.ledger())


def approve_ready(wb, case_id='invoice_1001'):
    s = st(wb, case_id)
    wb.approve(case_id, MANAGER, s.rev, 'ap', s.evidence, 'Checked against source.')
    return st(wb, case_id)


# ------------------------------------------------------------------ seeding and grouping

def test_all_twenty_documents_become_sixteen_cases_with_grouped_duplicates(env):
    states = env.list_cases()
    real = [s for s in states if s.case_id.startswith('invoice_1')]
    assert len(real) == 16
    assert sum(len(s.docs) for s in real) == 20
    by_id = {s.case_id: s for s in states}
    for grouped in ('invoice_1004', 'invoice_1011', 'invoice_1012', 'invoice_1013'):
        assert len(by_id[grouped].docs) == 2
    assert {d.role for d in by_id['invoice_1011'].docs} == {'effective', 'duplicate'}
    assert by_id['invoice_1004'].conflict


def test_workbench_decisions_match_the_core_labels_not_a_weaker_policy(env):
    for s in env.list_cases():
        if not s.case_id.startswith('invoice_1') or s.conflict:
            continue
        label = CASES[s.effective.filename]
        assert s.decision == label['decision'], s.case_id
        assert set(label['reasons']) <= set(s.reasons)
        assert s.status != 'ready_to_pay'
    # nothing is signed off or payable on first load
    assert {s.status for s in env.list_cases()} <= {'blocked', 'needs_version', 'awaiting_approval'}
    assert ledger_count(env) == 0


def test_explanations_and_inventory_table_agree_with_core_reasons(env):
    for s in env.list_cases():
        if not s.effective:
            continue
        assert set(s.explanations) == set(s.reasons)
        shown = {i['status'] for i in s.breakdown['items']}
        assert ('unknown' in shown) == ('unknown_item' in s.reasons)
        assert ('out_of_stock' in shown) == ('out_of_stock' in s.reasons)
        assert ('exceeded' in shown) == ('stock_exceeded' in s.reasons)
        assert (not s.breakdown['total_ok']) == ('total_mismatch' in s.reasons)


def test_ingest_is_idempotent_and_never_resets_existing_work(env):
    s = st(env, 'invoice_1008')
    env.assign('invoice_1008', CLERK, s.rev, 'Procurement', 'Check catalogue')
    again = Workbench(env.db_path, env.data_dir)           # a restart
    report = again.ingest()
    assert report['added'] == [] and len(report['unchanged']) == 23
    after = st(again, 'invoice_1008')
    assert after.row['assignee'] == 'Procurement' and after.rev == s.rev + 1
    assert [e['kind'] for e in again.history('invoice_1008')][:2] == ['assign', 'ingested']


def test_late_document_joins_case_and_voids_signoff(env):
    approve_ready(env)
    s = st(env, 'invoice_1001')
    assert 'ap' in s.approvals
    (env.data_dir / 'invoice_1001_copy.json').write_text(json.dumps({
        'invoice_number': 'INV-1001', 'vendor': 'Widgets Inc.', 'date': '2026-01-15', 'due_date': '2026-02-01',
        'line_items': [{'item': 'WidgetA', 'quantity': 1, 'unit_price': 1}], 'total': 1}))
    env.ingest()
    s2 = st(env, 'invoice_1001')
    assert len(s2.docs) == 2 and s2.conflict and not s2.approvals
    assert any(e['kind'] == 'approval_invalidated' for e in env.history('invoice_1001'))


# ------------------------------------------------------------------ INV-1004 version selection

def test_inv_1004_needs_an_explicit_authoritative_version(env):
    s = st(env, 'invoice_1004')
    assert s.status == 'needs_version' and s.reasons == ['revision_conflict']
    with pytest.raises(Refused):
        env.approve('invoice_1004', MANAGER, s.rev, 'ap', s.evidence, 'try anyway')
    with pytest.raises(Refused):
        env.pay('invoice_1004', MANAGER, s.rev, s.evidence)
    assert ledger_count(env) == 0

    s = env.select_version('invoice_1004', CLERK, s.rev, 'invoice_1004_revised.json', 'PO amendment confirmed', s.evidence)
    roles = {d.filename: d.role for d in s.docs}
    assert roles == {'invoice_1004.json': 'superseded', 'invoice_1004_revised.json': 'effective'}
    assert s.decision == 'approve' and s.invoice.total == 5940 and s.invoice.revision == 'R1'
    assert s.status == 'awaiting_approval'          # selection is not a sign-off
    # reselecting the original flips validation to that document and voids any sign-off
    s = approve_ready(env, 'invoice_1004')
    assert 'ap' in s.approvals
    s = env.select_version('invoice_1004', CLERK, s.rev, 'invoice_1004.json', 'Actually the original', s.evidence)
    assert s.invoice.total == 1890 and not s.approvals and s.status == 'awaiting_approval'
    assert [e['kind'] for e in env.history('invoice_1004')][:2] == ['approval_invalidated', 'select_version']


def test_selection_never_touches_source_files(env):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in env.data_dir.iterdir()}
    s = st(env, 'invoice_1004')
    env.select_version('invoice_1004', CLERK, s.rev, 'invoice_1004_revised.json', 'R1 is current', s.evidence)
    s = st(env, 'invoice_1007')
    env.edit_fields('invoice_1007', CLERK, s.rev, 'invoice_1007.csv', {'total': '15635'}, 'Printed total is wrong', s.evidence)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in env.data_dir.iterdir()}
    assert before == after
    for name in ('invoice_1004.json', 'invoice_1004_revised.json', 'invoice_1007.csv'):
        assert before[name] == CASES[name]['sha256']


def test_selecting_between_one_document_or_unknown_file_is_refused(env):
    s = st(env, 'invoice_1001')
    with pytest.raises(Refused):
        env.select_version('invoice_1001', CLERK, s.rev, 'invoice_1001.txt', 'only one', s.evidence)
    s = st(env, 'invoice_1004')
    with pytest.raises(Invalid):
        env.select_version('invoice_1004', CLERK, s.rev, 'invoice_1001.txt', 'wrong file', s.evidence)


# ------------------------------------------------------------------ corrections and revalidation

def test_correction_revalidates_and_preserves_before_after_history(env):
    s = st(env, 'invoice_9002')
    assert s.decision == 'reject' and 'total_mismatch' in s.reasons and s.status == 'blocked'
    original_total = s.invoice.total
    s = env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', {'total': '500.00', 'vendor': 'Typo Parts Inc.'},
                        'Total was mis-read from the scan; printed total is 500.00', s.evidence)
    assert s.decision == 'approve' and s.reasons == [] and s.status == 'awaiting_approval'
    doc = s.docs[0]
    assert doc.original.total == original_total == 5000          # immutable parsed original
    assert doc.effective.total == 500
    edits = env.edits('invoice_9002')
    assert [(e['field'], e['before'], e['after'], e['actor_name'], e['role']) for e in edits] == \
        [('total', '5000', '500.00', 'Demo AP Clerk', 'ap_clerk')]       # unchanged vendor was not logged
    ev = env.history('invoice_9002')[0]
    assert ev['kind'] == 'edit' and ev['detail']['before'] == 'reject' and ev['detail']['after'] == 'approve'
    assert ev['detail']['changes'][0]['original'] == '5000'
    assert 'mis-read' in ev['summary']


def test_correction_that_does_not_fix_the_problem_stays_blocked(env):
    s = st(env, 'invoice_1007')       # printed total is wrong AND stock is exceeded
    s = env.edit_fields('invoice_1007', CLERK, s.rev, 'invoice_1007.csv', {'total': '15635'}, 'Printed total wrong', s.evidence)
    assert 'total_mismatch' not in s.reasons
    assert 'stock_exceeded' in s.reasons and s.status == 'blocked' and s.blockers
    with pytest.raises(Refused):
        env.approve('invoice_1007', MANAGER, s.rev, 'ap', s.evidence, 'please')


def test_edit_validation_and_scope(env):
    s = st(env, 'invoice_9002')
    for bad, exc in [({'total': 'abc'}, Invalid), ({'total': 'NaN'}, Invalid), ({'line.0.quantity': ''}, Invalid),
                     ({'notes': 'x'}, Invalid), ({'line.9.item': 'x'}, Invalid), ({'total': '5000'}, Invalid)]:
        with pytest.raises(exc):
            env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', bad, 'reason here', s.evidence)
    with pytest.raises(Invalid):    # reason required
        env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', {'total': '500'}, ' ', s.evidence)
    assert env.edits('invoice_9002') == [] and st(env, 'invoice_9002').rev == s.rev


def test_history_tables_are_append_only(env):
    s = st(env, 'invoice_9002')
    env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', {'total': '500'}, 'typo fix', s.evidence)
    conn = sqlite3.connect(env.db_path)
    for sql in ('UPDATE events SET summary = "x"', 'DELETE FROM events', 'UPDATE field_edits SET after = "1"',
                'DELETE FROM field_edits', 'UPDATE documents SET sha256 = "x"'):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()


def test_vendor_change_flag_cannot_be_edited_away(env):
    s = st(env, 'invoice_1012')
    assert s.blockers == ['vendor_change']
    s = env.edit_fields('invoice_1012', CLERK, s.rev, s.effective.filename, {'vendor': 'QuickShip Distributors'},
                        'Spelling fix', s.evidence)
    assert 'vendor_change' in s.reasons and s.status == 'blocked'


# ------------------------------------------------------------------ assignment, escalation, persistence

def test_assignment_escalation_and_requests_persist_across_restart(env):
    clock = env.clock_obj
    s = st(env, 'invoice_1008')
    assert s.responsible == 'Procurement' and s.blockers == ['unknown_item']
    s = env.request_correction('invoice_1008', CLERK, s.rev, 'Vendor', 'Resend with catalogue item codes', s.evidence)
    assert s.responsible == 'Waiting on Vendor' and s.status == 'blocked'
    clock.advance(hours=3)
    s = env.escalate('invoice_1008', CLERK, s.rev, 'VP Finance', 'Vendor is unresponsive and amount is near 10K', s.evidence)
    assert s.row['workflow'] == 'correction_requested' and s.row['assignee'] == 'VP Finance'
    assert s.responsible == 'Waiting on Vendor'
    s = env.assign('invoice_1008', MANAGER, s.rev, 'Demo AP Clerk', 'Take over follow-up', s.evidence)

    restarted = Workbench(env.db_path, env.data_dir, clock=clock)
    restarted.ingest()
    after = st(restarted, 'invoice_1008')
    assert after.row['assignee'] == 'Demo AP Clerk' and after.row['workflow'] == 'correction_requested'
    assert set(after.blockers) == {'unknown_item', 'correction_requested'} and after.status == 'blocked'    # nothing was resolved
    hist = restarted.history('invoice_1008')
    assert [e['kind'] for e in hist][:3] == ['assign', 'escalate', 'request_correction']
    first = hist[2]
    assert (first['actor_name'], first['role'], first['ts']) == ('Demo AP Clerk', 'ap_clerk', '2026-11-06T12:00:00Z')
    assert 'catalogue item codes' in first['summary']
    assert hist[1]['ts'] == '2026-11-06T15:00:00Z' and 'unresponsive' in hist[1]['summary']


def test_waiting_age_resets_on_ownership_change_not_on_comments(env):
    clock = env.clock_obj
    s = st(env, 'invoice_1008')
    since = s.row['waiting_since']
    clock.advance(hours=2)
    env.comment('invoice_1008', CLERK, 'Looking into it')
    assert st(env, 'invoice_1008').row['waiting_since'] == since
    s = env.assign('invoice_1008', CLERK, st(env, 'invoice_1008').rev, 'Inventory / Warehouse', 'Check stock master')
    assert s.row['waiting_since'] == '2026-11-06T14:00:00Z'


def test_unknown_team_and_party_are_rejected(env):
    s = st(env, 'invoice_1008')
    with pytest.raises(Invalid):
        env.assign('invoice_1008', CLERK, s.rev, 'Somebody Else', 'why not')
    with pytest.raises(Invalid):
        env.request_correction('invoice_1008', CLERK, s.rev, 'Aliens', 'fix it')


# ------------------------------------------------------------------ approval, invalidation, stale writes

def test_edit_invalidates_prior_signoff_and_blocks_payment(env):
    s = st(env, 'invoice_9002')
    s = env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', {'total': '500'}, 'typo', s.evidence)
    s = env.approve('invoice_9002', MANAGER, s.rev, 'ap', s.evidence, 'Verified against the PDF')
    assert s.status == 'ready_to_pay' and s.payable
    s = env.edit_fields('invoice_9002', CLERK, s.rev, 'invoice_9002.json', {'due_date': '2026-04-01'}, 'due date typo', s.evidence)
    assert not s.approvals and s.status == 'awaiting_approval' and not s.payable
    kinds = [e['kind'] for e in env.history('invoice_9002')]
    assert kinds[:2] == ['approval_invalidated', 'edit']
    with pytest.raises(Refused, match='sign-off is missing'):
        env.pay('invoice_9002', MANAGER, s.rev, s.evidence)
    assert ledger_count(env) == 0
    conn = sqlite3.connect(env.db_path)       # the invalidation is persisted with a reason
    assert conn.execute('SELECT invalidated_reason FROM decisions').fetchone()[0] == 'invoice data was corrected'
    conn.close()


def test_stale_revision_cannot_overwrite_or_approve(env):
    tab_a = st(env, 'invoice_9002')
    tab_b = st(env, 'invoice_9002')
    env.edit_fields('invoice_9002', CLERK, tab_a.rev, 'invoice_9002.json', {'total': '500'}, 'typo', tab_a.evidence)
    before = len(env.history('invoice_9002'))
    with pytest.raises(Stale):
        env.edit_fields('invoice_9002', CLERK, tab_b.rev, 'invoice_9002.json', {'total': '9999'}, 'stale write', tab_b.evidence)
    with pytest.raises(Stale):
        env.approve('invoice_9002', MANAGER, tab_b.rev, 'ap', tab_b.evidence, 'approving what I saw')
    with pytest.raises(Stale):
        env.assign('invoice_9002', CLERK, tab_b.rev, 'Treasury', 'stale assignment')
    with pytest.raises(Stale):
        env.assign('invoice_9002', CLERK, None, 'Treasury', 'no revision at all')
    assert len(env.history('invoice_9002')) == before
    assert st(env, 'invoice_9002').docs[0].effective.total == 500


def test_signoff_is_bound_to_inventory_evidence(env):
    s = st(env, 'invoice_1001')
    seen = s.evidence
    conn = sqlite3.connect(env.db_path)
    conn.execute("UPDATE inventory SET stock = 0 WHERE item = 'WidgetA'")      # stock changes under the reviewer
    conn.commit()
    with pytest.raises(Stale, match='evidence'):
        env.approve('invoice_1001', MANAGER, s.rev, 'ap', seen, 'approve old view')
    fresh = st(env, 'invoice_1001')
    assert fresh.evidence != seen and 'out_of_stock' in fresh.reasons and fresh.status == 'blocked'

    conn.execute("UPDATE inventory SET stock = 15 WHERE item = 'WidgetA'")      # restored, then signed
    conn.commit()
    s = approve_ready(env)
    assert s.payable
    conn.execute("UPDATE inventory SET stock = 1 WHERE item = 'WidgetA'")       # drifts after sign-off
    conn.commit()
    drift = st(env, 'invoice_1001')
    assert not drift.approvals and drift.stale_approvals and not drift.payable
    with pytest.raises(Refused):
        env.pay('invoice_1001', MANAGER, drift.rev, drift.evidence)
    env.revalidate('invoice_1001', CLERK, drift.rev, drift.evidence)
    assert any(e['kind'] == 'approval_invalidated' for e in env.history('invoice_1001'))
    assert ledger_count(env) == 0
    conn.close()


def test_any_inventory_change_voids_signoff_even_when_validation_still_passes(env):
    s = approve_ready(env)                          # INV-1001 needs 10 WidgetA; stock 15
    seen = s.evidence
    conn = sqlite3.connect(env.db_path)
    conn.execute("UPDATE inventory SET stock = 14 WHERE item = 'WidgetA'")      # still sufficient
    conn.commit()
    conn.close()
    after = st(env, 'invoice_1001')
    assert after.decision == 'approve' and after.reasons == []                  # core outcome unchanged
    assert after.evidence != seen and not after.approvals and after.stale_approvals
    with pytest.raises(Stale):
        env.pay('invoice_1001', MANAGER, after.rev, seen)
    with pytest.raises(Refused, match='sign-off is missing'):
        env.pay('invoice_1001', MANAGER, after.rev, after.evidence)
    assert ledger_count(env) == 0


def test_approval_requires_reason_and_the_right_role(env):
    s = st(env, 'invoice_1001')
    for actor in (CLERK, OBSERVER, VP):
        with pytest.raises(Forbidden):
            env.approve('invoice_1001', actor, s.rev, 'ap', s.evidence, 'not mine to sign')
    with pytest.raises(Invalid):
        env.approve('invoice_1001', MANAGER, s.rev, 'ap', s.evidence, '')
    with pytest.raises(Refused):                    # VP gate does not apply to a 5,000 invoice
        env.approve('invoice_1001', VP, s.rev, 'vp', s.evidence, 'not required')
    s = approve_ready(env)
    saved = s.approvals['ap']
    assert (saved['actor_name'], saved['role'], saved['reason'], saved['ts']) == \
        ('Demo AP Manager', 'ap_manager', 'Checked against source.', '2026-11-06T12:00:00Z')
    with pytest.raises(Refused, match='already recorded'):
        env.approve('invoice_1001', MANAGER, s.rev, 'ap', s.evidence, 'twice')


def test_observer_cannot_change_anything(env):
    s = st(env, 'invoice_1001')
    for call in (lambda: env.comment('invoice_1001', OBSERVER, 'hi'),
                 lambda: env.assign('invoice_1001', OBSERVER, s.rev, 'Treasury', 'because'),
                 lambda: env.pay('invoice_1001', OBSERVER, s.rev, s.evidence),
                 lambda: env.edit_fields('invoice_1001', OBSERVER, s.rev, 'invoice_1001.txt', {'total': '1'}, 'because')):
        with pytest.raises(Forbidden):
            call()


# ------------------------------------------------------------------ high-value VP flow

def test_high_value_needs_vp_signoff_and_ap_signoff(env):
    s = st(env, 'invoice_9001')
    assert s.required == ['ap', 'vp'] and s.gates == ['high_value'] and not s.blockers
    assert s.decision == 'hold' and s.status == 'awaiting_approval'      # core hold, but eligible for the VP gate
    with pytest.raises(Forbidden):
        env.approve('invoice_9001', MANAGER, s.rev, 'vp', s.evidence, 'managers do not own this gate')
    with pytest.raises(Forbidden):
        env.approve('invoice_9001', CLERK, s.rev, 'ap', s.evidence, 'clerk cannot sign')
    s = env.approve('invoice_9001', MANAGER, s.rev, 'ap', s.evidence, 'Within PO, three-way matched')
    assert s.pending == ['vp'] and not s.payable
    with pytest.raises(Refused, match='VP Finance sign-off is missing'):
        env.pay('invoice_9001', MANAGER, s.rev, s.evidence)
    s = env.approve('invoice_9001', VP, s.rev, 'vp', s.evidence, 'High value reviewed and accepted')
    assert s.status == 'ready_to_pay' and s.approvals['vp']['role'] == 'vp_finance'
    with pytest.raises(Forbidden):
        env.pay('invoice_9001', CLERK, s.rev, s.evidence)
    s = env.pay('invoice_9001', VP, s.rev, s.evidence)
    assert s.status == 'paid' and s.payment['amount'] == '12000'
    row = env.ledger()[0]
    assert (row['vendor'], row['invoice_number'], row['currency'], row['source']) == \
        ('bigco ltd', '9001', 'USD', 'invoice_9001.json')


def test_vp_signoff_alone_is_not_enough_and_edit_voids_both(env):
    s = st(env, 'invoice_9001')
    s = env.approve('invoice_9001', VP, s.rev, 'vp', s.evidence, 'VP first')
    assert s.pending == ['ap'] and not s.payable
    s = env.approve('invoice_9001', MANAGER, s.rev, 'ap', s.evidence, 'AP second')
    assert s.payable
    s = env.edit_fields('invoice_9001', CLERK, s.rev, 'invoice_9001.json', {'line.0.quantity': '11', 'total': '11000'}, 'PO says 11 units', s.evidence)
    assert s.approvals == {} and len(s.stale_approvals) == 0 and s.pending == ['ap', 'vp']
    assert sum(e['kind'] == 'approval_invalidated' for e in env.history('invoice_9001')) == 2


def test_lowering_total_under_threshold_drops_the_vp_requirement_only_with_a_logged_correction(env):
    s = st(env, 'invoice_9001')
    s = env.edit_fields('invoice_9001', CLERK, s.rev, 'invoice_9001.json', {'line.0.quantity': '10', 'total': '10000'}, 'qty was 10', s.evidence)
    assert s.required == ['ap'] and 'high_value' not in s.reasons        # boundary: 10,000 is not high value
    assert env.edits('invoice_9001')[0]['reason'] == 'qty was 10'


# ------------------------------------------------------------------ payment safety

BLOCKED = ['invoice_1002', 'invoice_1003', 'invoice_1004', 'invoice_1005', 'invoice_1007', 'invoice_1008',
           'invoice_1009', 'invoice_1012', 'invoice_1013', 'invoice_1014', 'invoice_1016', 'invoice_9002']


@pytest.mark.parametrize('case_id', BLOCKED)
def test_unresolved_cases_can_never_be_paid_or_signed(env, case_id):
    s = st(env, case_id)
    assert s.status in ('blocked', 'needs_version') and not s.payable
    for actor, gate in ((MANAGER, 'ap'), (VP, 'vp')):
        with pytest.raises(Refused):
            env.approve(case_id, actor, s.rev, gate, s.evidence, 'trying to force it through')
    with pytest.raises(Refused):
        env.pay(case_id, VP, s.rev, s.evidence)
    assert ledger_count(env) == 0
    assert env.history(case_id)[0]['kind'] == 'payment_refused'      # the attempt itself is on the record
    assert st(env, case_id).rev == s.rev                              # refusals do not bump the revision


def test_rejected_case_cannot_be_paid_until_reopened_and_resigned(env):
    s = approve_ready(env)
    s = env.reject('invoice_1001', MANAGER, s.rev, 'Duplicate of a prior-year invoice', s.evidence)
    assert s.status == 'rejected' and s.rejection['reason'].startswith('Duplicate')
    with pytest.raises(Refused, match='rejected'):
        env.pay('invoice_1001', MANAGER, s.rev, s.evidence)
    with pytest.raises(Refused):
        env.edit_fields('invoice_1001', CLERK, s.rev, 'invoice_1001.txt', {'total': '1'}, 'edit while rejected')
    with pytest.raises(Forbidden):
        env.reopen('invoice_1001', CLERK, s.rev, 'let me reopen')
    s = env.reopen('invoice_1001', MANAGER, s.rev, 'Rejected in error', s.evidence)
    assert s.status == 'ready_to_pay'          # data is unchanged, so the earlier sign-off still binds
    s = env.pay('invoice_1001', MANAGER, s.rev, s.evidence)
    assert s.status == 'paid'


def test_repeat_and_replayed_payments_are_refused(env):
    s = approve_ready(env)
    old_rev, old_evidence = s.rev, s.evidence
    paid = env.pay('invoice_1001', MANAGER, s.rev, s.evidence)
    assert paid.status == 'paid' and ledger_count(env) == 1
    with pytest.raises(Stale):                         # the same stale browser form again
        env.pay('invoice_1001', MANAGER, old_rev, old_evidence)
    with pytest.raises(Refused, match='already recorded'):      # a fresh form for a paid case
        env.pay('invoice_1001', MANAGER, paid.rev, paid.evidence)
    assert ledger_count(env) == 1
    assert env.history('invoice_1001')[0]['kind'] == 'payment_refused'
    assert sum(e['kind'] == 'payment_recorded' for e in env.history('invoice_1001')) == 1


def test_concurrent_payments_record_exactly_one_row(env):
    s = approve_ready(env)
    rev, evidence = s.rev, s.evidence

    def attempt(_):
        other = Workbench(env.db_path, env.data_dir, clock=env.clock)
        try:
            other.pay('invoice_1001', MANAGER, rev, evidence)
            return 'paid'
        except (Stale, Refused) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(4)))
    assert outcomes.count('paid') == 1 and ledger_count(env) == 1
    conn = sqlite3.connect(env.db_path)
    assert conn.execute('SELECT COUNT(*) FROM case_payments').fetchone()[0] == 1
    conn.close()


def test_core_ledger_row_from_the_cli_blocks_the_workbench_payment(env):
    conn = open_database(env.db_path)
    process_batch([env.data_dir / 'invoice_1001.txt'], conn, record_payments=True)     # earlier CLI run
    conn.close()
    s = approve_ready(env)
    with pytest.raises(Refused, match='ledger already holds'):
        env.pay('invoice_1001', MANAGER, s.rev, s.evidence)
    assert ledger_count(env) == 1 and st(env, 'invoice_1001').status == 'ready_to_pay'
    assert env.history('invoice_1001')[0]['kind'] == 'payment_refused'
    assert 'ledger already holds' in env.history('invoice_1001')[0]['summary']


def test_corrected_vendor_name_cannot_sidestep_ledger_uniqueness(env):
    a = approve_ready(env, 'invoice_1006')
    env.pay('invoice_1006', MANAGER, a.rev, a.evidence)
    # a second case that is the same invoice after normalisation cannot be paid again
    (env.data_dir / 'invoice_1006_b.json').write_text(json.dumps({
        'invoice_number': 'INV-1006', 'vendor': 'Acme Industrial Supplies Co', 'date': '2026-01-25', 'due_date': '2026-02-10',
        'line_items': [{'item': 'WidgetA', 'quantity': 1, 'unit_price': 250}], 'total': 250}))
    env.ingest()
    b = st(env, 'invoice_1006_b')
    b = env.edit_fields('invoice_1006_b', CLERK, b.rev, 'invoice_1006_b.json', {'vendor': 'ACME industrial supplies'}, 'normalise name', b.evidence)
    b = approve_ready(env, 'invoice_1006_b')
    with pytest.raises(Refused, match='ledger already holds'):
        env.pay('invoice_1006_b', MANAGER, b.rev, b.evidence)
    assert ledger_count(env) == 1


# ------------------------------------------------------------------ misc

def test_unknown_case_and_document_lookup(env):
    with pytest.raises(NotFound):
        env.case('nope')
    with pytest.raises(NotFound):
        env.document_path('invoice_1001', 'invoice_1002.txt')
    with pytest.raises(NotFound):
        env.document_path('invoice_1001', '../AGENTS.md')


def test_document_whose_bytes_changed_is_withheld(env):
    path, fmt = env.document_path('invoice_1001', 'invoice_1001.txt')
    assert fmt == 'txt'
    path.write_text(path.read_text() + '\nTampered')
    with pytest.raises(Stale):
        env.document_path('invoice_1001', 'invoice_1001.txt')
    assert 'invoice_1001.txt' in Workbench(env.db_path, env.data_dir).ingest()['changed_on_disk']


def test_unreadable_document_becomes_a_blocked_case(env):
    (env.data_dir / 'broken.json').write_text('{nope')
    env.ingest()
    s = st(env, 'broken')
    assert s.decision == 'reject' and s.reasons == ['parse_error'] and s.status == 'blocked'
    with pytest.raises(Refused):
        env.pay('broken', MANAGER, s.rev, s.evidence)
    with pytest.raises(ValueError):
        Workbench(':memory:', env.data_dir)


def test_open_correction_request_stops_payment_until_explicit_resolution(env):
    s = approve_ready(env)
    s = env.request_correction(s.case_id, CLERK, s.rev, 'Vendor', 'Confirm remittance details', s.evidence)
    assert s.status == 'blocked' and 'correction_requested' in s.blockers
    assert not s.approvals and not s.payable
    s = env.assign(s.case_id, CLERK, s.rev, 'Treasury', 'Verify the request', s.evidence)
    s = env.escalate(s.case_id, CLERK, s.rev, 'AP Manager', 'Needs review', s.evidence)
    assert 'correction_requested' in s.blockers
    with pytest.raises(Refused):
        env.approve(s.case_id, MANAGER, s.rev, 'ap', s.evidence, 'Premature signoff')
    with pytest.raises(Refused):
        env.pay(s.case_id, MANAGER, s.rev, s.evidence)
    assert ledger_count(env) == 0
    s = env.resolve_correction(s.case_id, CLERK, s.rev, 'Vendor confirmed original details', s.evidence)
    assert s.status == 'awaiting_approval' and not s.approvals
    s = env.approve(s.case_id, MANAGER, s.rev, 'ap', s.evidence, 'Reviewed the resolution')
    assert env.pay(s.case_id, MANAGER, s.rev, s.evidence).status == 'paid'
    assert any(e['kind'] == 'correction_resolved' for e in env.history(s.case_id))


def test_resolution_does_not_clear_remaining_validation_defects(env):
    s = env.case('invoice_1008')
    s = env.request_correction(s.case_id, CLERK, s.rev, 'Vendor', 'Unknown item needs correction', s.evidence)
    with pytest.raises(Refused):
        env.resolve_correction(s.case_id, CLERK, s.rev, 'Looks fine to me', s.evidence)
    assert 'unknown_item' in env.case(s.case_id).blockers


@pytest.mark.parametrize('missing', [False, True])
def test_source_change_invalidates_approval_and_blocks_payment(env, missing):
    s = approve_ready(env)
    path = env.data_dir / 'invoice_1001.txt'
    if missing:
        path.unlink()
    else:
        path.write_text('New invoice contents')
    fresh = env.case(s.case_id)
    assert 'source_unavailable' in fresh.blockers and not fresh.approvals
    assert fresh.evidence != s.evidence and not fresh.payable
    with pytest.raises(Stale):
        env.pay(s.case_id, MANAGER, s.rev, s.evidence)
    with pytest.raises(Refused):
        env.approve(s.case_id, MANAGER, fresh.rev, 'ap', fresh.evidence, 'Approve missing evidence')
    with pytest.raises(Refused):
        env.pay(s.case_id, MANAGER, fresh.rev, fresh.evidence)
    assert ledger_count(env) == 0


def test_extreme_numeric_correction_returns_validation_error_without_changes(env):
    s = env.case('invoice_1001')
    with pytest.raises(Invalid):
        env.edit_fields(s.case_id, CLERK, s.rev, s.effective.filename,
                        {'line.0.unit_price': '1e100'}, 'Mistyped numeric value', s.evidence)
    assert env.case(s.case_id).rev == s.rev
    assert env.edits(s.case_id) == []


def test_ingested_numeric_overflow_is_a_blocked_case_not_a_broken_inbox(env):
    (env.data_dir / 'overflow.json').write_text(json.dumps({
        'invoice_number':'INV-9999','vendor':'Overflow Ltd','date':'2026-01-01','due_date':'2026-02-01',
        'line_items':[{'item':'WidgetA','quantity':1,'unit_price':'1e100'}],'total':'1e100'}))
    env.ingest()
    states = {s.case_id:s for s in env.list_cases()}
    assert states['overflow'].status == 'blocked'
    assert states['overflow'].reasons == ['parse_error']
