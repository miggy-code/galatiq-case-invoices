"""Ten invariants for payment, model boundaries, routing, the report and the CLI.

Invoices pay at most once, in the selected version and supported currency.
Failed model calls and readings that fail source checks hold the invoice.
The VP agent approves within code-enforced limits; the critic can veto.
Each exception has one owner, and fraud goes to internal audit.
The report renders document text as text; live calls require a chosen endpoint.
The separate evaluator scores accuracy on labeled invoices."""

import json
import re
import shutil
import sys
from collections import Counter
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from invoice_workflow.__main__ import main
from invoice_workflow.llm import llm_model
from invoice_workflow.report import write_report
from support import A, H, ROOT, S, Doc

# ---- Money ---------------------------------------------------------------------------------------

REVISION = A.but(
    revision='R1', lines=A.lines + (('WidgetB', 1, '500.00'),), notes='Reason: PO amendment 2 adds one WidgetB'
)


@pytest.mark.parametrize(
    'runs, paid',
    [
        pytest.param(
            [[('a.json', A.but(shipping=None))], [('a.csv', A.but(vendor='WIDGETS Incorporated'))]],
            ['1000.00'],
            id='null-charge-then-copy-in-another-format-and-vendor-spelling',
        ),
        pytest.param(
            [
                [('a.json', A), ('b.json', A.but(number='INV-7B', total='1000'))],
                [('c.json', A.but(number='INV-7C')), ('d.json', A.but(number='INV-7D', total='1000.0'))],
            ],
            ['1000.00'],
            id='resubmitted-under-new-numbers-with-the-total-written-differently',
        ),
        pytest.param([[('a.json', A)], [('b.json', REVISION)]], ['1000.00'], id='revision-after-the-original-was-paid'),
        pytest.param(
            [[('a.json', A), ('b.json', REVISION.but(number='INV-7-R1', revision=''))]],
            [],
            id='revision-under-a-new-number-holds-with-its-original',
        ),
        pytest.param(
            [[('a.json', A)], [('b.json', REVISION.but(number='INV-9', revision='', notes='Revision of INV-7'))]],
            ['1000.00'],
            id='revision-naming-a-paid-original-in-its-text',
        ),
        pytest.param(
            [[('a.json', REVISION.but(number='INV-7-R1', revision=''))], [('b.json', A)]],
            ['1500.00'],
            id='original-after-a-paid-renumbered-revision',
        ),
        pytest.param(
            [[('a.json', REVISION.but(number='INV-9', revision='', notes='Revision of INV-7'))], [('b.json', A)]],
            ['1500.00'],
            id='original-after-a-paid-revision-naming-it-in-text',
        ),
        pytest.param(
            [[('a-revision.json', REVISION), ('b-original.json', A)]],
            ['1500.00'],
            id='revision-sorted-before-its-original',
        ),
        pytest.param(
            [[('a.json', A), ('b.json', REVISION.but(lines=(('WidgetA', 5, '250.00'), ('WidgetB', 1, '500.00'))))]],
            [],
            id='revision-that-rewrites-an-original-line',
        ),
        pytest.param(
            [[('a.json', A), ('b.json', REVISION.but(remit_to='IBAN GB00 NEWB 0000 0000 01'))]],
            [],
            id='revision-that-changes-remittance-details',
        ),
        pytest.param(
            [[('a.json', A), ('b.json', A.but(notes='Payment update: please use our new bank account.'))]],
            [],
            id='copy-that-asks-for-a-new-bank-account',
        ),
    ],
)
def test_an_invoice_is_paid_at_most_once_whatever_copies_or_versions_arrive(ap, runs, paid):
    """Copies and revisions across formats and runs produce one payment for the selected version.
    A null charge pays on the first run before its duplicate arrives."""
    for batch in runs:
        ap.run(*(ap.file(name, doc) for name, doc in batch))
        assert ap.rail.sent == paid


@pytest.mark.parametrize('revision_first', [False, True], ids=['same-invoice', 'renumbered-revision-first'])
@pytest.mark.parametrize(
    'bank_call_crashes, second_run',
    [
        pytest.param(True, 'after the crash', id='bank-accepted-then-the-process-died'),
        pytest.param(False, 'during the first payment', id='second-run-starts-mid-payment'),
    ],
)
def test_a_crash_or_a_parallel_run_never_repeats_a_payment(ap, bank_call_crashes, second_run, revision_first):
    """The ledger commits a pending claim before the bank call and blocks a second payment.
    The second run opens its own ledger connection during the first payment call."""
    first = REVISION.but(number='INV-7-R1', revision='') if revision_first else A
    invoice = ap.file('a.json', first)
    later = ap.file('b.json', A)
    again = []
    ap.rail.crash_after_send = bank_call_crashes
    if second_run == 'during the first payment':
        ap.rail.during_send = lambda: again.append(ap.run(later))
    ap.run(invoice)
    ap.rail.crash_after_send = False
    if second_run == 'after the crash':
        again.append(ap.run(later))
    assert ap.rail.sent == (['1500.00'] if revision_first else ['1000.00'])
    result = again[0]['b.json']
    expected = (
        ('hold', 'ap_clerk', ['possible_revision'])
        if revision_first
        else ('hold', 'ap_operations', ['payment_pending'])
    )
    assert (result['decision'], result['owner'], result['reasons']) == expected


@pytest.mark.parametrize(
    'doc, paid, reasons',
    [
        pytest.param(A, ['1000.00'], [], id='dollar-signs'),
        pytest.param(A.but(sign='€'), [], ['currency_unknown'], id='euro-signs-at-catalog-prices'),
        pytest.param(A.but(sign='C$'), [], ['currency_unknown'], id='canadian-dollar-signs'),
        pytest.param(
            A.but(sign='', lines=(('WidgetA', 4, '240.00'),)), [], ['currency_unknown'], id='no-evidence-at-all'
        ),
    ],
)
def test_an_amount_is_paid_as_dollars_only_on_dollar_evidence_and_no_foreign_sign(ap, doc, paid, reasons):
    """USD inference requires dollar evidence and holds on contrary currency signs.
    Text uses the model path with an empty extracted currency."""
    result = ap.run(ap.file('a.txt', doc))['a.txt']
    assert ap.rail.sent == paid
    assert set(reasons) <= set(result['reasons'])


# ---- What a model may decide ----------------------------------------------------------------------

DEPOSIT = ('AMOUNT DUE $12,000.00', 'AMOUNT DUE $12,000.00\nDeposit received $500.00\nBALANCE DUE $11,500.00')
NO_QUANTITY = ('WidgetB: 10 units at', 'WidgetB (ref WB-10) at')
PACKS_AND_CREDIT = [
    ('WidgetA: 13 units at $250.00', 'WidgetA: 13 units at $250.00 (packs of 15)'),
    ('AMOUNT DUE $12,000.00', 'AMOUNT DUE $12,000.00\nCredit limit $12,500.00'),
]
OVERREAD = [('"quantity": "13"', '"quantity": "15"'), ('12000.00', '12500.00'), ('12,000.00', '12,500.00')]
PAID_THROUGH_THE_VP = ('approve', 'vp_agent', True, ['12000.00'])
HELD_FOR_A_CLERK = ('hold', 'ap_clerk', False, [])


def edit(text, *changes):
    for old, new in changes:
        text = text.replace(old, new)
    return text


@pytest.mark.parametrize(
    'printed, misread, outcome, reasons',
    [
        pytest.param([], [], PAID_THROUGH_THE_VP, [], id='every-value-quoted-from-the-document'),
        pytest.param(
            [], OVERREAD, HELD_FOR_A_CLERK, ['ungrounded_extraction'], id='an-overread-quoting-text-the-document-lacks'
        ),
        pytest.param(
            [NO_QUANTITY], [], HELD_FOR_A_CLERK, ['ungrounded_extraction'], id='a-quantity-borrowed-from-an-identifier'
        ),
        pytest.param([DEPOSIT], [], HELD_FOR_A_CLERK, ['extraction_uncertain'], id='two-different-amounts-due'),
        pytest.param(
            PACKS_AND_CREDIT,
            OVERREAD,
            HELD_FOR_A_CLERK,
            ['extraction_uncertain'],
            id='an-overread-quoting-numbers-printed-elsewhere',
        ),
    ],
)
def test_a_number_the_model_cannot_prove_from_the_document_never_reaches_vp_or_payment(
    ap, printed, misread, outcome, reasons
):
    """Numbers that fail source checks hold before VP review and payment.
    The $12,000 invoice uses a layout the regex parser cannot read.
    `printed` sets source values; `misread` sets extraction errors."""
    text = edit(H.text(), *printed)
    path = ap.file('h.txt', H, text=text, extraction=edit(edit(H.extraction(), *printed), *misread))
    result = ap.run(path)['h.txt']
    decision, owner, vp_asked, paid = outcome
    assert (result['decision'], result['owner']) == (decision, owner)
    assert set(reasons) <= set(result['reasons'])
    assert bool(ap.models.calls['vp']) is vp_asked
    assert ap.rail.sent == paid
    assert all(text[e['start'] : e['end']] == e['quote'] for e in result['evidence'] if e.get('start') is not None)


@pytest.mark.parametrize(
    'vp_says, critic_says, reasons, paid, vp_asked, critic_asked',
    [
        pytest.param('approves', 'finds nothing', [], ['12000.00'], 1, 1, id='true-facts/no-objection'),
        pytest.param('approves', 'objects', ['critic_unresolved'], [], 2, 2, id='true-facts/real-objection'),
        pytest.param(
            'approves', 'cites a missing fact', ['critic_unavailable'], [], 1, 1, id='true-facts/invented-objection'
        ),
        pytest.param(
            'approves on a false fact', 'finds nothing', ['vp_claim_mismatch'], [], 1, 1, id='false-fact/no-objection'
        ),
        pytest.param('holds', 'finds nothing', ['vp_hold'], [], 1, 0, id='vp-holds'),
    ],
)
def test_the_vp_must_cite_true_facts_and_the_critic_can_only_veto(
    ap, vp_says, critic_says, reasons, paid, vp_asked, critic_asked
):
    """VP approval requires matching factual claims and a passing critic.
    The loop allows one revision. The critic receives structured facts and claims,
    excluding the VP rationale and source document."""
    ap.models.vp_says, ap.models.critic_says = vp_says, critic_says
    result = ap.run(ap.file('h.json', H.but(notes='NOTE-CANARY: deliver to dock 4')))['h.json']
    assert ap.rail.sent == paid
    assert set(reasons) <= set(result['reasons'])
    assert (len(ap.models.calls['vp']), len(ap.models.calls['critic'])) == (vp_asked, critic_asked)
    assert not [payload for payload in ap.models.calls['critic'] if 'CANARY' in payload]


ORIGINAL_AND_REVISION = [
    A,
    A.but(revision='R1', lines=A.lines + (('WidgetB', 10, '500.00'),), shipping='0.01', notes='Reason: PO amendment 2'),
]


@pytest.mark.parametrize(
    'documents, vp_quotes, paid, reasons',
    [
        pytest.param(
            [A.but(lines=(('WidgetB', 50, '500.00'),), shipping='0.01')],
            None,
            [],
            ['vp_recommend'],
            id='total-one-cent-beyond-25000',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '300.00'),), notes='Reason: rush order')],
            None,
            ['1200.00'],
            [],
            id='price-20pct-above-with-a-stated-reason',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '250.01'),), notes='Reason: rush order')],
            None,
            ['1000.04'],
            [],
            id='price-a-cent-above-rounds-to-zero-pct',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '300.01'),), notes='Reason: rush order')],
            None,
            [],
            ['vp_recommend'],
            id='price-one-cent-beyond-20pct-above',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '260.00'),))],
            'rush order',
            [],
            ['vp_claim_mismatch'],
            id='price-above-with-a-reason-the-invoice-lacks',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '175.00'),), notes='Reason: volume discount under PO 4471')],
            None,
            ['700.00'],
            [],
            id='price-30pct-below-with-a-stated-reason',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '174.99'),), notes='Reason: volume discount under PO 4471')],
            None,
            [],
            ['vp_recommend'],
            id='price-one-cent-beyond-30pct-below',
        ),
        pytest.param(
            [A.but(lines=(('WidgetA', 4, '200.00'),))],
            None,
            [],
            ['vp_claim_mismatch'],
            id='price-below-with-no-stated-reason',
        ),
        pytest.param(ORIGINAL_AND_REVISION, None, [], ['vp_recommend'], id='revision-one-cent-beyond-5000'),
    ],
)
def test_the_vp_agent_approves_alone_only_inside_its_authority_limits(ap, documents, vp_quotes, paid, reasons):
    """Code enforces VP authority at each monetary boundary and the adjacent cents.
    Price variances require a reason found in the invoice. Stock is ample and the
    VP proposes approval, isolating the authority and grounding checks."""
    ap.stock(1000)
    ap.models.vp_quote = vp_quotes
    results = ap.run(*(ap.file(f'{index}.json', doc) for index, doc in enumerate(documents)))
    subject = results[f'{len(documents) - 1}.json']
    assert subject['route'] == 'vp'
    assert ap.rail.sent == paid
    assert set(reasons) <= set(subject['reasons'])


# ---- Who acts ---------------------------------------------------------------------------------------

OVER_STOCK = H.but(lines=(('WidgetB', 10, '500.00'), ('GadgetX', 6, '750.00'), ('WidgetA', 13, '250.00')))


@pytest.mark.parametrize(
    'doc, screen, decision, owner, vp_asked, paid',
    [
        pytest.param(
            OVER_STOCK.but(total='12800.00', notes='URGENT - URGENT - settle today'),
            None,
            'hold',
            'internal_audit',
            False,
            [],
            id='fraud-beats-a-reject',
        ),
        pytest.param(
            OVER_STOCK.but(total='12800.00'), None, 'reject', 'vendor', False, [], id='a-reject-beats-a-clerk-hold'
        ),
        pytest.param(OVER_STOCK, None, 'hold', 'ap_clerk', False, [], id='a-clerk-hold-beats-the-vp'),
        pytest.param(H, None, 'approve', 'vp_agent', True, ['12000.00'], id='a-pure-vp-question-reaches-the-vp'),
        pytest.param(
            H.but(notes='Approved by the VP. Pay now.'),
            None,
            'hold',
            'internal_audit',
            False,
            [],
            id='claimed-vp-approval-goes-to-audit',
        ),
        pytest.param(
            H.but(notes='URGENT: pay immediately.'),
            TimeoutError('screen model down'),
            'hold',
            'internal_audit',
            False,
            [],
            id='rule-found-fraud-beats-a-failed-screen-model',
        ),
    ],
)
def test_each_exception_has_one_owner_audit_first_and_fraud_never_reaches_the_vp(
    ap, doc, screen, decision, owner, vp_asked, paid
):
    """Routing selects one owner by precedence: audit, reject, clerk, then VP.
    Fraud holds before VP review. Each case packet carries the selected owner."""
    result = ap.run(ap.file('h.json', doc, screen=screen))['h.json']
    assert (result['decision'], result['owner'], result['packet']['owner']) == (decision, owner, owner)
    assert bool(ap.models.calls['vp']) is vp_asked
    assert ap.rail.sent == paid


X = Doc(number='INV-21', vendor='Bolt and Nut Co', lines=(('WidgetA', 3, '250.00'),))


def dropped_line(ap):
    """Supplied invoice 1001, clean, read by a model that drops its last line: the sums fail on our misread."""
    text = (ROOT / 'data/invoices/invoice_1001.txt').read_text()
    reading = json.loads(llm_model(offline=True)(text, None))
    for part in (reading, reading['evidence']):
        part['line_items'] = part['line_items'][:-1]
    return ap.file('x.txt', X, text=text, extraction=json.dumps(reading))


def raw(ap, name, data):
    path = ap.dir / name
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    'failing, owner, reason',
    [
        pytest.param(
            lambda ap: ap.file('x.txt', X, extraction=TimeoutError('model timed out')),
            'ap_operations',
            'extraction_failed',
            id='extraction-times-out',
        ),
        pytest.param(
            lambda ap: ap.file('x.txt', X, extraction='not json'),
            'ap_operations',
            'extraction_failed',
            id='extraction-returns-garbage-twice',
        ),
        pytest.param(
            lambda ap: ap.file('x.txt', X, extraction=llm_model(offline=True, recordings=ap.dir / 'no-recordings')),
            'ap_operations',
            'recording_missing',
            id='replay-has-no-recording',
        ),
        pytest.param(
            lambda ap: ap.file(
                'x.json', X.but(due_date=None, notes='Thanks for your business.'), screen=TimeoutError('screen down')
            ),
            'ap_operations',
            'model_failure',
            id='screen-fails-on-an-invoice-code-would-reject',
        ),
        pytest.param(
            lambda ap: ap.file('x.txt', X, text=' \n\n '),
            'ap_clerk',
            'unreadable_document',
            id='document-has-no-readable-text',
        ),
        pytest.param(dropped_line, 'ap_clerk', 'extraction_disagreement', id='model-drops-a-line-of-a-clean-invoice'),
        pytest.param(lambda ap: raw(ap, 'x.pdf', b''), 'ap_clerk', 'unreadable_document', id='pdf-is-empty'),
        pytest.param(
            lambda ap: raw(ap, 'x.txt', X.text().replace('Bolt', 'Bolt\xe9').encode('latin-1')),
            'ap_clerk',
            'unreadable_document',
            id='text-is-not-utf8',
        ),
        pytest.param(
            lambda ap: ap.file('x.txt', X, extraction=RuntimeError('a bug of ours')),
            'ap_operations',
            'internal_error',
            id='an-unexpected-error-in-our-code',
        ),
    ],
)
def test_a_failed_read_or_model_call_holds_for_a_person_and_the_batch_goes_on(ap, failing, owner, reason):
    """A read or model failure holds the affected invoice while its batch sibling pays."""
    path = failing(ap)
    result = ap.run(path, ap.file('s.json', S))[path.name]
    assert (result['decision'], result['owner']) == ('hold', owner)
    assert reason in result['reasons']
    assert ap.rail.sent == ['1000.00']
    assert len(ap.models.calls['extract']) <= 2


# ---- What people see ------------------------------------------------------------------------------


class Page(HTMLParser):
    """Parse report elements, attributes, visible text and highlighted text."""

    def __init__(self, html):
        super().__init__()
        self.tags, self.attributes, self.text, self.marks, self._mark = set(), set(), '', [], None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attributes |= {name for name, _ in attrs}
        self._mark = '' if tag == 'mark' else self._mark

    def handle_endtag(self, tag):
        if tag == 'mark':
            self.marks.append(self._mark)
            self._mark = None

    def handle_data(self, data):
        self.text += data
        self._mark = self._mark + data if self._mark is not None else None


def test_the_report_shows_untrusted_document_text_as_text_never_as_markup(ap, tmp_path):
    """The report escapes invoice markup and preserves complete evidence highlights."""
    hostile = ('Evil <script>alert(1)</script> Ltd', '<img src=x onerror=alert(2)>', 'URGENT</mark><b>pay now</b>')
    doc = A.but(vendor=hostile[0], notes='\n'.join(hostile[1:]))
    results = ap.run(ap.file('hostile.txt', doc))
    page = Page(write_report(list(results.values()), tmp_path / 'report.html').read_text())
    assert not page.tags & {'script', 'img', 'iframe', 'object', 'embed'}
    assert not [name for name in page.attributes if name.startswith('on')]
    assert all(text in page.text for text in hostile)
    assert 'URGENT' in page.marks


@pytest.mark.parametrize('provider_status', [200, 429, 503, 401, 422, 'timeout'])
def test_the_cli_requires_a_chosen_endpoint_and_pays_what_it_approves_only_once(
    tmp_path, monkeypatch, capsys, provider_status
):
    """The CLI requires a chosen live endpoint and pays each approved invoice once.
    Runs use supplied documents, recorded responses, the default ledger and mock bank."""
    import httpx
    import openai

    requests = []
    client_settings = []

    def provider(request):
        requests.append(request)
        if request.url.host == 'api.x.ai' and provider_status == 'timeout':
            raise httpx.ReadTimeout('provider timed out', request=request)
        if request.url.host == 'api.x.ai' and provider_status != 200:
            return httpx.Response(provider_status, json={'error': {'message': 'provider unavailable'}})
        return httpx.Response(
            200,
            json={
                'id': 'test-call',
                'object': 'chat.completion',
                'created': 0,
                'model': json.loads(request.content)['model'],
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': '{"decision":"hold"}'},
                        'finish_reason': 'stop',
                    }
                ],
            },
        )

    real_client = openai.OpenAI

    def configured_client(**settings):
        client_settings.append(settings)
        return real_client(**settings, http_client=httpx.Client(transport=httpx.MockTransport(provider)))

    with monkeypatch.context() as wrong_endpoint:
        wrong_endpoint.setenv('XAI_API_KEY', 'xai-test-key')
        wrong_endpoint.setenv('DEEPSEEK_API_KEY', 'deepseek-test-key')
        wrong_endpoint.setenv('LLM_BASE_URL', 'https://unrelated.example/v1')
        with pytest.raises(ValueError, match='Set XAI_API_KEY'):
            llm_model(recordings=tmp_path / 'wrong-endpoint')
        assert not requests

    with monkeypatch.context() as live:
        live.setenv('XAI_API_KEY', 'xai-test-key')
        live.setenv('DEEPSEEK_API_KEY', 'deepseek-test-key')
        live.setattr(openai, 'OpenAI', configured_client)
        model = llm_model(recordings=tmp_path / 'provider-recordings')
        if provider_status in (401, 422):
            with pytest.raises(openai.APIStatusError):
                model('a test invoice', None)
            assert [r.url.host for r in requests] == ['api.x.ai']
        else:
            assert json.loads(model('a test invoice', None)) == {'decision': 'hold'}
            expected = ['api.x.ai'] if provider_status == 200 else ['api.x.ai', 'api.deepseek.com']
            assert [r.url.host for r in requests] == expected
        assert requests[0].headers['authorization'] == 'Bearer xai-test-key'
        assert json.loads(requests[0].content)['model'] == 'grok-4.7'
        assert client_settings[0]['timeout'] == 180
        assert client_settings[0]['max_retries'] == 0
        assert json.loads(requests[0].content)['reasoning_effort'] == 'low'
        if provider_status in (429, 503, 'timeout'):
            assert requests[-1].headers['authorization'] == 'Bearer deepseek-test-key'
            assert json.loads(requests[-1].content)['model'] == 'deepseek-flash'
            assert 'reasoning_effort' not in json.loads(requests[-1].content)
            assert client_settings[-1]['timeout'] == 60

    monkeypatch.chdir(tmp_path)
    # A key without its provider endpoint must be refused before any output is created.
    with monkeypatch.context() as live:
        live.setenv('LLM_API_KEY', 'a-key-for-another-provider')
        live.delenv('LLM_BASE_URL', raising=False)
        live.setattr(sys, 'argv', ['main.py', f'--invoice_path={ROOT / "data/invoices/invoice_1001.txt"}'])
        with pytest.raises(SystemExit) as refused:
            main()
        assert refused.value.code == 2
        assert not (tmp_path / 'output').exists()

    shutil.copytree(ROOT / 'data/invoices', tmp_path / 'invoices')
    (tmp_path / 'invoices/.DS_Store').write_bytes(b'\x00\x00\x00\x01Bud1')  # Finder's folder file

    def run(out):
        monkeypatch.setattr(sys, 'argv', ['main.py', '--batch', str(tmp_path / 'invoices'), '--out', out])
        main()
        paid = Counter(
            (Decimal(amount), vendor)
            for amount, vendor in re.findall(r'^Paid (\S+) to (.+)$', capsys.readouterr().err, re.M)
        )
        results = [json.loads(line) for line in (tmp_path / out).read_text().splitlines()]
        assert len(results) == 20
        return paid, Counter((Decimal(r['total']), r['vendor']) for r in results if r['decision'] == 'approve')

    paid, approved = run('first.jsonl')
    assert paid and paid == approved
    assert run('second.jsonl')[0] == Counter()
