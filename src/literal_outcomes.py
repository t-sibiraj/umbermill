"""Bound a changed integer literal using observed comparisons of a builtin length."""

import operator

from .guards import decode_mask
from .length_boundary import PATTERNS


_ORDER = (operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge)
_SYMBOL = {b"<": operator.lt, b"<=": operator.le, b">": operator.gt, b">=": operator.ge}


def affected(selector, path, row, original, replacement, sites):
    before, after = original.encode(), replacement.encode()
    for first, last, literal_left in sites:
        end = last + len(after) - len(before)
        if end <= first or before[:first] != after[:first] or before[last:] != after[end:]:
            continue
        a = PATTERNS[literal_left].fullmatch(before[first:last])
        b = PATTERNS[literal_left].fullmatch(after[first:end])
        op, literal = (3, 1) if literal_left else (1, 3)
        if not a or not b or a[op] != b[op] or a[2] != b[2]:
            continue
        old, new = int(a[literal]), int(b[literal])
        position = first + a.start(op)
        cols = selector._relation_tokens.get(path, {}).get(row, {})
        for col, (start, stop) in cols.items():
            token = before[start:stop]
            if token.strip() != a[op] or start + token.index(a[op]) != position:
                continue
            masks = selector._relation_sites.get(path, {}).get(row, {}).get(col)
            if isinstance(masks, list) and len(masks) == 8:
                return _classify(selector, path, f"{path}|{row}|{col}", masks,
                                 _SYMBOL[a[op]], old, new, literal_left)
    return None


def _classify(selector, path, key, masks, compare, old, new, literal_left):
    def outcome(operation, value, boundary):
        return operation(boundary, value) if literal_left else operation(value, boundary)

    # Both comparisons are constant on each open interval between their
    # integer boundaries. Only the old less/equal/greater classes intersecting
    # a genuine divergence need candidates. Exclude impossible negative lengths.
    classes = set()
    for boundary in (old, new):
        for v in (boundary - 1, boundary, boundary + 1, 0):
            if v >= 0 and outcome(compare, v, old) != outcome(compare, v, new):
                classes.add((v > old) - (v < old))
    result = 0
    for direction in classes:
        value = old + direction
        candidates = None
        for i, alt in enumerate(_ORDER, 2):
            if not isinstance(masks[i], str) or outcome(compare, value, old) == outcome(alt, value, old):
                continue
            # Every visit in this class must have reported a divergent outcome
            # for each of these alternate operators in the independent traces.
            possible = decode_mask(masks[i]) | selector._isolated_index.comparison_guards(path, key, i)
            candidates = possible if candidates is None else candidates & possible
        if candidates is None:
            return None
        result |= candidates
    return result
