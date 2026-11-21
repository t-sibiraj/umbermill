"""Compare the realized insertion-ordered values of closed Python string maps."""

import ast

from .deferred import warning_free


def _strings(node):
    """Evaluate only native dict displays, literal unpacking, and dict union."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _strings(node.left) | _strings(node.right)
    if not isinstance(node, ast.Dict):
        raise ValueError("open expression")
    table = {}
    for key, value in zip(node.keys, node.values):
        if key is None:
            table.update(_strings(value))
        elif (isinstance(key, ast.Constant) and type(key.value) is str
              and isinstance(value, ast.Constant) and type(value.value) is str):
            table[key.value] = value.value
        else:
            raise ValueError("dynamic key or value")
    return table


def _assignments(source):
    tree = ast.parse(source, type_comments=True)
    for owner in tree.body:
        if not isinstance(owner, ast.ClassDef):
            continue
        for st in owner.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                target = st.targets[0]
            elif isinstance(st, ast.AnnAssign):
                target = st.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id.isupper() and st.value is not None:
                yield (owner.name, target.id, st.lineno), st.value


def _outside(source, node):
    raw = source.encode()
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    return raw[:start], raw[end:]


def same_mapping(path, before, after, changed_lines):
    """Keep source coordinates outside a single class assignment unchanged."""
    if before.count("\n") != after.count("\n") or not changed_lines:
        return False
    first, last = min(changed_lines), max(changed_lines)
    old = [(k, v) for k, v in _assignments(before) if v.lineno <= first <= last <= v.end_lineno]
    if len(old) != 1:
        return False
    owner, left = old[0]
    new = [v for k, v in _assignments(after) if k == owner]
    if len(new) != 1 or new[0].end_lineno != left.end_lineno:
        return False
    right = new[0]
    if _outside(before, left) != _outside(after, right):
        return False
    try:
        # A metaclass can enumerate the dictionary and invert duplicate values.
        # Both key order and values are therefore part of the observable result.
        equivalent = tuple(_strings(left).items()) == tuple(_strings(right).items())
    except ValueError:
        return False
    return equivalent and warning_free(path, before) and warning_free(path, after)
