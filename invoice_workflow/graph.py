"""One LangGraph workflow from load to pay. Code picks every transition; models never choose a step.

Per document:  load -> [extract] -> validate -> screen
Per batch:     documents -> reconcile -> [review] -> pay -> decide

Each node is a plain function of a pipeline entry, importable and testable without LangGraph.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .documents import PARSE_ERRORS, TEXT_SUFFIXES, UNREADABLE, Invoice, document_text, load_invoice
from .extraction import ModelStepFailed, extract_document, printed_totals
from .packet import build_packet, needs_packet
from .payment import mock_payment, pay
from .reconcile import reconcile, revised_numbers
from .review import run_vp, vp_eligible
from .routing import add_reasons, route, severity
from .screen import apply_screen
from .validation import MATH_REASONS, validate

log = logging.getLogger('invoice_workflow')


def log_stage(stage, result):
    log.debug(
        'stage=%s invoice=%s decision=%s owner=%s reasons=%s',
        stage,
        result.get('invoice_number') or result.get('source'),
        result.get('decision'),
        result.get('owner'),
        result.get('reasons'),
    )


# ---- Document stages ---------------------------------------------------------------------------


def load_stage(source, use_model):
    """Read the file. Structured files and the no-model path are parsed by code here."""
    if isinstance(source, Invoice):
        return {'inv': source, 'text': None}
    entry = {'source': source, 'text': None, 'evidence': [], 'extra': []}
    name = Path(source).name
    try:
        if Path(source).suffix.lower() in TEXT_SUFFIXES:
            try:
                entry['text'] = document_text(source)
            except UNREADABLE as exc:
                # We could not read it; that is not the vendor's error (POLICY 2, review F15).
                entry['result'] = route({'source': name, 'reasons': ['unreadable_document'], 'detail': str(exc)})
                return entry
            if not entry['text'].strip():
                entry['result'] = route({'source': name, 'reasons': ['unreadable_document']})
                return entry
            if use_model:
                entry['needs_model'] = True
                return entry
        entry['inv'] = load_invoice(source)
    except PARSE_ERRORS as exc:
        entry['result'] = route({'source': name, 'reasons': ['parse_error'], 'detail': str(exc)})
        return entry
    if entry['text'] is not None and len(printed_totals(entry['text'])) > 1:
        entry['extra'] = ['extraction_uncertain']
    return entry


def extract_stage(entry, model, offline, screen_model):
    name = Path(entry['source']).name
    try:
        entry['inv'], entry['evidence'], entry['extra'] = extract_document(
            entry['source'], entry['text'], model, offline
        )
    except ModelStepFailed as exc:
        result = {'source': name, 'reasons': [exc.reason], 'failed_step': 'extraction'}
        if exc.detail:
            result['detail'] = exc.detail
        entry['result'] = apply_screen(result, entry['text'], screen_model)
    except PARSE_ERRORS as exc:
        entry['result'] = route({'source': name, 'reasons': ['parse_error'], 'detail': str(exc)})
    return entry


def validate_stage(entry, conn):
    result = validate(entry['inv'], conn)
    if entry.get('evidence'):
        result['evidence'] = list(entry['evidence'])
    if {'extraction_uncertain', 'extraction_disagreement'} & set(entry.get('extra', [])):
        # The sums may fail because we misread the document, so it is not the vendor's error (decision 1):
        # drop the arithmetic reasons and let the extraction reason hold it for a clerk.
        result['reasons'] = [r for r in result['reasons'] if r not in MATH_REASONS]
    entry['result'] = add_reasons(result, *entry.get('extra', []))
    return entry


def screen_stage(entry, screen_model):
    entry['result'] = apply_screen(entry['result'], entry['inv'].notes, screen_model)
    return entry


def internal_error(result, stage):
    """Hold the affected invoice for AP operations and continue the batch."""
    log.exception(
        'stage=%s invoice=%s error=internal_error', stage, result.get('invoice_number') or result.get('source')
    )
    add_reasons(result, 'internal_error')


# ---- Batch stages (reconcile is in reconcile.py) ----------------------------------------------


def review_stage(primaries, conn, vp_model, critic_model):
    """Send invoices whose only open reasons are VP triggers to the VP agent and critic."""
    for entry in primaries:
        if vp_eligible(entry['result']) and vp_model is not None and critic_model is not None:
            try:
                run_vp(entry['inv'], entry['result'], conn, vp_model, critic_model)
            except Exception:
                internal_error(entry['result'], 'vp')
            log_stage('vp', entry['result'])


def pay_stage(primaries, conn, record_payments, payment=mock_payment):
    for entry in primaries:
        result = entry['result']
        if record_payments and result['decision'] == 'approve':
            outcome = pay(conn, entry['identity'], entry['inv'], payment, originals=revised_numbers(entry['inv']))
            result['mock_payment_recorded'] = outcome == 'paid'
            if outcome != 'paid':
                add_reasons(result, outcome)
            log_stage('pay', result)
        if entry.get('original') and (result['decision'] == 'approve' or 'already_recorded' in result['reasons']):
            add_reasons(entry['original']['result'], 'superseded')


def finish(entry):
    result = route(entry['result'])
    if needs_packet(result):
        result['packet'] = build_packet(result, entry.get('text'))
    log_stage('decide', result)
    for stage, line in brief_stages(entry, result):
        log.info('%-10s %s %s', stage, result.get('invoice_number') or result.get('source'), line)
    return result


def brief_stages(entry, result):
    """One line per stage of the brief (ingestion, validation, approval, payment), saying who did the work:
    the model or code."""
    reasons, source = result.get('reasons', []), str(result.get('source', ''))
    if result.get('failed_step') == 'extraction' or 'internal_error' in reasons and 'total' not in result:
        yield 'ingestion', f'failed: {", ".join(reasons)}'
        return
    if not result.get('vendor') and any(r in reasons for r in ('parse_error', 'unreadable_document')):
        yield 'ingestion', f'could not be read: {", ".join(reasons)}'
        return
    evidence = entry.get('evidence') or []
    if evidence:
        found = sum(1 for e in evidence if e.get('found'))
        yield 'ingestion', f'read by the model; {found} of {len(evidence)} values found in the document by code'
    else:
        yield 'ingestion', f'parsed by code ({Path(source).suffix.lstrip(".") or "invoice"})'
    prices = {p['reason']: p for p in result.get('prices', [])}
    found = [
        f'{r} ({prices[r]["item"]} ${prices[r]["unit_price"]} against catalog ${prices[r]["catalog_price"]})'
        if r in prices
        else r
        for r in reasons
        if severity(r) != 'audit'
    ]
    flagged = [r for r in reasons if severity(r) == 'audit']
    line = (
        'passed: arithmetic, stock, catalog prices, currency, duplicates' if not found else 'found ' + ', '.join(found)
    )
    yield 'validation', line + (f'; fraud screen flagged {", ".join(flagged)}' if flagged else '')
    vp = result.get('vp')
    if vp:
        quotes = '; '.join(f'"{j["quote"]}"' for j in vp.get('justifications', []))
        critic = ', '.join(c.get('status', '?') for c in vp.get('critic', [])) or 'not run'
        yield (
            'approval',
            (
                f'VP agent: {vp.get("decision")}{", quoting " + quotes if quotes else ""}; critic: {critic}; '
                f'round {vp.get("rounds", 1)} of 2; outcome: {vp.get("outcome")}'
            ),
        )
    elif result['decision'] == 'approve':
        yield 'approval', 'by rule: no open question, under $10,000, so no VP review needed'
    elif result.get('owner') == 'no_action':
        yield 'approval', f'not needed: {result["decision"]}'
    else:
        yield 'approval', f'not reached: {result.get("owner")} acts first'
    if result.get('mock_payment_recorded'):
        yield 'payment', f'paid {result.get("total")} to {result.get("vendor")}'
    elif result['decision'] == 'approve':
        yield 'payment', 'approved; no payment recorded in this run'
    else:
        yield 'payment', f'not paid: {result["decision"]} -> {result.get("next_step")}'


# ---- Graphs ------------------------------------------------------------------------------------


class DocumentState(TypedDict, total=False):
    source: object
    entry: dict


class BatchState(TypedDict, total=False):
    inputs: list
    entries: list
    primaries: list
    results: list


def build_document_graph(conn, *, model=None, offline=False, screen_model=None):
    def logged(stage, entry):
        if 'result' in entry:
            log_stage(stage, entry['result'])
        return {'entry': entry}

    graph = StateGraph(DocumentState)
    graph.add_node('load', lambda s: logged('load', load_stage(s['source'], model is not None)))
    graph.add_node('extract', lambda s: logged('extract', extract_stage(s['entry'], model, offline, screen_model)))
    graph.add_node('validate', lambda s: logged('validate', validate_stage(s['entry'], conn)))
    graph.add_node('screen', lambda s: logged('screen', screen_stage(s['entry'], screen_model)))
    graph.add_edge(START, 'load')
    graph.add_conditional_edges(
        'load',
        lambda s: 'done' if 'result' in s['entry'] else 'extract' if s['entry'].get('needs_model') else 'validate',
        {'extract': 'extract', 'validate': 'validate', 'done': END},
    )
    graph.add_conditional_edges(
        'extract', lambda s: 'done' if 'result' in s['entry'] else 'validate', {'validate': 'validate', 'done': END}
    )
    graph.add_edge('validate', 'screen')
    graph.add_edge('screen', END)
    return graph.compile()


def build_batch_graph(
    conn,
    *,
    record_payments=False,
    model=None,
    offline=False,
    screen_model=None,
    vp_model=None,
    critic_model=None,
    payment=mock_payment,
):
    document = build_document_graph(conn, model=model, offline=offline, screen_model=screen_model)

    def after_reconcile(state):
        ready = vp_model is not None and critic_model is not None
        return 'review' if ready and any(vp_eligible(e['result']) for e in state['primaries']) else 'pay'

    def run_document(source):
        try:
            return document.invoke({'source': source})['entry']
        except Exception:
            entry = {
                'source': source,
                'result': {'source': source.source if isinstance(source, Invoice) else Path(source).name},
            }
            internal_error(entry['result'], 'document')
            return entry

    graph = StateGraph(BatchState)
    graph.add_node('documents', lambda s: {'entries': [run_document(source) for source in s['inputs']]})
    graph.add_node('reconcile', lambda s: {'primaries': reconcile(s['entries'], conn)})
    graph.add_node('review', lambda s: review_stage(s['primaries'], conn, vp_model, critic_model) or {})
    graph.add_node('pay', lambda s: pay_stage(s['primaries'], conn, record_payments, payment) or {})
    graph.add_node('decide', lambda s: {'results': [finish(entry) for entry in s['entries']]})
    graph.add_edge(START, 'documents')
    graph.add_edge('documents', 'reconcile')
    graph.add_conditional_edges('reconcile', after_reconcile, {'review': 'review', 'pay': 'pay'})
    graph.add_edge('review', 'pay')
    graph.add_edge('pay', 'decide')
    graph.add_edge('decide', END)
    return graph.compile()


def run_batch(
    inputs,
    conn,
    *,
    record_payments=False,
    model=None,
    offline=False,
    screen_model=None,
    vp_model=None,
    critic_model=None,
    payment=mock_payment,
):
    """The pipeline's entry point. `payment` is the bank (default: the brief's mock_payment)."""
    graph = build_batch_graph(
        conn,
        record_payments=record_payments,
        model=model,
        offline=offline,
        screen_model=screen_model,
        vp_model=vp_model,
        critic_model=critic_model,
        payment=payment,
    )
    return graph.invoke({'inputs': list(inputs)})['results']
