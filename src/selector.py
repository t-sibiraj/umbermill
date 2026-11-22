"""Coverage-backed selection with explicit fallback reasons.

Coverage is observed execution, not a proof of impact. File mode is the default;
function mode is experimental and must be tested on unseen local changes.
"""

import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from . import BASE_COMMIT
from .patches import UnsupportedPatch, parse_patch


@dataclass
class Selection:
    tests: list[int]
    reason: str
    fallback: bool
    basis: str = "coverage"


def load_tests(path: Path) -> tuple[dict[int, float], dict[str, int]]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
        raise ValueError("tests.json must contain a tests list")
    durations, nodeids = {}, {}
    import math
    for expected_number, item in enumerate(payload["tests"]):
        if not isinstance(item, dict):
            raise ValueError("test entry must be an object")
        number, nodeid, seconds = item.get("number"), item.get("id"), item.get("seconds")
        if type(number) is not int or number != expected_number:
            raise ValueError("test numbers must be consecutive integers")
        if not isinstance(nodeid, str) or not nodeid or nodeid in nodeids:
            raise ValueError("test IDs must be nonempty and unique")
        try:
            valid_seconds = (
                not isinstance(seconds, bool)
                and isinstance(seconds, (float, int))
                and math.isfinite(seconds)
                and seconds >= 0
                and math.isfinite(seconds * 1000)
            )
        except OverflowError:
            valid_seconds = False
        if not valid_seconds:
            raise ValueError("test duration must be finite and nonnegative")
        durations[number], nodeids[nodeid] = float(seconds), number
    return durations, nodeids


class Selector:
    def __init__(self, nodeids: dict[str, int], maps_path: Path | None = None, source_root: Path | None = None, granularity: str = "file"):
        if granularity not in ("file", "function"):
            raise ValueError("unknown granularity")
        self.all_tests = sorted(nodeids.values())
        self.nodeids = nodeids
        self.source_root = source_root.resolve() if source_root else None
        self.granularity = granularity
        self.file_tests: dict[str, set[int]] = {}
        self.line_tests: dict[str, dict[int, set[int]]] = {}
        self.sources: dict[str, str] = {}
        self.unavailable = "no verified coverage map"
        if maps_path is None:
            return
        try:
            payload = json.loads(maps_path.read_text())
            self._load_maps(payload)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.unavailable = f"invalid coverage map: {exc}"

    def _load_maps(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("coverage map must be an object")
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("base_commit") != BASE_COMMIT or payload.get("complete") is not True:
            raise ValueError("map is incomplete, unsupported, or from a different base")
        if self.source_root is None:
            raise ValueError("source root is required to verify coverage")
        records = payload["tests"]
        if not isinstance(records, dict) or not set(self.nodeids).issubset(records):
            raise ValueError("map lacks a runtime test")
        hashes = payload["source_hashes"]
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError("missing source hashes")
        source_line_counts = {}
        for name, digest in hashes.items():
            if not isinstance(name, str):
                raise ValueError("invalid source name")
            source = (self.source_root / name).resolve()
            if not source.is_relative_to(self.source_root) or not source.is_file():
                raise ValueError("source path missing or outside source root")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("source hash mismatch")
            self.sources[name] = raw.decode("utf-8")
            source_line_counts[name] = len(self.sources[name].splitlines())
        for nodeid, number in self.nodeids.items():
            record = records[nodeid]
            if not isinstance(record, dict):
                raise ValueError("coverage test record must be an object")
            lines = record["lines"]
            recorded_files = record["files"]
            if not isinstance(lines, dict) or not isinstance(recorded_files, list):
                raise ValueError("coverage requires a lines object and files list")
            if any(not isinstance(filename, str) for filename in recorded_files):
                raise ValueError("coverage filenames must be strings")
            files = set(recorded_files) | set(lines)
            if not files.issubset(hashes):
                raise ValueError("coverage has unhashed sources")
            for filename in files:
                self.file_tests.setdefault(filename, set()).add(number)
            for filename, numbers in lines.items():
                if not isinstance(numbers, list):
                    raise ValueError("coverage line numbers must be a list")
                for lineno in numbers:
                    if type(lineno) is not int or not 1 <= lineno <= source_line_counts[filename]:
                        raise ValueError("bad coverage line")
                    self.line_tests.setdefault(filename, {}).setdefault(lineno, set()).add(number)
        self.unavailable = ""

    def _all(self, reason: str) -> Selection:
        return Selection(self.all_tests.copy(), reason, True)

    def select(self, patch: str) -> Selection:
        if self.unavailable:
            return self._all(self.unavailable)
        try:
            changes = parse_patch(patch)
        except UnsupportedPatch as exc:
            return self._all(str(exc))
        selected: set[int] = set()
        for change in changes:
            name = change.old_path
            if change.added or change.deleted or name != change.new_path:
                return self._all("file added, removed, or renamed")
            if not name.startswith("sqlglot/") or not name.endswith(".py") or name not in self.sources:
                return self._all("change outside mapped Python source")
            candidates = self.file_tests.get(name)
            if not candidates:
                return self._all("changed file has no observed coverage")
            if self.granularity == "file":
                selected.update(candidates)
                continue
            scope_tests = self._function_tests(name, change.old_lines, change.inserted_after)
            if scope_tests is None:
                return self._all("change outside a covered function body")
            selected.update(scope_tests)
        if not selected:
            return self._all("no positive impact evidence")
        return Selection(sorted(selected), f"{self.granularity} coverage", False)

    def _function_tests(self, name: str, changed: set[int], inserted_after: set[int]) -> set[int] | None:
        try:
            tree = ast.parse(self.sources[name])
        except SyntaxError:
            return None
        # Use outer function bodies: nested functions can change their callers.
        functions = []
        def visit(node, inside_function=False):
            is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_function and not inside_function:
                body_start = node.body[0].lineno
                functions.append((body_start, node.end_lineno))
            for child in ast.iter_child_nodes(node):
                visit(child, inside_function or is_function)
        visit(tree)
        # An insertion between functions or on a scope boundary is uncertain.
        points = set(changed)
        for position in inserted_after:
            points.update((position, position + 1))
        impacted = set()
        for point in points:
            scopes = [(lo, hi) for lo, hi in functions if lo <= point <= hi]
            if not scopes:
                return None
            lo, hi = scopes[0]
            hits = set()
            for line, tests in self.line_tests.get(name, {}).items():
                if lo <= line <= hi:
                    hits.update(tests)
            if not hits:
                return None
            impacted.update(hits)
        return impacted or None
