"""Narrow qualified literal class configuration edits with frozen read witnesses."""

import ast
import base64
import gzip
import hashlib
import json
from pathlib import Path
from .deferred import warning_free
from .fields import class_fields, literal_expression, roster_digest
from .patches import apply_change, parse_patch, UnsupportedPatch


def same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and ast.dump(a) == ast.dump(b))


def shape(x):
    if isinstance(x, dict):
        return ("dict", tuple((shape(k), shape(v)) for k, v in x.items()))
    if isinstance(x, (list, tuple)):
        return (type(x).__name__, tuple(map(shape, x)))
    return type(x).__name__


def immutable(x):
    if type(x) in (int, bool, str, bytes, float):
        return True
    return type(x) is tuple and all(map(immutable, x))


def outside_expression(source, node):
    lines = source.encode().splitlines(keepends=True)
    start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[: node.end_lineno - 1])) + node.end_col_offset
    raw = source.encode()
    return raw[:start], raw[end:]


def bindings(tree, symbol):
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == symbol
        ):
            sites.append(node)
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id == symbol
        ):
            sites.append(node)
        elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == symbol:
            sites.append(node)
    return sites


def pure_factory(source, tree, left, right):
    if not (
        isinstance(left, ast.Call)
        and isinstance(right, ast.Call)
        and isinstance(left.func, ast.Name)
        and same(left.func, right.func)
        and len(left.args) == len(right.args)
        and [k.arg for k in left.keywords] == [k.arg for k in right.keywords]
    ):
        return False
    for a, b in zip(
        [*left.args, *(k.value for k in left.keywords)],
        [*right.args, *(k.value for k in right.keywords)],
    ):
        av, bv = literal_expression(a), literal_expression(b)
        if not immutable(av) or not immutable(bv) or shape(av) != shape(bv):
            return False
    name = left.func.id
    if len(bindings(tree, name)) != 1:
        return False
    imports = [
        (n.module, a.name)
        for n in tree.body
        if isinstance(n, ast.ImportFrom) and not n.level and n.module
        for a in n.names
        if (a.asname or a.name) == name
    ]
    modules = []
    if not imports:
        modules = [(tree, name)]
    elif len(imports) == 1 and imports[0][0].startswith("sqlglot."):
        mod, func = imports[0]
        path = mod.replace(".", "/") + ".py"
        try:
            modules = [(ast.parse(source(path)), func)]
        except (ValueError, OSError, UnicodeError):
            return False
    else:
        return False
    m, symbol = modules[0]
    defs = [n for n in m.body if isinstance(n, ast.FunctionDef) and n.name == symbol]
    # Source-defined closure constructor with no eager operations or decorators.
    if len(defs) != 1 or defs[0].decorator_list or bindings(m, symbol) != defs:
        return False
    body = defs[0].body
    if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        lam = body[0].value
        return not lam.args.defaults and not any(lam.args.kw_defaults)
    if not (
        len(body) == 2
        and isinstance(body[0], ast.FunctionDef)
        and isinstance(body[1], ast.Return)
        and isinstance(body[1].value, ast.Name)
        and body[1].value.id == body[0].name
    ):
        return False
    inner = body[0]
    if inner.decorator_list or inner.args.defaults or any(inner.args.kw_defaults) or inner.type_params:
        return False
    postponed = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in m.body
    )
    return postponed or (
        inner.returns is None
        and all(
            a.annotation is None
            for a in [*inner.args.posonlyargs, *inner.args.args, *inner.args.kwonlyargs]
        )
    )


def delta(path, old, new, source):
    left, right = ast.parse(old, type_comments=True), ast.parse(new, type_comments=True)
    a, b = class_fields(left), class_fields(right)
    if a.keys() != b.keys():
        return None
    changed = [k for k in a if not same(a[k], b[k])]
    if len(changed) != 1:
        return None
    owner, field = changed[0]
    x, y = a[changed[0]], b[changed[0]]
    # Source position effects from inserted/deleted lines remain on the broad rule.
    if x.lineno != y.lineno or x.end_lineno != y.end_lineno or old.count("\n") != new.count("\n"):
        return None
    if isinstance(x, ast.Dict) and isinstance(y, ast.Dict):
        if len(x.keys) != len(y.keys) or not all(same(m, n) for m, n in zip(x.keys, y.keys)):
            return None
        diff = [i for i in range(len(x.keys)) if not same(x.values[i], y.values[i])]
        if len(diff) != 1:
            return None
        i = diff[0]
        key = x.keys[i]
        v = x.values[i]
        w = y.values[i]
        if not isinstance(key, ast.Constant) or type(key.value) is not str:
            return None
        l, r = literal_expression(v), literal_expression(w)
        if not (immutable(l) and immutable(r) and shape(l) == shape(r)) and not pure_factory(
            source, left, v, w
        ):
            return None
        if outside_expression(old, v) != outside_expression(new, w):
            return None
        key_value = key.value
    else:
        l, r = literal_expression(x), literal_expression(y)
        if not (immutable(l) and immutable(r) and shape(l) == shape(r)):
            return None
        if outside_expression(old, x) != outside_expression(new, y):
            return None
        # The original class body can use a literal via a bare name before class creation.
        if any(
            isinstance(n, ast.Name) and n.id == field and isinstance(n.ctx, ast.Load)
            for n in ast.walk(left)
        ):
            return None
        key_value = None

    class Replace(ast.NodeTransformer):
        def __init__(self, target):
            self.target = target

        def visit(self, n):
            return ast.Constant(None) if n is self.target else super().visit(n)

    if not same(Replace(x).visit(left), Replace(y).visit(right)):
        return None
    if not warning_free(path, old) or not warning_free(path, new):
        return None
    module = path[:-3].replace("/", ".").removesuffix(".__init__")
    return f"{module}:{owner}:{field}", key_value


class FieldSelector:
    def __init__(self, base, path, roster_file):
        self.base = base
        self.index = json.loads(gzip.decompress(path.read_bytes()))
        if (
            self.index["base_commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
            or roster_digest(json.loads(roster_file.read_bytes())["tests"])
            != self.index["roster_sha256"]
        ):
            raise ValueError("field roster mismatched")
        self.guards = sum(1 << k for k in self.index["guards"])

    def narrow(self, patch):
        changes = parse_patch(patch)
        if len(changes) != 1:
            return None
        c = changes[0]
        p = c.old_path
        if (
            c.new_path != p
            or c.added
            or c.deleted
            or not p.startswith("sqlglot/")
            or not p.endswith(".py")
        ):
            return None
        before = self.base._source(p)
        after = apply_change(before, c)
        diff = delta(p, before, after, self.base._source)
        if diff is None:
            return None
        name, key = diff
        if name.split(":", 1)[0] not in self.index["root_loaded"]:
            return None
        if key is None:
            if name not in self.index.get("eligible_fields", self.index["fields"]):
                return None
            entries = [
                self.index["fields"][name],
                self.index["reflection"].get(name.rsplit(":", 1)[0], ""),
            ]
        else:
            if name not in self.index["dicts"]:
                return None
            mapping = self.index["dicts"][name]
            entries = [mapping.get("*", ""), mapping.get("=" + key, "")]
            for other in self.index.get("derived", {}).get(name, []):
                entries.extend(
                    (
                        self.index["fields"].get(other, ""),
                        self.index["reflection"].get(other.rsplit(":", 1)[0], ""),
                    )
                )
        if "eager" in entries:
            return None
        readers = self.base._reader_index.changed(p, before, after) if self.base._reader_index else None
        if readers is None:
            deferred = self.base._deferred.get(p)
            if deferred is None:
                return None
            readers = self.base._reads.get(p, 0) | deferred[1]
        mask = self.guards | self.base._unmapped | readers
        for e in entries:
            mask |= int.from_bytes(base64.b64decode(e, validate=True), "little")
        return mask, name, key
