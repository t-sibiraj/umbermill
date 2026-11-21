"""Equivalent built-in operations observed in the original executor workload."""

import ast
import gzip
from functools import lru_cache
import hashlib
import json
import platform
import sys

from . import BASE_COMMIT
from .deferred import warning_free
from .module_cold import ROSTER_DIGEST


OBSERVATIONS_SHA256 = "7f2e68ae9c93e88542ea0bac3642f2c553d468807a0a5838a779b26eafa20402"


@lru_cache(maxsize=1)
def _sites(map_path):
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13) or sys.platform != "linux":
        return {}
    raw = (map_path / "operand-observations.json.gz").read_bytes()
    if hashlib.sha256(raw).hexdigest() != OBSERVATIONS_SHA256:
        return {}
    data = json.loads(gzip.decompress(raw))
    if data["base"] != BASE_COMMIT or data["roster"] != ROSTER_DIGEST:
        return {}
    return data["platforms"].get(platform.machine(), {})


def _changed_node(node, other, kind):
    if kind == "or":
        return (
            isinstance(node, ast.BinOp)
            and isinstance(other, ast.BinOp)
            and isinstance(node.op, ast.BitOr)
            and isinstance(other.op, ast.BitOr)
            and ast.dump(node.left) == ast.dump(other.right)
            and ast.dump(node.right) == ast.dump(other.left)
        )
    if kind == "slice":
        if not (isinstance(node, ast.Subscript) and isinstance(other, ast.Subscript)
                and isinstance(node.slice, ast.Slice) and isinstance(other.slice, ast.Slice)):
            return False
        step = other.slice.step
        if not (node.slice.step is None and isinstance(step, ast.Constant)
                and type(step.value) is int and step.value == 1):
            return False
        other.slice.step = None
        try:
            return ast.dump(node) == ast.dump(other)
        finally:
            other.slice.step = step
    return False


def equivalent(selector, path, change, original, modified):
    # The typed observation covers one existing operation with the same surrounding
    # function and expression. Source and compiler-metadata observers remain selected.
    if not path.startswith("sqlglot/executor/") or len(change.old_lines) != 1:
        return False
    try:
        value = _sites(selector._maps_path.parent).get(path)
    except (ValueError, TypeError, KeyError, OSError):
        return False
    if not value or value[0] != selector._source_hashes.get(path):
        return False
    line = next(iter(change.old_lines))
    locations = value[1].get(str(line))
    if not locations:
        return False
    left, right = original.splitlines(keepends=True), modified.splitlines(keepends=True)
    if len(left) != len(right) or left[:line-1] != right[:line-1] or left[line:] != right[line:]:
        return False
    if not (warning_free(path, original) and warning_free(path, modified)):
        return False
    before, after = ast.parse(original), ast.parse(modified)
    pairs = [(before, after)]
    changes = 0
    while pairs:
        old, new = pairs.pop()
        if type(old) is not type(new):
            return False
        if isinstance(old, ast.AST):
            if (getattr(old, "lineno", None) == line and getattr(old, "end_lineno", None) == line
                    and getattr(new, "lineno", None) == line and getattr(new, "end_lineno", None) == line):
                for start, end, kind in locations:
                    if old.col_offset == start and old.end_col_offset == end and _changed_node(old, new, kind):
                        changes += 1
                        break
                else:
                    pairs.extend((getattr(old, key), getattr(new, key)) for key in old._fields)
                continue
            pairs.extend((getattr(old, key), getattr(new, key)) for key in old._fields)
        elif isinstance(old, list) and len(old) == len(new):
            pairs.extend(zip(old, new))
        elif old != new:
            return False
    return changes == 1
