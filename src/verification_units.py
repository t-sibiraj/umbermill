"""Ordered test prefixes for an isolated diagnostic run of one pytest item."""

import ast
import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import platform
import sys
from types import ModuleType

from .impact import _PINNED_FIXTURE_HELPERS_SHA, _paired_inputs, _project_sql_fixture
from .patches import apply_change, parse_patch
from . import BASE_COMMIT


def digest(data):
    return hashlib.sha256(data).hexdigest()


def evidence_file(maps, nodeid):
    return Path(maps) / "verification-units" / (digest(nodeid.encode()) + ".json.gz")


def runtime_identity():
    return [sys.implementation.name, list(sys.version_info[:3]), sys.platform, platform.machine()]


@lru_cache(maxsize=2)
def _manifest(maps):
    try:
        payload = json.loads((Path(maps) / "verification-units" / "index.json").read_bytes())
        if (payload["version"] == 2 and payload["base"] == BASE_COMMIT
                and payload["runtime"] == runtime_identity()):
            payload["readers"] = {p: frozenset(nodes) for p, nodes in payload["readers"].items()}
            return payload
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def eligible(nodeid, changed_paths, maps_dir=None):
    paths = set(changed_paths)
    if len(paths) != 1 or len(nodeid.split("::")) != 3:
        return False
    path, = paths
    if path == nodeid.split("::", 1)[0]:
        return True
    index = _manifest(maps_dir or "/app/maps")
    return nodeid in index.get("readers", {}).get(path, ())


def method_anchors(source, nodeid):
    """Source positions at statement, loop-pass and subtest boundaries."""
    path, clsname, name = nodeid.split("::")
    if not path.startswith("tests/") or not path.endswith(".py"):
        return None
    classes = [n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == clsname]
    if len(classes) != 1:
        return None
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(methods) != 1 or methods[0].decorator_list:
        return None
    method = methods[0]
    lines = {s.lineno for s in method.body}
    for n in ast.walk(method):
        if isinstance(n, ast.For):
            lines.add(n.body[0].lineno)
        if isinstance(n, ast.With) and any(
            isinstance(i.context_expr, ast.Call)
            and isinstance(i.context_expr.func, ast.Attribute)
            and i.context_expr.func.attr == "subTest" for i in n.items
        ):
            lines.add(n.body[0].lineno)
    return method, lines


def fixture_records(text, helper):
    lines = _project_sql_fixture(text)
    if helper == "load_sql_fixture_pairs":
        halves = _paired_inputs(lines)
        return list(zip(halves[::2], halves[1::2]))
    records = []
    start = 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            records.append("\n".join(lines[start:i + 1]))
            start = i + 1
    return records


def fixture_token(frame, value):
    """Identify a record actually yielded by the pinned SQL helpers."""
    code = frame.f_code
    if code.co_name not in {"load_sql_fixtures", "load_sql_fixture_pairs"}:
        return None
    # These two pinned generators return None on exhaustion and yield only
    # str or (metadata, SQL, expected). CPython can report the RESUME offset
    # following a yield to the legacy trace hook.
    filename = frame.f_locals.get("filename")
    if type(filename) is not str:
        return None
    if type(value) is not str and not (
        type(value) is tuple and len(value) == 3 and type(value[0]) is dict
        and all(type(k) is str and type(v) is str for k, v in value[0].items())
        and type(value[1]) is str and type(value[2]) is str
    ):
        return None
    encoded = json.dumps([code.co_name, filename, value], separators=(",", ":"), sort_keys=True)
    return "sql:" + digest(encoded.encode())


def fixture_limits(old, new):
    limits = {}
    for name in ("load_sql_fixtures", "load_sql_fixture_pairs"):
        before, after = fixture_records(old, name), fixture_records(new, name)
        # Shifting indices can affect case numbers, state and paired metadata.
        if len(before) != len(after):
            continue
        changed = [i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        if changed and changed[-1] + 1 < len(after):
            limits[name] = changed[-1] + 1
    return limits


def loop_prefix(before, after):
    """Match one edited literal case list, keeping every earlier iteration."""
    if len(before.body) != len(after.body):
        return None
    edits = [(a, b) for a, b in zip(before.body, after.body) if ast.dump(a) != ast.dump(b)]
    if len(edits) != 1:
        return None
    a, b = edits[0]
    if (not isinstance(a, ast.For) or not isinstance(b, ast.For)
            or not isinstance(a.iter, (ast.Tuple, ast.List)) or type(a.iter) is not type(b.iter)
            or ast.dump(a.target) != ast.dump(b.target)
            or [ast.dump(x) for x in a.body + a.orelse] != [ast.dump(x) for x in b.body + b.orelse]
            or len(a.iter.elts) != len(b.iter.elts)):
        return None
    for item in a.iter.elts + b.iter.elts:
        ast.literal_eval(item)
    different = [i for i, (x, y) in enumerate(zip(a.iter.elts, b.iter.elts))
                 if ast.dump(x) != ast.dump(y)]
    if not different or different[-1] + 1 >= len(b.iter.elts):
        return None
    # The loop header's line-event count can vary with multi-line literals.
    # A plain first statement has one entry per iteration. With/try cleanup
    # may revisit a line, so choose the subtest body's first plain statement.
    first = b.body[0]
    if isinstance(first, ast.With):
        first = first.body[0]
    if not isinstance(first, (ast.Assign, ast.Expr, ast.Assert)) or first.end_lineno != first.lineno:
        return None
    return first.lineno, different[-1] + 2


def edited_boundary(old, change, nodeid, method):
    prior = method_anchors(old, nodeid)
    if prior is None:
        return None
    before = prior[0]
    if (any(not before.lineno < line <= before.end_lineno for line in change.old_lines)
            or any(not before.lineno <= line <= before.end_lineno for line in change.inserted_after)):
        return None
    new_lines = set()
    for hunk in change.hunks:
        pos = hunk.new_start
        for line in hunk.lines:
            if line.startswith("+"):
                new_lines.add(pos)
            if line.startswith((" ", "+")):
                pos += 1
        # A pure deletion can change the value used on its next untouched line.
        if not any(line.startswith("+") for line in hunk.lines):
            return None
    if not new_lines or any(not method.lineno < n <= method.end_lineno for n in new_lines):
        return None
    boundary = loop_prefix(before, method)
    if boundary is not None:
        return boundary
    end = max(new_lines)
    later = next((s.lineno for s in method.body if s.lineno > end), None)
    return (later, 1) if later is not None else None


class Prefix:
    def __init__(self, root, nodeid, anchors=None, expected=None, boundary=None, fixture=None):
        self.root = root
        self.nodeid = nodeid
        path, cls, method = nodeid.split("::")
        self.filename = str(root / path)
        self.qualname = f"{cls}.{method}"
        self.anchors = anchors
        self.expected = expected
        self.boundary = boundary
        self.fixture = fixture
        self.partial = set()

    def install_fixture(self):
        helpers = sys.modules.get("tests.helpers")
        if not isinstance(helpers, ModuleType) or vars(helpers).get("__file__") != str(self.root / "tests/helpers.py"):
            return []
        relative, limits = self.fixture
        aliases = []

        def bounded(original, limit):
            def read(filename):
                iterator = original(filename)
                if filename != relative:
                    yield from iterator
                    return
                try:
                    for index, value in enumerate(iterator):
                        if index == limit:
                            self.partial.add(self.nodeid)
                            break
                        yield value
                finally:
                    iterator.close()
            return read

        for helper, limit in limits.items():
            original = vars(helpers).get(helper)
            if original is None:
                continue
            wrapper = bounded(original, limit)
            for module in tuple(sys.modules.values()):
                if type(module) is not ModuleType:
                    continue
                file = vars(module).get("__file__")
                if not isinstance(file, str) or not file.startswith(str(self.root / "tests") + "/"):
                    continue
                for key, value in tuple(vars(module).items()):
                    if value is original:
                        aliases.append((module, key, original))
                        setattr(module, key, wrapper)
        return aliases

    def tracer(self):
        import pytest

        count = 0
        active = True
        inside = False
        helper_file = str(self.root / "tests/helpers.py")
        include_records = self.expected is not None and any(isinstance(v, str) for v in self.expected)

        def visit(unit):
            nonlocal count, active
            if self.expected is not None:
                if unit != self.expected[count]:
                    active = False
                    return
                count += 1
                stop = count == len(self.expected)
            else:
                if unit == self.boundary[0]:
                    count += 1
                stop = count == self.boundary[1]
            if stop:
                active = False
                self.partial.add(self.nodeid)
                pytest.skip("ordered verification prefix complete")

        def step(frame, event, arg):
            nonlocal inside
            if event == "return":
                inside = False
            if active and event == "line" and frame.f_lineno in self.anchors:
                visit(frame.f_lineno)
            return step

        def fixture(frame, event, arg):
            if active and inside and event == "return":
                token = fixture_token(frame, arg)
                if token is not None:
                    visit(token)
            return fixture

        def dispatch(frame, event, arg):
            nonlocal inside
            if (active and event == "call" and frame.f_code.co_filename == self.filename
                    and frame.f_code.co_qualname == self.qualname):
                inside = True
                return step
            if (active and inside and include_records and event == "call"
                    and frame.f_code.co_filename == helper_file):
                return fixture
            return None

        return dispatch


def replay_plugin(base_root, patched_root, patch, nodeids, maps_dir):
    ids = tuple(nodeids)
    if len(ids) != 1 or len(ids[0].split("::")) != 3:
        return None
    base, tree = Path(base_root).resolve(), Path(patched_root).resolve()
    nodeid = ids[0]
    try:
        changes = parse_patch(patch)
        if len(changes) != 1:
            return None
        change = changes[0]
        path = change.old_path
        if change.new_path != path or change.added or change.deleted:
            return None
        if not path.startswith(("sqlglot/", "tests/")):
            return None
        old = (base / path).read_bytes().decode()
        new = (tree / path).read_bytes().decode()
        if apply_change(old, change) != new:
            return None
        test_path = nodeid.split("::", 1)[0]
        if not test_path.startswith("tests/") or ".." in Path(test_path).parts:
            return None
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            if digest((tree / "tests/helpers.py").read_bytes()) != _PINNED_FIXTURE_HELPERS_SHA:
                return None
            limits = fixture_limits(old, new)
            if not limits:
                return None
            plan = Prefix(tree, nodeid, fixture=(path.removeprefix("tests/fixtures/"), limits))
        else:
            source = (tree / test_path).read_bytes()
            scope = method_anchors(source, nodeid)
            if scope is None:
                return None
            method, anchors = scope
            if path == test_path:
                boundary = edited_boundary(old, change, nodeid, method)
                if boundary is None:
                    return None
                plan = Prefix(tree, nodeid, anchors=anchors | {boundary[0]}, boundary=boundary)
            elif path.startswith("sqlglot/") and path.endswith(".py") and change.old_lines:
                maps = maps_dir or "/app/maps"
                identity = _manifest(maps).get("records", {}).get(nodeid)
                if not identity:
                    return None
                raw = evidence_file(maps, nodeid).read_bytes()
                if digest(raw) != identity:
                    return None
                data = json.loads(gzip.decompress(raw))
                if (data["version"] != 2 or data["nodeid"] != nodeid
                        or data["runtime"] != runtime_identity() or data["base"] != BASE_COMMIT
                        or data["fixture_helper_digest"] != _PINNED_FIXTURE_HELPERS_SHA
                        or data["test_digest"] != digest(source)
                        or data["files"].get(path) != digest(old.encode())):
                    return None
                lines = change.old_lines | change.inserted_after | {i + 1 for i in change.inserted_after}
                hits = [data["last"][path].get(str(i), -1) for i in lines]
                last = max(hits)
                steps = data["steps"]
                valid_steps = all(
                    x in anchors if type(x) is int else
                    isinstance(x, str) and x.startswith("sql:") and len(x) == 68
                    for x in steps
                )
                if last < 0 or last + 1 >= len(steps) or not valid_steps:
                    return None
                plan = Prefix(tree, nodeid, anchors=anchors, expected=steps[:last + 2])
            else:
                return None
    except (OSError, UnicodeError, EOFError, ValueError, SyntaxError, LookupError, TypeError, RecursionError):
        return None

    import pytest

    class Plugin:
        partial = plan.partial

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_call(self, item):
            if item.nodeid != nodeid or sys.gettrace() is not None:
                return (yield)
            aliases = []
            try:
                if plan.fixture:
                    aliases = plan.install_fixture()
                    if aliases:
                        # A method may inspect a callable's identity even if it
                        # stops consuming records before the bounded suffix.
                        plan.partial.add(nodeid)
                else:
                    # Python can observe tracing independently of reaching the
                    # planned cutoff. Require an uninstrumented run for a pass.
                    plan.partial.add(nodeid)
                    sys.settrace(plan.tracer())
                return (yield)
            finally:
                if not plan.fixture:
                    sys.settrace(None)
                for module, key, value in reversed(aliases):
                    setattr(module, key, value)

    return Plugin()
