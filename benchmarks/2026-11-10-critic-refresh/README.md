# Critic evaluation, refreshed (2026-11-10)

The critic cases in `data/external/critic-cases.json` predated the catalog: even the controls billed WidgetA at $1,200, 380% above catalog with no reason. Their recordings also predated the decision-rights and discount-rule prompts, so all 24 old trials replayed as invalid. The refresh re-priced the eight cases at catalog, kept what each one tests, added two discount cases for the 2026-11-10 rule, and gave the critic the VP's grounded stated-reason quotes as `run_vp` does. Labels were committed before this run.

Run: `uv run python -m invoice_workflow.evaluate_critic --live --out benchmarks/2026-11-10-critic-refresh/live-results.json` (DeepSeek `deepseek-flash`, 10 cases × 3 repetitions).

## Requests and cost
- **30 provider requests**, all successful.
- 25,002 input and 28,316 output tokens.
- **$0.042**, valuing all input at the cache-miss rate of $0.30/M and output at $1.20/M. Cost is calculated from token counts.

## Results
- Bad proposals caught: **18 of 18** (6 cases × 3).
- False vetoes on good proposals: **0 of 12**. Invalid responses: 0.
- Every case gave the same verdict on all three repetitions.
- Offline replay (`uv run python -m invoice_workflow.evaluate_critic`) exits 0.

Three bad-proposal types contain false factual claims: invented total, a price
claimed at catalog when it differs, and invented stock. Code also rejects these
claims. The other three types exercise judgment: an unexplained 20% premium,
a description that differs from the item, and a vague discount reason. The
critic caught all three judgment types across **9/9 trials**.
