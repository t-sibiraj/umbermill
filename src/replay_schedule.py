"""Design a deterministic assignment of selected test IDs to worker queues."""

import heapq


def pack(selected, durations):
    ordered = sorted(selected, key=lambda n: (-durations[n], n))
    result = []
    minimum = float("inf")
    for size in range(1, min(8, len(ordered)) + 1):
        queues = [[] for _ in range(size)]
        ready = [(0.0, index) for index in range(size)]
        for test in ordered:
            available, lane = ready[0]
            queues[lane].append(test)
            heapq.heapreplace(ready, (available + durations[test], lane))
        charge = max(seconds for seconds, _ in ready) + 2 * size
        if charge + 1e-9 < minimum:
            result, minimum = queues, charge
    return result
