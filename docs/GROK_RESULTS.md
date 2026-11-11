# Grok results

Grok 4.7 (`grok-4.7` at `https://api.x.ai/v1`), run live on 2026-11-11 with the DeepSeek backup off, so every
answer is Grok's.

## Invoice decisions

| Set | Documents | Standalone matched | Batch matched | False approvals |
|---|---:|---:|---:|---:|
| Supplied | 20 | 20/20 | 20/20 | 0 / 0 |
| Handwritten text | 20 | 20/20 | 20/20 | 0 / 0 |
| Paired attacks and controls | 24 | 24/24 | 24/24 | 0 / 0 |
| Generated boundary cases | 46 | 46/46 | 46/46 | 0 / 0 |

Standalone matching checks the decision, required reasons, labeled fields and owner. Batch matching checks the
batch decision. Both check required VP routing and exception packets.

## Evidence spans

585 selected line-item evidence spans checked, 0 issues: offsets, token boundaries and overlaps.

## Critic

Ten cases, three trials each: 18 of 18 planted bad approvals caught, 12 of 12 good proposals passed, 0 invalid
answers.

## Files

- [Invoice decisions, extraction and evidence spans](results/2026-11-11-grok/evaluate.json)
- [Critic trials](results/2026-11-11-grok/critic.json)
- [Mismatch lists](results/2026-11-11-grok/failures.json)
- [Request log](results/2026-11-11-grok/initial-timeouts.log)
- [Continuation events](results/2026-11-11-grok/resume-events.jsonl) and [final events](results/2026-11-11-grok/resume-events-low.jsonl)
- [Unit tests](results/2026-11-11-grok/pytest.txt) and [lint](results/2026-11-11-grok/ruff.txt)
- [Configuration and source hashes](results/2026-11-11-grok/provenance.json)

The model answers are saved in `evals/recordings/`.

## Reproduce

With `XAI_API_KEY` set:

```sh
env -u DEEPSEEK_API_KEY uv run python -m invoice_workflow.evaluate --live --recordings output/grok-recordings
env -u DEEPSEEK_API_KEY uv run python -m invoice_workflow.evaluate_critic --live --recordings output/grok-recordings --out output/grok-critic.json
```
