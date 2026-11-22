"""Replay typed, pure executor steps inside the real spawned TPC-DS workers."""

import ast
import builtins
from functools import lru_cache, partial
import hashlib
import inspect
import json
import math
import marshal
import os
import operator
from pathlib import Path
import pickle
import sqlite3
import textwrap
import time
import zlib

ASSET = Path("/app/maps/executor-steps.sqlite")
NODE = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
SOURCES = (
    "sqlglot/executor/python.py",
    "sqlglot/executor/table.py",
    "sqlglot/executor/env.py",
    "sqlglot/executor/context.py",
    "sqlglot/executor/__init__.py",
    "tests/test_executor.py",
)
PURE = frozenset(
    "ABS ADD AND BETWEEN BITWISEAND BITWISEOR BITWISEXOR COALESCE CONCAT CONCATWS "
    "DIV EQ GT GTE IF IN INT LEFT LENGTH LOWER LT LTE MOD MUL NEQ NOT OR ORDERED "
    "RIGHT SAFECONCAT STRPOSITION SUB SUBSTRING TRIM UPPER".split()
)
PRIMITIVES = (str, bytes, int, float, bool, type(None))
MIN_ROWS = 96


def upstream_only(path):
    """These edits can change the plan or compiled SQL, both observed before each lookup."""
    return (
        path
        in {
            "sqlglot/parser.py",
            "sqlglot/planner.py",
            "sqlglot/generator.py",
            "sqlglot/tokens.py",
            "sqlglot/tokenizer_core.py",
        }
        or path.startswith(
            ("sqlglot/optimizer/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/dialects/")
        )
    ) and path.endswith(".py")


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(value).digest()


def table_digest(rowsets):
    # Pickle preserves both table/row aliasing topology and the order and types of cells.
    # Scalar checks prevent user-defined reduction hooks, equality, or native methods from running.
    for rows in rowsets:
        if type(rows) is not list:
            return None
        for row in rows:
            if type(row) is not tuple or any(
                type(v) not in PRIMITIVES or type(v) is float and not math.isfinite(v) for v in row
            ):
                return None
    return fingerprint(pickle.dumps(rowsets, protocol=5))


def kernel_bindings():
    """Authenticate the actual evaluator callables, their closures and their bytecode."""
    from sqlglot.executor.env import ENV
    from types import CodeType, FunctionType

    def strip_path(code):
        constants = tuple(strip_path(v) if type(v) is CodeType else v for v in code.co_consts)
        return code.replace(co_filename="", co_consts=constants)

    def callable_shape(value):
        if type(value) is FunctionType:
            return [
                "function",
                value.__module__,
                value.__qualname__,
                fingerprint(marshal.dumps(strip_path(value.__code__))).hex(),
                [callable_shape(c.cell_contents) for c in value.__closure__ or ()],
                [callable_shape(x) for x in value.__defaults__ or ()],
            ]
        if type(value) in (tuple, list):
            return [type(value).__name__, *(callable_shape(x) for x in value)]
        if value is None or type(value) in (bool, int, str):
            return [type(value).__name__, value]
        if (
            getattr(value, "__module__", None) == "builtins"
            and getattr(builtins, getattr(value, "__name__", ""), None) is value
        ):
            return ["builtin", value.__name__]
        raise ValueError("unsupported native evaluator")

    return fingerprint(encode([(n, callable_shape(ENV[n])) for n in sorted(PURE)])).hex()


def create_tables(db):
    db.executescript(
        """
        CREATE TABLE steps (tag BLOB, inputs BLOB, action BLOB, ns INTEGER,
                            PRIMARY KEY(tag,inputs)) WITHOUT ROWID;
    """
    )


def save_step(db, key, action, started):
    elapsed = time.thread_time_ns() - started
    if elapsed < 400_000:
        return
    raw = zlib.compress(encode(action), 1)
    # Conflicting observations, including an earlier disqualification, stay excluded.
    db.execute(
        """INSERT INTO steps VALUES(?,?,?,?)
                  ON CONFLICT(tag,inputs) DO UPDATE SET
                  action=CASE WHEN action=excluded.action THEN action ELSE NULL END,
                  ns=MIN(ns,excluded.ns)""",
        (*key, raw, elapsed),
    )


def column(node):
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Subscript)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "scope"
        and isinstance(node.value.slice, ast.Constant)
        and type(node.value.slice.value) in (str, type(None))
        and isinstance(node.slice, ast.Constant)
        and type(node.slice.value) is str
    ):
        return node.value.slice.value, node.slice.value


@lru_cache(maxsize=512)
def inspect_code(code, predicate):
    if code is None:
        return (), (), None
    try:
        text = code.co_filename
        if len(text) > 10_000 or code != compile(text, text, "eval", optimize=2):
            return None
        root = ast.parse(text, mode="eval").body
        columns, calls = [], []

        def pure(node):
            ref = column(node)
            if ref is not None:
                columns.append(ref)
                return True
            if isinstance(node, ast.Constant):
                return type(node.value) in PRIMITIVES and (
                    type(node.value) is not float or math.isfinite(node.value)
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in PURE or node.keywords:
                    return False
                calls.append(node.func.id)
                return all(pure(argument) for argument in node.args)
            if isinstance(node, ast.Lambda):
                return not any(ast.iter_child_nodes(node.args)) and pure(node.body)
            if isinstance(node, ast.Compare):
                return all(
                    isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                    for op in node.ops
                ) and all(pure(arg) for arg in [node.left, *node.comparators])
            return False

        if predicate:
            return (tuple(columns), tuple(calls), None) if pure(root) else None
        ref = column(root)
        if ref is not None:
            return (ref,), (), ("column", ref)
        # Reuse the object from the live compiled program, including interned strings.
        if isinstance(root, ast.Constant) and pure(root):
            return (), (), ("constant", eval(code, {}))
    except (ValueError, TypeError, SyntaxError, RecursionError):
        pass
    return None


class StepStore:
    def __init__(self, name, mode):
        self.row_keys = {}
        self.mode = mode
        self.hit_receipt = (
            (Path(name).parent / f"replay-hit-{os.getpid()}") if mode == "rehearse" else None
        )
        from sqlglot.executor.env import ENV

        self.functions = {n: ENV[n] for n in PURE}
        if mode == "record":
            self.db = sqlite3.connect(
                Path(name) / f"steps-{os.getpid()}.sqlite", isolation_level=None
            )
            create_tables(self.db)
            self.tags = None
        else:
            self.db = sqlite3.connect(f"file:{name}?mode=ro&immutable=1", uri=True)
            self.tags = {row[0] for row in self.db.execute("SELECT DISTINCT tag FROM steps")}

    def key(self, signature, rows):
        tag = fingerprint(encode(signature))
        if self.tags is not None and tag not in self.tags:
            return None
        # Repeated scans of the same homogeneous immutable input need only
        # compare row identities. Retain a bounded number of actual lists to
        # preserve cross-table aliases and prevent object ID reuse.
        roots = tuple(map(id, rows))
        prior = self.row_keys.get(roots)
        if prior is not None:
            originals, snapshots, data = prior
            if all(
                live is old
                and len(live) == len(snapshot)
                and all(map(operator.is_, live, snapshot))
                for live, old, snapshot in zip(rows, originals, snapshots)
            ):
                return tag, data
        data = table_digest(rows)
        if data is None:
            return None
        if sum(map(len, rows)) <= 16_384:
            if len(self.row_keys) >= 6:
                self.row_keys.pop(next(iter(self.row_keys)))
            self.row_keys[roots] = rows, tuple(tuple(r) for r in rows), data
        return tag, data

    def action(self, key):
        row = self.db.execute("SELECT action FROM steps WHERE tag=? AND inputs=?", key).fetchone()
        if not row or row[0] is None:
            return None
        result = json.loads(zlib.decompress(row[0]))
        if self.hit_receipt is not None:
            self.hit_receipt.touch()
            self.hit_receipt = None
        return result

    def scan(self, step, context, iterator, condition, projections, sink):
        from sqlglot.executor.context import Context
        from sqlglot.executor.env import ENV
        from sqlglot.executor.table import RowReader, Table, TableIter

        if not (
            type(iterator) is TableIter
            and iterator.index == -1
            and type(iterator.table) is Table
            and type(context) is Context
            and type(sink) is Table
            and not sink.rows
        ):
            return None
        table = iterator.table
        if (
            len(table.rows) < MIN_ROWS
            or type(table.reader) is not RowReader
            or context.env["scope"] is not context.row_readers
            or "__builtins__" in context.env
            and context.env["__builtins__"] is not builtins.__dict__
            or any(reader is not table.reader for reader in context.row_readers.values())
        ):
            return None
        if type(step.offset) is not int or type(step.limit) not in (int, float):
            return None
        analyses = [
            inspect_code(condition, True),
            *(inspect_code(code, False) for code in projections),
        ]
        if any(item is None for item in analyses):
            return None
        if any(
            alias not in context.row_readers or field not in table.reader.columns
            for refs, _, _ in analyses
            for alias, field in refs
        ):
            return None
        if any(
            context.env.get(name) is not self.functions[name]
            for _, calls, _ in analyses
            for name in calls
        ):
            return None
        signature = [
            "scan",
            condition.co_filename if condition else None,
            [code.co_filename for code in projections],
            step.offset,
            repr(step.limit),
            "__builtins__" in context.env,
            list(context.row_readers),
            table.columns,
            list(table.reader.columns.items()),
            sink.columns,
        ]
        key = self.key(signature, (table.rows,))
        if key is None:
            return None
        columns = [projection for _, _, projection in analyses[1:]]
        return Scan(self, key, iterator, context, sink, columns)


class Scan:
    def __init__(self, store, key, iterator, context, sink, projections):
        self.store, self.key = store, key
        self.iterator, self.context, self.sink = iterator, context, sink
        self.projections = [
            (kind, iterator.table.reader.columns[value[1]] if kind == "column" else value)
            for kind, value in projections
        ]
        self.positions = []
        self.started = time.thread_time_ns() if store.mode == "record" else 0

    def record(self):
        save_step(
            self.store.db,
            self.key,
            [self.positions, self.iterator.index, "__builtins__" in self.context.env],
            self.started,
        )

    def restore(self):
        action = self.store.action(self.key)
        if action is None:
            return False
        positions, final, used_builtins = action
        rows = self.iterator.table.rows
        if self.projections:
            for pos in positions:
                row = rows[pos]
                self.sink.rows.append(
                    tuple(row[v] if kind == "column" else v for kind, v in self.projections)
                )
        else:
            self.sink.rows.extend(rows[pos] for pos in positions)
        if final >= 0 and rows:
            self.iterator.table.reader.row = rows[min(final, len(rows) - 1)]
        self.iterator.index = final
        if used_builtins:
            self.context.env.setdefault("__builtins__", builtins.__dict__)
        return True


def project_function(original, store):
    """Leave actual sink creation and patched expression generation before the cache point."""
    (node,) = ast.parse(textwrap.dedent(inspect.getsource(original))).body
    (index,) = [i for i, stmt in enumerate(node.body) if isinstance(stmt, ast.For)]
    loop = node.body[index]
    if not (
        isinstance(loop.iter, ast.Name)
        and loop.iter.id == "table_iter"
        and isinstance(loop.body[-1], ast.If)
        and isinstance(loop.body[-1].test, ast.Name)
        and loop.body[-1].test.id == "projections"
    ):
        raise ValueError("unknown projection loop")
    pre = ast.parse(
        "memo = step_store.scan(step, context, table_iter, condition, projections, sink)"
    ).body
    if store.mode == "record":
        capture = ast.parse("if memo is not None: memo.positions.append(table_iter.index)").body
        loop.body[-1:-1] = capture
        after = ast.parse("if memo is not None: memo.record()").body
        node.body[index : index + 1] = [*pre, loop, *after]
    else:
        early = ast.parse("if memo is not None and memo.restore(): return sink").body
        node.body[index:index] = [*pre, *early]
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = dict(original.__globals__, step_store=store)
    exec(compile(code, original.__code__.co_filename, "exec"), namespace)
    return namespace[node.name]


def set_function(original, store):
    from sqlglot import exp, planner
    from sqlglot.executor.context import Context
    from sqlglot.executor.table import Table

    def execute(self, step, context):
        if (
            type(step) is not planner.SetOperation
            or type(context) is not Context
            or step.op not in (exp.Intersect, exp.Except, exp.Union)
            or type(step.distinct) is not bool
            or type(step.limit) not in (int, float)
            or type(step.name) not in (str, type(None))
        ):
            return original(self, step, context)
        left, right = context.tables[step.left], context.tables[step.right]
        if (
            type(left) is not Table
            or type(right) is not Table
            or len(left.rows) + len(right.rows) < MIN_ROWS
        ):
            return original(self, step, context)
        key = store.key(
            [
                "set",
                step.op.__name__,
                step.distinct,
                step.name,
                repr(step.limit),
                left.columns,
                right.columns,
            ],
            (left.rows, right.rows),
        )
        if key is None:
            return original(self, step, context)
        if store.mode == "record":
            started = time.thread_time_ns()
            result = original(self, step, context)
            rows = result.tables[step.name].rows
            # Source references rather than row copies preserve identity for duplicate values.
            refs = {id(row): i for i, row in enumerate((*left.rows, *right.rows))}
            if all(id(row) in refs for row in rows):
                save_step(store.db, key, [refs[id(row)] for row in rows], started)
            return result
        positions = store.action(key)
        if positions is None:
            return original(self, step, context)
        table = self.table(left.columns)
        split = len(left.rows)
        table.rows = [left.rows[i] if i < split else right.rows[i - split] for i in positions]
        return self.context({step.name: table})

    return execute


def context_layout(context, codes, pure_calls, bindings):
    from sqlglot.executor.context import Context
    from sqlglot.executor.env import ENV
    from sqlglot.executor.table import RowReader, Table

    if type(context) is not Context or not context.tables or type(context.tables) is not dict:
        return None
    tables = list(context.tables.values())
    if any(
        type(t) is not Table
        or type(t.reader) is not RowReader
        or t.rows is not tables[0].rows
        or t.columns != tables[0].columns
        for t in tables
    ):
        return None
    if context._table is not None and context._table is not tables[0]:
        return None
    if (
        context.row_readers.keys() != context.tables.keys()
        or any(context.row_readers[n] is not t.reader for n, t in context.tables.items())
        or "__builtins__" in context.env
        and context.env["__builtins__"] is not builtins.__dict__
    ):
        return None
    for code in codes:
        result = inspect_code(code, pure_calls)
        if (
            result is None
            or any(
                n not in context.tables or c not in context.tables[n].reader.columns
                for n, c in result[0]
            )
            or any(context.env.get(n) is not bindings[n] for n in result[1])
        ):
            return None
    return (
        [(n, t.columns, list(t.reader.columns.items())) for n, t in context.tables.items()],
        [c.co_filename for c in codes],
    ), tables[0].rows


def sort_function(original, store):
    def sort(context, keys):
        if (
            not keys
            or not context.tables
            or len(next(iter(context.tables.values())).rows) < MIN_ROWS
        ):
            return original(context, keys)
        layout = context_layout(context, keys, True, store.functions)
        if layout is None:
            return original(context, keys)
        descriptor, rows = layout
        key = store.key(["sort", descriptor], (rows,))
        if key is None:
            return original(context, keys)
        if store.mode == "record":
            positions = {id(row): i for i, row in enumerate(rows)}
            started = time.thread_time_ns()
            result = original(context, keys)
            save_step(store.db, key, [positions[id(row)] for row in rows], started)
            return result
        permutation = store.action(key)
        if permutation is None:
            return original(context, keys)
        # CPython's sort evaluates keys in original order before it permutes this same list.
        original_last = rows[-1]
        context.table.rows[:] = [rows[i] for i in permutation]
        context.set_row(original_last)
        context.env.setdefault("__builtins__", builtins.__dict__)

    return sort


class Join:
    def __init__(self, store, key, executor, join, source, target, condition_context):
        self.store, self.key, self.executor, self.join = store, key, executor, join
        self.source, self.target = source, target
        self.condition_context = condition_context
        self.last = None
        self.matches = []
        self.started = time.thread_time_ns() if store.mode == "record" else 0

    def record(self):
        save_step(self.store.db, self.key, [self.matches, self.last], self.started)

    def restore(self):
        from sqlglot.executor.table import Table

        action = self.store.action(self.key)
        if action is None:
            return None
        matches, last = action
        src = self.source.table.rows
        rhs = self.target.table.rows
        for context, rows in ((self.source, src), (self.target, rhs)):
            context.env["scope"] = context.row_readers
            if rows:
                context.set_index(len(rows) - 1)
                context.env.setdefault("__builtins__", builtins.__dict__)
        table = Table(self.source.columns + self.target.columns)
        left, right = set(), set()
        final_row = None
        for i, j in matches:
            row = src[i] + rhs[j]
            table.append(row)
            if last is not None and i == last[0] and j == last[1]:
                final_row = row
            left.add(i)
            right.add(j)
        if self.condition_context is not None and last is not None:
            if final_row is None:
                final_row = src[last[0]] + rhs[last[1]]
            self.condition_context.set_row(final_row)
            self.condition_context.env.setdefault("__builtins__", builtins.__dict__)
        self.executor._append_unmatched_join_rows(table, self.join, src, rhs, left, right)
        return table


def hash_function(original, store):
    def prepare(executor, join, source, target, condition, ctx, src_keys, rhs_keys):
        from sqlglot.executor.context import Context
        from sqlglot.executor.env import ENV
        from sqlglot.executor.table import RowReader, Table

        if not src_keys or not rhs_keys:
            return None
        left = context_layout(source, src_keys, False, store.functions)
        right = context_layout(target, rhs_keys, False, store.functions)
        if left is None or right is None or len(left[1]) + len(right[1]) < MIN_ROWS:
            return None
        if join.get("side") not in ("", None, "LEFT", "RIGHT", "FULL", "INNER"):
            return None
        guard = None
        if condition is not None:
            evidence = inspect_code(condition, True)
            if (
                evidence is None
                or type(ctx) is not Context
                or ctx.env.get("scope") is not ctx.row_readers
                or "__builtins__" in ctx.env
                and ctx.env["__builtins__"] is not builtins.__dict__
                or ctx.row_readers.keys() != ctx.tables.keys()
                or any(
                    type(t) is not Table
                    or type(t.reader) is not RowReader
                    or ctx.row_readers[n] is not t.reader
                    for n, t in ctx.tables.items()
                )
                or any(
                    n not in ctx.tables or c not in ctx.tables[n].reader.columns
                    for n, c in evidence[0]
                )
                or any(ctx.env.get(name) is not store.functions[name] for name in evidence[1])
            ):
                return None
            guard = [
                condition.co_filename,
                [(n, list(t.reader.columns.items())) for n, t in ctx.tables.items()],
            ]
        elif ctx is not None:
            return None
        key = store.key(["hash", join.get("side"), left[0], right[0], guard], (left[1], right[1]))
        return Join(store, key, executor, join, source, target, ctx) if key is not None else None

    (node,) = ast.parse(textwrap.dedent(inspect.getsource(original))).body
    (start,) = [
        i
        for i, stmt in enumerate(node.body)
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.targets[0], ast.Name)
        and stmt.targets[0].id == "results"
    ]
    pre = ast.parse(
        "memo = step_join(self,join,source_context,join_context,condition,condition_context,source_key,join_key)"
    ).body
    if store.mode == "record":
        (match,) = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Call)
            and isinstance(n.test.func, ast.Attribute)
            and n.test.func.attr == "_join_matches"
        ]
        (loop,) = [n for n in ast.walk(node) if isinstance(n, ast.For) and match in n.body]
        mark = loop.body.index(match)
        loop.body[mark:mark] = ast.parse(
            "if memo is not None and condition is not None: memo.last = (source_index,join_index)"
        ).body
        match.body[:0] = ast.parse(
            "if memo is not None: memo.matches.append((source_index,join_index))"
        ).body
        node.body[-1:-1] = ast.parse("if memo is not None: memo.record()").body
    else:
        pre += ast.parse(
            "if memo is not None:\n    result = memo.restore()\n    if result is not None: return result"
        ).body
    node.body[start:start] = pre
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = dict(original.__globals__, step_join=prepare)
    exec(compile(code, original.__code__.co_filename, "exec"), namespace)
    return namespace[node.name]


def child_initializer(original, root, asset, mode, expected, schema, tables):
    original(schema, tables)
    from sqlglot.executor.python import PythonExecutor

    source = Path(inspect.getsourcefile(PythonExecutor)).resolve()
    if source != (Path(root) / "sqlglot/executor/python.py").resolve():
        return
    if kernel_bindings() != expected:
        return
    store = StepStore(asset, mode)
    PythonExecutor._project_and_filter = project_function(PythonExecutor._project_and_filter, store)
    PythonExecutor.set_operation = set_function(PythonExecutor.set_operation, store)
    PythonExecutor.hash_join = hash_function(PythonExecutor.hash_join, store)
    from sqlglot.executor.context import Context

    Context.sort = sort_function(Context.sort, store)


def enable_workers(module, tree, asset, mode, expected=None):
    expected = expected or kernel_bindings()
    original_pool = module.ProcessPoolExecutor
    original_initializer = module.initializer

    def pool(*args, **kwargs):
        if kwargs.get("initializer") is original_initializer:
            # Spawn pickles the original callback by its module and name. Keep that
            # binding intact and wrap only the argument supplied to the pool.
            kwargs["initializer"] = partial(
                child_initializer, original_initializer, str(tree), str(asset), mode, expected
            )
        return original_pool(*args, **kwargs)

    module.ProcessPoolExecutor = pool


@lru_cache(maxsize=1)
def metadata():
    if ASSET.is_file():
        from contextlib import closing

        try:
            with closing(sqlite3.connect(f"file:{ASSET}?mode=ro&immutable=1", uri=True)) as db:
                return json.loads(db.execute("SELECT data FROM metadata").fetchone()[0])
        except (sqlite3.Error, ValueError, TypeError):
            pass
    return {}


def install(tree, paths, members):
    if (
        tuple(members) != (NODE,)
        or len(paths) != 1
        or not upstream_only(paths[0])
        or os.getenv("PYTHONHASHSEED") != "0"
    ):
        return False
    info = metadata()
    if not info:
        return False
    if __package__:
        from .executor_cache import digest, identity
    else:
        from executor_cache import digest, identity
    if info.get("identity") != identity() or any(
        digest(tree / p) != h for p, h in info["sources"].items()
    ):
        return False
    from tests import test_executor

    enable_workers(test_executor, tree, ASSET, "replay", info["evaluators"])
    return True
