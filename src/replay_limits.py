"""Bound verification time and memory before the container reaches its limits."""

import os
from pathlib import Path
import sys
import time


_CGROUP = Path("/sys/fs/cgroup")
_PROC = Path("/proc")
_MIB = 1024 * 1024


def planning_seconds(count):
    """Allow longer batches more preparation time with output time reserved."""
    return min(160.0, 123.0 + max(0, count - 35) * 2.0)


class ReplayMemory:
    def __init__(self):
        self.enabled = sys.platform == "linux" and (_CGROUP / "memory.current").is_file()
        self.next_poll = 0.0
        self.limit = 3400 * _MIB
        self.exceeded = False
        self.page = os.sysconf("SC_PAGE_SIZE") if self.enabled else 0
        if self.enabled:
            try:
                maximum = int((_CGROUP / "memory.max").read_text())
                self.limit = min(self.limit, maximum * 83 // 100)
            except (ValueError, OSError):
                pass
        self.resident_limit = min(2500 * _MIB, self.limit * 9 // 10)

    def over(self, pid=None):
        if not self.enabled:
            return False
        now = time.monotonic()
        if now < self.next_poll:
            return False
        self.next_poll = now + .05
        if pid is not None:
            try:
                resident = int((_PROC / str(pid) / "statm").read_text().split()[1])
                if resident * self.page > self.resident_limit:
                    self.exceeded = True
                    return True
            except (OSError, ValueError, IndexError):
                pass
        try:
            if int((_CGROUP / "memory.current").read_text()) < self.limit:
                return False
            # memory.current includes reclaimable file cache. Anonymous
            # memory and shared tmpfs are the pressure attributable to jobs.
            sizes = dict(line.split() for line in (_CGROUP / "memory.stat").read_text().splitlines())
            self.exceeded = int(sizes.get("anon", 0)) + int(sizes.get("shmem", 0)) >= self.limit
            return self.exceeded
        except (OSError, ValueError):
            return False
