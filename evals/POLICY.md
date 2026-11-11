# AP policy

This file explains every rule the system applies, why it exists in business
terms, and how each edge case in the data is handled. It is the reference for
reviewers, for whoever changes a rule next, and for the evaluation labels in
`evals/expected-*.json`.

## How decisions are weighed

Acme loses about **$2M a year** on manual invoice processing, with a **30% error
rate** and **5-day delays**. Every rule trades off two kinds of mistake:

- **Paying an invoice we should not have** (wrong amount, wrong version,
  duplicate, fraud). Cash leaves the company and recovery means chasing the
  vendor. This is the expensive mistake.
- **Holding an invoice we could have paid.** It costs a reviewer a few minutes
  and delays the vendor. Cheap per invoice, but holds are what create the
  5-day delay, so they have to be rare, routed to the right person, and quick
  to resolve.

So clean invoices pay with no human involved, anything uncertain holds, and every
hold names one owner and one next step.

## Who decides what

The brief's "$10K needs VP scrutiny" is one example of a broader question: what
kind of question does this invoice raise, and who has the right to answer it?

| The question | Who decides | Why them | Examples |
|---|---|---|---|
| Is the data wrong? (math, missing fields, invalid values) | Code: **reject**, vendor corrects | No judgment needed; it is the vendor's error | 1007, 1009, 1013 |
| Are we unsure what the facts are? (unknown item, stock, unknown currency, a number we could not read with confidence) | **AP clerk** | Needs a lookup or a call, not authority | 1002, 1005, 1008, 1014, 1016 |
| Facts are clear, but paying is a business judgment | **VP agent**, checked by a critic; **human review** above the agent's limits | Needs reasoning over a stated justification | over $10K, price differs from catalog, revised invoice |
| Is someone trying to steal? | **Internal audit**, never the VP | The approver must not be the one who clears fraud; "the VP approved this" is itself an attack pattern | 1003, 1012, injection, authority, bank-change texts |
| Did one of our own checks fail to run? (model outage, malformed answer, missing recording) | **AP operations** re-runs the step | A technical failure is not evidence about the invoice; a re-run that finds a signal goes to internal audit | none in the supplied set |

Code assigns every reason to exactly one owner from a single table, so changing
who handles what is a one-line change.

## Outcomes

| Outcome | Meaning | Who acts |
|---|---|---|
| `approve` | Eligible for payment; the pipeline pays it through the ledger | Nobody |
| `hold` | Needs a person before any payment | The owner on the result: AP clerk, AP operations, human review or internal audit |
| `reject` | The data is wrong; return to the vendor for a corrected invoice | AP clerk sends the message |
| `duplicate` | Another copy of an invoice already handled; never paid | Nobody |
| `superseded` | An original replaced by an approved revision; never paid | Nobody |

Precedence when several reasons apply: fraud signals first (always an
internal-audit hold), then rejects, then clerk holds, then VP triggers. The VP is
reached only when every open reason is a VP trigger.

## Decisions

### 1. Arithmetic and required fields: reject, never auto-correct
- **Rule:**
  - Missing vendor, invoice number, date, due date, items or total rejects.
  - Every arithmetic check allows plus or minus $0.01, inclusive: quantity
    times printed unit price against the line amount, summed lines against the
    subtotal, printed rate times taxable subtotal against the tax, and subtotal
    plus tax plus shipping against the total.
  - The printed total must also be within $0.01 of a total computed
    independently from quantities, printed unit prices, the stated tax rate
    and shipping, so tolerated cents cannot stack. At 0%, expected tax is zero:
    $0.01 passes, $0.02 does not. Without a printed rate, the stated tax amount
    is used.
  - A clearly read discrepancy beyond one cent is the vendor's to fix: reject.
    A mismatch is flagged, never recalculated, and extraction is never retried
    to "fix" it. Computed amounts are comparison values only; the invoice and
    its payment are never rewritten.
  - If any of the numbers involved was read with low confidence (decision 2),
    the invoice holds for a clerk instead of rejecting, because the error is
    then ours.
  - Catalog-price differences are separate exact-percentage checks (decisions
    5 and 6), with no cent-level exemption.
- **Why:** An invoice that does not add up is the vendor's error to correct.
  Silently fixing it either overpays or underpays and hides the mistake. A
  consistent cutoff avoids manual exceptions. But returning an invoice to a
  vendor because we misread it wastes their time.
- **Edge cases:**
  - An empty or whitespace-only document holds as `unreadable_document`.
  - INV-1013's total is $50.00 more than its own lines add to.
  - INV-1007 prints $15,525 where subtotal plus tax is $15,635.
  - INV-1009 has a negative quantity and no vendor or due date.
  - Synthetic text invoice 20 charges $150.00 tax where its stated 6% of
    $2,000.00 is $120.00, and its total agrees with the overcharged tax, so
    only the rate check catches it: reject as `tax_mismatch`. When the model's
    reading has no rate, the regex reading's printed rate is kept.
  - Generated pattern 11 prints $2,500.02 against $2,500.00 plus zero tax:
    `total_mismatch`, owner `vendor`.
  - Generated pattern 25 (`stacked-cents`) is one cent high on the subtotal
    plus $0.01 tax at 0%: two cents over in total, so it rejects.
- **Handled by:** code (`invoice_findings`); reasons such as `total_mismatch`;
  owner `vendor`, via AP clerk, next step "return to vendor".

### 2. Numbers read from a document must be trustworthy before anyone relies on them
- **Rule:** Structured files (JSON, CSV, XML) are parsed by code. For text and PDF
  the LLM extracts each value with the exact text it read, and code decides how
  far to trust it. A money or quantity field is **confident** only when:
  - code finds the quoted text in the document (grounded)
  - the regex cross-check agrees, where it produced a value
  - the invoice's arithmetic closes using the extracted numbers

  Clear source numbers that do not add up are an arithmetic error under
  decision 1, not extraction uncertainty. The clerk exception applies when the
  source numbers themselves are uncertain or conflicting.

  A value that appears in several places (INV-1001's $5,000 subtotal and total)
  is confident when every occurrence carries the same value. A total printed
  twice with different values is not confident, whichever one was read. A
  model's own "confidence score" is never used.

  If any money or quantity field is not confident, or the model's reading and
  the regex reading disagree on the total or the lines, the invoice holds
  (`extraction_uncertain`, `extraction_disagreement`) for a clerk.
  Low-confidence numbers never reach the VP agent and never get paid.

  An image-only invoice has no text to ground against: it holds for an AP
  clerk as `unreadable_document`.
- **Why:** Every later check (stock, thresholds, price variance, VP reasoning) is
  only as good as the numbers it runs on. A VP agent reasoning over a misread
  total is confidently wrong.
- **Edge cases:** Synthetic text invoice 19 prints "Total: $1,000.00" and
  "Amount due: $1,250.00": held as `extraction_uncertain`, never rejected or
  paid. Synthetic text invoice 09 had a due date the model made up; grounding
  caught it and held it. OCR letter-O-for-zero is corrected only inside
  numbers and recorded. Item and vendor names are never fuzzy-matched.
- **Handled by:** code after extraction (`invoice_workflow/extraction.py`,
  `invoice_workflow/evidence.py`); reasons `ungrounded_extraction`,
  `extraction_disagreement`, `extraction_uncertain`.

### 3. Stock: hold unknown, zero-stock and over-stock items
- **Rule:** Each item must exist in inventory with enough stock. Repeated lines
  for the same item are added together first. Stock is a snapshot; it is not
  used up across unrelated invoices.
- **Why:** Paying for goods we cannot receive or never ordered is a direct loss.
  These are clerk holds, not rejects, because the fix can be on our side
  (inventory out of date, new item).
- **Edge cases:** INV-1002 asks for 20 GadgetX with 5 in stock. INV-1003 bills
  FakeItem (0 stock). INV-1008 and INV-1016 bill items not in inventory.
  INV-1010 bills WidgetA on two lines (8 + 4 = 12, within 15).
- **Handled by:** code against SQLite; reasons `stock_exceeded`, `out_of_stock`,
  `unknown_item`.

### 4. VP review: three triggers, one bounded loop

Every unresolved business judgment goes to **human review** (`human_review`).
The automated stage is the VP agent. Fraud signals have their own
internal-audit route.

- **Rule:** Code sends an invoice to the VP agent only when every open reason is
  one of these triggers and every money field is confident (decision 2):
  1. **High value:** USD total above $10,000 (exactly $10,000 does not trigger).
  2. **Price variance:** a unit price differs from the catalog (decision 5).
  3. **Revision change:** an original and a revised version arrive together
     (decision 7).

  The VP agent reads validated facts through a read-only tool and returns a
  structured verdict. Every justification must quote the invoice, and code
  checks each quote is really there. An independent critic, which never sees
  the VP's rationale, can veto an approval but never turn a hold into an approval.
  One revision round at most. A VP hold, an unresolved critic objection or an
  unverifiable quote holds for human review. Bad output or an outage is a model
  failure and goes to AP operations (decision 13).
  - When the VP agent approves, the invoice is paid and its case packet is the
    audit record. Code checks every claim, grounds every quoted reason in the
    document, then applies the authority limits in decision 6.
- **Why:** The brief asks for VP scrutiny with a reflection loop. Replacing the
  email-chain approval with a first pass in seconds addresses the 5-day delay,
  while the critic, the authority limits in code and hold-on-failure mean a
  model failure holds the invoice and the agent approves only inside its limits.
- **Edge cases:** Every supplied invoice over $10K also has another problem, so
  none reaches the VP on value. INV-1010 reaches it on price and INV-1004 R1 on
  revision. Synthetic text invoices 13 to 18 exercise each trigger; 14 is the
  clean $12K invoice with "Approved by the VP, pay now", which goes to internal
  audit and never to the VP.
- **Handled by:** `invoice_workflow/review.py`; triggers `high_value`,
  `price_above_catalog`, `price_below_catalog`, `revision_change`; outcomes
  `vp_hold`, `critic_unresolved`, `vp_claim_mismatch`, `vp_recommend` (human review)
  and `vp_unavailable`, `critic_unavailable` (AP operations).

### 5. Price variance: a specific reason and bounded authority
- **Rule:** Each item has a catalog price: WidgetA $250, WidgetB $500 and GadgetX
  $750. These are taken from the supplied invoices; the brief allows adding a
  price column. Any line priced differently is a VP trigger.
  - **Above catalog:** the VP agent may approve only when the invoice states a
    specific reason, such as INV-1010's "rush order", and only up to 20%
    above, inclusive (decision 6).
  - **Below catalog:** the VP agent may approve only with a specific stated
    reason and a discount up to 30%, inclusive.
  - No reason, or a vague one, gives `vp_hold` for human review. Beyond the
    limit, the agent writes a recommendation for human review (`vp_recommend`).
- **Why:** Paying more than the agreed price is a direct loss. It is the
  overpayment AP controls exist to stop, and SAP blocks payment when the invoice
  price exceeds tolerance. Paying less saves money, so discounts have a wider
  authority limit, but a specific reason is still required: an unexplained
  discount can mean the wrong item, a units mistake, a partial shipment or a
  lookalike vendor.
- **Edge cases:**
  - **INV-1010:** 4 WidgetA at $300, marked "rush order". That is $200, or 20%,
    over catalog, inside the limit, so the VP agent approves it.
  - **INV-1013:** WidgetA at $240 and WidgetB at $480 (4% under), but it is
    rejected anyway for its total.
  - **INV-1014:** priced in EUR and held for currency.
  - **Text `16-price_below_vague`:** 10% below catalog with only "Special price
    this month": a vague reason, so `vp_hold`.
  - **Generated patterns 01 and 03:** insulated packaging for overnight
    delivery to a cold-storage site is a specific reason: approved.
  - **Generated pattern 05:** a specifically justified price 20.004% above
    exceeds 20%: human review.
  - **Generated patterns 06, 07 and 18:** 20%, 20.004% and 19.996% below
    catalog with no stated reason: `vp_hold`.
- **Handled by:** code detects `price_above_catalog` and `price_below_catalog`;
  the VP agent judges; the critic is told to attack the justification for
  above-catalog lines.

### 6. How much the VP agent may decide alone
- **Rule:** The agent's authority is a table in code. Inside a limit it approves
  (subject to the critic). Outside a limit it writes a recommendation and the
  invoice holds for human review (`vp_recommend`). Limits compare exact
  percentages, without rounding, so rounding never widens authority.

  | Trigger | Agent may approve alone | Otherwise |
  |---|---|---|
  | High value | Total up to $25,000 | Human review |
  | Price above catalog | Up to 20% above (inclusive), only with a specific stated reason; no reason gives `vp_hold` | Human review |
  | Price below catalog | Up to 30% below (inclusive), only with a specific stated reason; no or vague reason gives `vp_hold` | Human review |
  | Revision change | Increase up to $5,000, with a stated reason | Human review |

- **Why:** The brief asks for a simulated VP, so the agent decides. But the model
  does not get trust because it is "the VP": each decision type earns autonomy
  with measured evidence. Widening a limit means re-running the evaluation
  (false approvals, the critic's catch rate on seeded bad proposals). In
  production, the same table is the delegation-of-authority matrix.
- **Edge cases:** With the brief's inventory, the largest invoice that passes
  the stock check is $12,500, so a unit test raises stock to exercise the
  $25,000 limit. Synthetic text invoice 18 (GadgetX 30% over catalog with a
  named surcharge) exercises a limit end to end. Tests place a price one cent
  beyond each price limit and expect human review.

### 7. Revisions: checked, then judged
- **Rule:** When an original and a revised invoice arrive together, code first
  checks the hard facts:
  - same vendor
  - no change to payment details
  - every original line unchanged
  - the revision passes all other checks
  - the original is not already paid

  If any check fails, both hold for a clerk. If all pass, the VP agent judges
  whether the stated reason explains the change, within decision 6's limits.
  On approval the revision is paid and the original becomes `superseded`. A
  revision that arrives alone is judged on its own merits. A revision for an
  invoice already paid holds: a person decides whether to pay the difference.
- **Why:** Most revisions are legitimate, so holding every one recreates the
  delay. But "please pay this updated invoice instead" is one of the most common
  payment-fraud patterns, so a revision is never trusted just because it is newer.
- **Edge cases:**
  - A changed revision arriving after payment holds across runs.
  - INV-1004 R1 adds 5 GadgetX ($4,050 with tax) to the original $1,890, citing
    a PO amendment.
  - A revision under a **new** invoice number (`INV-7-R1`, or text saying
    "revision of INV-7") is never linked automatically: when it names a number
    we already have from the same vendor, in the batch or in the ledger, it and
    that original hold for the AP clerk as `possible_revision`.
  - When such a revision pays first, its payment claim reserves the named
    original numbers, so a later original, or another revision naming the same
    original, holds as `possible_revision`. The claim and the reservations are
    committed together before the bank call, and stay when the bank call fails.
- **Handled by:** batch reconciliation in code (`invoice_workflow/reconcile.py`).
  "Same vendor" also covers a revision whose invoice number belongs to another
  vendor: it holds. "No change to payment details" compares structured bank
  fields and requires no fraud signal on either version. Failed checks hold both
  as `revision_conflict` with the failed checks listed; passing checks add
  `revision_change` to both, and the VP judges the revision. The payment ledger
  stops a second payment for the same vendor and invoice number, and a changed
  amount under a paid number holds as `revision_after_payment`.

### 8. Duplicates: pay once, and only when they really are the same invoice
- **Rule:** Copies in different formats count as duplicates only when vendor,
  invoice number, dates, items, prices and totals all match. One copy is
  processed and the others are `duplicate`. Same vendor and number with
  different content is a conflict (decision 7 if one is marked as a revision,
  otherwise a clerk hold). The same vendor, date and total under a new invoice
  number holds as `possible_duplicate`; amounts are compared as two-decimal
  values, so `1000` and `1000.00` are the same total.
- **Why:** Duplicate payment is a classic AP loss. But wrongly marking a
  different invoice as a duplicate leaves a real bill unpaid.
- **Edge cases:** INV-1011 PDF and text ($3,000 each): one is paid. INV-1012 and
  INV-1013 also arrive in two formats.
- **Handled by:** batch reconciliation (`economic_content`) and the ledger.

### 9. Fraud and injection signals: internal audit, surfaced first
- **Rule:**
  - **What triggers it:** payment pressure ("URGENT", "pay immediately"),
    instructions aimed at the system, claimed authority, and bank-detail changes.
  - **A vendor rename alone is a note, not a hold.** When an invoice says the
    vendor was renamed ("formerly", "now part of"), no bank or payment details
    changed, and every other check passes, it is approved. The rename goes on
    the result as a note: "update the vendor record". A rename together with a
    bank-detail change goes to internal audit.
  - **Detection:** rules catch the known phrasings. The LLM catches paraphrases,
    but can only add flags, and its quotes must be found in the document.
  - **Precedence:** the hold goes to internal audit, ahead of every other reason,
    and never to the VP.
  - **Where it appears:** on the result, with owner internal audit and the
    quoted evidence; first in the command-line summary; first on the review page.
  - **Bank-detail change:** the next step is "call the vendor on the number
    already on file", never the number in the invoice.
- **Why:** These are the highest-loss cases (INV-1003 is $100,000) and the
  cheapest to stop before payment. Separating the clearer from the approver is
  standard segregation of duties.
- **Edge cases:** INV-1003 (Fraudster LLC, urgency). INV-1012 ("QuickShip
  Distributers (formerly FastShip Ltd.)"): a rename only, with no bank change,
  correct math and stock, and a PO reference, so it is approved with a
  vendor-record note. One copy is paid and the other is a duplicate, as with
  INV-1011. The synthetic text invoices include injection, authority,
  bank-change and urgency cases.
- **Handled by:** `invoice_workflow/screen.py`; reasons `urgency_pressure`,
  `suspicious_instructions`, `payment_detail_change`. A rename is a `notes`
  entry on the result (`validation.py`, `rename_notes`), not a reason.

### 10. Currency: stated or clearly indicated
- **Rule:**
  - An explicit currency is used as stated.
  - Non-USD (INV-1014, EUR) is held for a clerk: there is no FX conversion.
  - With no stated currency, USD is accepted only when the document indicates it:
    a `$` sign, a US address, or every unit price matching the USD catalog.
    A contrary currency symbol or code anywhere prevents that inference.
  - Otherwise the invoice holds as `currency_unknown`.
  - The pipeline checks this, not the reviewer, and records which evidence it
    used as `currency_evidence`: `stated`, `dollar_sign`, `us_address`,
    `catalog_prices` or `none`. In a text or PDF document a currency counts as
    stated only when its code appears in the text.
- **Why:** Paying a EUR or CAD amount as USD silently misprices the invoice. But
  holding every invoice that omits a currency field would hold clean invoices
  whose prices make the currency obvious.
- **Edge cases:** INV-1006 and INV-1015 (CSV) state no currency and no address,
  but every price matches the catalog: approve. INV-1014 states EUR: hold.

### 11. Dates
- **Rule:** Missing or invalid due date rejects. Relative dates ("yesterday")
  are not resolved from the clock.
- **Why:** Without a due date the payment cannot be scheduled; guessing one
  risks late fees or paying early.
- **Edge cases:** INV-1003 has an invalid due date; INV-1009 has none.

### 12. Payment happens at most once
- **Rule:** Before calling `mock_payment`, the pipeline writes a ledger row
  keyed by vendor and invoice number, marked `pending`. If the row exists, it
  does not pay. After a successful call, the row is set to `paid`. A run that
  stops in between leaves `pending`, which holds as `payment_pending` for AP
  operations instead of retrying.
- **Why:** Re-running a batch must never pay anyone twice.
- **Handled by:** `invoice_workflow/payment.py` and `invoice_workflow/database.py`.

### 13. Any model failure holds, and AP operations re-runs it
- **Rule:**
  - Grok is the model. When a DeepSeek key is configured, a Grok connection
    failure, timeout, rate limit or server error retries the same call on
    DeepSeek before the failure handling below. Authentication errors,
    malformed answers, grounding failures and model holds never switch
    providers. Both providers face the same validation and authority checks.
  - A malformed answer, timeout, outage or missing recording holds the invoice
    with reason `model_failure` and the failed step named. It never crashes the
    batch and never pays.
  - The owner is **AP operations**, and the next step is "re-run the failed step".
  - **Fraud-screen failure:** the invoice holds even if validation would
    otherwise reject it, so it is not returned to the vendor before screening
    finishes. If the re-run finds a signal, it goes to internal audit (decision 9).
    If it finds nothing, the invoice continues through the normal flow.
- **Why:**
  - Model availability must never decide whether money moves.
  - A technical failure is not evidence of fraud. Sending outages to internal
    audit would fill their queue with noise and teach them to ignore it.
  - Holding instead of rejecting means we never return an invoice without first
    checking it for fraud signals.
- **Edge cases:** an outage during a batch holds every affected invoice under one
  owner, so it can be re-run in one action. A bug in our own code is treated the
  same way: that invoice holds as `internal_error` for AP operations, the error
  is logged with its traceback, and the rest of the batch goes on.
- **Handled by:** `invoice_workflow/routing.py` routing table and
  `invoice_workflow/llm.py`. The fraud screen's failure is `model_failure`; the
  other steps keep their own codes (`extraction_failed`, `recording_missing`,
  `vp_unavailable`, `critic_unavailable`). All are owned by AP operations and
  name the failed step in `failed_step`.

### 14. Every exception leaves with a case packet
- **Rule:**
  - **Which invoices:** every invoice that is not paid touchlessly gets a packet:
    - holds and rejects
    - duplicates and superseded originals
    - invoices approved through the VP agent, as their audit record
  - **What is in it:**
    - decision and owning team (vendor via AP clerk, AP clerk, AP operations,
      human review or internal audit)
    - each rule that fired, in plain words, with its decision number here
    - quoted evidence and where it is in the document
    - the numbers checked (printed versus computed)
    - the next step
    - the VP agent's reasoning and the critic's notes, when the VP ran
  - **Who builds it:** code builds it from the routing table and the evidence
    records. No model writes or edits it.
  - **Where it appears:** on each result in the pipeline output, grouped by owner
    in the command-line summary (internal audit first), and on the review page,
    `output/report.html`.
  - Clean invoices paid touchlessly get no packet; their result already shows
    every check passed.
- **Why:** The team receiving an exception can act without re-investigating.
  Code-built packets always match the decision; a model-written summary could
  state a reason the rule did not use. Sending every invoice through the VP agent
  would add cost and delay, expose the model to injected text it does not need
  to read, and empty the VP step of meaning.

## What is deterministic and what is not

| Step | Code or LLM | Notes |
|---|---|---|
| Parse JSON, CSV, XML | Code | |
| Read TXT and PDF | LLM | Returns values plus the quotes it read |
| Grounding, cross-check, confidence gate | Code | Decision 2 |
| Arithmetic, stock, currency, catalog prices, duplicates, revision hard checks | Code | Exact decimals and SQL |
| Fraud screen | Code rules, then LLM | LLM can only add flags; quotes verified |
| Who owns each reason; whether the VP is reached | Code | One table |
| VP verdict and justification | LLM | Quotes verified by code |
| Critic | LLM | Veto only |
| Agent authority limits | Code | Decision 6 table |
| Final decision, payment, ledger | Code | Models hold no tool that pays or writes |
| Case packet and explanations | Code | Decision 14; carries the VP rationale and critic notes when the VP ran |

## Labels

Label reviews against this policy are recorded in `label-review.json`. The
supplied documents' approve labels:

| Invoice | Label | Rule |
|---|---|---|
| INV-1001 | approve | A clean invoice pays without review. |
| INV-1004, R1 | approve alone; together, R1 approved through the VP and the original superseded | Decision 7 |
| INV-1006 | approve | Decision 10: currency inferred from catalog prices. |
| INV-1010 | approve through VP review | Decision 5: the rush-order reason justifies the $300 price. |
| INV-1011 PDF, text | approve; duplicate in batch | Decision 8 |
| INV-1012 PDF, text | approve; duplicate in batch | Decision 9: a rename with no bank change. |
| INV-1015 | approve | Decision 10 |

### Label file format

The JSON labels hold each input's SHA-256, the expected decision and reasons
(the minimum required findings, not an exhaustive list), the total and
aggregated quantities, alone and in a batch. Optional fields: `route` and
`batch_route` (`"vp"`: an approval that skipped the VP counts as a mismatch),
`owner` and `currency_evidence`. The scorer also fails any result that is not
paid touchlessly but has no case packet with an owner and a next step.
