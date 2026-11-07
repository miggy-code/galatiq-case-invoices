import argparse
import json
from pathlib import Path

from .core import open_database, process_batch


def main():
    parser=argparse.ArgumentParser(description='Offline deterministic invoice core (no VP/LLM stage).')
    parser.add_argument('path',type=Path,help='Invoice file or batch directory')
    parser.add_argument('--db',default=':memory:',help='SQLite inventory and mock-payment ledger')
    parser.add_argument('--record-mock-payments',action='store_true')
    args=parser.parse_args()
    if not args.path.exists():
        parser.error(f'Path not found: {args.path}')
    paths=sorted(p for p in args.path.iterdir() if p.is_file()) if args.path.is_dir() else [args.path]
    if not paths:
        parser.error('No documents')
    conn=open_database(args.db)
    try:
        for result in process_batch(paths,conn,record_payments=args.record_mock_payments):
            print(json.dumps(result))
    finally:
        conn.close()


if __name__=='__main__':
    main()
