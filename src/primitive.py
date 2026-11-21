"""Qualify primitive expressions observed throughout a complete process-pool unittest."""

import ast
import gzip
import json
import operator
import platform
import sys

from . import BASE_COMMIT
from .deferred import warning_free
from .patches import UnsupportedPatch, apply_change, parse_patch


_OPERATORS = {
    'Eq': operator.eq, 'NotEq': operator.ne, 'Lt': operator.lt,
    'LtE': operator.le, 'Gt': operator.gt, 'GtE': operator.ge,
}
_TYPES = (str, int, bool, type(None))


def _constant(token):
    if len(token) > 512 or not token or '\n' in token:
        raise ValueError('unsupported literal')
    expr = ast.parse(token, mode='eval').body
    if not isinstance(expr, ast.Constant) or type(expr.value) not in _TYPES:
        raise ValueError('expected a primitive constant')
    if ast.get_source_segment(token, expr) != token:
        raise ValueError('unexpected surrounding syntax')
    return expr.value


class PrimitiveObservations:
    def __init__(self, path, nodeids):
        self.files = {}
        self.number = None
        with gzip.open(path, 'rt') as stream:
            data = json.load(stream)
        if (data['base'] != BASE_COMMIT or sys.implementation.name != 'cpython'
                or list(sys.version_info[:2]) != data['python']
                or (sys.platform, platform.machine()) not in {tuple(t) for t in data['platforms']}):
            return
        self.number = nodeids.get(data['test'])
        self.files = data['files']

    def passing(self, selector, patch, selection):
        if self.number is None or selection.fallback or self.number not in selection.tests:
            return False
        header = patch.split('\n', 1)[0]
        if 'sqlglot/optimizer/' not in header and 'sqlglot/executor/' not in header:
            return False
        try:
            edits = parse_patch(patch)
            if len(edits) != 1:
                return False
            change = edits[0]
            path = change.old_path
            record = self.files.get(path)
            if (not record or path != change.new_path or change.added or change.deleted
                    or len(change.old_lines) != 1 or record[0] != selector._source_hashes.get(path)):
                return False
            line = next(iter(change.old_lines))
            sites = record[1].get(str(line))
            if not sites and not any(text.startswith("-") and text[1:].lstrip().startswith("return ")
                                     for text in change.hunks[0].lines):
                return False
            old = selector._source(path)
            new = apply_change(old, change)
            old_lines = old.splitlines(keepends=True)
            new_lines = new.splitlines(keepends=True)
            pos = line - 1
            if (len(old_lines) != len(new_lines) or old_lines[:pos] != new_lines[:pos]
                    or old_lines[pos + 1:] != new_lines[pos + 1:]):
                return False
            before, after = old_lines[pos].encode(), new_lines[pos].encode()
            candidate_return = before.lstrip().startswith(b'return ') and after.lstrip().startswith(b'return ')
            qualified = candidate_return and _unreachable_return(path, old, new, line, record[1])
            for start, stop, mode, op, values in sites or ():
                end = stop + len(after) - len(before)
                if (start >= end or before[:start] != after[:start]
                        or before[stop:] != after[end:]):
                    continue
                prior = _constant(before[start:stop].decode())
                changed = _constant(after[start:end].decode())
                # Independently require a syntactically valid compiled replacement. In
                # particular, a keyword or adjacent name can change its token boundary.
                if not _same_expression(old, new, line, start, stop, end):
                    return False
                if mode == 'arm':
                    unchanged = not values
                else:
                    compare = _OPERATORS[op]
                    unchanged = type(prior) is type(changed) and all(
                        type(value) in _TYPES and compare(value, prior) == compare(value, changed)
                        for value in values
                    )
                qualified |= unchanged
                break
            if not qualified or not warning_free(path, old) or not warning_free(path, new):
                return False
            for index in (selector._reader_index, selector._isolated_index):
                if index is None:
                    return False
                readers = index.changed(path, old, new, guard=False)
                if readers is None or readers:
                    return False
            return True
        except (KeyError, TypeError, ValueError, UnicodeError, SyntaxError, RecursionError, UnsupportedPatch):
            return False
        return False


def _same_expression(old, new, line, start, stop, end):
    before = ast.parse(old)
    after = ast.parse(new)
    for node in ast.walk(after):
        if (isinstance(node, ast.Constant) and (node.lineno, node.end_lineno) == (line, line)
                and (node.col_offset, node.end_col_offset) == (start, end)):
            for original in ast.walk(before):
                if (isinstance(original, ast.Constant) and (original.lineno, original.end_lineno) == (line, line)
                        and (original.col_offset, original.end_col_offset) == (start, stop)):
                    node.value = original.value
                    return ast.dump(before) == ast.dump(after)
    return False


def _unreachable_return(path, old, new, line, records):
    """A return on no baseline path cannot change this test or its recorded predecessors."""
    before, after = ast.parse(old), ast.parse(new)

    def returned(tree):
        matches = [n for n in ast.walk(tree) if isinstance(n, ast.Return)
                   and n.lineno == n.end_lineno == line and n.value is not None]
        return matches[0] if len(matches) == 1 else None

    original, replacement = returned(before), returned(after)
    if original is None or replacement is None:
        return False
    # An unevaluated expression can still change frame bindings or execution mode.
    for expr in (original.value, replacement.value):
        if any(isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await,
                              ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp,
                              ast.GeneratorExp, ast.Starred)) for n in ast.walk(expr)):
            return False
    replacement.value = original.value
    if ast.dump(before) != ast.dump(after):
        return False
    functions = [n for n in ast.walk(before) if isinstance(n, ast.FunctionDef)
                 and any(s is original for s in ast.walk(n))]
    if len(functions) != 1:
        return False
    owner = functions[0]
    # Loops, exception handlers, nested definitions and suspension require other
    # control-flow edges. Restrict this proof to straight-line code and if/else.
    allowed = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Pass,
               ast.If, ast.Raise, ast.Return)
    if any(not isinstance(n, allowed) for stmt in owner.body for n in ast.walk(stmt)
           if isinstance(n, ast.stmt)):
        return False
    if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for n in ast.walk(owner)):
        return False

    def outcomes(test):
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return {not v for v in outcomes(test.operand)}
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return {True, False}
        right = test.comparators[0]
        op = type(test.ops[0]).__name__
        if not isinstance(right, ast.Constant) or type(right.value) not in _TYPES or op not in _OPERATORS:
            return {True, False}
        for start, stop, kind, operation, values in records.get(str(right.lineno), ()):
            if (start, stop, kind, operation) == (right.col_offset, right.end_col_offset, "compare", op):
                # The observer records the exact left builtin before the comparison.
                # No values means it never completed on any captured path.
                if not all(type(v) in _TYPES for v in values):
                    return {True, False}
                return {_OPERATORS[op](v, right.value) for v in values}
        return {True, False}

    reached = False

    def falls_through(stmts):
        nonlocal reached
        for stmt in stmts:
            if stmt is original:
                reached = True
            if isinstance(stmt, (ast.Return, ast.Raise)):
                return False
            if isinstance(stmt, ast.If):
                choices = outcomes(stmt.test)
                yes = True in choices and falls_through(stmt.body)
                no = False in choices and falls_through(stmt.orelse)
                if not (yes or no):
                    return False
        return True

    falls_through(owner.body)
    if reached:
        return False
    from .readers import baseline, SIGNATURE_FIELDS
    # A new name (notably super or __class__) can change even an enclosing
    # frame's closure before the target return executes.
    old_codes = baseline(path, old)
    new_codes = baseline(path, new)
    if old_codes.keys() != new_codes.keys():
        return False
    fields = (*SIGNATURE_FIELDS, "co_freevars", "co_cellvars")
    for key, previous in old_codes.items():
        current = new_codes[key]
        if len(previous) != len(current):
            return False
        for a, b in zip(previous, current):
            if any(getattr(a, name) != getattr(b, name) for name in fields):
                return False
    return True
