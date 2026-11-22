"""Replay ordinary SQL fixture edits from a shared, unmodified Python checkpoint."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .patches import UnsupportedPatch, apply_change, parse_patch
from .probe_worker import bounded_worker, cpu


def _local_fixture(tree, base, relative):
    parts = Path(relative).parts
    parent = tree
    original = base
    if parts[:2] != ("tests", "fixtures") or ".." in parts:
        raise ValueError("unsupported fixture path")
    for name in parts[:-1]:
        parent, original = parent / name, original / name
        if parent.is_symlink():
            if parent.resolve() != original or not original.is_dir():
                raise ValueError("unexpected fixture directory")
            parent.unlink()
            parent.mkdir()
            for child in original.iterdir():
                (parent / child.name).symlink_to(child)
    return parent / parts[-1]


def _serve(request_file, result_file):
    from .runtime_checks import RuntimeChecks

    request = json.loads(Path(request_file).read_bytes())
    base = Path(request["base"])
    start = cpu()
    checks = RuntimeChecks(base, {}, {})
    checks.prepare()
    try:
        paths = {path for job in request["jobs"] for path in job["files"]}
        original = {p: (base / p).read_bytes() for p in paths}
        staged = {p: _local_fixture(checks.tree, base, p) for p in paths}
        # Imports which consumed an original fixture cannot be shared for edits to it.
        watched = {os.path.realpath(base / p): p for p in paths}
        watched.update({os.path.realpath(staged[p]): p for p in paths})
        read_at_import = checks.opened.intersection(paths)
        watching = True

        def track(event, args):
            if watching and event == "open" and isinstance(args[0], (str, bytes)):
                p = watched.get(os.path.realpath(os.fsdecode(args[0])))
                if p is not None:
                    read_at_import.add(p)

        sys.addaudithook(track)
        for node in sorted({node for job in request["jobs"] for _, node in job["members"]}):
            module = node.split("::", 1)[0].removesuffix(".py").replace("/", ".")
            importlib.import_module(module)
        watching = False
        for job in request["jobs"]:
            left = min(request["budget"] - (cpu() - start), job["budget"])
            remaining = request["deadline"] - time.monotonic()
            if left < 0.05 or remaining < .25:
                break
            if read_at_import.intersection(job["files"]):
                continue
            if len(list(Path("/proc/self/task").iterdir())) != 1:
                break
            saved = []
            try:
                for relative, data in job["files"].items():
                    dst = staged[relative]
                    if dst.read_bytes() != original[relative]:
                        raise ValueError("fixture checkpoint changed")
                    backup = checks.tree.parent / ("fixture-" + str(len(saved)))
                    dst.rename(backup)
                    saved.append((dst, backup))
                    dst.write_bytes(data.encode("utf-8"))
                members = job["members"]
                receipt = bounded_worker(checks.tree, members, checks.tree.parent, min(5., left + 1., remaining - .1), True, 1, left)
                if receipt.get("reason") == "complete":
                    admitted = {n for n, _ in members}
                    passed = set(receipt.get("passed", ()))
                    unresolved = set(receipt.get("unresolved", ()))
                    if not (passed & unresolved) and (passed | unresolved) <= admitted:
                        with Path(result_file).open("a") as f:
                            f.write(json.dumps([job["commit"], sorted(passed), sorted(unresolved)]) + "\n")
            finally:
                for dst, backup in reversed(saved):
                    backup.replace(dst)
                    if dst.read_bytes() != original[dst.relative_to(checks.tree).as_posix()]:
                        raise ValueError("fixture restoration failed")
    finally:
        checks.finish()


def passing(jobs, selector, checks):
    if checks is None or not checks.usable:
        return {}
    records = []
    guard = getattr(getattr(selector, "_reader_index", None), "guard", 0) | selector._unmapped
    for job in jobs:
        if not job.fixture or job.selection.fallback:
            continue
        members = [(n, checks.nodeids[n]) for n in job.selection.tests if not ((guard >> n) & 1)]
        if not members or len(members) > 24 or job.estimated_load > 8:
            continue
        try:
            files = {c.old_path: apply_change(selector._source(c.old_path), c) for c in parse_patch(job.text)}
        except (OSError, UnicodeError, UnsupportedPatch):
            continue
        records.append(dict(commit=job.path.stem, members=members, files=files,
                            budget=min(2., (2 + job.estimated_load) * .65)))
    if len(records) < 2:
        return {}
    budget = min(sum(r["budget"] for r in records), checks.budget - checks.worker_cpu)
    wall = min(12., checks.deadline - time.monotonic(), 2 * budget + 2.)
    if budget < .15 or wall < .25:
        return {}
    started = cpu()
    with tempfile.TemporaryDirectory() as tmp:
        request, result = Path(tmp) / "jobs", Path(tmp) / "results"
        request.write_text(json.dumps(dict(base=str(checks.root), jobs=records, budget=budget, deadline=time.monotonic() + wall)))
        result.touch()
        try:
            proc = subprocess.Popen([sys.executable, "-B", "-m", __name__, str(request), str(result)],
                                    cwd=Path(__file__).resolve().parents[1],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                proc.wait(timeout=wall + 1.)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                proc.wait()
            allowed = {r["commit"]: {n for n, _ in r["members"]} for r in records}
            passed = {}
            for line in result.read_text().splitlines():
                key, ids, unresolved = json.loads(line)
                proved, protected = set(ids), set(unresolved)
                if key in allowed and not (proved & protected) and (proved | protected) <= allowed[key]:
                    passed[key] = (proved, protected)
            return passed
        except (OSError, ValueError, TypeError):
            return {}
        finally:
            checks.worker_cpu += max(0., cpu() - started)


if __name__ == "__main__":
    _serve(*sys.argv[1:])
