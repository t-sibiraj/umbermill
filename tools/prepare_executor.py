"""Populate the local-image executor cache exclusively from the pinned SQLGlot checkout."""

import ast
from contextlib import closing
import inspect
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import subprocess
import resource
import textwrap
import unittest

sys.path.insert(0, "/app")
from umbermill.executor_cache import ASSET, BASE, MODULES, configure, digest, identity, pack, unpack

# Only deterministic SQL operators and built-ins from pinned fixture queries qualify.
# A function introduced by a different query must run in the real engine.
FUNCTIONS = frozenset("""
abs and as avg between case cast ceil ceiling coalesce count date_part date_trunc day
dec decimal distinct else exists extract floor from grouping if ifnull in lower ltrim max
min month not nullif numeric on or order over rank round row_number rtrim stddev_samp
strftime substr substring sum then trim upper values varchar when where year
""".split())
WORDS = re.compile(r"\b(?:current_\w+|today|now|random|uuid\w*|nextval|currval|"
                   r"information_schema|pg_catalog|duckdb_\w+|sqlite_\w+|system|temp|"
                   r"tablesample|sample|collate)\b|\$|::reg", re.I)


def pure_query(sql, conn):
    if type(sql) is not str or not re.match(r"^\s*(select|with|\(\s*select)\b", sql, re.I):
        return False
    if WORDS.search(sql) or "--" in sql or "/*" in sql or ";" in sql:
        return False
    # Exclude external table strings, quoted function names, arbitrary functions and
    # table-valued functions. A declined query simply continues to native DuckDB.
    if re.search(r"\b(?:from|join)\s*['\"]|[\"']\s*\(", sql, re.I):
        return False
    calls = re.findall(r"\b([a-z_][\w]*)\s*\(", sql, re.I)
    if not {name.lower() for name in calls} <= FUNCTIONS:
        return False
    statements = conn.extract_statements(sql)
    return len(statements) == 1 and statements[0].type.name == "SELECT"


def equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return pack(left) == pack(right)
    from pandas.testing import assert_frame_equal

    try:
        assert_frame_equal(left, right, check_exact=True)
    except AssertionError:
        return False
    return True


class Capture:
    """Cross-check each eligible read against a fresh database in the identical state."""

    def __init__(self, store, group, conn, reference):
        self.store, self.group = store, group
        self.conn, self.reference = conn, reference
        self.sql = None
        self.active = True

    def execute(self, sql, *args, **kwargs):
        self.sql = None
        if self.active and not args and not kwargs and pure_query(sql, self.conn):
            self.sql = sql
            self.conn.execute(sql)
            return self
        # Any unclassified operation may change the state. Stay native thereafter.
        self.active = False
        return self.conn.execute(sql, *args, **kwargs)

    def _result(self, kind):
        value = getattr(self.conn, kind)()
        sql, self.sql = self.sql, None
        if sql is not None:
            second = getattr(self.reference.execute(sql), kind)()
            raw = pack(value)
            if equal(value, second) and equal(value, unpack(raw)):
                self.store.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?)",
                                   (self.group, sql, kind, raw))
        return value

    def df(self):
        return self._result("df")

    def fetchdf(self):
        return self._result("fetchdf")

    def fetchall(self):
        return self._result("fetchall")

    def __getattr__(self, name):
        self.active = False
        return getattr(self.conn, name)


def setup_executor(store, info):
    import duckdb
    from umbermill.executor_queries import raw_rows, record_optimizer
    from tests import test_executor as test

    class SetupRecorder:
        def __init__(self, connection):
            self.connection = connection
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)
            return self.connection.execute(sql)

        def close(self):
            self.connection.close()

    streams = []

    class Connections:
        @staticmethod
        def connect():
            wrapper = SetupRecorder(duckdb.connect())
            streams.append(wrapper)
            return wrapper

    cls = test.TestExecutor
    real = test.duckdb
    test.duckdb = Connections
    try:
        cls.setUpClass()
    finally:
        test.duckdb = real
    try:
        info["pairs"] = {"tpch": "optimizer/tpc-h/tpc-h.sql", "tpcds": "optimizer/tpc-ds/tpc-ds.sql"}
        info["setup"], info["tables"], info["row_digests"] = {}, {}, {}
        for group, spy in zip(("tpch", "tpcds"), streams, strict=True):
            assert spy is getattr(cls, group + "_conn")
            sqls = getattr(cls, group + "_sqls")
            tables = getattr(cls, group + "_tables")
            assert sqls == list(test.load_sql_fixture_pairs(info["pairs"][group]))
            info["tables"][group] = list(tables)
            info["row_digests"][group] = {}
            info["setup"][group] = spy.statements
            assert len(spy.statements) == len(tables)
            for name, table in tables.items():
                blob = pack((table.columns, table.rows))
                info["row_digests"][group][name] = hashlib.sha256(raw_rows(table)).hexdigest()
                columns, values = unpack(blob)
                assert (columns, values) == (table.columns, table.rows)
                # Keep list/tuple and scalar types bit-for-bit across the row round trip.
                assert pack((columns, values)) == blob
                store.execute("INSERT INTO rows VALUES(?,?,?)", (group, name, blob))
            with closing(duckdb.connect()) as reference:
                for ddl in spy.statements:
                    reference.execute(ddl)
                for args, original, optimized in sqls:
                    queries = (original, optimized) if group == "tpch" else (original,) if args.get("execute") else ()
                    for sql in queries:
                        try:
                            texts = test.transpile(sql, write="duckdb")
                            rendered = texts[0]
                            if len(texts) == 1:
                                store.execute("INSERT OR REPLACE INTO rendered VALUES(?,?)", (sql, rendered))
                            if group == "tpcds":
                                try:
                                    record_optimizer(store, test.parse_one(sql), test.TPCDS_SCHEMA)
                                except (ValueError, TypeError, RecursionError, AttributeError):
                                    pass
                            if pure_query(rendered, spy.connection):
                                # The query has already passed the purity gate. Each reads
                                # the same unchanged fixture state independently.
                                Capture(store, group, spy.connection, reference).execute(rendered).fetchdf()
                        except (duckdb.Error, ValueError):
                            continue
    finally:
        cls.tearDownClass()


def setup_optimizer(store):
    import duckdb
    from tests import test_optimizer as test

    cls = test.TestOptimizer
    # Limit the build rehearsal to native reference checks made by these methods.
    methods = []
    for name in unittest.defaultTestLoader.getTestCaseNames(cls):
        source = inspect.getsource(getattr(cls, name))
        if "self.conn.execute(" in source and "self.pool" not in source:
            methods.append(name)
    original = cls.setUpClass.__func__

    def setup(typ):
        original(typ)
        live = typ.conn
        # Obtain the literal initial DDL from the real pristine setUpClass.
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
        initial = [x.args[0].value for x in ast.walk(tree) if isinstance(x, ast.Call)
                   and isinstance(x.func, ast.Attribute) and x.func.attr == "execute"
                   and len(x.args) == 1 and isinstance(x.args[0], ast.Constant)]
        if len(initial) != 1:
            raise ValueError("unsupported optimizer setup")
        reference = duckdb.connect()
        reference.execute(initial[0])
        typ.addClassCleanup(reference.close)
        typ.addClassCleanup(live.close)
        typ.conn = Capture(store, "optimizer", live, reference)

    cls.setUpClass = classmethod(setup)
    try:
        # A new class lifecycle for each method also resets the query state.
        for name in methods:
            store.execute("SAVEPOINT method")
            result = unittest.TestResult()
            unittest.TestSuite([cls(name)]).run(result)
            if not result.wasSuccessful() or result.skipped or result.testsRun != 1:
                store.execute("ROLLBACK TO method")
            store.execute("RELEASE method")
    finally:
        cls.setUpClass = classmethod(original)
    return methods


def main():
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))
    sources = {BASE / p for p in (*MODULES, "tests/helpers.py", "sqlglot/executor/table.py")}
    for group in ("tpc-h", "tpc-ds"):
        paths = BASE / "tests/fixtures/optimizer" / group
        sources.update(paths.glob("*.csv.gz"))
        sources.add(paths / (group + ".sql"))
    info = {"version": 1, "identity": identity(),
            "sources": {str(p.relative_to(BASE)): digest(p) for p in sorted(sources)}}
    with tempfile.TemporaryDirectory(dir=ASSET.parent) as scratch:
        dest = Path(scratch) / "executor.sqlite"
        with closing(sqlite3.connect(dest)) as store:
            store.executescript("""
                CREATE TABLE rows(dataset TEXT, name TEXT, data BLOB,
                                  PRIMARY KEY(dataset,name)) WITHOUT ROWID;
                CREATE TABLE answers(dataset TEXT, sql TEXT, kind TEXT, data BLOB,
                                     PRIMARY KEY(dataset,sql,kind)) WITHOUT ROWID;
                CREATE TABLE metadata(data TEXT);
                CREATE TABLE rendered(input TEXT PRIMARY KEY, output TEXT) WITHOUT ROWID;
                CREATE TABLE optimized(inputs BLOB PRIMARY KEY, expression BLOB) WITHOUT ROWID;
            """)
            setup_executor(store, info)
            info["optimizer_methods"] = setup_optimizer(store)
            store.execute("INSERT INTO metadata VALUES(?)", (json.dumps(info),))
            store.commit()
        reports = []
        for mode in ("original", "cached"):
            report = Path(scratch) / (mode + ".json")
            subprocess.run([sys.executable, "-B", __file__, "--rehearse", mode, str(dest), str(report)],
                           check=True, timeout=1800, cwd=BASE)
            reports.append(json.loads(report.read_bytes()))
        info["costs"] = {}
        control, cached = reports
        for node, (signature, elapsed) in cached.items():
            if node in control and signature == control[node][0] and signature is not None:
                info["costs"][node] = round(elapsed, 5)
        with closing(sqlite3.connect(dest)) as store:
            store.execute("UPDATE metadata SET data=?", (json.dumps(info),))
            store.commit()
        os.chmod(dest, 0o644)
        dest.replace(ASSET)


def charged():
    return sum(item.ru_utime + item.ru_stime for item in
               (resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)))


def rehearse(mode, asset, output):
    import hashlib
    import importlib
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    with closing(sqlite3.connect(asset)) as store:
        info = json.loads(store.execute("SELECT data FROM metadata").fetchone()[0])
        report = {}

        class Observer(unittest.TestResult):
            def startTest(self, case):
                super().startTest(case)
                self.started = charged()
                self.events = hashlib.sha256()
                self.succeeded = False

            def addSuccess(self, case):
                super().addSuccess(case)
                self.succeeded = True

            def addSubTest(self, case, subtest, error):
                super().addSubTest(case, subtest, error)
                self.events.update(subtest.id().encode())
                if error is not None:
                    self.events.update(b"FAIL")

            def stopTest(self, case):
                super().stopTest(case)
                name = f"{file}::{case.__class__.__name__}::{case._testMethodName}"
                report[name] = [self.events.hexdigest() if self.succeeded else None, charged() - self.started]

        for file, klass in MODULES.items():
            module = importlib.import_module(file[:-3].replace("/", "."))
            cls = getattr(module, klass)
            if mode == "cached":
                configure(BASE, store, info, file)
            methods = (info["optimizer_methods"] if klass == "TestOptimizer" else
                       unittest.defaultTestLoader.getTestCaseNames(cls))
            before = charged()
            result = Observer()
            unittest.TestSuite(cls(name) for name in methods).run(result)
            nodes = [f"{file}::{klass}::{name}" for name in methods]
            if result.testsRun != len(methods) or any(not hasattr(case, "_testMethodName")
                    for case, _ in result.errors):
                for n in nodes:
                    report.pop(n, None)
            else:
                # A singleton pays its class lifecycle including pool startup/shutdown.
                setup_cost = max(0.0, charged() - before - sum(report[n][1] for n in nodes))
                for n in nodes:
                    report[n][1] += setup_cost
        Path(output).write_text(json.dumps(report))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--rehearse"]:
        rehearse(*sys.argv[2:])
    else:
        main()
