"""Resolve test contexts that observed a regular path name or its parent directory."""

import ast
import base64
import gzip
import hashlib
import json
import platform
from pathlib import PurePosixPath
import sys

from . import BASE_COMMIT
from .module_cold import ROSTER_DIGEST

ATLAS_DIGEST = "c6f205103d2ef8f1f3da1dec1c7b157235c88bd4399b315f074363cc914c23cf"


def _load(selector):
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        return None
    map_path, numbers = selector._cold_module_input
    if selector.unavailable or map_path is None:
        return None
    ids = sorted(numbers, key=numbers.get)
    if hashlib.sha256("\0".join(ids).encode()).hexdigest() != ROSTER_DIGEST or any(
        numbers[n] != i for i, n in enumerate(ids)
    ):
        return None
    raw = map_path.with_name("path-observations.json.gz").read_bytes()
    if hashlib.sha256(raw).hexdigest() != ATLAS_DIGEST:
        return None
    data = json.loads(gzip.decompress(raw))
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if (
        arch not in data["architectures"]
        or data["base"] != BASE_COMMIT
        or data["roster"] != ROSTER_DIGEST
    ):
        return None

    def bits(value):
        n = int.from_bytes(base64.b64decode(value, validate=True), "little")
        if n.bit_length() > len(ids):
            raise ValueError("invalid namespace observation")
        return n

    return bits(data["guards"]), {
        k: {p: bits(n) for p, n in rows.items()} for k, rows in data["rows"].items()
    }


def path_readers(selector, old_path, new_path):
    """Return the union for witnessed names and directories. The caller verifies both tree entries."""
    try:
        if not hasattr(selector, "_path_observations"):
            selector._path_observations = _load(selector)
        atlas = selector._path_observations
        if not atlas or not (old_path or new_path):
            return None
        mask, rows = atlas
        from .passive_kernel import discharge

        mask &= ~discharge(selector, atlas, old_path, new_path)
        for value in (old_path, new_path):
            if value is None:
                continue
            name = PurePosixPath(value)
            if (
                str(name) != value
                or name.is_absolute()
                or any(p.startswith(".") or not p.isascii() for p in name.parts)
                or (name.suffix not in {".py", ".sql", ".md"}
                    and not (name.parts[:2] == ("tests", "fixtures")
                             and name.suffix in {".csv", ".json", ".txt", ".yaml", ".yml", ".tsv"}))
                or name.name in {"conftest.py", "__init__.py", "setup.py"}
                or name.name.startswith("test_")
                or name.name.endswith("_test.py")
            ):
                return None
            if name.suffix == ".py" and (
                name.parts[0] != "sqlglot"
                or any(not p.isidentifier() for p in name.with_suffix("").parts)
            ):
                return None
            parent = str(name.parent)
            for kind in (
                "metadata",
                "membership",
                "loader_stat",
                "metadata_mtime",
                "raw",
                "directory",
            ):
                mask |= rows.get(kind, {}).get(value, 0)
            # Non-projected directory observers and parent stat/mtime readers see a
            # different membership or timestamp after insertion/removal. Both parents
            # are evaluated on a move. Narrowly projected pytest discovery only tests
            # if the occurrence is a test filename, with standard regular-file kind.
            for kind in ("metadata", "metadata_mtime", "directory", "raw"):
                mask |= rows.get(kind, {}).get(parent, 0)
            if name.suffix == ".py":
                module = ".".join(name.with_suffix("").parts)
                mask |= rows.get("import", {}).get(module, 0)
                if value == new_path:
                    # Public find_spec() calls may only have read the directory on
                    # a negative lookup. Preserve their earlier lookup opportunity.
                    mask |= rows.get("loader_directory", {}).get(parent, 0)
                    mask |= rows.get("loader_stat", {}).get(parent, 0)
        return None if mask.bit_count() >= len(selector.all_tests) else mask
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _name_available(selector, name):
    # A newly importable child can displace an existing package attribute or a
    # namespace package even when the original child name had no import hit.
    if (selector.source_root / name.with_suffix("")).exists():
        return False
    source = selector._source(str(name.parent / "__init__.py"))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in {name.stem, "__getattr__", "__dir__"}:
                return False
        elif isinstance(node, ast.Name):
            if (isinstance(node.ctx, ast.Store) and node.id == name.stem
                    or node.id in {"globals", "exec", "setattr", "__getattr__", "__dir__"}):
                return False
        elif isinstance(node, ast.Constant) and node.value == name.stem:
            return False
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" or (a.asname or a.name.split(".")[0]) == name.stem
                   for a in node.names):
                return False
    return True


def new_module_unimported(selector, path):
    """Recognize an ordinary new child name outside active import or discovery requests."""
    if (
        not isinstance(path, str)
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return False
    if path_readers(selector, None, path) is None:
        return False
    name = PurePosixPath(path)
    # Only ordinary files inside established packages; no namespace package edges.
    if any(
        not (selector.source_root / parent / "__init__.py").is_file()
        for parent in name.parents
        if str(parent) != "."
    ):
        return False
    if not _name_available(selector, name):
        return False
    _, rows = selector._path_observations
    module = ".".join(name.with_suffix("").parts)
    if rows.get("import", {}).get(module) or rows.get("directory", {}).get(
        str(name.parent)
    ):
        return False
    return not any(
        rows.get(kind, {}).get(path)
        for kind in ("metadata", "membership", "raw", "loader_stat", "directory")
    )
