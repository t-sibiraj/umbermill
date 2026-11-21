"""Match one changed if-condition in an otherwise byte-identical pinned module."""

import ast
import base64
import warnings
from .deferred import warning_free


def decode_mask(s):
    return int.from_bytes(base64.b64decode(s, validate=True), "little")


class GuardMasks:
    """Apply complete, pinned cross-platform guard and process observations."""

    def __init__(self, index, guards):
        self.guards = guards
        self.by_path = {}
        for site in index["sites"]:
            path, line, _ = site.rsplit("|", 2)
            self.by_path.setdefault(path, set()).add(int(line))
        self.sites = index["sites"]
        multipart = index.get("multipart", {})
        self.multipart_sites = multipart.get("sites", {})
        self.multipart_pool = 0
        for number in multipart.get("pool", []):
            if type(number) is not int or number < 0:
                raise ValueError("invalid multipart worker owner")
            self.multipart_pool |= 1 << number
        self.pool = 0
        for stats in index["stats"]:
            for n in stats["pool_users"]:
                if type(n) is not int or n < 0:
                    raise ValueError("unowned pool group")
                self.pool |= 1 << n

    def select(self, change, item, readers, collection, baseline):
        key, operation = item
        encoded = self.sites.get(key, {}).get("negate")
        if encoded is None:
            return None
        (line,) = change.old_lines
        return baseline & (decode_mask(encoded) | readers | self.pool | self.guards | collection.get(line, 0))


def one_line_change(change):
    if (
        change.old_path != change.new_path
        or not change.old_path.startswith("sqlglot/")
        or not change.old_path.endswith(".py")
        or len(change.old_lines) != 1
        or len(change.hunks) != 1
    ):
        return
    hunk = change.hunks[0]
    removed = [s[1:] for s in hunk.lines if s.startswith("-")]
    added = [s[1:] for s in hunk.lines if s.startswith("+")]
    if len(removed) != 1 or len(added) != 1:
        return
    (number,) = change.old_lines
    return number, removed[0], added[0]


def relative_guard(line):
    if "\t" in line or "#" in line:
        return
    trimmed = line.lstrip(" ")
    indent = len(line) - len(trimmed)
    if not trimmed.endswith(":"):
        return
    if trimmed.startswith("if "):
        statement = trimmed
        keyword_delta = 0
    elif trimmed.startswith("elif "):
        statement = trimmed[2:]
        keyword_delta = 2
    else:
        return
    raw = (statement + "\n    pass\n").encode()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        node = ast.parse(raw).body[0]
        compile(raw, "<guard>", "exec", dont_inherit=True)
    if not isinstance(node, ast.If):
        return
    expr = node.test
    start, end = expr.col_offset, expr.end_col_offset
    return indent, keyword_delta, indent + keyword_delta + start, expr, raw[:start], raw[end:]


def match_hunk(change):
    pair = one_line_change(change)
    if pair is None:
        return
    line, old, new = pair
    try:
        a, b = relative_guard(old), relative_guard(new)
    except (SyntaxError, Warning, RecursionError, ValueError):
        return
    if not a or not b or a[:3] != b[:3] or a[4:] != b[4:]:
        return
    x, y = a[3], b[3]
    if (
        isinstance(x, ast.BoolOp)
        and isinstance(y, ast.BoolOp)
        and len(x.values) == len(y.values) == 2
        and type(x.op) != type(y.op)
        and all(ast.dump(m) == ast.dump(n) for m, n in zip(x.values, y.values))
    ):
        op = "flip"
    elif (
        isinstance(x, ast.UnaryOp) and isinstance(x.op, ast.Not) and ast.dump(x.operand) == ast.dump(y)
    ) or (isinstance(y, ast.UnaryOp) and isinstance(y.op, ast.Not) and ast.dump(y.operand) == ast.dump(x)):
        op = "negate"
    else:
        return
    return f"{change.old_path}|{line}|{a[2]}", op
