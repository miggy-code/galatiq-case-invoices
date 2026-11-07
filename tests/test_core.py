import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from invoices.core import Line, Invoice, evaluate, load_invoice, open_database, process_batch

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'evals/expected-main.json').read_text())['cases']

@pytest.fixture
def db():
    conn = open_database(':memory:')
    yield conn
    conn.close()

@pytest.mark.parametrize('case', CASES, ids=lambda c:c['file'])
def test_labeled_document(case, db):
    path = ROOT / 'data/invoices' / case['file']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case['sha256']
    invoice = load_invoice(path)
    result = evaluate(invoice, db)
    assert result['decision'] == case['decision']
    assert set(case['reasons']) <= set(result['reasons'])
    assert invoice.vendor == case['vendor']
    assert invoice.date == case['date']
    assert invoice.due_date == case['due_date']
    assert invoice.currency == case['currency']
    assert invoice.total == Decimal(case['total'])
    assert result['quantities'] == case['quantities']


def test_all_documents_are_labeled():
    assert {c['file'] for c in CASES} == {p.name for p in (ROOT/'data/invoices').iterdir()}


def test_batch_and_replay_are_idempotent(db):
    paths = sorted((ROOT/'data/invoices').iterdir())
    results = process_batch(paths, db, record_payments=True)
    assert {r['source']:r['decision'] for r in results} == {c['file']:c['batch_decision'] for c in CASES}
    assert db.execute('select count(*) from mock_payments').fetchone()[0] == 5
    process_batch(list(reversed(paths)), db, record_payments=True)
    assert db.execute('select count(*) from mock_payments').fetchone()[0] == 5


def clean():
    return Invoice('sample','INV-42','Widgets Inc.','2026-01-01','2026-02-01',
                   [Line('WidgetA', Decimal('1'), Decimal('250'))], Decimal('250'))


def test_repeated_lines_aggregate_before_stock_check(db):
    inv = replace(clean(), lines=[Line('Widget A',Decimal('8'),Decimal('250')),Line('WidgetA (rush order)',Decimal('8'),Decimal('250'))],total=Decimal('4000'))
    assert evaluate(inv, db)['decision'] == 'hold'
    assert 'stock_exceeded' in evaluate(inv, db)['reasons']

@pytest.mark.parametrize('total,decision',[('10000','approve'),('10000.01','hold')])
def test_high_value_boundary(total,decision,db):
    inv = replace(clean(), lines=[Line('WidgetA',Decimal('1'),Decimal(total))],total=Decimal(total))
    assert evaluate(inv, db)['decision'] == decision


def test_line_and_tax_math(db):
    inv = replace(clean(), lines=[Line('WidgetA',Decimal('1'),Decimal('250'),Decimal('251'))],subtotal=Decimal('250'), tax=Decimal('20'),tax_rate=Decimal('.05'),total=Decimal('270'))
    assert {'line_mismatch','tax_mismatch'} <= set(evaluate(inv,db)['reasons'])
    assert evaluate(inv,db)['decision'] == 'reject'


def test_equivalent_vendor_spelling_does_not_pay_twice(db):
    a=clean()
    b=replace(a,source='copy',vendor='WIDGETS Incorporated')
    results=process_batch([a,b],db,record_payments=True)
    assert [r['decision'] for r in results] == ['duplicate','approve']
    assert db.execute('select count(*) from mock_payments').fetchone()[0] == 1


def test_conflicting_versions_cannot_pay_in_any_order(db):
    a=clean()
    b=replace(a,source='revision',total=Decimal('500'),lines=[Line('WidgetA',Decimal('2'),Decimal('250'))],revision='R1')
    assert all(r['decision']=='hold' for r in process_batch([b,a],db,record_payments=True))
    assert db.execute('select count(*) from mock_payments').fetchone()[0] == 0


def test_sqlite_inventory_is_the_authority(db):
    db.execute("update inventory set stock=0 where item='WidgetA'")
    assert 'out_of_stock' in evaluate(clean(),db)['reasons']


def test_malformed_document_is_rejected_without_stopping_batch(tmp_path,db):
    bad=tmp_path/'broken.json'
    bad.write_text('{broken')
    results=process_batch([bad,clean()],db,record_payments=True)
    assert results[0]['decision']=='reject'
    assert results[0]['reasons']==['parse_error']
    assert results[1]['decision']=='approve'

@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity'])
def test_nonfinite_numbers_cannot_reach_payment(value,tmp_path,db):
    path=tmp_path/'hostile.json'
    path.write_text(json.dumps({'line_items':[{'item':'WidgetA','quantity':value,'unit_price':250}]}))
    result=process_batch([path],db,record_payments=True)[0]
    assert result['decision']=='reject'
    assert result['reasons']==['parse_error']
    assert db.execute('select count(*) from mock_payments').fetchone()[0]==0


def test_payment_is_unique_across_database_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'ledger.db'
    open_database(path).close()
    def record(_):
        conn=open_database(path)
        try:
            return process_batch([clean()],conn,record_payments=True)[0]['decision']
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions=list(pool.map(record,range(2)))
    assert sorted(decisions)==['approve','duplicate']
    conn=open_database(path)
    assert conn.execute('select count(*) from mock_payments').fetchone()[0]==1
    conn.close()


def test_rounding_tolerance_preserves_good_input_and_rejects_bad_input(db):
    assert evaluate(replace(clean(),total=Decimal('250.01')),db)['decision']=='approve'
    assert evaluate(replace(clean(),total=Decimal('250.02')),db)['decision']=='reject'


def test_working_label_scorer_catches_bad_outputs():
    from invoices.evaluate import score
    case={'file':'sample','decision':'hold','batch_decision':'hold','total':'250','quantities':{'WidgetA':1},'reasons':['stock_exceeded']}
    good={'decision':'hold','total':'250','quantities':{'WidgetA':1},'reasons':['stock_exceeded']}
    assert score([case],{'sample':good})['matched']==1
    bad={**good,'decision':'approve','reasons':[]}
    result=score([case],{'sample':bad})
    assert result['false_approvals']==1
    assert result['matched']==0
    assert result['mismatches'][0]['missing_reasons']==['stock_exceeded']
    assert score([case],{})['matched']==0


def test_email_sender_does_not_override_invoice_vendor(tmp_path):
    path=tmp_path/'email.txt'
    path.write_text('From: bills@example.com\nVendor: Actual Supplier Ltd.\n')
    assert load_invoice(path).vendor=='Actual Supplier Ltd.'


def test_equivalent_economics_cannot_hide_a_review_flag(db):
    a=clean()
    b=replace(a,source='copy',notes='formerly Another Vendor')
    results=process_batch([a,b],db,record_payments=True)
    assert all(r['decision']=='hold' for r in results)
    assert db.execute('select count(*) from mock_payments').fetchone()[0]==0


def test_corrupt_pdf_is_rejected_without_stopping_batch(tmp_path,db):
    path=tmp_path/'bad.pdf'
    path.write_bytes(b'%PDF-1.4 corrupt')
    results=process_batch([path,clean()],db)
    assert results[0]['decision']=='reject'
    assert results[0]['reasons']==['parse_error']
    assert results[1]['decision']=='approve'
