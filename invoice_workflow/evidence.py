"""Locate verbatim evidence without guessing between repeated matches."""

import re


def continues(text, i, step, lo=0, hi=None):
    """Whether text[i] extends the token next to it: a letter, digit or '-', or a '.' or ','
    with a digit beyond it ('3,000', '2.5'). 'qty 3, price' ends the token at the comma."""
    hi = len(text) if hi is None else hi
    if not lo <= i < hi:
        return False
    char = text[i]
    if char.isalnum() or char == '-':
        return True
    return char in '.,' and lo <= i + step < hi and text[i + step].isdigit()


def locate(text, quote, *, contexts=None, token=False):
    quote = quote if isinstance(quote, str) else ''
    candidates = []
    if quote:
        for start, end in contexts or [(0, len(text))]:
            # Any run of whitespace matches any other: a quote may join a line the document wraps.
            pattern = r'\s+'.join(re.escape(part) for part in quote.split())
            for match in re.finditer(pattern, text[start:end]):
                left = start + match.start()
                right = start + match.end()
                if token:
                    before = text[left - 1] if left > start else ''
                    quantity_prefix = (
                        before.lower() == 'x'
                        and quote[0].isdigit()
                        and (left - 2 < start or not text[left - 2].isalnum())
                    )
                    if (not quantity_prefix and continues(text, left - 1, -1, start, end)) or continues(
                        text, right, 1, start, end
                    ):
                        continue
                candidates.append({'start': left, 'end': right})
    ambiguous = len(candidates) > 1
    only = candidates[0] if len(candidates) == 1 else {}
    return {
        'start': only.get('start'),
        'end': only.get('end'),
        'found': bool(candidates),
        'ambiguous': ambiguous,
        'candidates': candidates,
    }
