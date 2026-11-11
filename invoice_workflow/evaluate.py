"""Score the pipeline against the labeled sets in evals/: false approvals first, offline by default."""

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .database import open_database
from .documents import document_text, invoice_key, item_key
from .graph import run_batch
from .llm import llm_model
from .evidence import continues
from .packet import needs_packet


def money(value):
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def amounts(result):
    """Subtotal, tax and unit prices as read, from the packet's numbers (printed subtotal, else computed)."""
    numbers = result.get('numbers') or {}
    subtotal = numbers.get('subtotal') or {}
    prices = {}
    for line in numbers.get('lines', []):
        prices.setdefault(item_key(line['item']), set()).add(money(line['unit_price']))
    return {
        'subtotal': money(subtotal.get('printed') or subtotal.get('computed')),
        'tax': money(numbers.get('tax')),
        'unit_prices': prices,
    }


def amount_differences(case, result):
    """Labeled subtotal, tax and unit prices must match what was read: a wrong tax line is a wrong payment."""
    read = amounts(result)
    differences = {}
    for key in ('subtotal', 'tax'):
        if key in case and read[key] != money(case[key]):
            differences[key] = {'expected': case[key], 'actual': None if read[key] is None else str(read[key])}
    if 'unit_prices' in case:
        expected = {item_key(k): {money(v)} for k, v in case['unit_prices'].items()}
        if read['unit_prices'] != expected:
            differences['unit_prices'] = {
                'expected': case['unit_prices'],
                'actual': {k: sorted(str(v) for v in vs) for k, vs in read['unit_prices'].items()},
            }
    return differences


def safety(cases, actual):
    """Count nonpayable labels and approvals; calculate 1 - 0.05**(1/n) when none approve."""
    nonpayable = [c for c in cases if c['decision'] != 'approve']
    approved = sum(actual.get(c['file'], {}).get('decision') == 'approve' for c in nonpayable)
    n = len(nonpayable)
    return {
        'nonpayable': n,
        'approved': approved,
        'upper_bound_95': round(1 - 0.05 ** (1 / n), 3) if n and not approved else None,
    }


def costs(cases, actual, *, batch=False):
    """Sum false-approval amounts, count payable approvals and holds, and score labeled owners."""
    expected_key = 'batch_decision' if batch else 'decision'
    at_risk = Decimal('0')
    false_approvals = []
    missed = []
    payable = paid = 0
    owners = {'checked': 0, 'wrong': [], 'matrix': {}}
    for case in cases:
        result = actual.get(case['file'])
        if result is None:
            continue
        expected = case[expected_key]
        if expected == 'approve':
            payable += 1
            if result['decision'] == 'approve':
                paid += 1
            else:
                missed.append({'file': case['file'], 'decision': result['decision'], 'owner': result.get('owner')})
        elif result['decision'] == 'approve':
            total = money(case.get('total')) or money(result.get('total')) or Decimal('0')
            at_risk += total
            false_approvals.append({'file': case['file'], 'total': str(total), 'expected': expected})
        if not batch and 'owner' in case and expected != 'approve':
            got = result.get('owner')
            owners['checked'] += 1
            row = owners['matrix'].setdefault(case['owner'], {})
            row[str(got)] = row.get(str(got), 0) + 1
            if got != case['owner']:
                owners['wrong'].append({'file': case['file'], 'expected': case['owner'], 'actual': got})
    return {
        'false_approvals': false_approvals,
        'dollars_at_risk': str(at_risk),
        'payable': payable,
        'paid_touchless': paid,
        'touchless_rate': round(paid / payable, 3) if payable else None,
        'payable_not_paid': missed,
        'owners': owners,
    }


def score(cases, actual, *, batch=False):
    expected_key = 'batch_decision' if batch else 'decision'
    mismatches = []
    false_approvals = 0
    for case in cases:
        result = actual.get(case['file'])
        expected = case[expected_key]
        if result is None:
            mismatches.append({'file': case['file'], 'error': 'missing_result'})
            continue
        if result['decision'] == 'approve' and expected != 'approve':
            false_approvals += 1
        differences = {}
        if result['decision'] != expected:
            differences['decision'] = {'expected': expected, 'actual': result['decision']}
        # An approval the label says needs VP review must have gone through the VP.
        if (
            case.get('batch_route' if batch else 'route') == 'vp'
            and result['decision'] == 'approve'
            and result.get('route') != 'vp'
        ):
            differences['route'] = {'expected': 'vp', 'actual': result.get('route')}
        # Every invoice not paid touchlessly leaves with a packet naming an owner and a next step.
        packet = result.get('packet') or {}
        if needs_packet(result) and not (packet.get('owner') and packet.get('next_step')):
            differences['packet'] = 'missing packet, owner or next step'
        if not batch:
            missing = sorted(set(case['reasons']) - set(result['reasons']))
            if missing:
                differences['missing_reasons'] = missing
            for key in ('vendor', 'date', 'due_date', 'currency', 'currency_evidence', 'owner'):
                if key in case and result.get(key) != case[key]:
                    differences[key] = {'expected': case[key], 'actual': result.get(key)}
            if 'total' in case and result.get('total') != case['total']:
                differences['total'] = {'expected': case['total'], 'actual': result.get('total')}
            if 'quantities' in case and result.get('quantities') != case['quantities']:
                differences['quantities'] = {'expected': case['quantities'], 'actual': result.get('quantities')}
            differences.update(amount_differences(case, result))
        if differences:
            mismatches.append({'file': case['file'], **differences})
    unexpected = sorted(set(actual) - {c['file'] for c in cases})
    return {
        'cases': len(cases),
        'matched': len(cases) - len(mismatches),
        'false_approvals': false_approvals,
        'mismatches': mismatches,
        'unexpected_results': unexpected,
    }


def score_fields(cases, actual):
    fields = (
        'vendor',
        'invoice_number',
        'date',
        'due_date',
        'currency',
        'total',
        'quantities',
        'subtotal',
        'tax',
        'unit_prices',
    )
    counts = {field: {'correct': 0, 'total': 0} for field in fields}
    differences = []
    for case in cases:
        result = actual.get(case['file'], {})
        for field in fields:
            expected = case.get(field)
            if field == 'invoice_number' and field not in case and case['file'].startswith('invoice_'):
                expected = 'INV-' + case['file'].split('_')[1].split('.')[0]
            elif field not in case:
                continue
            observed = result.get(field)
            if field == 'invoice_number':
                match = invoice_key(str(observed or '')) == invoice_key(str(expected))
            elif field == 'total':
                try:
                    match = Decimal(str(observed)) == Decimal(str(expected))
                except InvalidOperation:
                    match = False
            elif field in ('subtotal', 'tax', 'unit_prices'):
                observed = amounts(result)[field] if result else None
                match = not amount_differences({field: expected}, result) if result else False
                if field == 'unit_prices' and observed is not None:
                    observed = {k: sorted(str(v) for v in vs) for k, vs in observed.items()}
                elif observed is not None:
                    observed = str(observed)
            elif field == 'quantities' and isinstance(observed, dict):
                match = {item_key(k): Decimal(str(v)) for k, v in observed.items()} == {
                    item_key(k): Decimal(str(v)) for k, v in expected.items()
                }
            else:
                match = observed == expected
            counts[field]['total'] += 1
            counts[field]['correct'] += int(match)
            if not match:
                differences.append({'file': case['file'], 'field': field, 'expected': expected, 'actual': observed})
    return {'fields': counts, 'mismatches': differences}


def read_jsonl(path):
    results = {}
    for line in path.read_text().splitlines():
        if line.strip():
            result = json.loads(line)
            source = result['source']
            if source in results:
                raise ValueError(f'Duplicate result: {source}')
            results[source] = result
    return results


def evidence_span_issues(result, text):
    """Report selected line-item spans that overlap other fields or split tokens."""
    issues = []
    evidence = result.get('evidence', [])
    for entry in evidence:
        if not entry.get('field', '').startswith('line_items[') or entry.get('start') is None:
            continue
        start, end = entry['start'], entry['end']
        if start < 0 or end > len(text) or start >= end or text[start:end] != entry.get('quote'):
            issues.append({'field': entry['field'], 'issue': 'invalid_span'})
            continue
        quantity_prefix = (
            start
            and text[start - 1].lower() == 'x'
            and text[start].isdigit()
            and (start < 2 or not text[start - 2].isalnum())
        )
        if (not quantity_prefix and continues(text, start - 1, -1)) or continues(text, end, 1):
            issues.append({'field': entry['field'], 'issue': 'token_boundary'})
        for other in evidence:
            if other is entry or other.get('start') is None:
                continue
            if other['start'] <= start and other['end'] >= end:
                issues.append({'field': entry['field'], 'issue': 'overlap', 'other': other['field']})
                break
    return issues


SETS = {
    'supplied': ('evals/expected-main.json', 'data/invoices'),
    'text': ('evals/expected-text.json', 'evals/text-invoices'),
    'attack': ('evals/expected-attack.json', 'evals/attack-invoices'),
    'generated': ('evals/expected-generated.json', 'evals/generated-invoices'),
}


def replay_models():
    """Recorded model responses; replay never touches the network (a missing recording holds)."""

    return {
        'model': llm_model(offline=True),
        'offline': True,
        **{f'{task}_model': llm_model(offline=True, task=task) for task in ('screen', 'vp', 'critic')},
    }


def run_set(root, labels, folder, models):
    dataset = json.loads((root / labels).read_text())
    cases = dataset['cases']
    paths = [root / folder / case['file'] for case in cases]
    files = {p.name for p in (root / folder).iterdir() if p.is_file()}
    if files != {c['file'] for c in cases}:
        raise ValueError(f'Corpus coverage changed in {folder}; review labels')
    for case, path in zip(cases, paths, strict=True):
        if hashlib.sha256(path.read_bytes()).hexdigest() != case['sha256']:
            raise ValueError(f'Corpus hash changed: {path.name}; review labels')
    standalone = {}
    for path in paths:
        conn = open_database(':memory:')  # each document alone, with its own ledger
        try:
            standalone[path.name] = run_batch([path], conn, **models)[0]
        finally:
            conn.close()
    conn = open_database(':memory:')
    try:
        batch = {r['source']: r for r in run_batch(paths, conn, **models)}
    finally:
        conn.close()
    # Highlights must point at what was read: no line-item span that splits a token or overlaps another field.
    spans = {'checked': 0, 'issues': {}}
    for path in paths:
        if path.suffix.lower() in ('.txt', '.pdf'):
            evidence = standalone[path.name].get('evidence', [])
            spans['checked'] += sum(
                e.get('field', '').startswith('line_items[') and e.get('start') is not None for e in evidence
            )
            issues = evidence_span_issues(standalone[path.name], document_text(path))
            if issues:
                spans['issues'][path.name] = issues
    return {
        'standalone': score(cases, standalone),
        'batch': score(cases, batch, batch=True),
        'extraction': score_fields(cases, standalone),
        'evidence_spans': spans,
        'safety': safety(cases, standalone),
        'costs': {'standalone': costs(cases, standalone), 'batch': costs(cases, batch, batch=True)},
    }


def run(root, models=None):
    models = replay_models() if models is None else models
    report = {
        'label_status': 'working labels, not independently reviewed; results are against our labels',
        'mode': 'offline replay of recorded model responses' if models else 'regex and code only',
    }
    for name, (labels, folder) in SETS.items():
        report[name] = run_set(root, labels, folder, models)
    return report


def summary_line(name, c):
    rate = f"{c['touchless_rate']:.0%}" if c['touchless_rate'] is not None else 'n/a'
    return (
        f"{name}: ${c['dollars_at_risk']} at risk in {len(c['false_approvals'])} false approvals; "
        f"{c['paid_touchless']} of {c['payable']} payable paid touchless ({rate}); "
        f"{len(c['owners']['wrong'])} of {c['owners']['checked']} labeled owners wrong"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--results', type=Path, help='Score saved JSONL rather than running the core')
    parser.add_argument('--labels', type=Path, help='Label file for --results')
    parser.add_argument('--live', action='store_true', help='Make fresh provider requests for all four labeled sets')
    parser.add_argument('--recordings', type=Path, help='Read or save model responses in this directory')
    parser.add_argument(
        '--batch', action='store_true', help='The --results come from one batch run: score batch_decision'
    )
    args = parser.parse_args()
    if args.results:
        labels = json.loads((args.labels or args.root / 'evals/expected-main.json').read_text())
        actual = read_jsonl(args.results)
        cases = labels['cases']
        report = {
            'decisions': score(cases, actual, batch=args.batch),
            'extraction': score_fields(cases, actual),
            'safety': safety(cases, actual),
            'costs': costs(cases, actual, batch=args.batch),
        }
        print(json.dumps(report, indent=2))
        print(summary_line('results', report['costs']), file=sys.stderr)
        raise SystemExit(int(bool(report['decisions']['mismatches'] or report['decisions']['unexpected_results'])))
    if args.live and not any(os.environ.get(k) for k in ('LLM_API_KEY', 'XAI_API_KEY', 'DEEPSEEK_API_KEY')):
        parser.error('--live needs XAI_API_KEY, DEEPSEEK_API_KEY, or LLM_API_KEY with LLM_BASE_URL')
    models = None
    if args.live or args.recordings:
        models = {
            'model': llm_model(offline=not args.live, recordings=args.recordings),
            'offline': not args.live,
            **{
                f'{task}_model': llm_model(offline=not args.live, recordings=args.recordings, task=task)
                for task in ('screen', 'vp', 'critic')
            },
        }
    report = run(args.root, models=models)
    if args.live:
        report['mode'] = 'fresh live provider calls'
    print(json.dumps(report, indent=2))
    for name in SETS:
        s = report[name]['safety']
        bound = f", 95% upper bound {s['upper_bound_95']:.1%} if independent" if s['upper_bound_95'] is not None else ''
        print(f"{name}: {s['approved']} of {s['nonpayable']} nonpayable invoices approved{bound}", file=sys.stderr)
        print(summary_line(name, report[name]['costs']['standalone']), file=sys.stderr)
    raise SystemExit(
        int(
            any(
                report[s][k]['mismatches'] or report[s][k]['unexpected_results'] or report[s][k]['false_approvals']
                for s in SETS
                for k in ('standalone', 'batch')
            )
            or any(report[s]['evidence_spans']['issues'] for s in SETS)
        )
    )


if __name__ == '__main__':
    main()
