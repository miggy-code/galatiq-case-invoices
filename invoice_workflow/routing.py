"""Who acts on each reason. ROUTING is the policy table; route() is the only place an outcome is set."""

# Decision rights (POLICY.md "Who decides what"). Every reason has exactly one row:
# (severity, POLICY decision, what fired in plain words, next step). The severity picks the owner below.
# fmt: off
RETURN = 'Return to the vendor for a corrected invoice'
ROUTING = {
    # Internal audit: someone may be trying to steal; never the VP (decision 9).
    'suspicious_instructions': ('audit', 9, 'The invoice text gives instructions or claims approval aimed at our review', 'Internal audit: treat as attempted manipulation; confirm with the vendor on the number already on file'),
    'urgency_pressure': ('audit', 9, 'The invoice pressures for immediate payment', 'Internal audit: confirm the invoice with the vendor on the number already on file before any payment'),
    'payment_detail_change': ('audit', 9, 'The invoice changes bank or payment details', 'Internal audit: call the vendor on the number already on file, never the number in the invoice'),
    # Closed: never paid, nobody acts (decisions 7, 8 and 12).
    'duplicate_document': ('duplicate', 8, 'Another copy of the same invoice in this batch', 'No action: the other copy is handled'),
    'already_recorded': ('duplicate', 12, 'The ledger already has a payment for this vendor and invoice number', 'No action: already paid'),
    'superseded': ('superseded', 7, 'Replaced by an approved revision', 'No action: the revision is paid instead'),
    'payment_pending': ('operations', 12, 'A payment was started but never confirmed', 'AP operations: check whether money moved before any retry'),
    # AP operations: one of our own checks failed to run (decision 13).
    'model_failure': ('operations', 13, 'The fraud screen failed to run', 'AP operations: re-run the failed step'),
    'extraction_failed': ('operations', 13, 'Reading the document with the model failed', 'AP operations: re-run extraction'),
    'recording_missing': ('operations', 13, 'No recorded model response to replay for this document', 'AP operations: re-run extraction with a model configured'),
    'internal_error': ('operations', 13, 'An unexpected error in our code stopped this invoice', 'AP operations: check the log; nothing was paid'),
    'vp_unavailable': ('operations', 13, 'The VP agent failed or gave a malformed answer', 'AP operations: re-run the VP review'),
    'critic_unavailable': ('operations', 13, 'The critic failed or gave a malformed answer', 'AP operations: re-run the VP review'),
    # Vendor, via the AP clerk: the data is wrong (decisions 1 and 11).
    'parse_error': ('reject', 1, 'The file could not be parsed', RETURN),
    'missing_vendor': ('reject', 1, 'No vendor', RETURN),
    'missing_invoice_number': ('reject', 1, 'No invoice number', RETURN),
    'missing_date': ('reject', 11, 'No invoice date', RETURN),
    'missing_due_date': ('reject', 11, 'No due date', RETURN),
    'invalid_date': ('reject', 11, 'The invoice date is not a valid date', RETURN),
    'invalid_due_date': ('reject', 11, 'The due date is not a valid date', RETURN),
    'missing_items': ('reject', 1, 'No line items', RETURN),
    'missing_item': ('reject', 1, 'A line has no item', RETURN),
    'invalid_quantity': ('reject', 1, 'A quantity is zero or negative', RETURN),
    'invalid_price': ('reject', 1, 'A unit price is zero or negative', RETURN),
    'invalid_charges': ('reject', 1, 'Tax or shipping is negative or the tax rate is invalid', RETURN),
    'missing_total': ('reject', 1, 'No total', RETURN),
    'invalid_total': ('reject', 1, 'The total is zero or negative', RETURN),
    'line_mismatch': ('reject', 1, 'A line amount is not quantity times unit price', RETURN),
    'subtotal_mismatch': ('reject', 1, 'The subtotal is not the sum of the lines', RETURN),
    'tax_mismatch': ('reject', 1, 'The tax is not the stated rate times the subtotal', RETURN),
    'total_mismatch': ('reject', 1, 'The total is not subtotal plus tax and shipping', RETURN),
    # AP clerk: we are unsure what the facts are.
    'unknown_item': ('clerk', 3, 'An item is not in inventory', 'AP clerk: check whether we ordered this item; update inventory or return to the vendor'),
    'out_of_stock': ('clerk', 3, 'An item has zero stock', 'AP clerk: confirm receipt of the goods before payment'),
    'stock_exceeded': ('clerk', 3, 'A quantity exceeds stock on hand', 'AP clerk: confirm the quantity received before payment'),
    'unsupported_currency': ('clerk', 10, 'Priced in a currency other than USD; there is no FX conversion', 'AP clerk: arrange payment in that currency or ask for a USD invoice'),
    'currency_unknown': ('clerk', 10, 'No evidence the amounts are USD, or a sign they are not', 'AP clerk: confirm the currency with the vendor'),
    'unreadable_document': ('clerk', 2, 'No text could be read from the document', 'AP clerk: get a readable copy or key the invoice in'),
    'ungrounded_extraction': ('clerk', 2, 'An extracted value could not be found in the document', 'AP clerk: confirm or correct the highlighted values, then re-run'),
    'extraction_disagreement': ('clerk', 2, 'The model and the regex cross-check read different numbers', 'AP clerk: confirm or correct the highlighted values, then re-run'),
    'extraction_uncertain': ('clerk', 2, 'A money or quantity value was not read with confidence', 'AP clerk: confirm or correct the highlighted values, then re-run'),
    'possible_duplicate': ('clerk', 8, 'Same vendor, date and total as another invoice under a different number', 'AP clerk: check whether this is a resubmission before paying'),
    'revision_conflict': ('clerk', 7, 'Versions of this invoice disagree and the revision checks failed', 'AP clerk: confirm with the vendor which version is valid'),
    'possible_revision': ('clerk', 7, 'Looks like a revision of another invoice, under a new number', 'AP clerk: confirm which version to pay; a revision under a new number is never linked automatically'),
    'revision_after_payment': ('clerk', 7, 'A changed invoice arrived after the original was paid', 'AP clerk: decide whether to pay the difference'),
    # Human review: a business judgment above the agent or unresolved by it.
    'vp_hold': ('human_review', 4, 'The VP agent held the invoice', "Human review: review the agent's reasoning and decide"),
    'vp_recommend': ('human_review', 6, "The VP agent recommends approval beyond its authority", "Human review: approve or reject the agent's recommendation"),
    'critic_unresolved': ('human_review', 4, "The critic's objection survived one revision", "Human review: weigh the critic's objection and decide"),
    'vp_claim_mismatch': ('human_review', 4, 'The VP agent cited a fact or quote code could not verify', "Human review: review the invoice; the agent's approval was not trusted"),
    # VP triggers (decision 4): the VP agent decides within AUTHORITY in review.py.
    'high_value': ('vp', 4, 'Total above $10,000', 'VP review'),
    'price_above_catalog': ('vp', 5, 'A unit price is above the catalog price', 'VP review: approve only with a specific stated reason'),
    'price_below_catalog': ('vp', 5, 'A unit price is below the catalog price', 'VP review: approve only with a specific stated reason'),
    'revision_change': ('vp', 7, 'A revised invoice changes the original', 'VP review of the stated reason for the change'),
}
UNKNOWN_REASON = ('operations', None, 'A reason code missing from the routing table', 'AP operations: add the reason to the routing table')
# Precedence: the first severity present decides the outcome and the owner.
SEVERITIES = {
    'audit': ('hold', 'internal_audit'),
    'duplicate': ('duplicate', 'no_action'),
    'superseded': ('superseded', 'no_action'),
    'operations': ('hold', 'ap_operations'),
    'reject': ('reject', 'vendor'),
    'clerk': ('hold', 'ap_clerk'),
    'human_review': ('hold', 'human_review'),
    'vp': ('hold', 'human_review'),
}
# fmt: on


def severity(code):
    return ROUTING.get(code, UNKNOWN_REASON)[0]


def route(result):
    """Set decision, owner and next step from the reasons; the only place outcomes are chosen."""
    reasons = result.setdefault('reasons', [])
    present = {severity(r) for r in reasons}
    top = next((s for s in SEVERITIES if s in present), None)
    if top is None:
        result.update(decision='approve', owner=None, next_step=None)
    elif top == 'vp' and result.get('vp', {}).get('approved'):
        result.update(
            decision='approve', owner='vp_agent', next_step='None: approved by the VP agent within its authority'
        )
    else:
        decision, owner = SEVERITIES[top]
        first = next(r for r in reasons if severity(r) == top)
        result.update(decision=decision, owner=owner, next_step=ROUTING.get(first, UNKNOWN_REASON)[3])
    return result


def add_reasons(result, *codes):
    result['reasons'] = sorted(set(result.get('reasons', [])) | set(codes))
    return route(result)
