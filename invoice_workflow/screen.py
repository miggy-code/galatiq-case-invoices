"""Fraud screen: code rules for literal phrases, then a model for reworded ones. It can only add holds."""

import json
import re
from .routing import route
from .evidence import locate


RULES = {
    'suspicious_instructions': [
        r'\bignore\s+(?:all\s+)?previous\s+instructions\b',
        r'\b(?:pre[- ]approved|mark\s+as\s+approved|no\s+further\s+review|do\s+not\s+route'
        r'|skip\s+(?:all\s+)?review|bypass\s+controls)\b',
        r'\b(?:approved\s+by|approval\s+from|on\s+the\s+authority\s+of)\s+(?:the\s+)?(?:vp|ceo|cfo)\b',
    ],
    'urgency_pressure': [r'\bURGENT\b(?!\s+delivery)', r'(?i:\bpay\s+immediately\b|\bavoid\s+penalties\b)'],
    'payment_detail_change': [
        r'(?i:\b(?:changed\s+banks|new\s+(?:bank\s+)?account|bank\s+account\s+changed|new\s+payment\s+details|remit\s+to\s+(?:a\s+)?(?:new|different)\s+account)\b)',
    ],
}


def signal(reason, quote, text):
    if reason not in RULES or not isinstance(quote, str) or not quote:
        return None
    location = locate(text, quote)
    if not location['found']:
        return None
    return {'field': reason, 'value': reason, 'quote': quote, **location}


def rule_signals(text):
    """Literal phrases from RULES, each located in the text."""
    found = []
    for reason, patterns in RULES.items():
        flags = 0 if reason == 'urgency_pressure' else re.I
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags):
                item = signal(reason, match.group(), text)
                if item and item not in found:
                    found.append(item)
    return found


def screen(text, model=None):
    """Rules first, then the model. Every model signal must quote the text. Returns
    {'evidence': [...]} plus 'unavailable': True when the model failed (the invoice then holds)."""
    from openai import APIError

    found = rule_signals(text)
    if model is None or not text.strip():
        return {'evidence': found}
    try:
        data = json.loads(model(text, None))
        if not isinstance(data, dict) or not isinstance(data.get('signals'), list):
            raise ValueError('Invalid screening response')
    except (ValueError, TypeError, KeyError, FileNotFoundError, TimeoutError, ConnectionError, APIError):
        return {'evidence': found, 'unavailable': True}
    for item in data['signals']:
        if isinstance(item, dict):
            evidence = signal(item.get('reason'), item.get('quote'), text)
            if evidence and evidence not in found:
                found.append(evidence)
    return {'evidence': found}


def apply_screen(result, text, screen_model):
    """Add the screen's signals to a result as reasons; a failed screen holds (POLICY 13)."""
    screening = screen(text, screen_model)
    signals = screening['evidence']
    if signals:
        result.setdefault('evidence', []).extend(signals)
        result['reasons'] = sorted(set(result.get('reasons', [])) | {e['field'] for e in signals})
    if screening.get('unavailable'):
        # Hold even an invoice that would be rejected: never return it unscreened (decision 13).
        result['reasons'] = sorted(set(result.get('reasons', [])) | {'model_failure'})
        result['failed_step'] = 'fraud_screen'
    return route(result)
