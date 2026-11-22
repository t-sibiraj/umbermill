"""Pristine import transformer and key, shape, cache and scalar-read recording.

Native operations bypassing Python overrides, borrowed unwrapped views, detached
children, and state escaping the instrumented process need conservative treatment.
"""
import ast
import builtins
import contextvars
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import threading
import uuid


current = contextvars.ContextVar("field_item", default=os.environ.get("FIELD_CAPTURE_ITEM", "<collection>"))
_seen = set()
_lock = threading.Lock()
_session = uuid.uuid4().hex


def key_id(key):
    if type(key) is str:
        return ["str", key]
    if isinstance(key, type):
        return ["class", type.__getattribute__(key, "__module__"), type.__getattribute__(key, "__qualname__")]
    return ["opaque", type(key).__module__, type(key).__qualname__]


def record(table, operation, key=None, found=None):
    row = [current.get(), table, operation, key_id(key) if key is not None else None, found]
    text = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
    identity = (os.getpid(), text)
    if identity in _seen:
        return
    with _lock:
        if identity not in _seen:
            filename = f"{_session}-{os.getpid()}.jsonl"
            with (Path(os.environ["FIELD_TRACE_DIR"]) / filename).open("a", encoding="utf8") as stream:
                stream.write(text + "\n")
            _seen.add(identity)


class Watched(dict):
    def __init__(self, initial, origins):
        super().__init__(initial)
        self.origins = frozenset(origins)

    def note(self, operation, key=None, present=None):
        for origin in self.origins:
            record(origin, operation, key, present)

    def __getitem__(self, key):
        try:
            value = super().__getitem__(key)
        except KeyError:
            self.note("lookup", key, False)
            raise
        self.note("lookup", key, True)
        return value

    def get(self, key, default=None):
        absent = object()
        value = super().get(key, absent)
        self.note("get", key, value is not absent)
        return default if value is absent else value

    def __contains__(self, key):
        present = super().__contains__(key)
        self.note("membership", key, present)
        return present

    def merged(self, other, result):
        if result is NotImplemented:
            return result
        tags = self.origins | (other.origins if isinstance(other, Watched) else frozenset())
        return Watched(result, tags)

    def __or__(self, other):
        self.note("union")
        return self.merged(other, super().__or__(other))

    def __ror__(self, other):
        self.note("reverse-union")
        return self.merged(other, super().__ror__(other))

    def __ior__(self, other):
        self.note("update")
        if isinstance(other, Watched):
            self.origins |= other.origins
        return super().__ior__(other)

    def copy(self):
        self.note("copy")
        return Watched(super().copy(), self.origins)

    def __len__(self):
        self.note("length")
        return super().__len__()

    def __iter__(self):
        self.note("iteration")
        return super().__iter__()

    def __reversed__(self):
        self.note("reverse-iteration")
        return super().__reversed__()

    def keys(self):
        self.note("keys-view")
        return super().keys()

    def items(self):
        self.note("items-view")
        return super().items()

    def values(self):
        self.note("values-view")
        return super().values()

    def __setitem__(self, key, value):
        self.note("write", key)
        return super().__setitem__(key, value)

    def __delitem__(self, key):
        self.note("delete", key)
        return super().__delitem__(key)

    def setdefault(self, key, *value):
        self.note("setdefault", key)
        return super().setdefault(key, *value)

    def pop(self, key, *value):
        self.note("pop", key)
        return super().pop(key, *value)

    def popitem(self):
        self.note("popitem")
        return super().popitem()

    def clear(self):
        self.note("clear")
        return super().clear()

    def update(self, *args, **kwargs):
        self.note("update")
        return super().update(*args, **kwargs)

    def __repr__(self):
        self.note("representation")
        return super().__repr__()

    def __reduce_ex__(self, protocol):
        self.note("serialized")
        return dict, (dict(self),)

    def __eq__(self, other):
        self.note("equality")
        return super().__eq__(other)

    def __ne__(self, other):
        self.note("inequality")
        return super().__ne__(other)


def _mapping(value, owner):
    if not isinstance(value, dict):
        return value
    if isinstance(value, Watched):
        value.origins |= {owner}
        return value
    return Watched(value, [owner])


def _cache(value, cls):
    return _mapping(value, cls.__module__ + ":" + cls.__qualname__ + ":TRANSFORMS")


def _read(instance, field):
    cls = instance if isinstance(instance, type) else type(instance)
    for typ in type.__getattribute__(cls, "__mro__"):
        if field in type.__getattribute__(typ, "__dict__"):
            mod = type.__getattribute__(typ, "__module__")
            if mod.startswith("sqlglot."):
                name = type.__getattribute__(typ, "__qualname__")
                record(mod + ":" + name + ":" + field, "scalar-or-container")
            break
    return getattr(instance, field)


def call(name, args, original):
    return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), args, []), original)


class Instrument(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module
        self.owners = []

    def visit_ClassDef(self, node):
        self.owners.append(node.name)
        for stmt in node.body:
            target = stmt.target if isinstance(stmt, ast.AnnAssign) else (
                stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None)
            if (isinstance(target, ast.Name) and target.id.isupper()
                    and isinstance(stmt.value, ast.Dict)):
                owner = self.module + ":" + ".".join(self.owners) + ":" + target.id
                stmt.value = call("_uf_watch_mapping", [stmt.value, ast.Constant(owner)], stmt.value)
        self.generic_visit(node)
        self.owners.pop()
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr.isupper() and isinstance(node.ctx, ast.Load):
            return call("_uf_watch_field", [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if self.module == "sqlglot.generator" and node.name == "_build_dispatch":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name):
                    stmt.value = call("_uf_watch_cache", [stmt.value, ast.Name("cls", ast.Load())], stmt.value)
        return node


class SourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        altered = Instrument(fullname).visit(tree)
        return compile(ast.fix_missing_locations(altered), self.path, "exec", dont_inherit=True)


class PristineFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        # Let pytest own its usual test-module assertion-rewriting loader.
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (not spec or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)
                or not Path(spec.origin).resolve().is_relative_to(self.root)):
            return None
        spec.loader = SourceLoader(fullname, spec.origin)
        return spec


def install():
    root = Path(os.environ["FIELD_CAPTURE_SOURCE"]).resolve()
    if any(isinstance(x, PristineFinder) for x in sys.meta_path):
        return
    builtins._uf_watch_mapping = _mapping
    builtins._uf_watch_cache = _cache
    builtins._uf_watch_field = _read
    sys.meta_path.insert(0, PristineFinder(root))
    record("<process>", "startup")
