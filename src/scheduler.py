"""Balance test duration against the fixed cost of starting a worker."""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping


def _milliseconds(duration: float) -> int:
    """Use integer loads so float noise cannot change scheduling decisions."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("test durations must be finite, nonnegative numbers")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("test durations must be finite, nonnegative numbers")
    scaled = duration * 1000
    if not math.isfinite(scaled):
        raise ValueError("test duration is too large")
    return math.floor(scaled + 0.5)


def evaluate_schedule(
    workers: Iterable[Iterable[int]], durations: Mapping[int, float]
) -> float:
    """Return longest worker load plus two seconds per nonempty worker.

    Durations are rounded to milliseconds individually. Empty workers are
    ignored, matching the compact schedules returned by ``schedule_tests``.
    """
    loads: list[int] = []
    seen: set[int] = set()
    for worker in workers:
        load = 0
        nonempty = False
        for number in worker:
            if number in seen:
                raise ValueError("a test may appear only once in a schedule")
            seen.add(number)
            nonempty = True
            load += _milliseconds(durations[number])
        if nonempty:
            loads.append(load)
    if len(loads) > 8:
        raise ValueError("a schedule may use at most eight workers")
    return (max(loads, default=0) + 2000 * len(loads)) / 1000


def schedule_tests(
    test_numbers: Iterable[int],
    durations: Mapping[int, float],
    max_workers: int = 8,
) -> list[list[int]]:
    """Choose the cheapest longest-processing-time schedule across worker counts.

    This is a bounded greedy approximation, not an exact partition search.
    Each distinct selected test appears once. Ties prefer fewer workers, and
    input order does not affect the result. Complexity is O(n log n) with at
    most eight small-heap passes over the sorted jobs.
    """
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or not 1 <= max_workers <= 8
    ):
        raise ValueError("max_workers must be an integer from one to eight")

    jobs = [(number, _milliseconds(durations[number])) for number in set(test_numbers)]
    if not jobs:
        return []
    jobs.sort(key=lambda job: (-job[1], job[0]))

    longest = jobs[0][1]
    total = sum(duration for _, duration in jobs)
    limit = min(max_workers, len(jobs))
    if jobs[-1][1] == 0:
        # Beyond this count, every positive test already has its own worker.
        # A new worker used only for zero-duration tests adds overhead.
        limit = min(limit, max(1, sum(duration > 0 for _, duration in jobs)))
    bounds = sorted(
        (max(longest, (total + count - 1) // count) + 2000 * count, count)
        for count in range(1, limit + 1)
    )

    best = None
    best_workers: list[list[int]] = []
    for lower_bound, count in bounds:
        if best is not None and (lower_bound, count) >= best:
            break
        if count == 1:
            workers = [[number for number, _ in jobs]]
            cost = total + 2000
        else:
            workers = [[] for _ in range(count)]
            heap = [(0, worker) for worker in range(count)]
            for number, duration in jobs:
                load, worker = heap[0]
                workers[worker].append(number)
                heapq.heapreplace(heap, (load + duration, worker))
            cost = max(load for load, _ in heap) + 2000 * count
        if best is None or (cost, count) < best:
            best = (cost, count)
            best_workers = workers

    for worker in best_workers:
        worker.sort()
    return best_workers
