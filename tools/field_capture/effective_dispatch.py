"""Record effective parser table and generator-cache observations from a pristine roster.

From the pristine SQLGlot checkout, set PYTHONPATH to this directory and the checkout.
Set DISPATCH_ROSTER to the official tests.json and DISPATCH_OUTPUT to an empty destination
file. Run python -B -m pytest -p effective_dispatch -p no:cacheprovider -q tests.
The output is gzip JSON. No changes or expected outcomes are inputs to this observer.
"""
import ast
import atexit
import builtins
import enum
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import subprocess
from pathlib import Path
import sys
import threading

import pytest


ROOT = Path.cwd().resolve()
ROWS = json.loads(Path(os.environ["DISPATCH_ROSTER"]).read_bytes())["tests"]
IDS = {row["id"]: row["number"] for row in ROWS}
if sorted(IDS.values()) != list(range(len(ROWS))):
    raise ValueError("expected a dense roster")
# Bit above the roster represents activity outside a numbered method.
OUTSIDE = 1 << len(ROWS)
active = OUTSIDE
guard = 0
completed = set()
tables = {}
guarded_fields = {}
key_types = {}


def scoped(module):
    return module in ("sqlglot.generator", "sqlglot.parser") or module.startswith(
        ("sqlglot.generators.", "sqlglot.parsers."))


def stable(key):
    if type(key) is str:
        return "s:" + key
    if isinstance(key, type):
        return "c:" + key.__module__ + "." + key.__qualname__
    if isinstance(key, enum.Enum):
        return "e:" + type(key).__module__ + "." + type(key).__qualname__ + "." + key.name
    return "*"


class Observed(dict):
    def __init__(self, value, owner):
        super().__init__(value)
        self.events = tables.setdefault(owner, {})
        self.writes = guarded_fields.setdefault(owner, {})
        self.seen = {}

    def read(self, key):
        bit = active
        if self.seen.get(key) != bit:
            keyname = stable(key)
            self.events[keyname] = self.events.get(keyname, 0) | bit
            self.seen[key] = bit

    def event(self, name):
        self.events[name] = self.events.get(name, 0) | active

    def __getitem__(self, key):
        self.read(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.read(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.read(key)
        return super().__contains__(key)

    def __len__(self):
        self.event("#length")
        return super().__len__()

    def __iter__(self):
        self.event("#order")
        return super().__iter__()

    def __reversed__(self):
        self.event("#order")
        return super().__reversed__()

    def keys(self):
        self.event("#order")
        return super().keys()

    def write(self, key):
        name = stable(key)
        self.writes[name] = self.writes.get(name, 0) | active

    def __setitem__(self, key, value):
        self.write(key)
        return super().__setitem__(key, value)

    def __delitem__(self, key):
        self.write(key)
        return super().__delitem__(key)

    def pop(self, key, *args):
        self.read(key)
        self.write(key)
        return super().pop(key, *args)

    def setdefault(self, key, *args):
        self.read(key)
        self.write(key)
        return super().setdefault(key, *args)


def whole(method, writes=False):
    def observe(self, *args, **kwargs):
        self.event("*")
        if writes:
            self.writes["*"] = self.writes.get("*", 0) | active
        return method(self, *args, **kwargs)
    return observe


for method in ("items", "values", "copy", "__repr__", "__eq__", "__ne__", "__or__", "__ror__",
               "__reduce__", "__reduce_ex__", "popitem", "update", "clear", "__ior__"):
    setattr(Observed, method, whole(getattr(dict, method), method in ("popitem", "update", "clear", "__ior__")))


def declared(value, owner):
    if type(value) is not dict:
        raise ValueError("unexpected class dictionary")
    known = key_types.setdefault(owner, {})
    for key in value:
        if type(key) is not str:
            name = stable(key)
            if name != "*":
                alias = key.__name__ if isinstance(key, type) else name.rsplit(".", 1)[-1]
                if alias in known and known[alias] != name:
                    known[alias] = ""
                else:
                    known[alias] = name
    return Observed(value, owner)


def cached(value, cls):
    # Include dynamic test-defined subclasses and class declarations outside our catalog.
    # Attribute cached uses to each base so parent replacements retain descendants.
    names = [c.__module__ + "." + c.__qualname__ + ":@dispatch" for c in cls.__mro__ if scoped(c.__module__)]
    result = Observed(value, names[0])
    if len(names) > 1:
        # Record concrete names and their ancestry; the packer never guesses test-local subclasses.
        for name in names[1:]:
            ancestry.setdefault(name, set()).add(names[0])
    return result


ancestry = {}


def wrap(func, args, where):
    return ast.copy_location(ast.Call(ast.Name(func, ast.Load()), args, []), where)


class Rewrite(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_ClassDef(self, cls):
        for stmt in cls.body:
            target = stmt.target if isinstance(stmt, ast.AnnAssign) else (
                stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id.isupper() and isinstance(stmt.value, ast.Dict):
                name = self.module + "." + cls.name + ":" + target.id
                stmt.value = wrap("_record_declared_dispatch", [stmt.value, ast.Constant(name)], stmt.value)
        return cls

    def visit_FunctionDef(self, node):
        if self.module == "sqlglot.generator" and node.name == "_build_dispatch":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name) and stmt.value.id == "dispatch":
                    stmt.value = wrap("_record_cached_dispatch", [stmt.value, ast.Name("cls", ast.Load())], stmt.value)
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
                    call = stmt.value
                    if (isinstance(call.func, ast.Name) and call.func.id == "dict" and len(call.args) == 1
                            and isinstance(call.args[0], ast.Attribute) and call.args[0].attr == "TRANSFORMS"):
                        # This is a transparent cache copy. Observe the eventual effective lookups.
                        call.func = ast.Name("_record_cache_snapshot", ast.Load())
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tree = Rewrite(name).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not scoped(name):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            if Path(spec.origin).resolve().is_relative_to(ROOT):
                spec.loader = Loader(name, spec.origin)
                return spec
        raise ValueError("unexpected source of " + name)


tracked = {}
uncertain = set()
local = threading.local()
threads = {}


def audit(event, args):
    global guard
    if event in ("subprocess.Popen", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec", "os.system", "_posixsubprocess.fork_exec"):
        guard |= active
        if not getattr(local, "launch", False):
            uncertain.add(active)


def expect(call, pid):
    def run(*args, **kwargs):
        old = getattr(local, "launch", False)
        local.launch = True
        try:
            result = call(*args, **kwargs)
        finally:
            local.launch = old
        tracked.setdefault(active, set()).add(pid(result, args))
        return result
    return run


original_start = threading.Thread.start


def start_thread(thread, *args, **kwargs):
    global guard
    guard |= active
    threads.setdefault(active, []).append(thread)
    return original_start(thread, *args, **kwargs)


def instrument():
    if any(scoped(name) for name in sys.modules):
        raise ValueError("SQLGlot imported before observer installation")
    builtins._record_declared_dispatch = declared
    builtins._record_cached_dispatch = cached
    builtins._record_cache_snapshot = lambda value: {k: v for k, v in dict.items(value)}
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(audit)
    threading.Thread.start = start_thread


def child_summary():
    if os.getpid() != start_pid or active == OUTSIDE:
        return
    receipt = {"owner":str(active.bit_length()-1),
               "unobserved":bool(tracked or uncertain or any(t.is_alive() for t in threads.get(active,()))),
               "tables":{k:list(v) for k,v in tables.items() if v},
               "writes":{k:list(v) for k,v in guarded_fields.items() if v},
               "ancestry":{k:list(v) for k,v in ancestry.items()}}
    out = Path(os.environ["DISPATCH_RECEIPTS"]) / (str(start_pid) + ".json.gz")
    out.write_bytes(gzip.compress(json.dumps(receipt,separators=(",",":")).encode(),mtime=0))


def child_install():
    global active, start_pid
    owner = int(os.environ["DISPATCH_OWNER"])
    active = 1 << owner
    start_pid = os.getpid()
    instrument()
    atexit.register(child_summary)


def pytest_configure(config):
    instrument()
    if "DISPATCH_RECEIPTS" in os.environ:
        import multiprocessing.util as util
        subprocess.Popen.__init__ = expect(subprocess.Popen.__init__, lambda result,args:args[0].pid)
        if hasattr(util, "spawnv_passfds"):
            util.spawnv_passfds = expect(util.spawnv_passfds,lambda result,args:result)
        if hasattr(os, "posix_spawn"):
            os.posix_spawn = expect(os.posix_spawn,lambda result,args:result)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global active
    active = 1 << IDS[item.nodeid] if item.nodeid in IDS else OUTSIDE
    if active != OUTSIDE and "DISPATCH_RECEIPTS" in os.environ:
        os.environ["DISPATCH_OWNER"] = str(IDS[item.nodeid])
    try:
        yield
    finally:
        if any(t.is_alive() for t in threads.get(active, ())):
            uncertain.add(active)
        os.environ.pop("DISPATCH_OWNER",None)
        active = OUTSIDE


def pytest_runtest_logreport(report):
    global guard
    if report.nodeid not in IDS:
        return
    if report.when == "call" and report.passed:
        completed.add(IDS[report.nodeid])
    if report.failed or report.skipped:
        guard |= 1 << IDS[report.nodeid]


def pytest_sessionfinish(session, exitstatus):
    unknown = (OUTSIDE - 1) ^ sum(1 << n for n in completed)
    stamp = hashlib.sha256()
    for path in sorted((ROOT / "sqlglot").rglob("*.py")):
        stamp.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    roster = sorted((r["number"], r["id"], r["file"]) for r in ROWS)
    def pack(rows):
        return {k:{j:hex(mask) for j,mask in v.items()} for k,v in rows.items() if v}
    result = {"version":1, "source":stamp.hexdigest(),
              "roster":hashlib.sha256(json.dumps(roster,separators=(",",":")).encode()).hexdigest(),
              "count":len(ROWS), "guard":hex((guard | unknown) & (OUTSIDE - 1)), "passed":len(completed),
              "tables":pack(tables), "writes":pack(guarded_fields), "declared":sorted(key_types),
              "key_types":key_types, "ancestry":{k:sorted(v) for k,v in ancestry.items()}}
    if "DISPATCH_RECEIPTS" in os.environ:
        result["outside_spawn"] = bool(guard & OUTSIDE)
        result["child_audit"] = {str(n):{"pids":sorted(tracked.get(1<<n,())), "uncertain":1<<n in uncertain}
                                 for n in completed}
    raw = json.dumps(result,sort_keys=True,separators=(",",":")).encode()
    Path(os.environ["DISPATCH_OUTPUT"]).write_bytes(gzip.compress(raw,mtime=0))
