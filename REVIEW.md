# Parent review

Scope: Sonnet commit 41bf8d20e85aa7c360d223390ea29217a27bd326 versus
6c6e98f, including service state transitions, HTTP guards, views and tests.
Reviewer: Codex. Original tests and deterministic corpus eval independently run.

Three verified findings were fixed before publication:

1. High: an outstanding correction request did not block an already-approved
   case from payment. Request lifecycle now blocks/invalidate approvals and
   requires audited resolution plus fresh approval; reassignment preserves it.
2. High: changed/missing original file bytes did not affect payment eligibility.
   Current source availability now participates in derived blockers/evidence.
3. Medium: numeric overflow was caught by validation but raised again in the
   display calculation, breaking requests or the whole inbox for an ingested
   document. Numeric edit bounds and safe error rendering now contain this.

No further actionable findings survived this review. Evidence: six new backend
regression cases failed before fixes; final suite 110 passed, including two new
real HTTP flows. Deterministic corpus working-label eval and whitespace checks
pass. No additional browser pass, Python 3.11 execution or CI run was performed.

Residual limits: local actor selection is not authentication; payments are
SQLite mock rows; labels and routing need human review. Source checks do not
provide an atomic filesystem/SQLite snapshot against external writers.
No LLM pipeline, production payments, deployment or observability integration
is included. See WORKBENCH.md for full implementation and evidence boundaries.
