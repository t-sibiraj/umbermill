"""Verify economical selections against the actual patch before assigning worker slack."""

import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from functools import lru_cache
from .probe_worker import bounded_worker, cpu
from .patches import UnsupportedPatch, parse_patch, apply_change


def numeric_field_line(change):
    """Further restrict a proved class-field edit to existing scalar or tuple data."""
    import ast
    from .guards import one_line_change

    pair = one_line_change(change)
    if pair is None:
        return False
    try:
        for line in pair[1:]:
            lhs, equal, rhs = line.strip().partition("=")
            if equal and lhs.strip().isidentifier():
                value = ast.literal_eval(rhs.strip())
            else:
                (key, value), = ast.literal_eval("{" + line.strip() + "}").items()
                if type(key) is not str:
                    return False
            elements = value if type(value) is tuple else (value,)
            if not elements or not all(type(item) in (int, bool) for item in elements):
                return False
    except (ValueError, SyntaxError, TypeError, AttributeError, RecursionError):
        return False
    return True


class RuntimeChecks:
    def __init__(self, root, durations, nodeids):
        self.root = root.resolve()
        self.durations = durations
        self.nodeids = {n: node for node, n in nodeids.items()}
        self.tree = self.temp = self.rule_checks = None
        self.usable = sys.platform == "linux" and Path("/sys/fs/cgroup/cpu.stat").is_file()
        self.cpu = cpu
        self.opened = set()
        self.deadline = time.monotonic() + 75
        self.cwd = Path.cwd()
        self.budget = 10.0
        self.ratio = 0.8
        self.cap = 0.55
        self.limit = 24
        self.worker_cpu = 0.0
        self.group_budget = 0.0
        self.group_start = 0.0
        atexit.register(self.finish)

    def finish(self):
        if self.temp:
            os.chdir(self.cwd)
            self.temp.cleanup()

    def prepare(self):
        import importlib

        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="umbermill-probe-")
        self.tree = Path(self.temp.name, "source")

        def copy_directory(src, dst):
            dst.mkdir()
            for entry in src.iterdir():
                target = dst / entry.name
                if entry.is_dir() and entry.name not in {"__pycache__", "fixtures", "examples"}:
                    copy_directory(entry, target)
                elif entry.suffix == ".py" and not entry.is_dir():
                    shutil.copyfile(entry, target)
                else:
                    target.symlink_to(entry)

        self.tree.mkdir()
        for entry in self.root.iterdir():
            target = self.tree / entry.name
            if entry.name in {"tests", "sqlglot"}:
                copy_directory(entry, target)
            else:
                target.symlink_to(entry)
        self.baseline_threads = len(list(Path("/proc/self/task").iterdir()))
        os.chdir(self.tree)
        sys.path.insert(0, str(self.tree))
        os.environ["PYTHONPATH"] = str(self.tree)
        active = True
        where = str(self.tree) + "/"
        original = str(self.root) + "/"

        def audit(event, args):
            if active and event == "open" and isinstance(args[0], (str, bytes)):
                resolved = os.path.realpath(os.fsdecode(args[0]))
                if resolved.startswith(where):
                    self.opened.add(resolved[len(where) :])
                elif resolved.startswith(original):
                    self.opened.add(resolved[len(original) :])

        sys.addaudithook(audit)
        importlib.import_module("sqlglot")
        active = False
        for module in tuple(sys.modules.values()):
            path = getattr(module, "__file__", None)
            if isinstance(path, str):
                resolved = os.path.realpath(path)
                if resolved.startswith(where):
                    self.opened.add(resolved[len(where) :])

    @lru_cache(maxsize=192)
    def uses_pool(self, node):
        module = self.root / node.split("::", 1)[0]
        text = module.read_bytes()
        return b"ProcessPoolExecutor" in text or b"multiprocessing" in text

    def passing(self, patch, selection, selector):
        ids = selection.tests
        if not self.usable or not ids or selection.fallback:
            return set()
        might_replay = "sqlglot/optimizer/" in patch[:256]
        if not might_replay and len(ids) > self.limit:
            return set()
        total_seconds = sum(self.durations[n] for n in ids)
        if not might_replay and total_seconds > self.budget:
            return set()
        if time.monotonic() >= self.deadline or self.worker_cpu >= self.budget:
            return set()
        self.group_start = cpu()
        try:
            changes = parse_patch(patch)
        except UnsupportedPatch:
            # An independently qualified file move or creation can still have
            # a small schedule. This verifier only replays ordinary edits.
            return set()
        if len(changes) != 1:
            return set()
        ch = changes[0]
        filename = ch.old_path
        if (
            ch.added
            or ch.deleted
            or filename != ch.new_path
            or not filename.startswith("sqlglot/")
            or not filename.endswith(".py")
            or filename.endswith("/__init__.py")
        ):
            return set()
        # Keep module import and fixture-affecting edits on the original schedule.
        for line in patch.splitlines():
            if line[:1] in {"+", "-"} and not line.startswith(("---", "+++")):
                code = line[1:].lstrip()
                if code.startswith(
                    (
                        "import ",
                        "from ",
                        "@",
                        "def ",
                        "async def ",
                        "class ",
                        "raise ",
                        "global ",
                        "nonlocal ",
                    )
                ):
                    return set()
        from .scheduler import schedule_tests, evaluate_schedule

        original = evaluate_schedule(schedule_tests(ids, self.durations), self.durations)
        # These scoped readers can depend on test order and remain eligible. Free
        # slack packing preserves the underlying makespan for both schedules.
        guard = getattr(getattr(selector, "_reader_index", None), "guard", 0) or 0
        reserved = [n for n in ids if (guard >> n) & 1]
        residual = evaluate_schedule(schedule_tests(reserved, self.durations), self.durations)
        potential = max(0.0, original - residual)
        self.group_budget = min(potential * self.ratio, self.budget - self.worker_cpu)
        if self.group_budget < 0.06:
            return set()
        source = (self.root / filename).resolve()
        if not source.is_relative_to(self.root) or self.root / filename != source:
            return set()
        old = source.read_bytes()
        new = apply_change(old.decode(), ch).encode()
        try:
            compile(new, filename, "exec")
        except SyntaxError:
            return set()
        if might_replay and filename.startswith("sqlglot/optimizer/"):
            from .rule_checks import RuleChecks

            if self.rule_checks is None:
                self.rule_checks = RuleChecks(self)
            return self.rule_checks.run(ch, old, new, selection, selector)
        ids = [n for n in ids if not guard >> n & 1]
        # A complete group can remove the final worker's setup charge. Admit a
        # larger group only when its estimated run fits the available CPU and
        # every selection can be checked together in the original order.
        if (
            len(ids) > self.limit
            or (total_seconds > self.cap and (
                reserved or total_seconds > self.group_budget
                or selection.reason != "observed class field consumers" or not numeric_field_line(ch)
            ))
            or any(self.uses_pool(self.nodeids[n]) for n in ids)
        ):
            return set()
        if self.tree is None:
            self.prepare()
        warm_cpu = cpu() - self.group_start
        self.worker_cpu += warm_cpu
        available = max(0.0, self.group_budget - warm_cpu)
        if available < 0.035:
            return set()
        dest = self.tree / filename
        if dest.read_bytes() != old:
            return set()
        start = cpu()
        available = min(available, self.group_budget - (start - self.group_start))
        if available < 0.03:
            return set()
        try:
            dest.write_bytes(new)
            fork = (
                filename not in self.opened
                and len(list(Path("/proc/self/task").iterdir())) == self.baseline_threads
            )
            receipt = bounded_worker(
                self.tree,
                [(n, self.nodeids[n]) for n in sorted(ids)],
                Path(self.temp.name),
                min(6.0, max(0.1, self.deadline - time.monotonic())),
                fork,
                self.baseline_threads,
                available,
            )
        finally:
            dest.write_bytes(old)
        passed = set(receipt.get("passed", ())) if receipt.get("reason") == "complete" else set()
        if not passed <= set(ids):
            passed = set()
        charged = cpu() - start
        self.worker_cpu += charged
        return passed
