# DeepSeek Flash: recorded-response evaluation

- Date: 2026-11-06 (America/Lima)
- Model requested: `deepseek-flash`, base URL `https://api.deepseek.com`
- Datasets: 20 supplied documents; 12 handwritten text invoices in
  `evals/text-invoices/`. Scores use the labels in `evals/`.
- Method: live extraction and screening calls were recorded, then the pipeline
  replayed those responses after fixes to null handling and extraction logic.
  These results are **replay after fixes**.
  Structured files use deterministic parsing. Later versions use a different
  prompt and new recordings.

| Dataset | Decision matches | False approvals | Extraction fields |
| --- | ---: | ---: | --- |
| Supplied, batch | 20/20 | 0 | Each field group 19/20 |
| Handwritten text | 12/12 | 0 | Each field group 12/12 |

Field groups are vendor, invoice number, invoice date, due date, currency, total,
and aggregated item quantities. Supplied invoice 1008 failed model extraction
twice and was held, accounting for each missing field. The four adversarial text
invoices were held; the normal, urgent-delivery, and discount controls were
approved. Text invoices 10 and 12 changed from regex-only rejects to grounded
 approvals. Fraud signals take precedence over rejection and route to audit;
 the label for invoice 1003 was updated accordingly.

The output artifacts are [supplied.jsonl](supplied.jsonl) and
[text.jsonl](text.jsonl). They contain the pipeline's results and evidence for
these synthetic invoices.

| Dataset | Input tokens | Output tokens | Summed call latency | Cost |
| --- | ---: | ---: | ---: | ---: |
| Supplied | 7,636 | 31,309 | 218.94 s | $0.03986 |
| Handwritten text | 6,642 | 20,454 | 146.84 s | $0.02654 |

Usage includes extraction and screening calls, including retries. Summed latency
is not wall-clock duration. Cost is calculated at peak cache-miss prices ($0.30 per million input tokens and $1.20 per million
output tokens).
[DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/).

The supplied run is batch-reconciled; its decision counts above use the
`batch_decision` labels.
