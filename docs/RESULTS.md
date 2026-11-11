# Offline replay results

The evaluators replay the recorded DeepSeek (`deepseek-flash`) answers in `evals/recordings/`, with no API key
set, and score every labeled set in `evals/`. The live Grok results are in [GROK_RESULTS.md](GROK_RESULTS.md).

The [provenance](results/2026-11-11/provenance.json) records the check time, Python version, platform, commands,
exit codes and SHA-256 hashes of the code, tests, fixtures and recordings.

## Invoice decisions

All paired values below are **standalone / batch**. Standalone runs use a fresh
ledger for each document; batch runs reconcile copies and revisions together.

| Set | Documents | Standalone matched | Batch matched | Should not pay | Paid anyway | Dollars at risk |
|---|---:|---:|---:|---:|---:|---:|
| Supplied | 20 | 20/20 | 20/20 | 10 / 13 | 0 / 0 | $0 / $0 |
| Handwritten text | 20 | 20/20 | 20/20 | 15 / 15 | 0 / 0 | $0 / $0 |
| Paired attacks and controls | 24 | 24/24 | 24/24 | 11 / 12 | 0 / 0 | $0 / $0 |
| Generated boundary cases | 46 | 46/46 | 46/46 | 24 / 26 | 0 / 0 | $0 / $0 |

“Should not pay” counts documents whose label requires a decision other than
approve in that mode. “Paid anyway” counts those documents that the pipeline
approved; dollars at risk sum their invoice amounts. Every labeled payable
document was approved in both modes.

Standalone matching checks the decision, required reasons, labeled fields and
owner. Both modes check required VP routing and exception packets; batch matching
checks the batch decision.

Source: [evaluation JSON](results/2026-11-11/evaluate.json) and
[summary](results/2026-11-11/evaluate-summary.txt).

## Evidence spans

The evaluator checked **587 selected line-item evidence spans**, with **0 issues**:
106 supplied, 81 handwritten text, 192 paired attacks and controls, and 208
generated boundary cases. It checks offsets, token boundaries and overlaps.

Source: `evidence_spans` in the [evaluation JSON](results/2026-11-11/evaluate.json).

## Critic replay

Across ten cases with three replay trials each, the critic caught **18/18 seeded
bad-proposal trials**, passed **12/12 good-proposal trials**, and returned **0
invalid or unavailable responses**. False vetoes: **0/12**.

Source: [critic JSON](results/2026-11-11/critic.json).

## Tests

**60 test cases passed.** The suite checks payment, authority, routing, evidence,
failure handling, provider selection, the report and the CLI.

Source: [pytest output](results/2026-11-11/pytest.txt).

## Reproduce

From the repository root:

```sh
# Tests
env -u LLM_API_KEY UV_OFFLINE=1 uv run pytest -q
# Invoice decisions and evidence spans
env -u LLM_API_KEY UV_OFFLINE=1 uv run python -m invoice_workflow.evaluate
# Critic trials
env -u LLM_API_KEY UV_OFFLINE=1 uv run python -m invoice_workflow.evaluate_critic
```

All three exit 0. The evaluators use in-memory ledgers.
