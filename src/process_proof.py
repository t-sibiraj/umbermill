"""Protect an original fresh-process assertion from unrelated dormant function edits."""

import ast
import platform
import sys
import warnings

from .map_cache import load_map
from . import BASE_COMMIT
from .guards import one_line_change
from .deferred import warning_free, callback_change, compile_source
from .patches import UnsupportedPatch, apply_change, parse_patch


# These constructs can affect a function object before the function is called.
_NESTED = (
    ast.Lambda,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.NamedExpr,
)


def inert_body_statement(old, new):
    indent = len(old) - len(old.lstrip())
    if (
        new[:indent] != old[:indent]
        or len(new) - len(new.lstrip()) != indent
        or new.rstrip().endswith(chr(92))
    ):
        return False
    # Compile in context so CPython's import-time SyntaxWarnings remain significant.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        before = ast.parse("def probe():\n    " + old[indent:]).body[0].body
        source = "def probe():\n    " + new[indent:]
        after = ast.parse(source).body[0].body
        compile(source, "<changed statement>", "exec")
    if len(before) != 1 or len(after) != 1:
        return False
    a, b = before[0], after[0]
    if not isinstance(a, (ast.Return, ast.Raise, ast.Assign)) or not isinstance(
        b, (ast.Return, ast.Raise, ast.Assign)
    ):
        return False
    targets = lambda n: (
        [ast.dump(t) for t in n.targets] if isinstance(n, ast.Assign) else []
    )
    if targets(a) != targets(b):
        return False
    for stmt in (a, b):
        for node in ast.walk(stmt):
            if (
                isinstance(node, _NESTED)
                or isinstance(node, ast.Name)
                and node.id in {"super", "__class__", "__debug__"}
            ):
                return False
    return True


class FreshProcess:
    def __init__(self, path, nodeids):
        data = load_map(path)
        if (
            data["base"] != BASE_COMMIT
            or data["python"] != list(sys.version_info[:2])
            or sys.implementation.name != "cpython"
            or platform.machine() not in data["machines"]
            or sys.platform != "linux"
        ):
            raise ValueError("fresh process observations do not match this interpreter")
        self.number = nodeids[data["node"]]
        self.files = {
            name: (digest, set(lines))
            for name, (digest, lines) in data["files"].items()
        }
        self.bodies = data.get("bodies", {})
        self.metadata = set(data.get("metadata", ()))
        self.prefix = sum(1 << nodeids[n] for n in data["prefix"])
        self.test_file = data["node"].split("::")[0]
        self.test_sha = data["test_sha"]

    def passing(self, selector, patch, selection):
        if self.number not in selection.tests:
            return False
        try:
            changes = parse_patch(patch)
            if len(changes) != 1:
                return False
            change = changes[0]
            path = change.old_path
            if (
                path not in self.files
                or selector._source_hashes.get(self.test_file) != self.test_sha
            ):
                return False
            pair = one_line_change(change)
            if pair is None:
                return False
            line, old, new = pair
            digest, eligible = self.files[path]
            if selector._source_hashes.get(path) != digest:
                return False
            bodies = self.bodies.get(path, {})
            length_site = (
                getattr(selector, "_length_sites", {}).get(path, {}).get(str(line))
                if path in self.metadata
                else None
            )
            if line not in eligible and str(line) not in bodies and not length_site:
                return False
            source = selector._source(path)
            if source.splitlines()[line - 1] != old:
                return False
            modified = apply_change(source, change)
            if not warning_free(path, modified):
                return False
            warm = (
                selector._reader_index.changed(path, source, modified, guard=False)
                if selector._reader_index
                else None
            )
            cold = (
                selector._isolated_index.changed(path, source, modified, guard=False)
                if selector._isolated_index
                else None
            )
            if warm is None or cold is None or ((warm | cold) & self.prefix):
                return False
            if length_site:
                from .length_boundary import equivalent

                if equivalent(path, source, modified, length_site):
                    return True
            if line in eligible and inert_body_statement(old, new):
                return True
            if str(line) not in bodies:
                return False
            match = callback_change(
                compile_source(path, source.encode())[0],
                compile_source(path, modified.encode())[0],
                observed_body=True,
            )
            if match is None:
                return False
            (name, first), changed_lines = match
            return all([name, first] in bodies.get(str(n), ()) for n in changed_lines)
        except (
            OSError,
            UnsupportedPatch,
            KeyError,
            ValueError,
            TypeError,
            Warning,
            SyntaxError,
            RecursionError,
        ):
            return False
