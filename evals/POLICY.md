# Main-v1 working labels

Source: upstream main commit 2f150152b962ccd24b35e515e56b04f673d410bc,
README.md and all 20 documents in data/invoices/. These are Codex-authored
working expectations, not an official answer key or independent human labels.
They are fixed before implementing the core. A reviewer should confirm the
assumptions before treating the dataset as a benchmark. No v2 rules are imported.

README requirements: SQLite stock checks, unknown-item flags, ingestion of
messy formats, VP scrutiny for amounts over $10K. The remaining choices below
are explicit prototype assumptions, since main does not define a full AP policy.

- `approve` means eligible for subsequent simulated VP/payment stages. It is
  not evidence of VP sign-off or an actual payment. `hold` requires human review;
  `reject` requires corrected data. Reject takes precedence over hold.
- Reject missing vendor/id/date/due date/items/total, invalid dates, nonpositive
  quantities/prices/totals, and inconsistent money (one cent rounding tolerance).
  Relative due dates such as 'yesterday' are not resolved from the runtime clock.
- Hold unknown, zero-stock, and over-stock items. Aggregate repeated item lines
  within each invoice. Stock is a validation snapshot, not a consumable budget
  across unrelated invoices (no goods-receipt workflow was supplied).
- Hold USD totals strictly greater than 10000; the boundary itself is eligible.
- Missing currency defaults to USD for this fixture set. Explicit EUR is held;
  no FX conversion, vendor registry, or price master is invented.
- Keep vendor identity conservative: normalize case/punctuation and common legal
  suffixes; never fuzzy-merge names or silently accept a 'formerly' rename.
- Numeric OCR O -> 0 is allowed only in a numeric token with another digit,
  including the year in 2O26. Record every correction. No fuzzy item matching:
  spacing/case and parenthetical line descriptions may be normalized.
- Standalone labels assess a file in isolation. Batch labels additionally hold
  both original INV-1004 and its different R1 until a human selects a version.
  Equivalent multi-format documents use the lexically first filename as the
  representative; later copies are `duplicate` (never payable). Compare economic
  content, dates and revision, not byte equality. Conflicting content always holds.
- The local SQLite mock-payment ledger uniquely keys normalized vendor + invoice
  number across repeats and revisions. It records a mock payment only for an
  approved, reconciled batch. No external banking call exists.

The JSON labels include input SHA-256, expected decision/reasons, total, and
aggregated quantities. Expected reasons are minimum required findings, not an
exhaustive diagnostic list. Both observation layers can later consume these
same labels/results; neither determines the expected answer.

## Label correction log

- 2026-11-06: initial INV-1007 label was hold for stock/high value. During
  arithmetic verification, independently adding the printed subtotal 14750 and
  tax 885 gives 15635, not the printed total 15525. Corrected standalone/batch
  to reject and added total_mismatch. Source bytes and original total preserved.
