"""Record frozen attack fixtures once per prompt, with a hard provider-request cap.

Run from the repo root with LLM_API_KEY in the process environment. Existing
recordings replay. No SDK retries. The key is never written to disk.
"""
import json
from pathlib import Path
from openai import OpenAI
from invoice_workflow.database import open_database
from invoice_workflow.graph import run_batch
from invoice_workflow.llm import llm_model


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
CAP = 150
import os
client = OpenAI(api_key=os.environ['LLM_API_KEY'], base_url='https://api.deepseek.com', timeout=60, max_retries=0)
create = client.chat.completions.create
calls = json.loads((OUT/'requests.json').read_text()) if (OUT/'requests.json').exists() else []
for item in calls:
    if 'usage' not in item:
        item['status'] = 'interrupted; usage unknown'
def capped(**kwargs):
    if len(calls) >= CAP:
        raise RuntimeError('150 provider request cap reached; stop')
    item = {'request':len(calls)+1, 'model':kwargs['model']}
    calls.append(item)
    (OUT/'requests.json').write_text(json.dumps(calls,indent=2)+'\n')
    response = create(**kwargs)
    item['usage'] = response.usage.model_dump() if response.usage else None
    (OUT/'requests.json').write_text(json.dumps(calls,indent=2)+'\n')
    print(f"request {len(calls)}/{CAP}", flush=True)
    return response
client.chat.completions.create = capped
def cached(task):
    replay = llm_model(offline=True,task=task)
    live = llm_model(client=client,task=task)
    def call(text,issue):
        try:
            return replay(text,issue)
        except FileNotFoundError:
            return live(text,issue)
    return call
models = {'model':cached('extract'), 'offline':False,
          **{f'{task}_model':cached(task) for task in ('screen','vp','critic')}}
labels = json.loads((ROOT/'evals/expected-attack.json').read_text())['cases']
paths = [ROOT/'evals/attack-invoices'/c['file'] for c in labels]
standalone=[]
for path in paths:
    conn=open_database(':memory:')
    try:
        standalone.extend(run_batch([path],conn,record_payments=False,**models))
    finally:
        conn.close()
(OUT/'standalone.json').write_text(json.dumps(standalone,indent=2)+'\n')
conn=open_database(':memory:')
try:
    batch=run_batch(paths,conn,record_payments=False,**models)
finally:
    conn.close()
(OUT/'batch.json').write_text(json.dumps(batch,indent=2)+'\n')
print(json.dumps({'provider_requests':len(calls)}))
