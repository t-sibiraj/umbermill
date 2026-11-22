"""Capture original SQLGlot operand values in an ordered pytest prefix or a fresh item."""

import argparse
import ast
import builtins
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import platform
import sys


def identity_operand(node):
    if isinstance(node, ast.Name):
        return ("local", node.id)
    if isinstance(node, ast.Constant) and (node.value is None or type(node.value) is bool):
        return ("constant", node.value)
    return None


def bool_shape(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        part = bool_shape(node.operand)
        return ("not", part) if part else None
    if (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot))):
        left, right = identity_operand(node.left), identity_operand(node.comparators[0])
        if left and right:
            return ("is" if isinstance(node.ops[0], ast.Is) else "is-not", left, right)
    part = identity_operand(node)
    return ("boolean", part) if part else None


def integer(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            return -node.operand.value
    return None


def capture(base, roster_file, output, first, after, cold):
    raw_roster = json.loads(roster_file.read_bytes())["tests"]
    roster = [r["id"] for r in sorted(raw_roster, key=lambda r: r["number"])]
    wanted = set(roster[first:after])
    if not wanted or min(first, after) < 0 or (cold and len(wanted) != 1) or (not cold and first != 0):
        raise ValueError("expected a natural prefix or one cold item")
    node_numbers = {s: i for i, s in enumerate(roster)}
    selected = []
    completed = set()
    bad = set()
    process_users = set()
    current = None
    records, sites, sources = {}, {}, {}
    unavailable = object()

    def store(key, value, boolean=False):
        scope = -1 if current is None else current
        row = records.setdefault(key, {})
        if value is unavailable or row.get(scope, ()) is None:
            row[scope] = None
        elif boolean:
            row[scope] = row.get(scope, 0) | (1 << value)
        else:
            values = row.setdefault(scope, set())
            values.add(value)
            if len(values) > 64:
                row[scope] = None

    def number_probe(key, value):
        store(key, value if type(value) is int and value.bit_length() <= 80 else unavailable)
        return value

    def boolean_probe(key):
        frame = sys._getframe(1)
        if not frame.f_code.co_flags & 1:
            store(key, unavailable, True)
            return True
        local = frame.f_locals

        def evaluate(spec):
            tag = spec[0]
            if tag == "constant":
                return spec[1]
            if tag == "local":
                name = spec[1]
                code = frame.f_code
                if name not in code.co_varnames and name not in code.co_cellvars and name not in code.co_freevars:
                    return unavailable
                return local.get(name, unavailable)
            a = evaluate(spec[1])
            if a is unavailable:
                return a
            if tag in ("boolean", "not"):
                if type(a) is not bool:
                    return unavailable
                return not a if tag == "not" else a
            b = evaluate(spec[2])
            if b is unavailable:
                return b
            return (a is b) if tag == "is" else (a is not b)

        _, left, right = sites[key]
        a, b = evaluate(left), evaluate(right)
        store(key, unavailable if a is unavailable or b is unavailable else int(a) * 2 + int(b), True)
        return True

    def audited(event, args):
        if event in ("os.fork", "os.forkpty", "subprocess.Popen", "os.posix_spawn"):
            process_users.add(-1 if current is None else current)

    class Source(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = Path(self.get_filename(fullname)).resolve()
            relative = path.relative_to(base).as_posix()
            raw = path.read_bytes()
            sources[relative] = hashlib.sha256(raw).hexdigest()
            lines = [0]
            for line in raw.splitlines(keepends=True):
                lines.append(lines[-1] + len(line))
            tree = ast.parse(raw, relative)
            for node in ast.walk(tree):
                expr = node.test if isinstance(node, ast.If) else node
                if isinstance(node, ast.If) and isinstance(expr, ast.BoolOp) and len(expr.values) == 2:
                    left, right = map(bool_shape, expr.values)
                    if left and right:
                        key = f"{relative}|{expr.lineno}|{expr.col_offset}"
                        sites[key] = ("boolean", left, right)
                        hook = ast.Call(ast.Name("_umbermill_bool_probe", ast.Load()), [ast.Constant(key)], [])
                        node.test = ast.copy_location(ast.BoolOp(ast.And(), [hook, expr]), expr)
                if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                    continue
                op = type(node.ops[0]).__name__
                if op not in {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}:
                    continue
                left, right = node.left, node.comparators[0]
                a, b = integer(left), integer(right)
                if (a is None) == (b is None):
                    continue
                literal, dynamic = (left, right) if a is not None else (right, left)
                if literal.lineno != literal.end_lineno:
                    continue
                key = f"{relative}|{node.lineno}|{node.col_offset}"
                start = lines[literal.lineno - 1] + literal.col_offset
                end = lines[literal.end_lineno - 1] + literal.end_col_offset
                sites[key] = ("integer", start, end, integer(literal), op, a is not None)
                hook = ast.Call(ast.Name("_umbermill_number_probe", ast.Load()), [ast.Constant(key), dynamic], [])
                value = ast.copy_location(hook, dynamic)
                if a is not None:
                    node.comparators[0] = value
                else:
                    node.left = value
            return compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None:
                return None
            if not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                raise ValueError("unobserved production loader")
            spec.loader = Source(fullname, spec.origin)
            return spec

    import pytest
    if any(n == "sqlglot" or n.startswith("sqlglot.") for n in sys.modules):
        raise ValueError("production code imported before observer")
    builtins._umbermill_number_probe = number_probe
    builtins._umbermill_bool_probe = boolean_probe
    sys.addaudithook(audited)
    sys.meta_path.insert(0, Finder())
    os.chdir(base)
    sys.path.insert(0, str(base))

    class Plugin:
        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, items):
            numbered = [item for item in items if item.nodeid in node_numbers]
            expected = [roster[first]] if cold else roster
            if [item.nodeid for item in numbered] != expected:
                raise ValueError("unexpected roster or original order")
            if not cold:
                sentinel = numbered[after] if after < len(numbered) else None
                if sentinel is not None:
                    del items[items.index(sentinel) + 1:]
            selected.extend(item.nodeid for item in items if item.nodeid in wanted)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            nonlocal current
            current = node_numbers[item.nodeid] if item.nodeid in wanted else None
            try:
                return (yield)
            finally:
                current = None

        def pytest_runtest_logreport(self, report):
            if report.nodeid in wanted:
                if report.failed or report.skipped:
                    bad.add(report.nodeid)
                if report.when == "teardown" and report.passed:
                    completed.add(report.nodeid)

    files = [roster[first]] if cold else sorted({row["file"] for row in raw_roster})
    status = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", *files], plugins=[Plugin()])
    if status or bad or set(selected) != wanted or completed != wanted:
        raise ValueError("incomplete or failing capture")
    data = {}
    for key, contexts in records.items():
        good = {}
        for n in contexts:
            if n < 0 or any(owner <= n for owner in process_users):
                continue
            # Preserve every earlier value and attributed collection/teardown
            # value. A changed previous invocation may affect the target even
            # when its own original invocation returned an equivalent result.
            prefix = [v for owner, v in contexts.items() if owner <= n]
            if None in prefix:
                continue
            if sites[key][0] == "boolean":
                bits = 0
                for observed in prefix:
                    bits |= observed
                good[str(n)] = bits
            else:
                good[str(n)] = sorted(set.union(*prefix))
        if good:
            data[key] = [sites[key], good]
    result = dict(roster=hashlib.sha256("\0".join(roster).encode()).hexdigest(), cold=cold,
                  python=list(sys.version_info[:3]), arch=platform.machine(), selected=selected,
                  platform=sys.platform, sources=sources, data=data)
    output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("base", type=Path); p.add_argument("roster", type=Path); p.add_argument("output", type=Path)
    p.add_argument("first", type=int); p.add_argument("after", type=int); p.add_argument("--cold", action="store_true")
    a = p.parse_args()
    capture(a.base.resolve(), a.roster.resolve(), a.output.resolve(), a.first, a.after, a.cold)
