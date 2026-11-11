# Generated boundary set, 2026-11-10

An authored challenge set covering monetary boundaries and document formats.
Scores use the labels frozen before this run.

## Construction

The frozen manifest, generator, 40 TXT/text-PDF documents, four image-only PDFs
and 44 labels were committed before the first DeepSeek request. The
manifest is the oracle: literal facts and expected outcomes cite POLICY decision
numbers. The renderer uses Decimal for every monetary calculation and imports
no production invoice code. `fpdf2` is in dev dependencies; dependencies are
locked. All printed subtotals, tax and unit prices are labeled. Conflicting
totals intentionally have no single expected total; scans have no extraction
expectations because this pipeline has no OCR.

Two full generation runs produced identical hashes for all 45 outputs
(`deterministic-hashes.json`). Every text-PDF source row survives text extraction;
all four scan PDFs have empty extracted text. All 24 PDFs were rendered with
pypdfium2 and visually inspected in a contact sheet. `provenance.json` freezes
baseline, POLICY, prompt implementation, manifest and dependency hashes.

The 20 economic patterns have two renderings each. Except for the threshold
pairs, siblings use different vendor/number identities and quantities, so they
have distinct totals and no accidental batch duplicates. Patterns 08/09
intentionally preserve identity and economics across formats (POLICY 8): the
lexically first PDF is processed, and the TXT is labeled duplicate in batch.
26/44 standalone documents are payable controls.

The frozen labels use exact 20% price boundaries: $300.01 and $199.99 differ
from catalog by 20.004% and require human review. The two-cent arithmetic case
is labeled clerk hold; the tax-overcharge case is labeled reject. The
$10,000.01 case uses a one-cent tax line at 0% within the arithmetic tolerance.

## Recording

`record.py` runs this set alone through the existing evaluator API, replaying
any existing request before making a new request. It uses the existing
`llm_model` recorder, including read-only VP tool exchanges and VP/critic audit
records. The SDK has `max_retries=0`; a wrapper counts actual provider requests
(including both VP turns) and terminates before request 301. `requests.json`
contains per-request token usage and status. No request is sent solely to repeat
an identical prompt. Standalone and batch share recordings where keys match.

Model: `deepseek-flash`, provider `https://api.deepseek.com`, temperature 0.
Extraction and VP use non-thinking mode; screen/critic keep their existing
configuration. The API key is in the process environment only;
it is never written to any file or included in the request ledger. Existing
labels/recordings are preserved. No production payments are used (the existing
local mock runs in fresh in-memory databases).

## Requests, cost and replay results

- **102 actual provider requests**, all successful, zero retries/errors; below the 300 cap.
- 45,727 input tokens (16,640 cache-hit), 15,799 output tokens.
- **$0.032677 token-based cost estimate**, valuing all input at peak cache-miss
  $0.30/M and output at $1.20/M. Using reported cache hits at peak $0.006/M gives $0.027785. [Official rates checked during this run](https://api-docs.deepseek.com/quick_start/pricing/).
- The live set report and offline generated report are byte-equivalent as JSON
  objects. No post-prediction label or document changes were made.

| Set | Standalone matched | Batch matched | False approvals standalone / batch |
|---|---:|---:|---:|
| supplied | 20/20 | 20/20 | 0 / 0 |
| text | 20/20 | 20/20 | 0 / 0 |
| generated | 24/44 | 27/44 | 2 / 2 |

`uv run python -m invoice_workflow.evaluate`: **exit 1**, intentionally preserved findings.
`uv run pytest -q`: **45 passed**, 13 existing deprecation warnings.
Generated evidence: 168 selected line-field spans checked, zero span issues.
Nonpayable approvals: supplied **0/10**, text **0/14**, generated **2/18**.

## Every generated mismatch

Each row below covers both the `.txt` and `.pdf` named by the pattern prefix,
unless stated. Full differences are in `offline-results.json`, observed owners
and reason codes in `observed.json`; live report is in `live-results.json`.

| Prefix | Expected | Actual | Read of the mismatch |
|---|---|---|---|
| 01-above-299.99-reason | approve via VP | hold, human_review, vp_claim_mismatch | Pipeline finding: the VP approves in prose but supplies no usable stated-reason quote. The note wraps across source lines; quote grounding must preserve that evidence. |
| 03-above-300.00-reason | approve via VP | hold, ap_clerk, ungrounded_extraction/extraction_uncertain | Pipeline finding: comma-delimited numeric tokens are rejected by evidence grounding despite unambiguous quantity and unit price. |
| 05-above-300.01-reason | hold, vp_recommend | hold, vp_claim_mismatch; missing vp_recommend | Pipeline quote finding plus authority comparison: 20.004% was rounded to 20.00% by the code at the time; exact comparison fixed it (FAILURES 13). No false approval here. |
| 06-below-200.00 | approve via VP | hold, ap_clerk, ungrounded_extraction/extraction_uncertain | Pipeline finding: comma-delimited key-value grounding, same family as 03. |
| 07-below-199.99 | hold, human_review, vp_recommend | approve, vp_agent | Authority comparison: exact 20.004% discount is outside literal POLICY 6, but rounded code treats it as 20.00%. These are the two false approvals against frozen labels, in each mode. |
| 09-vp-over-10000 | approve standalone; PDF approve/TXT duplicate in batch | clerk hold standalone and PDF batch; TXT duplicate batch matches | Pipeline finding: comma-delimited key-value grounding stops the otherwise valid threshold invoice. |
| 11-rounding-two-cents | hold, ap_clerk, extraction_uncertain | reject, vendor, total_mismatch | The frozen label requires a clerk hold; the pipeline rejected the two-cent discrepancy as an arithmetic error. |
| 12-tax-rate-correct | approve | hold, ap_clerk, ungrounded_extraction/extraction_uncertain | Pipeline finding: comma-delimited key-value grounding. |
| 15-catalog-key-value | approve | hold, ap_clerk, ungrounded_extraction/extraction_uncertain | Pipeline finding: comma-delimited key-value grounding. |
| 18-discount-200.01 | approve via VP | hold, ap_clerk, ungrounded_extraction/extraction_uncertain | Pipeline finding: comma-delimited key-value grounding. |

20 standalone mismatching files; 17 batch mismatching files. Pattern 05's
missing reason is standalone-only scoring, and 09 TXT is correctly duplicate in
batch. No field-value mismatches, unexpected results or missing recordings.
No mismatches were fixed, and no labels were changed after seeing predictions.
Pattern 19 covers two catalog-priced item rows with varied quantities in the
same row order.
