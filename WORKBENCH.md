# Invoice review workbench

A local, server-rendered review UI over the existing deterministic core. It is a
prototype for demonstrating the exception workflow, **not** production software:
the "demo actor" selector is not authentication and payments are local mock
ledger rows only. No model calls, secrets, external payment APIs, Phoenix/Panoptes
wiring or network services were added; `pyproject.toml`/`uv.lock` are unchanged
(stdlib `http.server` + `sqlite3`, plus the `pypdf` the core already used).

## Start

```sh
uv sync --frozen
uv run python -m invoices.workbench            # http://127.0.0.1:8765/
# options: --data-dir data/invoices --db output/workbench.db --port 8765
```

`output/workbench.db` is created on first run (gitignored). It holds inventory,
the mock-payment ledger and all review state, so work survives reloads and
restarts. Starting again re-reads the data directory without resetting anything:
known files are skipped, new files are added (a file joining an existing case
voids that case's sign-offs), and a file whose bytes changed on disk is reported
and its evidence withheld. Binds to loopback only. Choose a demo actor (top right)
to act; the default is a read-only Observer.

## What it does

**Exception inbox (`/`)**: all 20 main documents seeded through `invoices.core`
become 16 cases (INV-1004's two JSONs, and the PDF/TXT or JSON/PDF pairs of
INV-1011/1012/1013, are grouped; extra copies are never payable). Columns:
invoice, vendor, amount and currency, status, explicit blockers (red) and gates
(amber), responsible team or person, waiting age. Filters: status, responsible,
free-text search.

**Evidence and resolution (`/case/<id>`)**:
- Original document beside parsed fields: PDF in a local viewer; text, JSON, CSV,
  XML shown escaped and served as `text/plain` with `nosniff`. Documents are
  looked up through rows recorded at ingestion, SHA-256 re-checked on every
  read; no client path is ever opened.
- Parsed vs corrected values, line calculations (qty x price vs stated), subtotal/tax/total
  checks, aggregated inventory requested vs stock, and a plain-language reason per code.
- INV-1004: side-by-side original vs R1 (revision, subtotal, tax, total, added GadgetX line,
  core result), then an explicit "mark as authoritative" action. The other version becomes
  `superseded`; validation runs on the selected one.
- Correct extracted fields: file bytes and the parsed original are immutable; each change is
  stored as an append-only before/after row with actor, role, reason and time. Saving
  revalidates at once (core `evaluate` on the corrected invoice). `notes` is deliberately not
  editable so a vendor-change flag cannot be edited away.
- Request correction, assign, escalate, note: all need a reason and are logged. They do not
  clear any blocker; only corrected data or a chosen version does. There is no global
  force-approve.

**Approval and history (same page)**: four gates shown separately: (1) core eligibility,
(2) AP Manager sign-off, (3) VP Finance sign-off (only when core reports `high_value`, USD > 10,000;
Manager cannot sign it, only VP can), (4) mock payment. Approval needs no blockers, a selected
version, and a reason; saved with actor, role, time and evidence hash. History lists every event
(ingest, edits with before/after, selection, assignment, approvals, invalidations, refused and
recorded payments).

## Safety rules implemented (server-side)

- Evidence hash = policy version + document hashes/roles + every (corrected) invoice + validation
  result + inventory levels for the invoice's items. A sign-off only counts while the hash matches,
  so an edit, version change, added document or inventory change voids it (and logs an
  `approval_invalidated` event).
- Optimistic concurrency: every form carries the case revision and evidence hash; a stale
  submission gets HTTP 409 and nothing changes (two tabs cannot overwrite or approve stale data).
  Comments do not need a revision.
- Mock payment: re-checks everything inside one `BEGIN IMMEDIATE` transaction (no blockers, version
  chosen, not rejected, AP and, if required, VP sign-off valid for current evidence). It writes the
  core's unique `mock_payments(vendor, invoice_number)` row and a `case_payments` row. A repeat, a
  replay of a stale form, a concurrent attempt, or an identity already in the ledger (including from
  the CLI) is refused and the refusal is logged. Disabled buttons are only a hint.
- All state changes are POST; foreign `Origin` (including `null`) and foreign `Host` are refused;
  SameSite=Strict cookie; CSP allows no script at all; every dynamic value is HTML-escaped;
  history tables are append-only via SQLite triggers; an unknown cookie falls back to read-only.

## Validation evidence (2026-11-06, local Python 3.14.2, macOS)

| Command | Result |
|---|---|
| `uv run pytest -q` | **102 passed** in 2.13s (39 baseline `test_core.py`, 45 `test_workbench.py`, 18 `test_workbench_http.py`) |
| `uv run python -m invoices.evaluate` | exit 0; standalone 20/20 and batch 20/20 matched, 0 false approvals, 0 mismatches |

Core, `evals/`, `data/`, `pyproject.toml`, `uv.lock` unchanged (`git diff` empty for them).

New tests use real SQLite files and real fixtures (no mocks): INV-1004 selection and re-selection;
correction -> revalidation with before/after history; assignment/escalation/request persisted across a
new `Workbench` instance; approval invalidation by edit, by version change, by added document, by
inventory change (including a change that leaves core output identical); stale revision and stale
evidence writes; 12 unresolved cases (including the INV-1004 version conflict) refused for approval and payment with a
logged refusal and zero ledger rows; rejected cases blocked until reopened; high-value flow
(clerk/observer forbidden, Manager cannot VP-sign, VP cannot AP-sign, payment needs both); sequential
replay, stale-form replay, 4-way concurrent payment (exactly one row), CLI-created ledger row, and a
renamed-vendor second case; HTTP: GET never mutates (row counts and revisions compared), no GET on
action URLs, XSS strings escaped in inbox and case page, traversal and cross-case file requests 404,
tampered file 409, Host/Origin/content-type/size guards, role cookie enforcement, full
select -> sign -> pay -> replay flow over HTTP. I also sabotaged nine rules one at a time (payment
re-check, revision check, evidence check, VP requirement, inventory in hash, invalidation, blocker check,
ledger pre-check, invoice data in hash); the suite caught every one after I added two missing tests.

**UI verified in a real browser** (T3 preview, Chromium) by clicking and typing, not screenshots alone:
actor switch; INV-1004 select R1 -> Manager sign-off -> payment button disabled until eligible -> mock
payment recorded and case locked; INV-1011 PDF visible beside parsed fields; INV-9002 (synthetic
typo) correction form -> status Blocked to Awaiting sign-off with before/after row; two tabs on one
case where the stale tab's sign-off returned 409 and wrote nothing; INV-9001 (synthetic, USD 12,000)
Manager sign-off -> payment disabled -> VP sign-off -> payment; INV-1008 request-correction (blocker
and unpaid status remained); INV-9003 hostile strings rendered as text (0 injected elements); INV-1007
math table; server restart kept all state. Not checked: other browsers, narrow/mobile layout,
keyboard-only use, screen readers.

## Limitations and shortcuts (disclosed)

- **No real auth or RBAC.** Roles come from a cookie any viewer can change. Teams (AP Operations,
  Procurement, ...) and the reason-to-team routing are an invented prototype table; no employees,
  vendor registry, price master or FX data exist. Separation of duties is only by role, not person.
- The shipped corpus has no high-value invoice that is otherwise clean, so the VP flow cannot be
  exercised on it. Tests and my UI run used synthetic files (INV-9001/9002/9003) in temp copies; the
  repository's `data/invoices/` is untouched. To try it, point `--data-dir` at a copy with an added JSON.
- Vendor-change (INV-1012) and non-USD (INV-1014) cases stay blocked in the tool by design: no source of
  truth exists to clear them, so they can only be requested/assigned/escalated. Unknown-item and stock
  cases can be "cleared" only by correcting extracted fields, which is audited but still a human
  assertion, not independent verification.
- No add/remove line items, no document upload in the UI (drop a file in the data directory and
  restart), no inventory editing UI, no notifications, no due-date/overdue logic.
- "Waiting" counts from the last status or ownership change, starting at first ingestion; the fixtures
  carry no received-at timestamps.
- Concurrency is verified with threads and separate connections on one machine (SQLite file locking),
  not multi-host. The revision check is coarse: any case change invalidates other open forms.
- Evidence hash binds the data the validation used; it does not detect a changed parser version
  (parsed snapshots are stored at ingestion). Text display is capped at 200 KB per document.
- Working labels in `evals/` remain author-written expectations, unreviewed and unchanged.
- Only Python 3.14.2 was run; 3.11+ is the declared minimum and untested.

## Files

`invoices/workbench/{policy,service,views,server,__main__,__init__}.py`, `tests/test_workbench.py`,
`tests/test_workbench_http.py`, `WORKBENCH.md`, one-line `.gitignore` addition, a pointer in
`IMPLEMENTATION.md`.

## Parent review and fixes (2026-11-06)

Codex independently reviewed commit 41bf8d2 against baseline 6c6e98f, reran all
102 tests and the corpus eval, then reproduced three gaps with real temporary
SQLite/datadir copies:

- A correction requested after sign-off left the case payable. An open request
  now adds a payment/approval blocker, invalidates sign-offs, survives assignment
  or escalation, and needs an explicit audited resolution. Remaining validation
  defects must be fixed before resolution; fresh approval is then required.
  The UI says 'Record correction request'; no message is sent externally.
- Changing/removing a source file left the stored evidence hash/sign-off valid.
  Case state now verifies original files, includes availability in the evidence
  hash, and blocks approval/payment when any case document is missing/changed.
  Restore the exact original file to make evidence available again. This check
  does not provide filesystem isolation against concurrent external writers.
- Extreme finite numeric input could raise during the arithmetic display and
  break the inbox for an ingested document. Corrections now enforce magnitude
  <= 1 trillion and at most 12 fractional digits; evaluation arithmetic failures
  produce a blocked case without attempting an invalid arithmetic breakdown.

Policy version bumped to workbench-2, so pre-fix approvals require fresh review.
Six new backend regression cases failed before these changes; two new real HTTP
flows verify request -> resolve -> fresh sign-off -> payment and numeric failure
handling. The prior assignment test now checks that reassignment preserves an
open correction request instead of implicitly clearing it.

Final local checks: `uv run pytest -q`: 110 passed; `uv run python -m
invoices.evaluate`: exit 0, 20 standalone and 20 batch working labels matched;
`git diff --check`: clean. Core/labels/fixtures/dependencies remain unchanged.
Parent verification used real HTTP tests, without an additional browser pass.
The Sonnet browser observations above remain separately attributed. CI has not
run; no cloud deployment or production-auth claim is made.
