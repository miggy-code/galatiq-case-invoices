# Steps 2 and 3: expected outcomes and deterministic core

Dedicated local clone: `~/HQ/workspace/agents/codex/work/galatiq-invoices`.
Upstream: https://github.com/galatiq-ai/galatiq-case-invoices,
main at `2f150152b962ccd24b35e515e56b04f673d410bc`.
The original README and all upstream input documents are preserved.

## Run

Requires Python 3.11+ and uv. No API keys, Docker, server, or network calls at
runtime. Install from the lockfile, then run:

```sh
uv sync --frozen
uv run pytest -q
uv run python -m invoices.evaluate
uv run python -m invoices data/invoices
```

The last command emits one JSON object per document. A single-file path is also
accepted. Default operation uses an in-memory SQLite database and records no
payments. To exercise the local mock ledger across repeated invocations:

```sh
uv run python -m invoices data/invoices --db output/inventory.db --record-mock-payments
```

Create `output/` first. This records local mock rows only. The unique normalized
vendor/invoice key prevents repeated payment even across database connections.
Both revisions of INV-1004 remain held in a full batch; a human must select a
revision before a later workflow can pay it. Stock is not decremented.

## What exists

- `evals/expected-main.json`: 20 document labels, source hashes, expected extracted
  vendor/dates/currency/total/aggregate quantities, minimum diagnostic reasons,
  and standalone/batch decisions.
- `evals/POLICY.md`: spec requirements versus working assumptions and label edits.
- `invoices/core.py`: JSON/CSV/XML/TXT/text-based PDF parsing, numeric OCR evidence,
  conservative normalization, Decimal math, SQLite stock checks, conservative
  review gates, cross-format duplicate and revision reconciliation, local mock
  payment ledger. Parse errors become per-file rejections; database/I/O failures
  surface rather than pretending the batch succeeded.
- `invoices/evaluate.py`: validates corpus coverage/hashes and checks results
  against the labels, including false approvals. Exit status is nonzero for any
  mismatch. The callable scorer can later grade results from other pipelines.
- `tests/test_core.py`: real documents, real PDF extraction and SQLite, plus
  malformed input, money/stock boundaries, duplicate/revision behavior, concurrent
  mock-payment attempts and known-good/known-bad scorer tests.

## Review workbench

A local review UI over this core lives in `invoices/workbench/`; see `WORKBENCH.md` for start
command, behavior, validation evidence and limitations
(`uv run python -m invoices.workbench`).

## Verification on 2026-11-06

- `uv run pytest -q`: 39 passed.
- `uv run python -m invoices.evaluate`: all 20 standalone and all 20 batch working
  expectations matched, with no false approvals against these labels.
- Test-first evidence: initial pytest collection failed with missing
  `invoices.core`; implementing it exposed parser/arithmetic failures, then green.
- Initial INV-1007 hold label was corrected after independently checking the
  printed arithmetic; see the policy log. The eval is a characterization of
  author-written expectations, not independently reviewed benchmark accuracy.
- CI not configured or run. Local Python 3.14.2; other Python versions untested.

## Scope and remaining work

Only steps 2 and 3 were requested. No v2 policy/corpus, Bella message, LLM calls,
LangGraph, VP reflection, live payment, Phoenix/Panoptes integration, or lab
execution was added. `approve` here means core eligibility, not VP sign-off.

The deterministic text parser supports the supplied layouts, not arbitrary
vendor documents or image-only scans. New extraction should feed the same
`from_mapping`/`evaluate` boundary. No vendor bank registry, FX rates, inventory
reservation, external payment idempotency, or production AP policy is claimed.
Due dates are required by the working main policy; this differs from v2.

The labels and JSON results are observation-neutral. A later Phoenix or Panoptes
experiment can use the same expected answers while capturing different traces;
this implementation does not establish that either existing Harbor lab can
already run invoice-level evaluations. Keep eval labels out of model inputs.
