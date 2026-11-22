"""Discharge observed tests for an equivalent operand in both original contexts."""

import gzip
import hashlib
import json
import operator
import platform
import re
import sys

from . import BASE_COMMIT
from .guards import match_hunk
from .module_cold import ROSTER_DIGEST


_DIGEST = "5df3235e6e4244c292617672c7ce92e7ad212421fb87d3cfd732a45672582fff"
_COMPARE = {"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt,
            "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
_INTEGER = re.compile(rb"-?(?:0|[1-9][0-9]{0,30})")


def _load(selector):
    if sys.version_info[:3] != (3, 13, 18) or sys.platform != "linux":
        return {}
    file, ids = selector._cold_module_input
    roster = sorted(ids, key=ids.get)
    if (file is None or hashlib.sha256("\0".join(roster).encode()).hexdigest() != ROSTER_DIGEST
            or any(ids[node] != n for n, node in enumerate(roster))):
        return {}
    contents = file.with_name("paired-operands.json.gz").read_bytes()
    if hashlib.sha256(contents).hexdigest() != _DIGEST:
        return {}
    data = json.loads(gzip.decompress(contents))
    if data["base"] != BASE_COMMIT or data["roster"] != ROSTER_DIGEST:
        return {}
    rows = {}
    for key, payload in data["architectures"].get(platform.machine(), {}).items():
        path, line, _ = key.rsplit("|", 2)
        rows.setdefault((path, int(line)), []).append((key, *payload))
    return rows


def unchanged(selector, change, before, after):
    """Return only IDs with a complete agreeing positive observation in both contexts."""
    if len(change.old_lines) != 1 or change.inserted_after or change.old_path != change.new_path:
        return 0
    try:
        if not hasattr(selector, "_paired_operand_rows"):
            selector._paired_operand_rows = _load(selector)
        line, = change.old_lines
        rows = selector._paired_operand_rows.get((change.old_path, line), ())
        if not rows:
            return 0
        a, b = before.encode(), after.encode()
        for key, shape, samples in rows:
            if shape[0] == "boolean":
                if match_hunk(change) == (key, "flip"):
                    # TT and FF are unchanged for a flip. The recorder admits
                    # only resolved boolean locals and pure identity expressions.
                    return sum(1 << int(n) for n, seen in samples.items() if seen and not seen & 6)
                continue
            _, start, stop, old, operation, left = shape
            end = stop + len(b) - len(a)
            if end <= start or a[:start] != b[:start] or a[stop:] != b[end:]:
                continue
            text = b[start:end]
            if not _INTEGER.fullmatch(text):
                continue
            new = int(text)
            fn = _COMPARE[operation]
            def same(value):
                return fn(old, value) == fn(new, value) if left else fn(value, old) == fn(value, new)
            return sum(1 << int(n) for n, values in samples.items() if values and all(map(same, values)))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 0
    return 0
