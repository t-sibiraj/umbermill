"""Project observed text and code-object consumers onto a supplied source change."""

import base64
import gzip
import json
import types
from .deferred import compile_source
from .position import constant_key
from functools import lru_cache
from bisect import bisect_left

CODE_FIELDS = (
    "co_argcount",
    "co_posonlyargcount",
    "co_kwonlyargcount",
    "co_nlocals",
    "co_stacksize",
    "co_flags",
    "co_code",
    "co_names",
    "co_varnames",
    "co_filename",
    "co_name",
    "co_qualname",
    "co_firstlineno",
    "co_linetable",
    "co_exceptiontable",
    "co_freevars",
    "co_cellvars",
)
SIGNATURE_FIELDS = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_varnames", "co_flags")


def same_code(before, after):
    if any(getattr(before, k) != getattr(after, k) for k in CODE_FIELDS):
        return False
    if len(before.co_consts) != len(after.co_consts):
        return False
    for a, b in zip(before.co_consts, after.co_consts):
        if type(a) is not type(b):
            return False
        if isinstance(a, types.CodeType):
            if not same_code(a, b):
                return False
        elif constant_key(a) != constant_key(b):
            return False
    return True


def get_codes(code):
    found = {}
    stack = [code]
    while stack:
        now = stack.pop()
        found.setdefault((now.co_qualname, now.co_firstlineno), []).append(now)
        stack.extend(c for c in now.co_consts if isinstance(c, types.CodeType))
    return found


@lru_cache(128)
def baseline(path, source):
    return get_codes(compile_source(path, source.encode())[0])


def number(value, size):
    if value is None:
        return None
    bits = int.from_bytes(base64.b64decode(value, validate=True), "little")
    if bits.bit_length() > size:
        raise ValueError("reader index out of roster")
    return bits


class ReaderIndex:
    def __init__(self, path, commit, size):
        data = json.loads(gzip.decompress(path.read_bytes()))
        if data["base_commit"] != commit:
            raise ValueError("reader source identity mismatch")
        self.size = size
        self.paths = data["paths"]
        self.guard = number(data["guard"], size)
        self.position_guard_exceptions = data.get("position_guard_exceptions", {})
        self.operators = data.get("operators", {})
        self.lines = data.get("lines", {})
        self.untraced = data.get("untraced", {})
        self.absent_cold_sources = {p: number(v, size) for p, v in data.get("absent_cold_sources", {}).items()}

    def changed(self, path, before, after, *, guard=True):
        row = self.paths.get(path)
        if row is None or row["raw"] is None:
            return None
        result = number(row["raw"], self.size) | (self.guard if guard else 0)
        if not row["code"]:
            return result
        old = baseline(path, before)
        new = baseline(path, after)
        for name, line, kind, encoded in row["code"]:
            left = old.get((name, line), [])
            right = new.get((name, line), [])
            if len(left) == len(right) == 1:
                a, b = left[0], right[0]
                if kind == "signature":
                    if all(getattr(a, k) == getattr(b, k) for k in SIGNATURE_FIELDS):
                        continue
                elif kind == "opaque" and same_code(a, b):
                    continue
            readers = number(encoded, self.size)
            if readers is None:
                return None
            result |= readers
        return result

    def comparison_guards(self, path, site, op):
        encoded = self.operators.get(site)
        return number(self.untraced.get(path, ""), self.size) | (
            number(encoded[op], self.size) if encoded is not None else 0
        )

    def executed_ranges(self, path, ranges):
        result = 0
        for n, executed in self.lines.get(path, {}).items():
            for begin, end in ranges:
                i = bisect_left(executed, begin)
                if i < len(executed) and executed[i] <= end:
                    result |= 1 << int(n)
                    break
        return result
