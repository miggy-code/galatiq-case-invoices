"""VP agent and critic for the three VP triggers; model output never controls routing directly."""

import json
from decimal import Decimal

from .database import inventory
from .documents import item_key
from .evidence import locate
from .routing import add_reasons, severity
from .validation import HIGH_VALUE_USD_THRESHOLD, validate

# POLICY decision 6: what the VP agent may approve alone. Placeholders to tune with evaluation evidence.
AUTHORITY = {
    'high_value': Decimal('25000'),  # invoice total, USD
    'price_above_catalog': Decimal('20'),  # percent above catalog, inclusive
    'price_below_catalog': Decimal('30'),  # percent below catalog, inclusive (Miguel, 2026-11-10)
    'revision_change': Decimal('5000'),  # increase over the original, USD
}
# Triggers whose approval must quote the document's stated reason (decisions 5 and 7).
NEEDS_REASON = ('price_above_catalog', 'price_below_catalog', 'revision_change')


def triggers(result):
    return [r for r in result['reasons'] if severity(r) == 'vp']


def vp_eligible(result):
    """The VP is reached only when every open reason is a VP trigger (decision 4)."""
    return bool(result['reasons']) and len(triggers(result)) == len(result['reasons'])


def measure(trigger, result):
    # Exact, not the rounded display figure: $199.99 against $250 is 20.004% below, beyond a 20% limit.
    prices = [
        (Decimal(p['unit_price']) - Decimal(p['catalog_price'])) / Decimal(p['catalog_price']) * 100
        for p in result.get('prices', [])
    ]
    return {
        'high_value': lambda: Decimal(result['total']),
        'price_above_catalog': lambda: max(p for p in prices if p > 0),
        'price_below_catalog': lambda: max(-p for p in prices if p < 0),
        'revision_change': lambda: Decimal(result['revision_of']['increase']),
    }[trigger]()


def beyond_authority(result):
    return [t for t in triggers(result) if measure(t, result) > AUTHORITY[t]]


def review_facts(invoice, conn, result=None):
    result = result or validate(invoice, conn)
    catalog = {item_key(name): (stock, price) for name, stock, price in inventory(conn)}
    facts = {
        'invoice': {
            'number': invoice.number,
            'vendor': invoice.vendor,
            'date': invoice.date,
            'due_date': invoice.due_date,
            'currency': invoice.currency,
            'subtotal': None if invoice.subtotal is None else str(invoice.subtotal),
            'tax': str(invoice.tax),
            'shipping': str(invoice.shipping),
            'total': str(invoice.total),
            'lines': [
                {
                    'item': line.item,
                    'quantity': str(line.quantity),
                    'unit_price': str(line.unit_price),
                    'catalog_price': catalog.get(item_key(line.item), (None, None))[1],
                    'stock': catalog.get(item_key(line.item), (None, None))[0],
                }
                for line in invoice.lines
            ],
        },
        'validation': {'decision': result['decision'], 'reasons': result['reasons']},
        'triggers': triggers(result),
        'policy': {
            'high_value_usd_threshold': str(HIGH_VALUE_USD_THRESHOLD),
            'high_value_review': 'A high_value trigger routes to VP review; VP may approve after review.',
            'arithmetic': 'Validated by code: line amounts, subtotal, tax, shipping and total agree.',
            'purchase_orders': 'No purchase-order data exists in this prototype; a cited PO cannot be checked.',
            'review_scope': 'price, revision and description anomalies',
        },
    }
    if result.get('prices'):
        facts['prices'] = [
            {k: p[k] for k in ('line', 'item', 'unit_price', 'catalog_price', 'variance_pct', 'reason')}
            for p in result['prices']
        ]
    if result.get('revision_of'):
        facts['revision'] = result['revision_of']
    return facts


def _value(facts, path):
    value = facts
    for part in path.split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError('Unknown fact path')
    if isinstance(value, (dict, list)):
        raise ValueError('Citation must identify a scalar')
    return value


def _proposal(raw, facts):
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get('decision') not in ('approve', 'hold')
        or not isinstance(data.get('rationale'), str)
        or not isinstance(data.get('claims'), list)
    ):
        raise ValueError('Invalid proposal')
    for claim in data['claims']:
        if not isinstance(claim, dict) or not isinstance(claim.get('path'), str) or 'value' not in claim:
            raise ValueError('Invalid claim')
        _value(facts, claim['path'])
        if isinstance(claim['value'], (dict, list)):
            raise ValueError('Invalid claim value')
    justifications = data.setdefault('justifications', [])
    if not isinstance(justifications, list) or not all(
        isinstance(j, dict) and isinstance(j.get('trigger'), str) and isinstance(j.get('quote'), str)
        for j in justifications
    ):
        raise ValueError('Invalid justifications')
    return data


def grounded_justifications(proposal, open_triggers, text):
    """Code grounds each stated-reason quote in the document; returns (grounded, all required found)."""
    grounded = []
    for item in proposal['justifications']:
        location = locate(text, item['quote'].strip())
        if item['trigger'] in open_triggers and item['quote'].strip() and location['found']:
            grounded.append(
                {
                    'trigger': item['trigger'],
                    'quote': item['quote'].strip(),
                    'start': location['start'],
                    'end': location['end'],
                    'candidates': location['candidates'],
                }
            )
    covered = {j['trigger'] for j in grounded}
    return grounded, all(t in covered for t in open_triggers if t in NEEDS_REASON)


def critique_proposal(facts, proposal, critic_model):
    """Critique structured claims against facts, excluding the VP rationale."""
    raw = None
    try:
        payload = {'facts': facts, 'proposal': {'decision': proposal['decision'], 'claims': proposal['claims']}}
        raw = critic_model(json.dumps(payload, sort_keys=True), None)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('issues'), list):
            raise ValueError('Invalid critique')
        for issue in data['issues']:
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get('path'), str)
                or 'value' not in issue
                or not isinstance(issue.get('description'), str)
                or not issue['description'].strip()
            ):
                raise ValueError('Invalid issue')
            actual = _value(facts, issue['path'])
            if type(actual) is not type(issue['value']) or actual != issue['value']:
                raise ValueError('Incorrect fact citation')
        return {'status': 'issues' if data['issues'] else 'pass', 'issues': data['issues'], 'response': raw}
    except Exception as exc:
        return {'status': 'invalid', 'issues': [], 'error': type(exc).__name__, 'response': raw}


def run_vp(invoice, result, conn, vp_model, critic_model):
    """Bounded VP and critic loop, one revision at most. Adds a reason or marks the VP approval."""
    open_triggers = triggers(result)
    facts = review_facts(invoice, conn, result)
    # Only a trigger that needs a stated reason shows the VP the document; the critic never sees it.
    vp_facts = (
        {**facts, 'document': {'text': invoice.notes}} if any(t in NEEDS_REASON for t in open_triggers) else facts
    )
    history = []
    summary = {'triggers': open_triggers, 'rounds': 0}
    result.update(route='vp', vp=summary, review={'facts': facts, 'history': history})

    def stop(reason, **detail):
        summary.update(detail, outcome=reason)
        return add_reasons(result, reason)

    issue = None
    for attempt in range(2):
        summary['rounds'] = attempt + 1
        raw = None
        try:
            payload = {'facts': vp_facts}
            if issue is not None:
                payload['critique'] = issue
            raw = vp_model(json.dumps(payload, sort_keys=True), None)
            proposal = _proposal(raw, vp_facts)
            entry = {'proposal': proposal, 'response': raw}
            history.append(entry)
        except Exception as exc:
            result['review']['error'] = type(exc).__name__
            result['failed_step'] = 'vp'
            history.append({'response': raw, 'error': type(exc).__name__})
            return stop('vp_unavailable')
        summary.update(decision=proposal['decision'], rationale=proposal['rationale'])
        # A hold can never be revived by a critic or subsequent revision.
        if proposal['decision'] == 'hold':
            return stop('vp_hold')
        justifications, complete = grounded_justifications(proposal, open_triggers, invoice.notes)
        summary['justifications'] = justifications
        if not complete:
            return stop('vp_claim_mismatch', problem='a stated-reason quote is missing or not in the document')
        critic_facts = {
            **facts,
            'justifications': [{'trigger': j['trigger'], 'quote': j['quote']} for j in justifications],
        }
        critique = critique_proposal(critic_facts, proposal, critic_model)
        entry['critique'] = critique
        summary.setdefault('critic', []).append({'status': critique['status'], 'issues': critique['issues']})
        if critique['status'] == 'invalid':
            result['failed_step'] = 'critic'
            return stop('critic_unavailable')
        if critique['status'] == 'pass':
            # Enforce factual claims even if the critic misses them.
            if any(
                type(_value(vp_facts, c['path'])) is not type(c['value']) or _value(vp_facts, c['path']) != c['value']
                for c in proposal['claims']
            ):
                return stop('vp_claim_mismatch', problem='a claim does not match the validated facts')
            beyond = beyond_authority(result)
            if beyond:
                return stop('vp_recommend', recommendation='approve', beyond_authority=beyond)
            summary.update(outcome='approved', approved=True)
            return add_reasons(result)
        issue = critique['issues']
    return stop('critic_unresolved')
