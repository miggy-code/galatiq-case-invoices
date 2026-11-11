# Known failures

**Rule: a known failure becomes a labeled case.** Every mistake the system has made, or that a review or test showed it would make, gets a guard: a labeled invoice that `invoice_workflow.evaluate` replays, or a row in one of the ten tests in `tests/test_invariants.py`. The guard is added before or with the fix, and it is shown to fail without the fix. From then on the mistake cannot come back unnoticed:
- `uv run python -m invoice_workflow.evaluate` exits non-zero on any label mismatch, false approval or bad evidence highlight.
- `uv run pytest -q` fails on any broken invariant.

This file is the index. Nothing reads it at run time, and no prompt or rule changes itself. A person approves every fix.

**Where a fix goes:** the cheapest layer that holds.
1. Data, such as a catalog or vendor row, or a code rule.
2. Then a general prompt instruction written by a person. Never paste a raw document into a prompt.
3. Then a case in the critic evaluation.

## Log

| # | Found | What went wrong | Money at risk | Stage | Fix | Guard: what fails if it comes back |
|---|---|---|---|---|---|---|
| 1 | 2026-11-06, first model run | Regex-only reading rejected valid text invoices with layouts it could not parse | Valid invoices sent back to the vendor | extract | Model extraction checked by grounding | Text invoices `10-discount`, `12-typo_vendor` labeled approve |
| 2 | 2026-11-07, span audit | Grounding took the first match of a number, so 23 of 38 quantity highlights pointed inside header fields | Evidence shown to a reviewer pointed at the wrong text | grounding | Ground each value within its own row | `evaluate` evidence-span check: fails on highlights that split tokens or overlap |
| 3 | 2026-11-07, critic experiment | Critic treated `high_value` as a ban rather than a review route, and cited proposal paths rather than fact paths | VP approvals vetoed for the wrong reason | critic | General prompt clarification | `data/external/critic-cases.json` via `python -m invoice_workflow.evaluate_critic`. Cases re-priced at catalog and re-recorded 2026-11-10: 18 of 18 bad-proposal trials caught (6 cases, 3 runs each), 0 false vetoes. Code-side, test 5 already blocks any claim that cites a false fact |
| 4 | 2026-11-08, expert review F13 | The same invoice resubmitted under a new number would be paid twice | Full invoice paid twice | rule | `possible_duplicate` | Test 1, row `resubmitted-under-new-numbers-with-the-total-written-differently` |
| 5 | 2026-11-08, expert review F14 | A euro invoice would be paid as dollars | Wrong amount and currency paid | rule | `currency_unknown` on a contrary sign | Test 3, rows `euro-signs-at-catalog-prices`, `canadian-dollar-signs` |
| 6 | 2026-11-08, expert review F15 | An empty or unreadable file had no clear outcome | An invoice silently dropped | load | `unreadable_document` | Test 8, row `document-has-no-readable-text` |
| 7 | 2026-11-08, expert review F16 | A revision arriving after the original was paid was marked duplicate, dropping its extra $4,050 | Underpayment nobody sees | rule | `revision_after_payment` hold | Test 1, row `revision-after-the-original-was-paid` |
| 8 | 2026-11-09, eval | The model read text invoice 02's `Amt:` total row as a line amount | A valid invoice held | extract | Prompt says total rows are invoice-level; row-amount parsing fixed | Text invoice `02-rush_delivery` labeled approve |
| 9 | 2026-11-09, test rewrite | On a layout the regex cannot read, the model built a total from other printed numbers. Every quote was real and the math closed | $12,500 paid on $12,000 due | extract | A total that differs from the printed total holds | Test 4, row `an-overread-quoting-numbers-printed-elsewhere` |
| 10 | 2026-11-09, test rewrite | The model returned the tax amount but dropped the printed rate, so the rate check never ran | A tax overcharge paid | extract | Keep the regex's printed rate when the model has none | Text invoice `20-tax_overcharge` labeled reject |
| 11 | 2026-11-10, generated set | A number followed by a bare comma (`quantity 3, unit price $250.00,`) was read as part of a longer number, so grounding rejected the numeric span | Clean invoices held | grounding | A comma or period joins a number only when a digit follows | Generated patterns 12, 15 (and every prose-line pattern) labeled approve |
| 12 | 2026-11-10, generated set | A VP stated-reason quote that joins a wrapped line was not found in the document | Justified price approvals held for human review | review | Any run of whitespace in a quote matches any other | Generated patterns 01, 03 labeled approve via VP |
| 13 | 2026-11-10, generated set | The VP agent's 20% limits used the variance rounded to 0.01%, so 20.004% counted as 20% | The agent approved alone beyond its authority (false approval) | review | Compare limits on the exact variance | Generated patterns 05, 07 labeled human review; test 6 rows `price-one-cent-beyond-20pct-above`, `price-one-cent-beyond-30pct-below` |
| 14 | 2026-11-10, label review | Each arithmetic step allowed a cent, so a printed subtotal one cent high plus $0.01 tax at 0% totalled two cents over the lines and still passed | Small overpayments that stack unnoticed | rule | The total must also be within a cent of one computed from the lines, the printed rate and shipping | Generated pattern 25 `stacked-cents` (TXT and PDF) labeled reject |
| 15 | 2026-11-10, blind holdout | A text revision numbered `HO-REV-31-R1`, saying "Revision of HO-REV-31", was not linked to its original: revisions are linked only by an identical number plus a structured `revision` field. The batch paid the $1,000 original and the $1,750 revision | Original paid on top of its replacement | reconcile | Never linked automatically: a revision under a new number that names a number we have from the same vendor, in the batch or the ledger, holds with that original for the AP clerk as `possible_revision`. Payment claims also retain named originals atomically, preventing payment in the reverse arrival order and while a revision payment is pending | Test 1 rows `revision-under-a-new-number-holds-with-its-original`, `revision-naming-a-paid-original-in-its-text`, `original-after-a-paid-renumbered-revision` and `original-after-a-paid-revision-naming-it-in-text`; test 2 renumbered-revision-first crash and overlapping-run cases |
| 16 | 2026-11-10, code review | The duplicate check compared totals as text, so `INV-7` at `1000.00` and a resubmission `INV-7B` at `1000` or `1000.0` both paid, in one batch and across runs. Row 4's fix only covered identical spellings | The same invoice paid twice | reconcile, ledger | Canonical two-decimal amount in the ledger and the duplicate check; ledger schema 4 | Test 1 row `resubmitted-under-new-numbers-with-the-total-written-differently` |
| 17 | 2026-11-10, code review | One document could crash the whole batch: `"shipping": null` in JSON raised in validation, and a price a cent above catalog ($250.01, 0.004%) was labelled below catalog from the rounded percentage, so the authority check crashed. Nothing was paid for anyone | Every invoice in the batch delayed | validate, review | JSON null reads as absent; price direction from the exact prices; any unexpected error in our code holds that invoice as `internal_error` for AP operations and the batch goes on | Test 1 row `null-charge-then-copy-in-another-format-and-vendor-spelling`; test 6 row `price-a-cent-above-rounds-to-zero-pct`; test 8 row `an-unexpected-error-in-our-code` |
| 18 | 2026-11-10, code review | Our read failures went back to the vendor: an empty PDF or non-UTF-8 text rejected as `parse_error`, and a Finder `.DS_Store` in the batch folder was rejected as an invoice | A valid invoice returned to the vendor | load, CLI | Unreadable text and PDF hold for a clerk as `unreadable_document`; the CLI skips dotfiles and non-invoice formats and logs them | Test 8 rows `pdf-is-empty`, `text-is-not-utf8`; test 10 (`test_the_cli_requires_a_chosen_endpoint_and_pays_what_it_approves_only_once`) runs with a `.DS_Store` in the folder |
| 19 | 2026-11-11, code review | A model that dropped a line from a clean invoice made the sums fail, and the invoice was rejected to the vendor as `total_mismatch`. The regex reading had all the lines and the check that compares the two readings fired, but it did not stop the reject | A correct invoice returned to the vendor for our misread | validate | When the model's reading and the regex reading disagree, arithmetic reasons are dropped and the invoice holds for a clerk, as it already did for an uncertain number | Test 8 row `model-drops-a-line-of-a-clean-invoice` |

## Adding one
1. Reproduce it as a labeled invoice, with its model responses recorded so replay is offline, or as a row in the test whose invariant it breaks. Watch it fail.
2. Fix it in the cheapest layer that holds, and watch the guard pass.
3. Add a row here naming the fix and the guard.

## Guards checked against the bugs they guard

Each bug below was reintroduced in production code; the named guard failed, and the code was restored byte for byte.

| Bug reintroduced | Guard that failed |
|---|---|
| JSON `null` charges read as values | Test 1 `null-charge-then-copy-in-another-format-and-vendor-spelling` |
| No `possible_duplicate` detection | Test 1 `resubmitted-under-new-numbers-with-the-total-written-differently` (paid twice) |
| Duplicate totals compared as text | The same test 1 row (paid twice) |
| Authority limits measured on the rounded percentage | Test 6 `price-one-cent-beyond-20pct-above` and `price-one-cent-beyond-30pct-below` (both paid) |
| No stop on an unresolved critic objection | Test 5 `true-facts/real-objection` |
| No stop on an invalid critic answer | Test 5 `true-facts/invented-objection` |
| No chosen-endpoint check in the CLI | Test 10 |
