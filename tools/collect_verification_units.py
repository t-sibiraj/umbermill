"""Collect optional first-party unit evidence, each pytest item in a fresh process.

Usage: python tools/collect_verification_units.py BASE MAPS PYTEST_NODEID [...]
"""

import argparse
import ast
import base64
import fcntl
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

# The source checkout calls the package src; the release calls it umbermill.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from umbermill.verification_units import (
        BASE_COMMIT, digest, evidence_file, fixture_token, method_anchors, runtime_identity,
    )
except ModuleNotFoundError as exc:
    if exc.name != "umbermill":
        raise
    from src.verification_units import (
        BASE_COMMIT, digest, evidence_file, fixture_token, method_anchors, runtime_identity,
    )


IMPACT_SHA = "f57c85894a47dac97762308b2be807bbf312206db182d4c33c067c5934ff4514"


def write_asset(target, contents):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_bytes(contents)
        temporary.chmod(0o644)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def pinned_catalog(maps):
    raw = (maps / "impact.json.gz").read_bytes()
    if digest(raw) != IMPACT_SHA:
        raise ValueError("unexpected pinned impact catalog")
    catalog = json.loads(gzip.decompress(raw))
    if catalog["base_commit"] != BASE_COMMIT or catalog["complete"] is not True:
        raise ValueError("invalid source catalog")
    def bits(value):
        if isinstance(value, int):
            value = catalog["mask_table"][value]
        if isinstance(value, list):
            return sum(1 << n for n in value)
        return int.from_bytes(base64.b64decode(value, validate=True), "little")
    roster = catalog["roster"]
    fixtures = {}
    for name, mask in catalog["reads"].items():
        if name.startswith("tests/fixtures/") and name.endswith(".sql"):
            observed = bits(mask)
            fixtures[name] = [case for i, case in enumerate(roster) if observed & (1 << i)]
    return set(roster), catalog["source_hashes"], fixtures


def discover(base, roster, limit):
    """Round-robin across test modules, preferring methods with separable work."""
    files = sorted({node.split("::", 1)[0] for node in roster if node.count("::") == 2})
    groups = []
    for path in files:
        candidates = []
        for cls in ast.parse((base / path).read_bytes()).body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef) or fn.decorator_list:
                    continue
                nodeid = "::".join((path, cls.name, fn.name))
                if nodeid not in roster:
                    continue
                spans = list(ast.walk(fn))
                loops = sum(isinstance(n, ast.For) for n in spans)
                cases = sum(isinstance(n, ast.Attribute) and n.attr == "subTest" for n in spans)
                size = len(fn.body) + 3 * loops + 2 * cases
                if size >= 3:
                    candidates.append((size, nodeid))
        if candidates:
            groups.append(sorted(candidates, key=lambda item: (-item[0], item[1])))
    chosen = []
    for round_no in range(max(map(len, groups), default=0)):
        available = [g[round_no] for g in groups if round_no < len(g)]
        for _, node in sorted(available, key=lambda item: (-item[0], item[1])):
            chosen.append(node)
            if len(chosen) == limit:
                return chosen
    return chosen


def collect(base, maps, nodeid):
    import pytest

    parts = nodeid.split("::")
    if len(parts) != 3 or not parts[0].startswith("tests/") or ".." in Path(parts[0]).parts:
        raise ValueError("expected a project-relative unittest method node ID")
    path, cls, method_name = parts
    test_file = base / path
    source = test_file.read_bytes()
    scope = method_anchors(source, nodeid)
    if scope is None:
        return False
    _, anchors = scope
    steps, last = [], {}
    filename = str(test_file)
    qualified = f"{cls}.{method_name}"
    root_prefix = str(base) + "/"
    source_prefix = root_prefix + "sqlglot/"
    helpers = str(base / "tests/helpers.py")
    active = None
    invocations = 0

    def method(frame, event, arg):
        nonlocal active
        if event == "line" and frame.f_lineno in anchors:
            steps.append(frame.f_lineno)
        if event == "return":
            active = None
        return method

    def dispatch(frame, event, arg):
        nonlocal active, invocations
        if event != "call":
            return None
        code = frame.f_code
        if code.co_filename == filename and code.co_qualname == qualified:
            invocations += 1
            active = frame
            return method
        if active is None:
            return None
        if code.co_filename == helpers and code.co_name in {"load_sql_fixtures", "load_sql_fixture_pairs"}:
            def fixture(frame, event, value):
                if event == "return":
                    token = fixture_token(frame, value)
                    if token is not None:
                        steps.append(token)
                return fixture
            return fixture
        if not code.co_filename.startswith(source_prefix):
            return None
        records = last.setdefault(code.co_filename[len(root_prefix):], {})

        def record(child, event, arg):
            if event == "line" and steps:
                records[child.f_lineno] = len(steps) - 1
            return record

        return record

    phases = set()
    clean = True

    class Recorder:
        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, session, config, items):
            retained = [item for item in items if item.nodeid == nodeid]
            if len(retained) != 1:
                raise pytest.UsageError(f"expected exactly one item for {nodeid}")
            config.hook.pytest_deselected(items=[i for i in items if i is not retained[0]])
            items[:] = retained

        def pytest_runtest_logreport(self, report):
            nonlocal clean
            if report.nodeid == nodeid:
                phases.add(report.when)
                if not report.passed:
                    clean = False

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_call(self, item):
            if sys.gettrace() is not None:
                raise RuntimeError("unit recording requires an untraced interpreter")
            sys.settrace(dispatch)
            try:
                return (yield)
            finally:
                sys.settrace(None)

    os.chdir(base)
    sys.path.insert(0, str(base))
    status = pytest.main(["-q", "-p", "no:cacheprovider", "tests"], plugins=[Recorder()])
    if (status != 0 or not clean or phases != {"setup", "call", "teardown"}
            or invocations != 1 or len(steps) < 2 or not last):
        return False
    data = {
        "version": 2,
        "base": BASE_COMMIT,
        "nodeid": nodeid,
        "test_digest": digest(source),
        "fixture_helper_digest": digest((base / "tests/helpers.py").read_bytes()),
        "runtime": runtime_identity(),
        "files": {path: digest((base / path).read_bytes()) for path in last},
        "steps": steps,
        "last": last,
    }
    target = evidence_file(maps, nodeid)
    write_asset(target, gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    return True


def publish(maps, candidates, requested, hashes, readers):
    directory = maps / "verification-units"
    directory.mkdir(parents=True, exist_ok=True)
    # Cumulative captures can run separately or be retried with shorter lists.
    # Publish the catalog only after checking every referenced record.
    with (directory / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest_path = directory / "index.json"
        if manifest_path.is_file():
            previous = json.loads(manifest_path.read_bytes())
            if (previous.get("version") == 2 and previous.get("runtime") == runtime_identity()
                    and previous.get("base") == BASE_COMMIT):
                candidates.update(previous["records"])
        records = {}
        for nodeid in sorted(candidates):
            asset = evidence_file(maps, nodeid)
            if not asset.is_file():
                continue
            raw = asset.read_bytes()
            data = json.loads(gzip.decompress(raw))
            if (data["version"] != 2 or data["nodeid"] != nodeid
                    or data["base"] != BASE_COMMIT or data["runtime"] != runtime_identity()
                    or data["test_digest"] != hashes.get(nodeid.split("::")[0])
                    or any(hashes.get(p) != sha for p, sha in data["files"].items())):
                continue
            covered = [p for p, lines in data["last"].items()
                       if any(i + 1 < len(data["steps"]) for i in lines.values())]
            if not covered:
                continue
            records[nodeid] = digest(raw)
            for path in covered:
                readers.setdefault(path, []).append(nodeid)
        manifest = {"base": BASE_COMMIT, "version": 2, "runtime": runtime_identity(),
                    "records": records, "readers": readers, "requested": len(requested)}
        write_asset(manifest_path, json.dumps(manifest, separators=(",", ":")).encode())
        return set(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("maps", type=Path)
    parser.add_argument("nodeids", nargs="*")
    parser.add_argument("--discover", type=int, default=0, metavar="N")
    parser.add_argument("--wall-seconds", type=float, default=60)
    parser.add_argument("--per-item-seconds", type=float, default=12)
    parser.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    base, maps = args.base.resolve(), args.maps.resolve()
    if args.one:
        if len(args.nodeids) != 1:
            parser.error("worker requires one node ID")
        return 0 if collect(base, maps, args.nodeids[0]) else 1
    if args.discover < 0 or args.wall_seconds <= 0 or args.per_item_seconds <= 0:
        parser.error("invalid recording limits")
    roster, hashes, readers = pinned_catalog(maps)
    nodes = list(dict.fromkeys([*args.nodeids, *(discover(base, roster, args.discover) if args.discover else [])]))
    if not nodes or not set(nodes) <= roster:
        parser.error("expected at least one original test method")
    end = time.monotonic() + args.wall_seconds
    completed = set()
    for nodeid in nodes:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--one",
                   str(base), str(maps), nodeid]
        # Start a separate process group. Timeouts must also release workers
        # spawned by the test's class fixtures.
        with subprocess.Popen(command, cwd=base, start_new_session=True) as child:
            try:
                code = child.wait(timeout=min(args.per_item_seconds, remaining))
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                code = -1
        if code:
            print(f"No complete unit record for {nodeid}", file=sys.stderr)
            continue
        completed.add(nodeid)
    records = publish(maps, completed, nodes, hashes, readers)
    print(f"{len(set(nodes) & records)} of {len(nodes)} requested methods available.", flush=True)
    return 0 if args.discover or set(args.nodeids) <= records else 1


if __name__ == "__main__":
    raise SystemExit(main())
