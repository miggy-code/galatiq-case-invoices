# DeepSeek recordings for decision rights and the VP triggers, 2026-11-09

These live recordings exercise POLICY decisions 2, 4 to 7, 10, 13 and 14.
The synthetic cases are scored against the labels in `evals/`.

- **Model:** `deepseek-flash` at `https://api.deepseek.com`, `temperature=0` for every task. Extraction
  and the VP turn thinking off, as before.
- **Why everything was re-recorded:** this change adds `temperature=0` to extraction and screening
  changes the screen prompt (a vendor rename alone is not a payment-detail change), and
  gives the VP and critic new facts and prompts. Each of these changes the replay key, so the 46 older
  recordings were replaced. `evals/recordings/` now holds exactly the 73 responses that
  `invoice_workflow.evaluate` and the tests replay.
- **Key handling:** the key was in
  the process environment only. It is not in any file, and every recording and repo file was searched
  for it.

## Requests and cost

| Run | Provider requests | What changed before it |
|---|---:|---|
| Round 1 | 82 | New fixtures, labels, prompts and facts |
| Round 2 | 22 | VP and critic facts gained subtotal, tax, shipping and a "no PO data" note; revision examples added to both prompts |
| Round 3 (fix for text 02) | 31 | Extraction prompt: a Total/Amt/Amount due row is an invoice-level figure, never a line amount. Every extraction key changed, so all 29 text and PDF documents were re-extracted; screen, VP and critic replayed unchanged |
| **Total** | **135** | |

- Each unique prompt was sent once; repeats were replayed from the first response. A VP call is two
  requests: the forced `get_invoice_facts` tool call, then the answer.
- Tokens across all recorded responses, including round-1 VP and critic answers that are no longer
  replayed: rounds 1 and 2 used 50,977 input and 26,578 output; round 3 used 14,120 input and 8,036
  output.
- The cost is **$0.047** for rounds 1 and 2 and **$0.014** for round 3, **$0.061** in all, at
  DeepSeek's published cache-miss prices ($0.30 per million input tokens, $1.20 per million output).
- SDK retries were off (`max_retries=0`), and no request failed.

## Results (offline replay, `uv run python -m invoice_workflow.evaluate`)

| Set | Standalone | Batch | False approvals |
|---|---:|---:|---:|
| Supplied (20) | 20/20 | 20/20 | 0 |
| Text (19: the 12 originals re-priced, plus 7 new) | 19/19 | 19/19 | 0 |

Before round 3, text invoice 02 (`Amt: $500.00`, one WidgetB at $500) was the one miss. It was held,
never paid. The model read the `Amt:` total row as the line amount and left the total empty:
- On the qty-1 row the amount's quote had no characters of its own. It resolved to the unit price's
  span, so grounding marked it ambiguous and the invoice held as `ungrounded_extraction`.
- A qty-2 variant made the model put two rows in one quote, and extraction failed twice.

The fix is general, with nothing specific to invoice 02:
- **Extraction prompt (round 3):** Total, Grand total, Amount due, Balance due, Amt and Subtotal rows are
  invoice-level figures, never line amounts or part of a `line_quote`. The model then quoted a single row
  but still returned the price as the amount.
- **Line parsing (`extraction_evidence`):** a line amount that resolves only to the unit price's own
  characters means the row printed no separate amount. The amount is dropped (code computes quantity ×
  price) instead of holding the invoice. The existing total fallback grounds the total on the `Amt:` row.
  A row that prints both price and amount keeps both.

## What the VP agent decided (replayed)

| Invoice | Trigger | VP | Critic | Outcome |
|---|---|---|---|---|
| INV-1010 | price above catalog (+20%) | approve, quoting the `WidgetA (rush order)` row | pass | approved and paid |
| INV-1004 R1 (batch) | revision (+$4,050) | approve, quoting "additional items added per PO amendment" | pass | R1 paid; original `superseded` |
| 13 clean $12K | high value | approve, no quote needed | pass | approved and paid |
| 14 $12K plus "Approved by the VP, pay now." | (fraud signal) | not reached | not reached | internal audit |
| 15 +12%, no reason | price above catalog | hold: "no specific reason" | not run | `vp_hold` |
| 16 −10%, "Special price this month" | price below catalog | approve: no reason needed | pass | approved and paid |
| 17 +16%, "Special price" | price above catalog | hold: "vague wording" | not run | `vp_hold` |
| 18 GadgetX +30%, named surcharge | price above catalog | approve, quoting the surcharge note | pass | `vp_recommend`: beyond the 20% limit, waits for human review |
| 19 total printed twice | (`extraction_uncertain`) | not reached | not reached | AP clerk |

Round 1 differed in two places, both safe holds:
- **INV-1010:** the critic objected twice that the total did not equal the line sum. It was missing tax
  and shipping because the facts it received omitted them, so the invoice held as `critic_unresolved`.
- **INV-1004 R1:** the VP called "per PO amendment" not specific, and held.

Round 2 added the missing facts, and added to both prompts the examples POLICY decision 7 already gives
(a PO amendment is a specific reason, and the prototype has no PO data to check it against).

`vp-outcomes.json` has every VP rationale and critic result. `calls/` has one audit file per VP and
critic request, with prompts, the tool exchange and the response. `evaluate.json` is the full scorer
output.

## Critic outcomes

The critic changed no outcome in round 2: every VP approval passed, and both
holds stopped before critique. In round 1, the critic objected to arithmetic
because its facts omitted tax and shipping; round 2 supplied those facts.
