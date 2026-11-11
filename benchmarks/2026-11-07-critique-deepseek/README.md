# DeepSeek VP and critic experiment, 2026-11-07

Synthetic VP and critic experiment. Labels and metadata were excluded from model inputs; invoice numbers were blinded. The five-case critic set was frozen before responses were observed. Payments used a local mock SQLite ledger.

The same requested and returned model ID, `deepseek-flash`, was used for VP and critic with temperature 0 requested and max tokens 4096. Critic used the provider default thinking setting, which [ignores temperature](https://api-docs.deepseek.com/guides/thinking_mode/); VP required thinking disabled for forced tool choice. The critic sees structured claims and facts, never the VP rationale. Recordings include prompts, responses and, for VP, the tool exchange. Each fresh attempt also has a unique audit file; the hash-only file is for replay.

| Critic experiment | Relevant grounded vetoes on bad | Controls passed | Invalid |
| --- | ---: | ---: | ---: |
| Baseline development, 2 per case | 5/10 | 0/6 | 11/16 |
| Selected development, 3 per case | 15/15 | 9/9 | 0/24 |
| Frozen holdout, 3 per case | 5/6 | 9/9 | 1/15 |

The initial critic repeatedly treated `high_value` as an absolute prohibition rather than a review route; it also cited proposal paths rather than actual fact paths. A general clarification fixed both observed failure classes. Each selected-dev objection was inspected for relevance. Nine bad development trials contained false scalar claims that code already blocks, including the price-spike case that also had a fuzzy anomaly. Six bad development trials and all six holdout bad trials had truthful claims and relied on the critic's price or description judgment. On holdout the one invalid result was empty JSON output and safely held. The five valid holdout objections were relevant on manual inspection.

The actual local `process_batch` path was tested with the same frozen initial VP proposals for the VP-only and VP-plus-critic comparisons. All 15 cases were high value, so code alone held every case and automated none. After the final VP prompt iteration, the VP held all six anomaly trials and approved all nine controls; adding the critic changed zero decisions and the mock ledger recorded nine payments. The preceding VP prompt held the same six anomaly trials but approved only four of nine controls. These development results were used to tune the VP.

The first 15 VP trials failed with DeepSeek 400 because its thinking mode did not support the forced tool choice. Disabling thinking for VP resolved that transport error. The next 15 produced unsupported bracket-style paths; code held them. The VP prompt was clarified to request dotted paths and explain high-value routing. Its final tuning addressed observed unsupported requests for absent price/receipt evidence and refusal to consider plausible surcharge explanations. The critic prompt was not changed after the holdout.

`summary.json` contains chart-ready counts and per-stage usage. Recorded latency sums call durations across concurrent calls. Raw baseline, revisions, holdout, natural paired results, exact system prompts, and per-attempt recordings are alongside this report.

Run a fresh, authorized test using `uv run python -m invoice_workflow.evaluate_critic --live --repetitions 3 --workers 4 --out PATH`; without `--live`, responses are replayed. Live calls use `LLM_API_KEY` or `DEEPSEEK_API_KEY` from the environment. No key is recorded. Local tests: `uv run pytest -q` (73 passed, 13 dependency deprecation warnings) and `uv run python -m invoice_workflow.evaluate` (20/20 standalone and batch; zero false approvals against labels).
