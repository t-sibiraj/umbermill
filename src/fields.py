"""Discover literal-bearing class fields without consulting changes or answers."""

import ast
import hashlib
import json


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
