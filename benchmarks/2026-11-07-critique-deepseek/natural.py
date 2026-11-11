"""Live paired comparison, using identical frozen VP proposals and local ledger."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys

from invoice_workflow.database import open_database
from invoice_workflow.documents import from_mapping
from invoice_workflow.graph import run_batch
from invoice_workflow.llm import llm_model
from invoice_workflow.review import _proposal, _value, review_facts

root=Path(__file__).resolve().parent
key=os.environ.get('LLM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
if not key:
    raise RuntimeError('Credential unavailable')
os.environ['LLM_API_KEY']=key
cases=json.loads(Path(sys.argv[1]).read_text())['cases']
repetitions=int(sys.argv[2]); name=sys.argv[3]

def run(pair):
    case, repeat=pair
    conn=open_database(':memory:')
    try:
        inv=from_mapping(case['input']['invoice'], case['id'])
        facts=review_facts(inv,conn)
        vp=llm_model(task='vp',recordings=root/(name+'-calls'))
        critic=llm_model(task='critic',recordings=root/(name+'-calls'))
        code=run_batch([inv],conn)[0]
        try:
            raw=vp(json.dumps({'facts':facts},sort_keys=True),None)
            proposed=_proposal(raw,facts)
            valid=all(type(_value(facts,c['path'])) is type(c['value']) and _value(facts,c['path'])==c['value'] for c in proposed['claims'])
            solo='approve' if proposed['decision']=='approve' and valid else 'hold'
            counter=[0]
            def frozen(text, issue):
                counter[0]+=1
                return raw if counter[0]==1 else vp(text,issue)
            combined=run_batch([inv],conn,record_payments=True,vp_model=frozen,critic_model=critic)[0]
        except Exception as exc:
            proposed=None; solo='hold'; combined={'decision':'hold','error':type(exc).__name__}
        return {'case':case['id'],'repeat':repeat,'code':code['decision'],'proposal':proposed,
                'vp_only':solo,'combined':combined,'mock_payment_count':conn.execute('select count(*) from mock_payments').fetchone()[0]}
    finally:
        conn.close()

jobs=[(c,i) for c in cases for i in range(1,repetitions+1)]
results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    futures=[pool.submit(run,j) for j in jobs]
    for future in concurrent.futures.as_completed(futures):
        results.append(future.result())
        (root/(name+'.json')).write_text(json.dumps(results,indent=2)+'\n')
