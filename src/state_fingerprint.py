"""Bounded, process-independent fingerprints for selected replay namespaces."""

import __future__
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import struct
import sys
import types

from . import BASE_COMMIT


FORMAT = 1
_META = {"__builtins__", "__loader__", "__spec__", "__file__", "__cached__", "__path__"}
_SCALARS = (type(None), bool, int, str, bytes, float, complex)


class Unclear(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(packed(value)).hexdigest()


def runtime():
    return [FORMAT, BASE_COMMIT, sys.version, sys.implementation.cache_tag, platform.machine(),
            importlib.util.MAGIC_NUMBER.hex(), sys.flags.optimize, os.environ.get("PYTHONHASHSEED"),
            hash("ordinary namespace fingerprint")]


def _symbol(obj):
    if isinstance(obj, type):
        return type.__getattribute__(obj, "__module__"), type.__getattribute__(obj, "__qualname__")
    return obj.__module__, obj.__qualname__


def _canonical(obj):
    module, name = _symbol(obj)
    current = sys.modules.get(module)
    if type(current) is not types.ModuleType or not isinstance(name, str):
        return None
    for part in name.split("."):
        if type(current) is not types.ModuleType and not isinstance(current, type):
            return None
        table = vars(current) if type(current) is types.ModuleType else type.__getattribute__(current, "__dict__")
        if part not in table:
            return None
        current = table[part]
    return (module, name) if current is obj else None


def _scalar(value):
    kind = type(value)
    if kind in (float, complex):
        components = [value] if kind is float else [value.real, value.imag]
        return [kind.__name__, "".join(struct.pack("!d", component).hex() for component in components)]
    if kind in (int, str, bytes):
        if (kind is int and value.bit_length() > 100000
                or kind in (str, bytes) and len(value) > 1500000):
            raise Unclear("large scalar")
        if kind is bytes:
            return ["bytes", len(value), hashlib.sha256(value).hexdigest()]
        if kind is str and len(value) > 256:
            return ["str-sha256", len(value), hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()]
    return [kind.__name__, value]


def _ordering(value):
    if type(value) in _SCALARS:
        return packed(_scalar(value))
    if isinstance(value, type):
        key = _canonical(value)
        if key:
            return packed(["type", *key])
    raise Unclear("unordered collection contains an opaque key")


class Encoder:
    def __init__(self, name, roots):
        self.name = name
        self.roots = [Path(p).resolve() for p in roots]
        self.seen = {}
        self.keep = []
        self.nodes = []
        self.edges = 0

    def filename(self, value):
        p = Path(value)
        for root in self.roots:
            if p.is_relative_to(root):
                return ["project", p.relative_to(root).as_posix()]
        return ["external", value]

    def index(self, value, depth=0):
        self.edges += 1
        if self.edges > 42000 or depth > 48:
            raise Unclear("namespace exceeds traversal budget")
        key = id(value)
        if key in self.seen:
            return self.seen[key]
        token = len(self.nodes)
        if token >= 10000:
            raise Unclear("namespace exceeds node budget")
        self.seen[key] = token
        self.keep.append(value)
        self.nodes.append(None)
        self.nodes[token] = self.describe(value, depth + 1)
        return token

    def describe(self, value, depth):
        def ref(obj):
            return self.index(obj, depth)

        kind = type(value)
        if kind in _SCALARS:
            return _scalar(value)
        if value is Ellipsis or value is NotImplemented:
            return ["singleton", type(value).__name__]
        if kind in (tuple, list, set, frozenset):
            if len(value) > 12000:
                raise Unclear("large collection")
            if kind in (set, frozenset):
                keyed = sorted(((_ordering(v), v) for v in value), key=lambda item: item[0])
                if len({k for k, _ in keyed}) != len(keyed):
                    raise Unclear("ambiguous unordered collection")
                items = [v for _, v in keyed]
            else:
                items = value
            return [kind.__name__, [ref(v) for v in items]]
        if kind is dict:
            if len(value) > 10000:
                raise Unclear("large mapping")
            return ["dict", [[ref(k), ref(v)] for k, v in value.items()]]
        if kind is types.ModuleType:
            if sys.modules.get(value.__name__) is not value:
                raise Unclear("uncanonical module")
            return ["module", value.__name__]
        if kind is types.CodeType:
            return ["code", self.filename(value.co_filename), value.co_name, value.co_qualname, value.co_firstlineno,
                    value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount, value.co_nlocals,
                    value.co_flags, value.co_stacksize, ref(value.co_code), ref(value.co_linetable),
                    ref(value.co_exceptiontable), list(value.co_names), list(value.co_varnames),
                    list(value.co_cellvars), list(value.co_freevars), [ref(v) for v in value.co_consts]]
        if kind is types.FunctionType:
            if value.__module__ != self.name:
                label = _canonical(value)
                if label:
                    return ["external-function", *label]
                raise Unclear("noncanonical imported callable")
            cells = []
            for cell in value.__closure__ or ():
                cells.append(ref(cell))
            return ["function", value.__module__, value.__name__, value.__qualname__, ref(value.__code__),
                    ref(value.__defaults__), ref(value.__kwdefaults__), ref(value.__annotations__),
                    ref(vars(value)), ref(value.__doc__), cells]
        if kind is types.CellType:
            try:
                return ["cell", ref(value.cell_contents)]
            except ValueError as error:
                if isinstance(error, Unclear):
                    raise
                return ["empty-cell"]
        if isinstance(value, type):
            origin, name = _symbol(value)
            if origin != self.name:
                label = _canonical(value)
                if not label:
                    raise Unclear("noncanonical imported type")
                return ["external-type", *label]
            if type(value) is not type:
                raise Unclear("custom metaclass")
            table = type.__getattribute__(value, "__dict__")
            return ["class", origin, name, [ref(v) for v in value.__bases__], [ref(v) for v in value.__mro__],
                    [ref(v) for v in type.__subclasses__(value)],
                    [[k, ref(v)] for k, v in table.items()]]
        if kind in (staticmethod, classmethod):
            return [kind.__name__, ref(value.__func__), ref(vars(value))]
        if kind is property:
            return ["property", ref(value.fget), ref(value.fset), ref(value.fdel), ref(value.__doc__)]
        if kind in (types.MemberDescriptorType, types.GetSetDescriptorType):
            return [kind.__name__, value.__name__, ref(value.__objclass__)]
        if kind is types.BuiltinFunctionType:
            label = _canonical(value)
            if label:
                return ["builtin", *label]
        if kind is __future__._Feature:
            return ["future", ref(value.optional), ref(value.mandatory), ref(value.compiler_flag)]
        raise Unclear(f"opaque value of type {kind.__name__}")

    def graph(self, values):
        roots = [[key, self.index(value)] for key, value in values]
        return digest([roots, self.nodes])


def fingerprint(module, roots):
    table = vars(module)
    name = module.__name__
    if sys.modules.get(name) is not module or not (name == "sqlglot" or name.startswith("sqlglot.")):
        raise Unclear("unexpected module")
    entries = [(k, v) for k, v in table.items() if k not in _META]
    try:
        return {"whole": Encoder(name, roots).graph(entries), "unclear": [], "owners": {}}
    except (Unclear, RecursionError, TypeError, OverflowError, ValueError) as exc:
        reason = str(exc)
    # A partial class fingerprint is diagnostic. It cannot certify the module.
    owners = {}
    for key, value in entries:
        if type(value) is type and value.__module__ == name and len(owners) < 8:
            try:
                owners[key] = Encoder(name, roots).graph([(key, value)])
            except (Unclear, RecursionError, TypeError, OverflowError, ValueError):
                owners[key] = None
    return {"whole": None, "unclear": [reason], "owners": owners}


def eligible(source):
    """Allow fresh imports only when all edited top-level declarations have an inspectable form."""
    tree = ast.parse(source)
    return (len(source) < 210000 and not any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in tree.body)
            and any(isinstance(n, ast.ClassDef) or isinstance(n, ast.Assign) for n in tree.body))
