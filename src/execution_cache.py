"""Retain tests that can consume a cached result after its construction."""

import ast
import dis
from types import CodeType

from .deferred import compile_source


def _cache_name(name):
    value = name.lower().strip("_")
    if name in {"CACHE", "UNCACHE", "Cache", "Uncache"}:
        return False
    return (value in {"cache", "caches", "memo"} or value.startswith(("cache_", "cached_", "memo_"))
            or value.endswith(("_cache", "_caches", "_memo")))


def _frames(code):
    for item in code.co_consts:
        if isinstance(item, CodeType):
            yield item
            yield from _frames(item)


def _cache_index(selector, cold_mask):
    # Resolve by unqualified member name so calls through subclasses and
    # re-exports include every matching implementation. Run once per batch.
    dependencies = {}
    roots = []
    observing = {}
    for path in selector._source_hashes:
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            continue
        source = selector._source(path)
        compiled, _ = compile_source(path, source.encode())
        decorated = set()
        if "cache" in source.lower() or "memo" in source.lower():
            for node in ast.walk(ast.parse(source, path)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _cache_name(ast.unparse(decorator)) for decorator in node.decorator_list
                ):
                    decorated.add(node.name)
        for frame in _frames(compiled):
            dependencies.setdefault(frame.co_name, set()).update(frame.co_names)
            names = {n for n in frame.co_names if _cache_name(n)}
            if frame.co_name not in decorated and not names:
                continue
            if frame.co_name in decorated:
                # The wrapper itself serves a cached value without a source
                # line in the wrapped method. Retain its cold module users.
                mask = cold_mask(selector, path)
                if mask is None:
                    raise ValueError("missing cold observations for cached source")
            else:
                mask = 0
            roots.append((frame, names, mask))
            sites = selector._sites.get(path, {})
            collection = selector._collection_sites.get(path, {})
            by_name = {}
            for op in dis.get_instructions(frame):
                if (op.opname.startswith("LOAD_") and isinstance(op.argval, str)
                        and op.argval in names and op.positions.lineno):
                    by_name.setdefault(op.argval, set()).add(op.positions.lineno)
            for name, lines in by_name.items():
                readers = selector._isolated_index.executed_ranges(path, [(n, n) for n in lines])
                for line in lines:
                    readers |= sites.get(line, 0) | collection.get(line, 0)
                observing[name] = observing.get(name, 0) | readers
    readers = {}
    for frame, names, mask in roots:
        for name in names:
            mask |= observing.get(name, 0)
        pending = [frame.co_name, *frame.co_names]
        seen = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            readers[name] = readers.get(name, 0) | mask
            pending.extend(child for child in dependencies.get(name, ()) if child not in seen)
    return readers


def cached_consumers(selector, path, frames, cold_mask):
    if not hasattr(selector, "_execution_cache_readers"):
        selector._execution_cache_readers = _cache_index(selector, cold_mask)
    hidden = 0
    for before, after in frames:
        hidden |= selector._execution_cache_readers.get(before.co_name, 0)
        for code in (before, after):
            if any(op.opname in {"STORE_GLOBAL", "DELETE_GLOBAL", "STORE_DEREF", "DELETE_DEREF"}
                   for op in dis.get_instructions(code)):
                raise ValueError("unbounded shared binding can outlive the calling test")
            if set(code.co_names) & {"eval", "exec", "globals", "setattr", "delattr",
                                     "__import__", "import_module", "write_text", "write_bytes",
                                     "putenv", "chdir", "chroot", "unlink", "rename"}:
                raise ValueError("unbounded reflection or external effects")
    return hidden
