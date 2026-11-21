"""Match an adjacent integer threshold with an unchanged length expression."""
import re

from .deferred import warning_free

# Normalize each inequality to its inclusive integer boundary on the dynamic side.
REVERSED = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
NUMBER = rb"-?(?:0|[1-9][0-9]{0,40})"
OP = rb"(?:<=|>=|<|>)"
PATTERNS = (
    re.compile(rb"(" + OP + rb")([ \t]*)(" + NUMBER + rb")"),
    re.compile(rb"(" + NUMBER + rb")([ \t]*)(" + OP + rb")"),
)


def boundary(operator, value, literal_left):
    if literal_left:
        operator = REVERSED[operator]
    if operator in (">", ">="):
        return "at_least", value + (operator == ">")
    return "at_most", value - (operator == "<")


def equivalent(path, before, after, sites):
    if not sites:
        return False
    original, changed = before.encode(), after.encode()
    delta = len(changed) - len(original)
    for first, last, literal_left in sites:
        end = last + delta
        if end <= first or changed[:first] != original[:first] or changed[end:] != original[last:]:
            continue
        pattern = PATTERNS[literal_left]
        old = pattern.fullmatch(original[first:last])
        new = pattern.fullmatch(changed[first:end])
        if old is None or new is None or old[2] != new[2]:
            continue
        op, number = (3, 1) if literal_left else (1, 3)
        if old[op] == new[op]:
            continue
        previous = boundary(old[op].decode(), int(old[number]), literal_left)
        updated = boundary(new[op].decode(), int(new[number]), literal_left)
        if previous == updated and warning_free(path, before) and warning_free(path, after):
            return True
    return False
