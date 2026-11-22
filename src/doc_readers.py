"""Retain baseline tests that inspect runtime documentation after a docstring edit."""

import ast
from functools import lru_cache

from .patches import UnsupportedPatch, apply_change, parse_patch


@lru_cache(maxsize=64)
def documents(source):
    result = []
    pending = [("", ast.parse(source))]
    while pending:
        prefix, node = pending.pop()
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = getattr(node, "name", "")
            prefix = ".".join(filter(None, (prefix, name)))
            value = ast.get_docstring(node, clean=False)
            if value is not None:
                result.append((prefix, value))
        pending.extend((prefix, child) for child in ast.iter_child_nodes(node))
    return tuple(result)


@lru_cache(maxsize=1)
def observer_files():
    try:
        from ._doc_manifest import OBSERVERS
    except ImportError:
        return ()
    return OBSERVERS


def source_doc_readers(selector, patch, nodeids):
    """Supply positive observer IDs; an empty result never justifies dropping another test."""
    files = observer_files()
    if not files or not hasattr(selector, "_source"):
        return set()
    try:
        for change in parse_patch(patch):
            path = change.old_path
            if not path.startswith("sqlglot/") or not path.endswith(".py") or change.added or change.deleted:
                continue
            old = selector._source(path)
            if documents(old) != documents(apply_change(old, change)):
                break
        else:
            return set()
    except (UnsupportedPatch, OSError, SyntaxError, ValueError, RecursionError):
        return set()
    mask = 0
    for path in files:
        mask |= selector._files.get(path, 0) | selector._reads.get(path, 0) | selector._collection.get(path, 0)
    return {number for node, number in nodeids.items() if node.partition("::")[0] in files or mask >> number & 1}
