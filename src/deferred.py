"""Bound an edit to a single previously deferred callback in a pinned module."""
import dis
import inspect
import sys
import warnings
from functools import lru_cache
from types import CodeType


def _same_constant(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same_constant(a, b) for a, b in zip(left, right))
    return type(left) in {int, str, bytes, bool} and left == right


@lru_cache(32)
def compile_source(path: str, source: bytes):
    # Multiple independent proofs inspect the same immutable source pair.
    from .compiled import baseline_code
    frozen = baseline_code(path, source)
    if frozen is not None:
        return frozen
    with warnings.catch_warnings(record=True) as diagnostics:
        warnings.simplefilter("always")
        result = compile(source, path, "exec", dont_inherit=True)
    return result, not diagnostics


def warning_free(path: str, source: str) -> bool:
    try:
        return compile_source(path, source.encode("utf-8"))[1]
    except (Warning, SyntaxError):
        return False


def callback_change(before: CodeType, after: CodeType, *, site_lines=False, observed_body=False):
    """Return one callback only when every enclosing executable remains identical."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
        return None
    changed = []
    differing_offsets = set()

    def compare(old, new):
        # CPython code equality omits co_qualname and co_stacksize. Require
        # both explicitly before using it to compare the remaining fields.
        if old.co_qualname != new.co_qualname or old.co_stacksize != new.co_stacksize:
            return False
        old_children = [x for x in old.co_consts if isinstance(x, CodeType)]
        new_children = [x for x in new.co_consts if isinstance(x, CodeType)]
        if len(old_children) != len(new_children):
            return False
        if old == new:
            return all(compare(left, right) for left, right in zip(old_children, new_children))
        if old_children:
            def scrub(code):
                return code.replace(co_consts=tuple(None if isinstance(c, CodeType) else c for c in code.co_consts))
            # Preserve the positions and count of code constants, even if None
            # also occurs as an ordinary module constant.
            if (tuple(type(c) for c in old.co_consts) != tuple(type(c) for c in new.co_consts)
                    or old.co_qualname != new.co_qualname or scrub(old) != scrub(new)):
                return False
            return all(compare(left, right) for left, right in zip(old_children, new_children))
        if not observed_body and not (old.co_name == "<lambda>" or ".<locals>." in old.co_qualname):
            return False
        if old.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
            return False
        if not old.co_flags & inspect.CO_OPTIMIZED or not old.co_consts or not new.co_consts:
            return False
        if type(old.co_consts[0]) is not type(new.co_consts[0]) or old.co_consts[0] != new.co_consts[0]:
            return False  # a docstring can be read without executing the callback
        if (old.co_qualname != new.co_qualname or old.co_linetable != new.co_linetable
                or old.replace(co_code=new.co_code, co_consts=new.co_consts) != new):
            return False
        left, right = list(dis.get_instructions(old)), list(dis.get_instructions(new))
        if len(left) != len(right):
            return False
        # A test can publish a value that later tests read without calling
        # the callback. Keep explicit nonlocal writes and dynamic execution
        # outside this refinement.
        if set(old.co_names) & {"__import__", "import_module", "exec", "eval", "setattr", "delattr"}:
            return False
        if any(op.opname.startswith(("IMPORT_", "DELETE_")) or op.opname in
               {"STORE_GLOBAL", "STORE_DEREF", "STORE_NAME", "STORE_ATTR", "STORE_SUBSCR"}
               for op in left):
            return False
        saw_difference = False
        for a, b in zip(left, right):
            if a.opname != b.opname or a.offset != b.offset:
                return False
            if a.opname in {"LOAD_CONST", "LOAD_SMALL_INT", "RETURN_CONST"}:
                # Folding two equal constants can renumber later constants.
                # Compare typed constants, including nested keyword-name tuples.
                if _same_constant(a.argval, b.argval):
                    continue
                if type(a.argval) is not type(b.argval) or type(a.argval) not in {int, str}:
                    return False
                differing_offsets.add(a.offset)
                saw_difference = True
                continue
            if a.arg == b.arg and type(a.argval) is type(b.argval) and a.argval == b.argval:
                continue
            if a.opname == "COMPARE_OP":
                if a.argval == b.argval or a.arg & 16 != b.arg & 16:
                    return False  # preserve Boolean coercion mode (CPython 3.13)
            else:
                return False
            differing_offsets.add(a.offset)
            saw_difference = True
        if saw_difference:
            changed.append(old)
        return saw_difference

    if not compare(before, after) or len(changed) != 1:
        return None
    leaf = changed[0]
    key = (leaf.co_qualname, leaf.co_firstlineno)
    stack = [before]
    identities = 0
    other_lines = set()
    while stack:
        code = stack.pop()
        identities += (code.co_qualname, code.co_firstlineno) == key
        if code is not leaf:
            other_lines.update(line for _, _, line in code.co_lines() if line is not None)
        stack.extend(c for c in code.co_consts if isinstance(c, CodeType))
    lines = {line for _, _, line in leaf.co_lines() if line is not None}
    if identities == 1 and lines:
        exclusive = lines - other_lines
        if observed_body:
            reached = {line for start,end,line in leaf.co_lines()
                       if line is not None and any(start <= offset < end for offset in differing_offsets)}
            return (key, reached) if reached else None
        if site_lines:
            # A line-level hit overapproximates execution of a changed instruction.
            # Shared definition/class-construction lines cannot identify a caller.
            executed = {line for start,end,line in leaf.co_lines()
                        if any(start<=offset<end for offset in differing_offsets)}
            return key, lines, exclusive, executed if executed and executed<=exclusive else set()
        return key, lines, exclusive
    return None
