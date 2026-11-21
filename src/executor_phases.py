"""Use complete parent and spawned-worker execution for a deferred executor return."""

import ast
import gzip
import json
import platform
import sys

from . import BASE_COMMIT
from .deferred import warning_free
from .patches import UnsupportedPatch, apply_change, parse_patch
from .readers import SIGNATURE_FIELDS, baseline


def _same_return(path, old, new, line):
    original, patched = ast.parse(old), ast.parse(new)

    def expression(tree):
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
    after.value = before.value
    if ast.dump(original) != ast.dump(patched):
        return False
    if not warning_free(path, old) or not warning_free(path, new):
        return False
    first, second = baseline(path, old), baseline(path, new)
    if first.keys() != second.keys():
        return False
    # An unevaluated name can still affect frame and closure construction.
    fields = (*SIGNATURE_FIELDS, 'co_names', 'co_freevars', 'co_cellvars')
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
        with gzip.open(path, 'rt') as stream:
            data = json.load(stream)
        if (data['base'] == BASE_COMMIT and sys.implementation.name == 'cpython'
                and sys.version_info[:2] == (3, 13) and sys.platform == 'linux'):
            self.files = data['files']
            for name, paths in data['platforms'].get(platform.machine(), {}).items():
                if name in nodeids:
                    self.tests[nodeids[name]] = paths

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
            if (len(left) != len(right) or left[:pos] != right[:pos]
                    or left[pos + 1:] != right[pos + 1:] or not left[pos].lstrip().startswith('return ')
                    or not right[pos].lstrip().startswith('return ') or not _same_return(path, old, new, line)):
                return ()
            for index in (selector._reader_index, selector._isolated_index):
                if index is None or index.changed(path, old, new, guard=False) != 0:
                    return ()
            return candidates
        except (KeyError, IndexError, TypeError, ValueError, SyntaxError, RecursionError, UnsupportedPatch):
            return ()
