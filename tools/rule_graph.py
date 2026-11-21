"""Preserve expression arguments, aliasing, parent links and annotated state across replay."""

import enum, hashlib, importlib, json
from functools import lru_cache

FIELDS = tuple(
    sorted(("args", "parent", "arg_key", "index", "comments", "_type", "_meta", "_hash"))
)


@lru_cache(maxsize=None)
def known_shape(cls):
    slots = {slot for parent in cls.__mro__ for slot in parent.__dict__.get("__slots__", ())}
    return not slots.difference(FIELDS)


@lru_cache(maxsize=None)
def graph_type(mod, name):
    if not mod.startswith("sqlglot.") or not all(
        part.isidentifier() and not part.startswith("_") for part in name.split(".")
    ):
        raise ValueError("non-SQLGlot type")
    obj = importlib.import_module(mod)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def freeze(roots):
    from sqlglot import exp

    objects, records, refs = [], [], {}

    def ref(x):
        if x is None or type(x) in (str, bytes, int, bool, float):
            if isinstance(x, bytes):
                raise TypeError("bytes outside expected graph")
            return x
        if isinstance(x, enum.Enum):
            return ["enum", type(x).__module__, type(x).__qualname__, x.name]
        if type(x) not in (dict, list) and not isinstance(x, exp.Expr):
            raise TypeError(type(x).__name__)
        ident = id(x)
        if ident not in refs:
            refs[ident] = len(objects)
            objects.append(x)
        return ["ref", refs[ident]]

    initial = [ref(x) for x in roots]
    for x in objects:
        if type(x) is dict:
            if not all(type(k) is str for k in x):
                raise TypeError("nonstring AST argument")
            record = ["dict", [[k, ref(v)] for k, v in x.items()]]
        elif type(x) is list:
            record = ["list", [ref(v) for v in x]]
        else:
            if getattr(x, "__dict__", {}):
                raise TypeError("extra instance state")
            cls = type(x)
            if not known_shape(cls):
                raise TypeError("unknown AST slots")
            record = [
                "expr",
                cls.__module__,
                cls.__qualname__,
                [ref(getattr(x, k)) for k in FIELDS],
            ]
        records.append(record)
    return [initial, records], objects


def thaw(graph):
    from sqlglot import exp

    roots, records = graph
    objects = []
    for rec in records:
        if rec[0] == "dict":
            obj = {}
        elif rec[0] == "list":
            obj = []
        else:
            cls = graph_type(rec[1], rec[2])
            if not issubclass(cls, exp.Expr):
                raise ValueError("invalid expression type")
            obj = object.__new__(cls)
        objects.append(obj)

    def val(v):
        if not isinstance(v, list):
            return v
        if v[0] == "ref":
            return objects[v[1]]
        cls = graph_type(v[1], v[2])
        if not issubclass(cls, enum.Enum):
            raise ValueError("invalid enum")
        return cls[v[3]]

    for obj, rec in zip(objects, records):
        if rec[0] == "dict":
            obj.update((k, val(v)) for k, v in rec[1])
        elif rec[0] == "list":
            obj.extend(val(v) for v in rec[1])
        else:
            for k, v in zip(FIELDS, rec[3]):
                setattr(obj, k, val(v))
    return [val(v) for v in roots], objects


def digest(input_expr, returned, initial):
    return hashlib.sha256(encode(freeze([input_expr, returned, *initial])[0])).hexdigest()
