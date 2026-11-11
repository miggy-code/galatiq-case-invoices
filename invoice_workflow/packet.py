"""Case packets (POLICY decision 14): built by code from the routing table and evidence records."""

from .routing import ROUTING, UNKNOWN_REASON

# Summary and report order: internal audit first, paid last.
OWNERS = {
    'internal_audit': 'Internal audit',
    'human_review': 'Human review',
    'ap_operations': 'AP operations',
    'ap_clerk': 'AP clerk',
    'vendor': 'Vendor, via the AP clerk (rejected)',
    'no_action': 'No action (duplicate or superseded)',
    'vp_agent': 'Approved by the VP agent',
}


def needs_packet(result):
    """Every invoice not paid touchlessly gets a packet; a VP approval keeps one as its audit record."""
    return result['decision'] != 'approve' or result.get('route') == 'vp'


def _quote(entry):
    return {
        'field': entry['field'],
        'quote': entry['quote'],
        'start': entry.get('start'),
        'end': entry.get('end'),
        'candidates': entry.get('candidates', []),
    }


def _evidence(code, result):
    evidence = result.get('evidence', [])
    if code in ('price_above_catalog', 'price_below_catalog'):
        lines = {f"line_items[{p['line']}].unit_price" for p in result.get('prices', []) if p['reason'] == code}
        return [_quote(e) for e in evidence if e['field'] in lines and e.get('found')]
    if code == 'ungrounded_extraction':
        return [_quote(e) for e in evidence if not e.get('found') or (e.get('ambiguous') and e.get('start') is None)]
    if code in ('extraction_uncertain', 'extraction_disagreement'):
        return [_quote(e) for e in evidence if e.get('confident') is False]
    if code in ('total_mismatch', 'subtotal_mismatch', 'tax_mismatch', 'high_value', 'possible_duplicate'):
        return [_quote(e) for e in evidence if e['field'] == 'total' and e.get('found')]
    if code == 'revision_change':
        return [j for j in result.get('vp', {}).get('justifications', []) if j['trigger'] == code]
    return [_quote(e) for e in evidence if e['field'] == code]


def _detail(code, result):
    if code in ('price_above_catalog', 'price_below_catalog'):
        return '; '.join(
            f"{p['item']} at ${p['unit_price']} vs catalog ${p['catalog_price']} ({float(p['variance_pct']):+.2f}%)"
            for p in result.get('prices', [])
            if p['reason'] == code
        )
    if code == 'revision_change' and result.get('revision_of'):
        r = result['revision_of']
        sign = '' if r['increase'].startswith('-') else '+'
        return (
            f"{r['revision']} changes the total from ${r['original_total']} "
            f"to ${r['revised_total']} ({sign}{r['increase']})"
        )
    if code == 'revision_change' and result.get('superseded_by'):
        return f"decided together with the revision {result['superseded_by']}"
    if code == 'revision_conflict' and result.get('revision_checks_failed'):
        return 'failed: ' + '; '.join(result['revision_checks_failed'])
    if code in ('model_failure', 'extraction_failed', 'vp_unavailable', 'critic_unavailable') and result.get(
        'failed_step'
    ):
        return f"failed step: {result['failed_step']}"
    if code == 'vp_recommend':
        return 'beyond the agent\'s authority: ' + ', '.join(result.get('vp', {}).get('beyond_authority', []))
    return None


def build_packet(result, text=None):
    reasons = []
    for code in result['reasons']:
        _, policy, plain, step = ROUTING.get(code, UNKNOWN_REASON)
        reason = {
            'code': code,
            'text': plain,
            'policy_decision': policy,
            'next_step': step,
            'evidence': _evidence(code, result),
        }
        detail = _detail(code, result)
        if detail:
            reason['detail'] = detail
        reasons.append(reason)
    packet = {
        'decision': result['decision'],
        'owner': result['owner'],
        'next_step': result['next_step'],
        'reasons': reasons,
        'numbers': result.get('numbers'),
        'notes': result.get('notes', []),
    }
    if result.get('vp'):
        packet['vp'] = result['vp']
    if text:
        packet['document'] = text
    return packet


def summary(results):
    """Plain-text run summary grouped by owner, internal audit first."""
    paid = [r for r in results if r['decision'] == 'approve']
    lines = [
        f"{len(results)} documents: {sum(not needs_packet(r) for r in paid)} paid touchlessly, "
        f"{sum(r.get('route') == 'vp' for r in paid)} approved by the VP agent, "
        f"{sum(r['decision'] == 'hold' for r in results)} held, "
        f"{sum(r['decision'] == 'reject' for r in results)} rejected, "
        f"{sum(r['decision'] in ('duplicate', 'superseded') for r in results)} duplicate or superseded"
    ]
    for owner, label in OWNERS.items():
        group = [r for r in results if needs_packet(r) and r.get('owner') == owner]
        if group:
            lines.append(f'{label} ({len(group)}):')
            lines += [
                f"  {r['source']}: {r['decision']} [{', '.join(r['reasons'])}] -> {r['next_step']}" for r in group
            ]
    return '\n'.join(lines)
