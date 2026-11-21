"""Use complete parent and spawned-worker execution for deferred executor expressions."""

import ast
import gzip
import hashlib
import json
import platform
import sys

from . import BASE_COMMIT
from .deferred import warning_free
from .patches import UnsupportedPatch, apply_change, parse_patch
from .readers import SIGNATURE_FIELDS, baseline

_ASSET_DIGEST = "887290cfd542edaa0ced21487cf0a6a038db0ca143b866377927c46fbe2848b4"


def _deferred_expression(path, old, new, line, handler=False):
    original, patched = ast.parse(old), ast.parse(new)

    def expression(tree):
        if handler:
            # Even a handler that didn't catch the exception evaluates its type.
            # Require the dispatch itself, including the entire type expression,
            # to be located on the original, unvisited line.
            nodes = [h for owner in ast.walk(tree) if isinstance(owner, ast.Try)
                     for h in owner.handlers if h.lineno == line]
            if len(nodes) != 1:
                return None
            clause = nodes[0]
            expected = clause.type
            if expected is None or expected.lineno != line or expected.end_lineno != line:
                return None
            names = expected.elts if isinstance(expected, ast.Tuple) else [expected]
            if not names or any(not isinstance(name, ast.Name) for name in names):
                return None
            return clause
        statements = [s for s in ast.walk(tree) if isinstance(s, ast.Return)
                      and s.lineno == s.end_lineno == line]
        if len(statements) != 1 or statements[0].value is None:
            return None
        stmt = statements[0]
        forbidden = (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda,
                     ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)
        return None if any(isinstance(n, forbidden) for n in ast.walk(stmt.value)) else stmt

    before, after = expression(original), expression(patched)
    if before is None or after is None:
        return False
    if handler:
        after.type = before.type
    else:
        after.value = before.value
    if ast.dump(original) != ast.dump(patched):
        return False
    if not warning_free(path, old) or not warning_free(path, new):
        return False
    first, second = baseline(path, old), baseline(path, new)
    if first.keys() != second.keys():
        return False
    # An unevaluated name can still affect frame and closure construction.
    fields = (*SIGNATURE_FIELDS, 'co_freevars', 'co_cellvars')
    if not handler:
        fields += ('co_names',)
    else:
        fields += ('co_stacksize',)
    for key, previous in first.items():
        current = second[key]
        if len(previous) != len(current):
            return False
        for a, b in zip(previous, current):
            if any(getattr(a, name) != getattr(b, name) for name in fields):
                return False
    return True


class ExecutorPhases:
    def __init__(self, path, nodeids):
        self.tests = {}
        self.files = {}
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _ASSET_DIGEST:
            return
        data = json.loads(gzip.decompress(raw))
        if (data['base'] == BASE_COMMIT and sys.implementation.name == 'cpython'
                and sys.version_info[:2] == (3, 13) and sys.platform == 'linux'):
            self.files = data['files']
            arch = platform.machine()
            for name, paths in data['platforms'].get(arch, {}).items():
                if name in nodeids:
                    self.tests[nodeids[name]] = paths
            extra = data.get('regular', {}).get(arch)
            if extra:
                lines = [frozenset(row) for row in extra['line_sets']]
                for name, refs in extra['tests'].items():
                    if name in nodeids:
                        self.tests[nodeids[name]] = {p: lines[i] for p, i in refs.items()}

    def passing(self, selector, patch, selection):
        if (not self.tests or selection.fallback
                or not patch.startswith('diff --git a/sqlglot/executor/')):
            return ()
        try:
            edits = parse_patch(patch)
            if len(edits) != 1:
                return ()
            change = edits[0]
            path = change.old_path
            if (path not in self.files or self.files[path] != selector._source_hashes.get(path)
                    or path != change.new_path or change.added or change.deleted
                    or len(change.old_lines) != 1):
                return ()
            line = next(iter(change.old_lines))
            candidates = [n for n, reached in self.tests.items() if n in selection.tests
                          and path in reached and line not in reached[path]]
            if not candidates:
                return ()
            old = selector._source(path)
            new = apply_change(old, change)
            left, right = old.splitlines(keepends=True), new.splitlines(keepends=True)
            pos = line - 1
            if len(left) != len(right) or not 0 <= pos < len(left):
                return ()
            old_line, new_line = left[pos].lstrip(), right[pos].lstrip()
            handler = old_line.startswith('except ') and new_line.startswith('except ')
            if (left[:pos] != right[:pos]
                    or left[pos + 1:] != right[pos + 1:]
                    or not (handler or old_line.startswith('return ') and new_line.startswith('return '))
                    or not _deferred_expression(path, old, new, line, handler)):
                return ()
            for index in (selector._reader_index, selector._isolated_index):
                if index is None or index.changed(path, old, new, guard=False) != 0:
                    return ()
            return candidates
        except (KeyError, IndexError, TypeError, ValueError, SyntaxError, RecursionError, UnsupportedPatch):
            return ()
