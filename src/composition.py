"""Use a whole baseline condition activation to bound a behavioral sibling in a qualified composition."""

import ast
import io
import tokenize

from .guards import decode_mask, one_line_change
from .patches import apply_change
from .deferred import warning_free


def connective_column(old, new):
    def tokens(line):
        return list(tokenize.generate_tokens(io.StringIO(line.lstrip(' ') + '\n').readline))

    if '\t' in old or '\t' in new:
        return None
    try:
        a, b = tokens(old), tokens(new)
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return None
    if len(a) != len(b):
        return None
    pairs = [(u, v) for u, v in zip(a, b) if (u.type, u.string) != (v.type, v.string)]
    if len(pairs) != 1 or {pairs[0][0].string, pairs[0][1].string} != {'and', 'or'}:
        return None
    # AST columns are UTF-8 byte offsets. Keep this narrow scanner on ASCII lines.
    if not old.isascii() or not new.isascii():
        return None
    return len(old) - len(old.lstrip(' ')) + pairs[0][0].start[1]


def qualified(selector, change, coarse):
    guards = getattr(selector, '_composition_guards', None)
    warm, cold = selector._reader_index, selector._isolated_index
    pair = one_line_change(change)
    if not (guards and warm and cold and pair):
        return None
    line, old_line, new_line = pair
    column = connective_column(old_line, new_line)
    if column is None:
        return None
    path = change.old_path
    before = selector._source(path)
    after = apply_change(before, change)
    if not (warning_free(path, before) and warning_free(path, after)):
        return None
    old = ast.parse(before, path, type_comments=True)
    new = ast.parse(after, path, type_comments=True)
    tests = []
    for tree in (old, new):
        found = [n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.BoolOp)
                 and (n.test.lineno, n.test.col_offset) <= (line, column) < (n.test.end_lineno, n.test.end_col_offset)]
        if len(found) != 1:
            return None
        tests.append(found[0])
    a, b = tests
    if a.test.lineno == a.test.end_lineno:
        return None
    if (a.test.lineno, a.test.col_offset) != (b.test.lineno, b.test.col_offset):
        return None
    if any(isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
                          ast.Yield, ast.YieldFrom, ast.Await)) for n in ast.walk(a.test)):
        return None
    b.test = a.test
    if ast.dump(old) != ast.dump(new):
        return None
    key = f'{path}|{a.test.lineno}|{a.test.col_offset}'
    encoded = guards.multipart_sites.get(key)
    if not isinstance(encoded, str):
        return None
    reach = decode_mask(encoded)
    actual = warm.changed(path, before, after, guard=False)
    independent = cold.changed(path, before, after)
    if actual is None or independent is None or reach.bit_length() > len(selector.all_tests):
        return None
    # Every actual predicate activation, opaque pool consumer, file executor,
    # raw/code observer, and ambiguous guard remains selected. Collection hits
    # within this predicate also remain, regardless of its recorded activation.
    coverage = selector._collection_sites.get(path, {})
    collected = 0
    for member in range(a.test.lineno, a.test.end_lineno + 1):
        collected |= coverage.get(member, 0)
    return (reach | actual | independent | collected | selector._files.get(path, 0) |
            selector._unmapped | ((guards.multipart_pool | guards.guards) & coarse))
