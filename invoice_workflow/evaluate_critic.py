"""Measure seeded VP proposals without allowing revisions to hide critic misses."""

import argparse
from collections import Counter
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .database import open_database
from .documents import from_mapping
from .llm import llm_model
from .review import critique_proposal, grounded_justifications, review_facts, vp_eligible

CASES = Path(__file__).resolve().parents[1] / 'data/external/critic-cases.json'


def score_trials(trials):
    bad = [t for t in trials if t['expected_veto']]
    good = [t for t in trials if not t['expected_veto']]
    caught = sum(t['status'] == 'issues' for t in bad)
    false_vetoes = sum(t['status'] == 'issues' for t in good)
    grouped = {}
    for trial in trials:
        grouped.setdefault(trial['case'], []).append(trial)
    return {
        'trials': len(trials),
        'bad_proposals': len(bad),
        'good_proposals': len(good),
        'grounded_catches': caught,
        'catch_rate': caught / len(bad) if bad else None,
        'false_vetoes': false_vetoes,
        'false_veto_rate': false_vetoes / len(good) if good else None,
        'invalid_or_unavailable': sum(t['status'] == 'invalid' for t in trials),
        'blocked_good_proposals': sum(t['status'] != 'pass' for t in good),
        'per_case': {
            key: {
                'statuses': dict(Counter(t['status'] for t in values)),
                'distinct_critiques': len({json.dumps(t['issues'], sort_keys=True) for t in values}),
            }
            for key, values in grouped.items()
        },
        'evidence': (
            'No grounded decision changes observed; critic value is unproven.'
            if not caught
            else 'Grounded vetoes observed against author-written labels.'
        ),
    }


def run_evaluation(critic_model, *, repetitions=3, cases_path=CASES, workers=1):
    if repetitions < 1:
        raise ValueError('repetitions must be positive')
    dataset = json.loads(Path(cases_path).read_text())
    trials = []
    conn = open_database(':memory:')
    try:
        jobs = []
        for case in dataset['cases']:
            invoice = from_mapping(case['input']['invoice'], case['id'])
            facts = review_facts(invoice, conn)
            if not vp_eligible(facts['validation']):
                raise ValueError(f"Fixture is not eligible for VP review: {case['id']}")
            # As in run_vp: the critic sees the VP's stated-reason quotes that code found in the document.
            proposal = {'justifications': [], **case['input']['proposal']}
            grounded, _ = grounded_justifications(proposal, facts['triggers'], invoice.notes)
            if grounded:
                facts = {**facts, 'justifications': [{'trigger': j['trigger'], 'quote': j['quote']} for j in grounded]}
            for repeat in range(repetitions):
                jobs.append((case, facts, repeat))

        def run(job):
            case, facts, repeat = job
            critique = critique_proposal(facts, case['input']['proposal'], critic_model)
            return {'case': case['id'], 'repeat': repeat + 1, 'expected_veto': case['expected']['veto'], **critique}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            trials = list(pool.map(run, jobs))
    finally:
        conn.close()
    return {'label_status': dataset['label_status'], 'summary': score_trials(trials), 'trials': trials}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=CASES)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--live', action='store_true', help='Make fresh provider requests')
    parser.add_argument('--recordings', type=Path)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if args.live and not any(os.environ.get(k) for k in ('LLM_API_KEY', 'XAI_API_KEY', 'DEEPSEEK_API_KEY')):
        parser.error('--live needs XAI_API_KEY, DEEPSEEK_API_KEY, or LLM_API_KEY with LLM_BASE_URL')
    report = run_evaluation(
        llm_model(offline=not args.live, task='critic', recordings=args.recordings),
        repetitions=args.repetitions,
        cases_path=args.cases,
        workers=args.workers,
    )
    report['mode'] = (
        'fresh live calls' if args.live else 'offline replay; repeated recordings do not measure live model variance'
    )
    output = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(output)
    else:
        print(output, end='')
    summary = report['summary']
    return int(
        bool(
            summary['invalid_or_unavailable']
            or summary['false_vetoes']
            or summary['grounded_catches'] != summary['bad_proposals']
        )
    )


if __name__ == '__main__':
    raise SystemExit(main())
