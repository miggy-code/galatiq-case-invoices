"""Top-up after the grounding and authority fixes (cap 60): cases that now reach the VP. Authorized one-pass recording, replay first; hard cap at the HTTP request boundary.

uv run python benchmarks/2026-11-10-generated-set/topup-after-fixes/record.py
Key from DEEPSEEK_API_KEY in the environment only. Never prints the key. No SDK retries.
"""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from invoice_workflow.evaluate import run_set
from invoice_workflow.llm import llm_model
from openai import OpenAI

OUT = Path(__file__).resolve().parent
COUNT = OUT/'requests.json'


def main():
    # Refuse accidental repeat live experiments; use the offline evaluator instead.
    if COUNT.exists():
        raise SystemExit('Request ledger exists; use offline replay')
    os.environ['LLM_BASE_URL'] = 'https://api.deepseek.com'
    os.environ['LLM_MODEL'] = 'deepseek-flash'
    os.environ['VP_MODEL'] = 'deepseek-flash'
    os.environ['CRITIC_MODEL'] = 'deepseek-flash'
    os.environ['LLM_API_KEY'] = os.environ['DEEPSEEK_API_KEY']
    client = OpenAI(api_key=os.environ['LLM_API_KEY'], base_url=os.environ['LLM_BASE_URL'], max_retries=0, timeout=60)
    requests = []

    def create(**kwargs):
        if len(requests) >= 60:
            raise SystemExit('60 request cap reached')
        entry = {'request':len(requests)+1, 'status':'started'}
        requests.append(entry)
        COUNT.write_text(json.dumps(requests, indent=2)+'\n')
        try:
            result = client.chat.completions.create(**kwargs)
        except Exception as exc:
            entry.update(status='error', error=type(exc).__name__)
            raise
        else:
            entry.update(status='ok', model=result.model, usage=result.usage.model_dump())
            return result
        finally:
            COUNT.write_text(json.dumps(requests, indent=2)+'\n')

    proxy = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    def model(task):
        replay = llm_model(offline=True, task=task)
        live = llm_model(client=proxy, task=task)
        def call(text, issue):
            try:
                return replay(text, issue)
            except FileNotFoundError:
                return live(text, issue)
        return call
    models = {'model':model('extract'), **{f'{t}_model':model(t) for t in ('screen','vp','critic')}}
    try:
        report = run_set(ROOT,'evals/expected-generated.json','evals/generated-invoices',models)
        (OUT/'live-results.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'requests':len(requests),'matched':report['standalone']['matched'],'batch_matched':report['batch']['matched']}))
    finally:
        os.environ.pop('LLM_API_KEY', None)
        client.close()


if __name__ == '__main__':
    main()
