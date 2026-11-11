"""Payment at most once (POLICY decision 12): ledger row `pending`, call the brief's mock_payment, then `paid`."""

import contextlib
import logging
import sys
import time

from .database import claim_payment, ledger_row, mark_paid

log = logging.getLogger('invoice_workflow')


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def pay(conn, identity, inv, payment=mock_payment, *, originals=()):
    """Return 'paid', 'already_recorded', 'payment_pending' or 'possible_revision'.
    `payment` is the bank: the brief's mock_payment unless a caller supplies another."""
    if not claim_payment(conn, identity, inv, originals):
        if ledger_row(conn, identity) is None:
            return 'possible_revision'
        # Another run may be paying this invoice right now: give it a moment before calling it stuck.
        for _ in range(20):
            if ledger_row(conn, identity)[0] == 'paid':
                return 'already_recorded'
            time.sleep(0.1)
        return 'payment_pending'
    try:
        # The brief's function prints; keep stdout for the JSONL results.
        with contextlib.redirect_stdout(sys.stderr):
            response = payment(inv.vendor, inv.total)
        if not isinstance(response, dict) or response.get('status') != 'success':
            raise RuntimeError(f'mock_payment returned {response!r}')
    except Exception as exc:
        # The row stays pending: a person checks whether money moved; never retried automatically.
        log.error('stage=pay invoice=%s error=%s', inv.number, type(exc).__name__)
        return 'payment_pending'
    mark_paid(conn, identity)
    return 'paid'
