"""Discover literal-bearing class fields without consulting changes or answers."""

import ast
import hashlib
import json
from pathlib import Path


def roster_digest(rows):
    canonical = sorted((r["number"], r["id"], r["file"]) for r in rows)
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def literal_expression(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return None
    if type(value) in (int, bool, str, bytes, tuple, dict, frozenset, float, set, list):
        return value


def class_fields(tree):
    result = {}

    def walk(body, prefix):
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            qname = prefix + node.name
            for stmt in node.body:
                value = target = None
                if isinstance(stmt, ast.AnnAssign):
                    value, target = stmt.value, stmt.target
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    value, target = stmt.value, stmt.targets[0]
                if value is None or not isinstance(target, ast.Name):
                    continue
                actual = literal_expression(value)
                if actual is None and not (target.id.isupper() and isinstance(value, ast.Dict)):
                    continue
                if target.id.isupper() or (
                    type(actual) is dict
                    and actual
                    and all(type(k) is str and type(v) is bool for k, v in actual.items())
                ):
                    result[(qname, target.id)] = value
            walk(node.body, qname + ".")

    walk(tree.body, "")
    return result


def projection(stmt):
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Attribute)
        and isinstance(stmt.targets[0].value, ast.Name)
        and isinstance(stmt.value, ast.SetComp)
    ):
        return None
    dst = stmt.targets[0]
    comp = stmt.value
    if len(comp.generators) != 1:
        return None
    g = comp.generators[0]
    if not (
        isinstance(g.target, ast.Tuple)
        and len(g.target.elts) == 2
        and all(isinstance(t, ast.Name) for t in g.target.elts)
        and isinstance(comp.elt, ast.Name)
        and comp.elt.id == g.target.elts[0].id
        and len(g.ifs) == 1
        and isinstance(g.ifs[0], ast.Name)
        and g.ifs[0].id == g.target.elts[1].id
        and not g.is_async
        and isinstance(g.iter, ast.Call)
        and not g.iter.args
        and not g.iter.keywords
        and isinstance(g.iter.func, ast.Attribute)
        and g.iter.func.attr == "items"
        and isinstance(g.iter.func.value, ast.Attribute)
        and isinstance(g.iter.func.value.value, ast.Name)
        and g.iter.func.value.value.id == dst.value.id
    ):
        return None
    return dst, g


def inventory(root):
    found = {}
    for source in sorted((root / "sqlglot").rglob("*.py")):
        module = (
            source.relative_to(root)
            .with_suffix("")
            .as_posix()
            .replace("/", ".")
            .removesuffix(".__init__")
        )
        tree = ast.parse(source.read_bytes())
        for stmt in ast.walk(tree):
            projected = projection(stmt)
            if projected:
                found["$derived:" + projected[0].attr] = source.relative_to(root).as_posix()
        for qname, field in class_fields(tree):
            found[f"{module}:{qname}:{field}"] = source.relative_to(root).as_posix()
    return found
