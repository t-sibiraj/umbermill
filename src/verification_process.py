"""Materialize exact source changes and supervise disposable verification processes."""

import contextlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time

from .patches import apply_change
from .probe_worker import cpu
from .replay_limits import ReplayMemory

WORKER = Path(__file__).with_name("verification_worker.py")


def prepare_tree(base, tree, changes):
    replacements = {}
    for change in changes:
        path = change.old_path
        if path in replacements or not path.startswith(("sqlglot/", "tests/")):
            return False
        source = base / path
        if source.resolve() != source or not source.is_file():
            return False
        replacements[path] = apply_change(source.read_bytes().decode(), change)
    tree.mkdir()
    for item in base.iterdir():
        destination = tree / item.name
        if item.name in {"tests", "sqlglot"}:
            shutil.copytree(item, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        elif item.name not in {".git", ".pytest_cache", "__pycache__"}:
            destination.symlink_to(item)
    for path, content in replacements.items():
        (tree / path).write_bytes(content.encode())
    return True


def stop_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


class Processes:
    def __init__(self, base):
        self.base = base
        self.server = None
        self.offline = False

    def close(self):
        if self.server is None:
            return
        server = self.server
        self.server = None
        children = Path(f"/proc/{server.pid}/task/{server.pid}/children")
        with contextlib.suppress(OSError):
            for pid in children.read_text().split():
                stop_group(int(pid))
        with contextlib.suppress(BrokenPipeError):
            server.stdin.close()
        try:
            server.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            stop_group(server.pid)
            server.wait()
        server.stdout.close()

    def hot(self):
        if self.offline:
            return False
        if self.server is None:
            self.server = subprocess.Popen([sys.executable, "-I", "-B", str(WORKER), "checkpoint", str(self.base)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           start_new_session=True)
            ready, _, _ = select.select([self.server.stdout], [], [], 1.5)
            if not ready or self.server.stdout.readline().strip() != b"ready":
                self.offline = True
                self.close()
                return False
        return self.server.poll() is None

    def run(self, job, path, limits, budget, deadline, hot):
        journal = path.with_suffix(".jsonl")
        status = path.with_suffix(".status")
        job["journal"] = str(journal)
        path.write_text(json.dumps(job, separators=(",", ":")))
        active = None
        item_start = start = cpu()
        finish_time = min(deadline, time.monotonic() + 2.0 + 4.0 * budget)
        events = []
        offset = 0
        process = None
        memory = ReplayMemory()
        def observe():
            nonlocal active, item_start, offset
            if not journal.exists():
                return
            with journal.open('rb') as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    row = json.loads(line)
                    events.append(row)
                    offset += len(line)
                    if row[0] == 'start':
                        active = row[1]
                        item_start = cpu()
                    elif row[0] == 'end':
                        active = None
        try:
            if hot:
                self.server.stdin.write(json.dumps(str(path)).encode() + b"\n")
                self.server.stdin.flush()
            else:
                process = subprocess.Popen([sys.executable, "-I", "-B", str(WORKER), str(path)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
            while True:
                observe()
                if hot and status.exists() and status.read_text().strip():
                    rc = int(status.read_text())
                    break
                if hot and self.server.poll() is not None:
                    rc = 2
                    break
                if process is not None and process.poll() is not None:
                    rc = process.returncode
                    break
                now = cpu()
                if (time.monotonic() >= finish_time or now-start >= budget or
                        memory.over(process.pid if process is not None else None) or
                        (active is not None and now-item_start > limits[active])):
                    return "stop", active, events
                time.sleep(.015)
            observe()
            # A patch can exit with the activation sentinel after recording a failure.
            if hot and rc == 10 and not events:
                return "fresh", None, events
            if rc or not events or events[0] != ["ready"] or events[-1] not in (["complete",0], ["complete",1]):
                return "uncertain", None, events
            return "complete", None, events
        finally:
            if process is not None:
                if process.poll() is None:
                    stop_group(process.pid)
                    process.wait()
                else:
                    # A pool started by a test can outlive the interpreter.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            if hot and not status.is_file():
                # The checkpoint reports completion before accepting another request.
                self.close()
            # A phase failure can be flushed between the watchdog's last read
            # and process-group termination. In-flight return values share this list.
            observe()
