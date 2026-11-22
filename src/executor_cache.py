"""Reuse pinned fixture data and external query answers inside a single patched test worker."""

import ast
from collections.abc import Mapping
import hashlib
import importlib
import json
import os
from pathlib import Path
import pickle
import platform
import sqlite3
import sys
import zlib


BASE = Path("/app/base")
ASSET = Path("/app/maps/executor-fixtures.sqlite")
MODULES = {"tests/test_executor.py": "TestExecutor", "tests/test_optimizer.py": "TestOptimizer"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity():
    from importlib.metadata import version

    return [sys.implementation.cache_tag, sys.version, platform.machine(),
            *(version(pkg) for pkg in ("duckdb", "pandas", "numpy")),
            *(os.environ.get(k) for k in ("TZ", "LC_ALL", "LANG", "SKIP_INTEGRATION"))]


def pack(value):
    return zlib.compress(pickle.dumps(value, protocol=5), 1)


def unpack(raw):
    return pickle.loads(zlib.decompress(raw))


class FixtureTables(Mapping):
    """Materialize named tables on demand. Spawned workers receive ordinary dictionaries."""

    def __init__(self, store, group, names, table_class):
        self.store, self.group = store, group
        self.names, self.table_class = tuple(names), table_class
        self.tables = {}

    def __len__(self):
        return len(self.names)

    def __iter__(self):
        return iter(self.names)

    def __getitem__(self, key):
        if key not in self.tables:
            row = self.store.execute(
                "SELECT data FROM rows WHERE dataset=? AND name=?", (self.group, key)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            columns, values = unpack(row[0])
            self.tables[key] = self.table_class(columns=columns, rows=values)
        return self.tables[key]

    def __reduce__(self):
        return dict, (), None, None, iter(self.items())


class FixtureDatabase:
    """Only the recorded, immutable initial state qualifies for answer reuse."""

    def __init__(self, store, group, open_connection, setup_sql):
        self.store, self.group = store, group
        self.open_connection, self.setup_sql = open_connection, setup_sql
        self.live = None
        self.pending = None
        self.closed = False

    def _open(self):
        if self.live is None:
            self.live = self.open_connection()
            if self.closed:
                self.live.close()
            else:
                for sql in self.setup_sql:
                    self.live.execute(sql)
        if self.pending is not None:
            sql, consumed = self.pending
            self.pending = None
            self.live.execute(sql)
            if consumed:
                getattr(self.live, consumed)()
        return self.live

    def execute(self, sql, *args, **kwargs):
        if self.live is None and not self.closed and type(sql) is str and not args and not kwargs:
            methods = self.store.execute(
                "SELECT kind FROM answers WHERE dataset=? AND sql=?", (self.group, sql)
            ).fetchall()
            if methods:
                self.pending = sql, None
                return self
        # On an unfamiliar statement the native connection takes over permanently.
        return self._open().execute(sql, *args, **kwargs)

    def _fetch(self, method, args, kwargs):
        if self.pending and self.pending[1] is None and not args and not kwargs:
            sql = self.pending[0]
            row = self.store.execute(
                "SELECT data FROM answers WHERE dataset=? AND sql=? AND kind=?",
                (self.group, sql, method),
            ).fetchone()
            if row is not None:
                result = unpack(row[0])
                self.pending = sql, method
                return result
        return getattr(self._open(), method)(*args, **kwargs)

    def df(self, *args, **kwargs):
        return self._fetch("df", args, kwargs)

    def fetchdf(self, *args, **kwargs):
        return self._fetch("fetchdf", args, kwargs)

    def fetchall(self, *args, **kwargs):
        return self._fetch("fetchall", args, kwargs)

    def close(self):
        self.closed = True
        if self.live is not None:
            self.live.close()
        self.pending = None

    def __getattr__(self, name):
        return getattr(self._open(), name)


def scalar_body_edit(original, changed):
    """Require a single literal/operator substitution inside an existing function."""
    old, new = ast.parse(original), ast.parse(changed)

    def differences(a, b, in_body=False):
        if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
            if type(a.value) is not type(b.value) or a.kind != b.kind:
                raise ValueError
            return int(repr(a.value) != repr(b.value)) if in_body or a.value == b.value else 2
        if type(a) is not type(b):
            families = (ast.boolop, ast.cmpop, ast.unaryop, ast.operator)
            if in_body and any(isinstance(a, family) and isinstance(b, family) for family in families):
                return 1
            raise ValueError
        if isinstance(a, ast.AST):
            return sum(differences(getattr(a, k), getattr(b, k), in_body or
                       (isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) and k == "body"))
                       for k in a._fields)
        if isinstance(a, list) and len(a) == len(b):
            return sum(differences(x, y, in_body) for x, y in zip(a, b))
        if a != b:
            raise ValueError
        return 0

    try:
        return differences(old, new) == 1
    except (ValueError, RecursionError):
        return False


def install_executor_cache(tree, changed_paths, member_nodeids):
    """Install only in a disposable worker with authoritative, complete patch paths."""
    if not ASSET.is_file() or len(changed_paths) != 1 or not member_nodeids:
        return False
    files = {n.split("::", 1)[0] for n in member_nodeids}
    if len(files) != 1 or not files <= MODULES.keys():
        return False
    path = changed_paths[0]
    if (not path.startswith("sqlglot/") or not path.endswith(".py") or ".." in Path(path).parts
            or path == "sqlglot/executor/table.py"):
        return False
    tree = Path(tree).resolve()
    if not scalar_body_edit((BASE / path).read_bytes(), (tree / path).read_bytes()):
        return False
    store = sqlite3.connect(f"file:{ASSET}?mode=ro&immutable=1", uri=True)
    keep = False
    try:
        meta = json.loads(store.execute("SELECT data FROM metadata").fetchone()[0])
        if meta.get("version") != 1 or meta["identity"] != identity():
            return False
        for relative, expected in meta["sources"].items():
            if not (tree / relative).samefile(BASE / relative) and digest(tree / relative) != expected:
                return False
        (file,) = files
        if any(node not in meta["costs"] for node in member_nodeids):
            return False
        configure(tree, store, meta, file)
        if file == "tests/test_executor.py":
            if __package__:
                from . import executor_steps
            else:
                import executor_steps
            executor_steps.install(tree, changed_paths, member_nodeids)
            if __package__:
                from . import executor_queries
            else:
                import executor_queries
            if (tuple(member_nodeids) == (executor_queries.METHOD,)
                    and path in executor_queries.OWNERS
                    and executor_queries.eligible(path, (BASE / path).read_bytes(), (tree / path).read_bytes())
                    and executor_queries.cost().get("identity") == meta["identity"]):
                executor_queries.install(importlib.import_module("tests.test_executor"), store, meta, ASSET)
        keep = True
        return True
    finally:
        if not keep:
            store.close()


def configure(tree, store, meta, file):
    module = importlib.import_module(file[:-3].replace("/", "."))
    if Path(module.__file__).resolve() != (tree / file).resolve():
        raise ValueError("incorrect test module origin")
    cls = getattr(module, MODULES[file])
    if file == "tests/test_executor.py":
        def setup(typ):
            for group in ("tpch", "tpcds"):
                tables = FixtureTables(store, group, meta["tables"][group], module.Table)
                ddl = [sql.replace(str(BASE / "tests/fixtures/"), str(tree / "tests/fixtures/"))
                       for sql in meta["setup"][group]]
                setattr(typ, group + "_tables", tables)
                setattr(typ, group + "_conn", FixtureDatabase(store, group, module.duckdb.connect, ddl))
                fixture_file = meta["pairs"][group]
                setattr(typ, group + "_sqls", list(module.load_sql_fixture_pairs(fixture_file)))
            typ.cache = {}
    else:
        original = cls.setUpClass.__func__

        def setup(typ):
            original(typ)
            connection = typ.conn
            # Original setup also initializes SQLGlot schema and the real process pool.
            typ.conn = FixtureDatabase(store, "optimizer", lambda: connection, ())
            typ.addClassCleanup(connection.close)
    cls.setUpClass = classmethod(setup)
