# Acme invoice processing

Acme pays every invoice by hand. About 30% have errors, each takes about five days, and the errors cost around $2M a year. This pipeline pays the clean invoices in seconds with nobody involved. Every other invoice goes to the one person who can fix it, with the reason, the evidence and the next step attached.

**The design in one sentence:** models read documents and judge reasons, and code decides whether money moves.

The original assignment is in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md).

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python main.py --invoice_path=data/invoices/invoice_1001.txt
uv run python main.py --batch data/invoices
open output/report.html        # the review page: who has to act, and why
```

The brief's form works too once the environment is active: `source .venv/bin/activate`, then `python main.py --invoice_path=data/invoices/invoice_1001.txt`. The supplied files are named `invoice_1001.txt` to `invoice_1016.*`.

The batch reports INV-1001 as already paid, because the first command paid it: nothing is paid twice. The ledger lives in `output/ap.db`; delete `output/` to start fresh, or pass `--db :memory:`.

**No API key is needed.** Without a key, every model call is replayed from a recording in `evals/recordings/`. A document with no recording holds for AP operations.

**To call Grok live,** set your xAI key. The model is Grok 4.7, at low reasoning effort with a 180-second deadline per request:

```sh
export XAI_API_KEY=...
uv run python main.py --batch data/invoices
```

With `DEEPSEEK_API_KEY` also set, DeepSeek is the backup: a Grok timeout, connection failure, rate limit or server error retries that call on DeepSeek, and the switch is logged. An invalid answer, failed grounding or a hold never switches providers, and each provider receives only its own key.

For any other OpenAI-compatible endpoint, set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL`. The CLI refuses a key without `LLM_BASE_URL`, so a key is never sent to an endpoint you didn't choose. `VP_MODEL` and `CRITIC_MODEL` override the model for those two tasks.

### What you'll see

Each invoice logs the brief's four stages, saying who did the work:

```text
ingestion  INV-1010 read by the model; 21 of 21 values found in the document by code
validation INV-1010 found price_above_catalog (WidgetA (rush order) $300.00 against catalog $250)
approval   INV-1010 VP agent: approve, quoting "WidgetA (rush order)"; critic: pass; round 1 of 2; outcome: approved
payment    INV-1010 paid 7185.00 to Consolidated Materials Group
```

An invoice that needs no judgment says `approval ... by rule: no open question, under $10,000, so no VP review needed`. Results go to stdout as one short JSON line per document: decision, owner, reasons and next step. The full results, with the evidence and the agent's reasoning, go to `output/results.jsonl` (`--out` to change). The review page groups every unpaid invoice by owner, with the fraud phrases and extracted values highlighted in the source text.

### Which sample shows what

| Run | Shows |
|---|---|
| `invoice_1001.txt` | A clean invoice paid with nobody involved |
| `invoice_1003.txt` | A $100,000 fraud attempt held for internal audit, never sent to the approver |
| `invoice_1010.txt` | The approval agent and the critic: 20% over catalog, approved because the document states a reason |
| `evals/text-invoices/13-high_value_clean.txt` | The brief's $10K rule on a clean invoice (every supplied invoice over $10K also has a factual problem, so it goes to the person who can settle the fact first) |
| `--batch data/invoices` | Everything: duplicates across formats, a revision, a foreign currency, unknown items |

## How it works

```text
per document:  load -> extract (model, text and PDF only) -> validate (code) -> fraud screen (code rules + model)
per batch:     reconcile duplicates and revisions (code) -> VP agent + critic (models, only when judgment is needed)
               -> pay at most once through a ledger (code) -> route every exception to one owner (code)
```

One [LangGraph](https://langchain-ai.github.io/langgraph/) workflow. Code chooses every transition; models never choose a step and have no payment or write tools.

### What is code and what is AI

| Code (deterministic) | Model, and how code bounds it |
|---|---|
| Parsing JSON, CSV and XML | **Extraction** of text and PDF invoices. The vendor, invoice number, dates, total and every line's item, quantity and price must quote the document; code finds each quote, a second reading by regex must agree, and the arithmetic must close to the cent. Otherwise the invoice holds for a clerk. |
| Arithmetic, catalog prices, stock, unknown items, currency | **Fraud screen** for reworded pressure or bank changes. It can only add a hold. Literal phrases are also caught by code rules. |
| Duplicates, revisions, and a pay-at-most-once ledger | **Approval agent (the "VP")**, for one question: is the reason this invoice states good enough? It decides only inside authority limits written in code, and code checks every fact and quote it cites. |
| Routing: each reason has one owner and one next step, in one table | **Critic**, a second model that looks for reasons not to pay. It can only veto. |
| Authority limits; holding on any failure, of a model or of our code | |

The approval agent runs only when every open question is a judgment call: a total over $10,000, a price off catalog, or a revised invoice. Its limits: up to $25,000, up to 20% above catalog or 30% below with a stated reason, and up to $5,000 added by a revision. Beyond those it recommends and a person decides. It gets its facts through a read-only function call, answers in JSON that code validates, and revises at most once if the critic objects.

### Who gets what

| Owner | Gets | Example |
|---|---|---|
| Internal audit | Pressure, instructions aimed at our review, bank-detail changes | INV-1003 |
| Vendor | Data that is wrong: bad arithmetic, missing fields, negative quantities | INV-1009 |
| AP operations | One of our own steps failed (model timeout, missing recording, a bug) | — |
| AP clerk | We are unsure of a fact: stock, unknown item, currency, an uncertain reading | INV-1002, INV-1008 |
| Human review | A business judgment beyond the agent's limits, or one the agent held | — |

Every rule, with its business reason, is in [evals/POLICY.md](evals/POLICY.md).

## Tests and evaluation

On Grok 4.7, all 110 labeled documents match their labels, standalone and in batch, with 0 false approvals, and the critic catches 18 of 18 planted bad approvals while passing 12 of 12 good ones ([docs/GROK_RESULTS.md](docs/GROK_RESULTS.md)). Offline replay gives the same result ([docs/RESULTS.md](docs/RESULTS.md)).

```sh
uv run pytest -q                                   # ten invariant tests: what can never happen with money
uv run python -m invoice_workflow.evaluate         # replays the labeled invoice sets in evals/ and scores every decision
uv run python -m invoice_workflow.evaluate_critic  # replays planted bad approvals against the critic
```

All three run offline from the recordings. The tests replace only the model and the bank; everything else runs for real.

To run the evaluation live on Grok alone, with the DeepSeek backup off:

```sh
env -u DEEPSEEK_API_KEY uv run python -m invoice_workflow.evaluate --live --recordings output/grok-recordings
env -u DEEPSEEK_API_KEY uv run python -m invoice_workflow.evaluate_critic --live --recordings output/grok-recordings --out output/grok-critic.json
```

## Read the code in this order

1. **`graph.py`**, `build_batch_graph` and `build_document_graph`, at the bottom: the pipeline as named stages, in order.
2. **`routing.py`**, `ROUTING` and `route()`: every reason, its owner and its next step, in one table. `route()` is the only place an outcome is set, and re-routing a reason is a one-row change.
3. **`validation.py`**, `validate()`: the checks code makes (arithmetic, stock, catalog prices, currency).
4. **`review.py`**, `AUTHORITY` and `run_vp()`: what the approval agent may approve alone, and the bounded loop with the critic.
5. **`payment.py`**, `pay()`: claim the payment in the ledger, call the bank, then mark it paid.
6. **`tests/test_invariants.py`**: the ten test names state what can never happen with money.

The rest is supporting detail:
- `documents.py`: reading JSON, CSV, XML and text files.
- `extraction.py`: the model's reading of text and PDF, and every check on it (`extract_document`).
- `screen.py`: the fraud screen.
- `reconcile.py`: duplicates and revisions.
- `database.py`: all SQL.
- `prompts.py`: every instruction a model gets.
- `llm.py`: the model provider (Grok, with DeepSeek as backup), and record and replay of model calls.
- `packet.py` and `report.py`: the case packets and the review page.

## Repository

| Path | What |
|---|---|
| `invoice_workflow/` | The pipeline, one module per job (see the reading order above) |
| `tests/test_invariants.py` | The specification: what can never happen with money |
| `evals/` | Labeled invoice sets, [POLICY.md](evals/POLICY.md), [FAILURES.md](evals/FAILURES.md), recordings for offline replay |
| `benchmarks/` | Dated live model runs |
| `data/invoices/` | The 20 supplied documents |
