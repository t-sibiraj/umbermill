"""Qualify small, source-pinned optimizer replay sets for a concrete function-body token edit."""

import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import contextlib
import time
import signal
import sys
import tokenize

from .scheduler import schedule_tests, evaluate_schedule

ROOT = Path("/app/rule-evidence")
ALLOWED = {">", "<", "==", "!=", "<=", ">=", "is", "and", "or", "True", "False", "None"}


def selected_refs(rows, original, updated, selection, selector, durations, name, change):
    old_nodes = ast.parse(original)
    fns = [
        n
        for n in old_nodes.body
        if isinstance(n, ast.FunctionDef)
        and change.old_lines
        and all(n.lineno < i <= n.end_lineno for i in change.old_lines)
    ]
    if len(fns) != 1:
        return
    tests = rows.get(name + "::" + fns[0].name)
    if not tests:
        return

    def tokens(raw):
        return [
            (v.type, v.string)
            for v in tokenize.generate_tokens(io.StringIO(raw.decode()).readline)
            if v.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING}
        ]

    left, right = tokens(original), tokens(updated)
    if len(left) != len(right):
        return
    diff = [(a, b) for a, b in zip(left, right) if a != b]
    if not 1 <= len(diff) <= 2 or any(
        a[0] != b[0]
        or not (a[0] in (tokenize.STRING, tokenize.NUMBER) or a[1] in ALLOWED and b[1] in ALLOWED)
        for a, b in diff
    ):
        return
    readers = selector._reader_index.changed(name, original.decode(), updated.decode())
    if readers is None:
        return
    veto = readers | selector._unmapped
    for line in change.old_lines:
        veto |= selector._collection_sites.get(name, {}).get(line, 0)
    refs = [(i, tests[str(i)]) for i in selection.tests if str(i) in tests and not veto >> i & 1]
    ids = set(selection.tests)

    def cost(items):
        return evaluate_schedule(schedule_tests(items, durations), durations)

    initial = cost(ids)
    if not refs or initial - cost(ids - {i for i, _ in refs}) < 1.0:
        return
    # A complete group of at most twenty distinct baseline invocations is economical.
    if len({(rule, sha) for _, entries in refs for rule, sha, _ in entries}) > 20:
        return
    return refs, initial - cost(ids - {i for i, _ in refs})


class RuleChecks:
    def __init__(self, checks):
        self.checks = checks
        index = json.loads(gzip.decompress((ROOT / "index.json.gz").read_bytes()))
        roster = sorted(checks.nodeids.items())
        if index["pinned"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or index["seed"] != 0:
            raise ValueError("different source revision or seed")
        if (
            hashlib.sha256(json.dumps(roster, separators=(",", ":")).encode()).hexdigest()
            != index["roster"]
        ):
            raise ValueError("different roster")
        self.hashes = index["hashes"]
        self.rows = index["keys"]

    def run(self, change, raw, updated, selection, selector):
        checks = self.checks
        name = change.old_path
        if selection.reason not in {
            "changed statement callers plus text readers",
            "changed function callers plus text readers",
        }:
            return set()
        if self.hashes.get(name) != hashlib.sha256(raw).hexdigest():
            return set()
        option = selected_refs(
            self.rows, raw, updated, selection, selector, checks.durations, name, change
        )
        if not option:
            return set()
        refs, saving = option
        limit = min(checks.group_budget, checks.ratio * saving)
        start = checks.group_start
        if checks.tree is None:
            checks.prepare()
        dest = checks.tree / name
        if dest.read_bytes() != raw:
            return set()
        try:
            dest.write_bytes(updated)
            req = Path(checks.temp.name, "rules.json")
            req.write_text(json.dumps({"refs": refs}))
            vars = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
            receipt = Path(checks.temp.name, "rule-output")
            timed = False
            stop = time.monotonic() + min(6.0, max(0.1, checks.deadline - time.monotonic()))
            with receipt.open("wb") as output:
                with subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "/app/rule-worker/verify_saved.py",
                        str(checks.tree),
                        str(req),
                    ],
                    env=vars,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                ) as proc:
                    while (
                        proc.poll() is None
                        and time.monotonic() < stop
                        and checks.cpu() - start < limit
                    ):
                        time.sleep(0.012)
                    if proc.poll() is None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        timed = True
                    proc.wait()
            if timed or proc.returncode:
                return set()
            result = json.loads(receipt.read_bytes())
        finally:
            dest.write_bytes(raw)
            checks.worker_cpu += checks.cpu() - start
        good = set(result["verified"])
        if good - {i for i, _ in refs}:
            return set()
        return good
