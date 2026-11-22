"""Keep serial verification on one CPU while it receives enough processor time."""

import os
import sys
import time


def available_cpu_time():
    accounting = os.times()
    return accounting.user + accounting.system + accounting.children_user + accounting.children_system


class CpuPlacement:
    def __init__(self, allowed, selected=None):
        self.allowed = allowed
        self.selected = selected
        self.wall = time.monotonic()
        self.cpu = available_cpu_time()

    @classmethod
    def start(cls):
        if sys.platform != "linux" or not hasattr(os, "sched_getaffinity"):
            return cls(None)
        try:
            allowed = os.sched_getaffinity(0)
            if len(allowed) < 2:
                return cls(None)
            # Spread simultaneous invocations across their preassigned CPU set.
            choices = sorted(allowed)
            selected = choices[os.getpid() % len(choices)]
            os.sched_setaffinity(0, {selected})
            return cls(allowed, selected)
        except (OSError, ValueError):
            return cls(None)

    def release_if_delayed(self):
        if self.allowed is None:
            return
        elapsed = time.monotonic() - self.wall
        if elapsed < 0.5:
            return
        consumed = available_cpu_time() - self.cpu
        if elapsed - consumed <= max(0.35, consumed * 0.70):
            self.wall = time.monotonic()
            self.cpu = available_cpu_time()
            return
        try:
            # Respect an external policy change between callbacks.
            if os.sched_getaffinity(0) == {self.selected}:
                os.sched_setaffinity(0, self.allowed)
        except OSError:
            pass
        self.allowed = None
