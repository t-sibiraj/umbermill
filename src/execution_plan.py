"""Plan existing production-body edits from their first observable instructions."""

import dis
import inspect
import sys
from types import CodeType

from .deferred import compile_source
from .guards import decode_mask, match_hunk
from .impact import _test_hits, _valid_path
from .length_boundary import equivalent
from .module_cold import cold_mask
from .patches import UnsupportedPatch, apply_change, parse_patch
from .position import constant_key
from .readers import same_code
from .relation import one_relation


def _children(code):
    return {i: child for i, child in enumerate(code.co_consts) if isinstance(child, CodeType)}


def _outer(code, children):
    return code.replace(co_consts=tuple(None if i in children else value
                                       for i, value in enumerate(code.co_consts)))


def _body_changes(old, new):
    """Descend through unchanged makers of functions, classes and closures."""
    if same_code(old, new):
        return []
    left, right = _children(old), _children(new)
    if left.keys() != right.keys():
        return None
    if left and same_code(_outer(old, left), _outer(new, right)):
        changed = []
        for i in left:
            part = _body_changes(left[i], right[i])
            if part is None:
                return None
            changed.extend(part)
        return changed
    if not old.co_flags & inspect.CO_OPTIMIZED or old.co_name.startswith("__"):
        return None
    # The creating frame, defaults, signature, docstring and bound closure
    # can be observed without ever calling the function.
    layout = ("co_name", "co_qualname", "co_firstlineno", "co_flags", "co_varnames",
              "co_cellvars", "co_freevars", "co_argcount", "co_posonlyargcount",
              "co_kwonlyargcount")
    if any(getattr(old, field) != getattr(new, field) for field in layout):
        return None
    old_doc = old.co_consts[0] if old.co_consts else None
    new_doc = new.co_consts[0] if new.co_consts else None
    if isinstance(old_doc, str) or isinstance(new_doc, str):
        if old_doc != new_doc:
            return None
    if any(not same_code(child, right[i]) for i, child in left.items()):
        return None
    return [(old, new)]


def _lines(before, after):
    """Use changed operand instructions when the original control flow matches."""
    whole = {line for _, _, line in before.co_lines() if line is not None and line > 0}
    left, right = list(dis.get_instructions(before)), list(dis.get_instructions(after))
    if len(left) != len(right) or before.co_exceptiontable != after.co_exceptiontable:
        return whole
    lines = set()
    for a, b in zip(left, right):
        if a.opcode != b.opcode or a.offset != b.offset:
            return whole
        if a.opcode in dis.hasconst:
            if isinstance(a.argval, CodeType) and isinstance(b.argval, CodeType):
                equal = same_code(a.argval, b.argval)
            elif isinstance(a.argval, CodeType) or isinstance(b.argval, CodeType):
                equal = False
            else:
                equal = constant_key(a.argval) == constant_key(b.argval)
        else:
            equal = a.arg == b.arg and a.argval == b.argval
        if not equal:
            if a.opcode in dis.hasjump or a.positions.lineno is None:
                return whole
            # PEP 626 line events can be on a neighboring instruction for
            # multiline expressions. Keep the entire original source span.
            lines.update(range(a.positions.lineno, (a.positions.end_lineno or a.positions.lineno) + 1))
    return lines or whole


def _outcomes(selector, change, before, after):
    """A missing operand observation leaves ordinary instruction coverage intact."""
    if len(change.old_lines) != 1 or change.inserted_after:
        return None
    path = change.old_path
    line, = change.old_lines
    row = str(line)
    lengths = selector._length_sites.get(path, {}).get(row)
    if lengths and equivalent(path, before, after, lengths):
        return 0
    if lengths:
        from .literal_outcomes import affected

        changed = affected(selector, path, row, before, after, lengths)
        if changed is not None:
            return changed
    relation = one_relation(path, before, after, row,
                            selector._relation_tokens.get(path, {}).get(row))
    if relation is not None:
        key, op = relation
        _, _, column = key.split("|")
        observed = selector._relation_sites.get(path, {}).get(row, {}).get(column)
        if isinstance(observed, list) and len(observed) == 8 and isinstance(observed[op], str):
            return decode_mask(observed[op]) | selector._isolated_index.comparison_guards(path, key, op)
    guards = getattr(selector, "_composition_guards", None)
    item = match_hunk(change) if guards is not None else None
    if item:
        # The recorded reach set also covers a newly evaluated right operand
        # that raises, mutates state, or invokes a custom truth conversion.
        reach = guards.sites.get(item[0], {}).get("negate")
        if isinstance(reach, str):
            return decode_mask(reach) | guards.guards | guards.pool
    return None


def _file_plan(selector, change, *, allow_observed_operands):
    path = change.old_path
    if (change.added or change.deleted or path != change.new_path or not _valid_path(path)
            or not path.startswith("sqlglot/") or not path.endswith(".py")
            or path.endswith("/__init__.py") or path not in selector._sites):
        return None
    before = selector._source(path)
    after = apply_change(before, change)
    old, old_clean = compile_source(path, before.encode())
    new, new_clean = compile_source(path, after.encode())
    if not (old_clean and new_clean):
        return None
    frames = _body_changes(old, new)
    if frames is None:
        return None
    regular = selector._reader_index.changed(path, before, after, guard=False)
    isolated = selector._isolated_index.changed(path, before, after)
    if regular is None or isolated is None:
        return None
    observers = regular | isolated
    if before == after:
        return 0
    eager = selector._deferred.get(path)
    if eager is None:
        # Missing startup and between-test evidence cannot establish a new
        # exclusion. The ordinary selector can still use its existing proofs.
        return None
    if not frames:
        return observers
    coverage = selector._sites[path]
    collection = selector._collection_sites.get(path, {})
    reach = 0
    for previous, current in frames:
        if (previous.co_qualname, previous.co_firstlineno) in eager[0]:
            return None
        lines = _lines(previous, current)
        if not lines or any(collection.get(line) for line in lines):
            return None
        intervals = [(line, line) for line in lines]
        ordinary = 0
        for first, last in intervals:
            ordinary |= _test_hits(coverage, first, last)
        reach |= ordinary | selector._isolated_index.executed_ranges(path, intervals)
    can_project = allow_observed_operands and len(frames) == 1
    value_reach = _outcomes(selector, change, before, after) if can_project else None
    if value_reach is not None:
        if value_reach.bit_length() > len(selector.all_tests):
            return None
        reach = value_reach
    if can_project:
        from .observed_operands import unchanged

        reach &= ~unchanged(selector, change, before, after)
    return reach | observers | _cache_readers(selector, path, frames)


def _cache_readers(selector, path, frames):
    """Widen for lookup caches, memoized callers, and mutable global bindings."""
    from .execution_cache import cached_consumers

    return cached_consumers(selector, path, frames, cold_mask)


def changed_comparisons(selector, patch):
    """Preserve positive operand observations even when a broader plan takes over."""
    if patch.count("\n--- ") + patch.startswith("--- ") < 2:
        return set()
    result = 0
    try:
        changes = parse_patch(patch)
        for change in changes:
            path = change.old_path
            if (len(change.old_lines) != 1 or change.inserted_after
                    or path != change.new_path or not path.startswith("sqlglot/")):
                continue
            row = str(next(iter(change.old_lines)))
            candidates = selector._relation_sites.get(path, {}).get(row, {})
            if not candidates:
                continue
            old = selector._source(path)
            token_types = selector._relation_tokens.get(path, {}).get(row, {})
            matched = one_relation(path, old, apply_change(old, change), row, token_types)
            if matched is None:
                continue
            site, operator = matched
            column = site.rsplit("|", 1)[-1]
            observed = candidates.get(column)
            if isinstance(observed, list) and len(observed) == 8 and isinstance(observed[operator], str):
                mask = decode_mask(observed[operator])
                if mask.bit_length() <= len(selector.all_tests):
                    result |= mask
    except (OSError, ValueError, KeyError, TypeError, UnsupportedPatch, SyntaxError, RecursionError):
        # Keep positive observations already established for independent files.
        pass
    return {n for n in selector.all_tests if (result >> n) & 1} if result else set()


def select(selector, changes, patch):
    """Return the union of candidates, or defer the entire patch on uncertainty."""
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13)
            or selector.unavailable or not selector._reader_index or not selector._isolated_index
            or not changes or len({change.old_path for change in changes}) != len(changes)):
        return None
    try:
        from .process_state import uncertain_process_state
        from .registry_state import uncertain_registry_state

        if uncertain_registry_state(patch, selector) or uncertain_process_state(patch, selector):
            return None
        result = selector._unmapped
        for change in changes:
            mask = _file_plan(selector, change, allow_observed_operands=len(changes) == 1)
            if mask is None:
                return None
            result |= mask
        return {n for n in selector.all_tests if result & (1 << n)}
    except (OSError, ValueError, TypeError, KeyError, IndexError, SyntaxError, RecursionError):
        return None
