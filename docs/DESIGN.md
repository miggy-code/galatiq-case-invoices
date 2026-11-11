# Design rules

## Put computable decisions in code

Code parses structured files, checks arithmetic, catalog prices and stock,
reconciles duplicates and revisions, enforces authority limits and routes outcomes.
Models read language and judge stated reasons. Code checks their outputs before
using them. The VP gets facts through a read-only tool and can approve only within
limits enforced by code. The critic can veto an approval.

## Prefer a hold to an unsupported payment

Uncertain readings, unresolved facts and failed pipeline steps hold the invoice
for a person. Clear data errors return to the vendor; fraud signals go to internal
audit. One routing table assigns each reason an owner and a next step.

## Claim the payment before calling the bank

The SQLite ledger records a pending claim before calling the mock bank, then
marks it paid. The claim also reserves the original numbers named by a revision.
A conflicting claim prevents another bank call. A failed call
leaves the claim pending for a person to investigate.

## Keep case evidence and the report after the decision

Source quotes are checked during extraction and review. After the decision, code
builds the case packet and its evidence for the reviewer. The HTML report displays
those results, grouped by owner. Neither the packet nor the report feeds back into
the decision.

## Measure the safeguard

The evaluator scores invoice decisions in standalone and batch modes, counts
false approvals and their dollars at risk, and checks selected evidence spans.
Seeded bad proposals and good controls exercise the critic directly. Invariant
tests exercise payment, authority, routing and failure handling. The
[results](RESULTS.md) record the current measurements and reproduction commands.
