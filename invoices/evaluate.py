"""Run working-label checks without either observability dependency."""
import argparse
import hashlib
import json
from pathlib import Path

from .core import evaluate, load_invoice, open_database, process_batch


def score(cases, actual, *, batch=False):
    expected_key='batch_decision' if batch else 'decision'
    mismatches=[]
    false_approvals=0
    for case in cases:
        result=actual.get(case['file'])
        expected=case[expected_key]
        if result is None:
            mismatches.append({'file':case['file'],'error':'missing_result'})
            continue
        if result['decision']=='approve' and expected!='approve':
            false_approvals+=1
        differences={}
        if result['decision']!=expected:
            differences['decision']={'expected':expected,'actual':result['decision']}
        if not batch:
            missing=sorted(set(case['reasons'])-set(result['reasons']))
            if missing:
                differences['missing_reasons']=missing
            for key in ('vendor','date','due_date','currency'):
                if key in case and result.get(key)!=case[key]:
                    differences[key]={'expected':case[key],'actual':result.get(key)}
            if result['total']!=case['total']:
                differences['total']={'expected':case['total'],'actual':result['total']}
            if result['quantities']!=case['quantities']:
                differences['quantities']={'expected':case['quantities'],'actual':result['quantities']}
        if differences:
            mismatches.append({'file':case['file'],**differences})
    unexpected=sorted(set(actual)-{c['file'] for c in cases})
    return {'cases':len(cases),'matched':len(cases)-len(mismatches),
            'false_approvals':false_approvals,'mismatches':mismatches,'unexpected_results':unexpected}


def run(root):
    dataset=json.loads((root/'evals/expected-main.json').read_text())
    cases=dataset['cases']
    paths=[root/'data/invoices'/case['file'] for case in cases]
    files={p.name for p in (root/'data/invoices').iterdir() if p.is_file()}
    if files!={c['file'] for c in cases}:
        raise ValueError('Corpus coverage changed; review labels')
    for case,path in zip(cases,paths):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=case['sha256']:
            raise ValueError(f'Corpus hash changed: {path.name}; review labels')
    conn=open_database(':memory:')
    try:
        standalone={p.name:evaluate(load_invoice(p),conn) for p in paths}
        batch={r['source']:r for r in process_batch(paths,conn)}
        return {'policy':dataset['policy'],'label_status':'working, not independently reviewed',
                'standalone':score(cases,standalone),'batch':score(cases,batch,batch=True)}
    finally:
        conn.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    report=run(args.root)
    print(json.dumps(report,indent=2))
    raise SystemExit(int(any(report[k]['mismatches'] or report[k]['unexpected_results'] for k in ('standalone','batch'))))


if __name__=='__main__':
    main()
