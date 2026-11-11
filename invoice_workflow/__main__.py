"""Command line: python main.py --invoice_path=FILE, or --batch DIR. Results to stdout, logs to stderr."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .database import open_database
from .documents import DOCUMENT_SUFFIXES
from .llm import llm_model
from .graph import run_batch
from .packet import summary
from .report import write_report

RESULT_SUMMARY = (
    'source',
    'invoice_number',
    'vendor',
    'total',
    'currency',
    'decision',
    'owner',
    'reasons',
    'next_step',
)


def main():
    parser = argparse.ArgumentParser(description='Invoice extraction, validation, VP review and mock payment.')
    parser.add_argument('path', nargs='?', type=Path, help='Invoice file or batch directory')
    parser.add_argument('--invoice_path', type=Path, help='Invoice file')
    parser.add_argument('--batch', type=Path, help='Directory of invoices')
    parser.add_argument(
        '--db', default='output/ap.db', help='SQLite inventory and payment ledger (default output/ap.db)'
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=Path('output/results.jsonl'),
        help='Full JSONL results, with evidence and the agent\'s reasoning (default output/results.jsonl)',
    )
    parser.add_argument(
        '--report',
        type=Path,
        default=Path('output/report.html'),
        help='HTML review report (default output/report.html)',
    )
    parser.add_argument(
        '--offline', action='store_true', help='Replay recorded model responses; never call the network'
    )
    parser.add_argument('--verbose', action='store_true', help='Also log each model call')
    args = parser.parse_args()
    choices = [p for p in (args.path, args.invoice_path, args.batch) if p is not None]
    if len(choices) != 1:
        parser.error('Provide one path, --invoice_path, or --batch')
    path = choices[0]
    if not path.exists():
        nearby = (
            sorted(p.name for p in path.parent.glob('*') if p.suffix.lower() in DOCUMENT_SUFFIXES)
            if path.parent.is_dir()
            else []
        )
        hint = f'. Files there: {", ".join(nearby[:4])}{", ..." if len(nearby) > 4 else ""}' if nearby else ''
        parser.error(f'Path not found: {path}{hint}')
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file())
        # Finder's .DS_Store and other files that are not invoices are not the vendor's error: skip them.
        paths = [p for p in files if not p.name.startswith('.') and p.suffix.lower() in DOCUMENT_SUFFIXES]
    else:
        files = paths = [path]
    if not paths:
        parser.error('No documents')
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s', stream=sys.stderr
    )
    if len(files) > len(paths):
        logging.getLogger('invoice_workflow').info(
            'skipped=%s (not an invoice format)', [p.name for p in files if p not in paths]
        )
    for noisy in ('httpx', 'httpcore', 'openai'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if os.environ.get('LLM_API_KEY') and not args.offline and not os.environ.get('LLM_BASE_URL'):
        # Never send a key to a default endpoint it was not issued for (a grader's xAI key to DeepSeek).
        parser.error(
            'LLM_API_KEY is set but LLM_BASE_URL is not. Set both, and LLM_MODEL; for Grok: '
            'LLM_BASE_URL=https://api.x.ai/v1 LLM_MODEL=grok-4.7. Or run with --offline to replay recordings.'
        )
    offline = args.offline or not any(os.environ.get(k) for k in ('LLM_API_KEY', 'XAI_API_KEY', 'DEEPSEEK_API_KEY'))
    if offline and not args.offline:
        print(
            'No model API key set: replaying recorded model responses (offline). A document with no recording holds.',
            file=sys.stderr,
        )
    models = {task: llm_model(offline=offline, task=task) for task in ('extract', 'screen', 'vp', 'critic')}
    if args.db != ':memory:':
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = open_database(args.db)
    except RuntimeError as exc:
        parser.exit(2, f'{exc}\n')
    try:
        results = run_batch(
            paths,
            conn,
            record_payments=True,
            model=models['extract'],
            offline=offline,
            screen_model=models['screen'],
            vp_model=models['vp'],
            critic_model=models['critic'],
        )
    finally:
        conn.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(''.join(json.dumps(result) + '\n' for result in results))
    for result in results:  # stdout: one short structured line per document
        print(json.dumps({key: result.get(key) for key in RESULT_SUMMARY}))
    print(summary(results), file=sys.stderr)
    print(f'Results: {args.out}', file=sys.stderr)
    print(f'Report: {write_report(results, args.report)}', file=sys.stderr)


if __name__ == '__main__':
    main()
