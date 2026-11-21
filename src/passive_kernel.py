"""Use the original Linux syscall prefix to qualify known child-process guards."""

import base64
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import PurePosixPath
import platform
import sys
from . import BASE_COMMIT
from .module_cold import ROSTER_DIGEST

DIGEST = "007a8ad363af7aaf12b91d1743e1a146bcbd13baa5a8edb09871e672bead7866"


@lru_cache(1)
def evidence(impact):
    raw = impact.with_name("passive-kernel.json.gz").read_bytes()
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        return None
    d = json.loads(gzip.decompress(raw))
    if (
        d["base"] != BASE_COMMIT
        or d["roster"] != ROSTER_DIGEST
        or d["platform"] != platform.machine()
    ):
        return None
    return (
        frozenset(d["parents"]),
        frozenset(d["blocked"]),
        int.from_bytes(base64.b64decode(d["owner"], validate=True), "little"),
        (1 << (d["through"] + 1)) - 1,
    )


def discharge(selector, atlas, old, new):
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or not atlas
        or not (old or new)
    ):
        return 0
    try:
        data = evidence(selector._cold_module_input[0])
        if not data:
            return 0
        parents, blocked, owners, prefix = data
        guard, rows = atlas
        if owners & ~guard:
            return 0
        for path in (old, new):
            if path is None:
                continue
            name = PurePosixPath(path)
            parent = str(name.parent)
            if (
                str(name) != path
                or parent not in parents
                or name.name in blocked
                or name.name.startswith(".")
            ):
                return 0
            if not (
                (name.suffix == ".sql" and name.parts[:2] == ("tests", "fixtures"))
                or (name.suffix == ".md" and name.parts[0] == "posts")
            ):
                return 0
            # Whole-test and unowned observations of the original file are
            # retained, even when the worker that made the observation is known.
            observed = selector._reads.get(path, 0) | selector._collection.get(path, 0)
            for k in (
                "raw",
                "membership",
                "metadata",
                "metadata_mtime",
                "loader_stat",
                "directory",
            ):
                observed |= rows.get(k, {}).get(path, 0)
            for k in ("directory", "raw", "metadata", "metadata_mtime"):
                observed |= rows.get(k, {}).get(parent, 0)
            if observed & prefix:
                return 0
        return owners
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return 0


def passive_prefix_guards(selector, edit):
    """Retain child guards for a sibling operation that might influence an earlier test."""
    from .path_observations import _load

    try:
        if not hasattr(selector, "_path_observations"):
            selector._path_observations = _load(selector)
        atlas = selector._path_observations
        if not atlas:
            return (1 << len(selector.all_tests)) - 1
        return atlas[0] & ~discharge(selector, atlas, edit.old, edit.new)
    except (OSError, ValueError, TypeError, KeyError):
        return (1 << len(selector.all_tests)) - 1
