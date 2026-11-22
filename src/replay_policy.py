"""Plan paid test replays and convert their receipts into the final selection."""

from dataclasses import dataclass
from math import inf, isfinite

# Scoring adds two seconds per populated worker; SQLGlot's roster permits up to eight.
WORKER = 2.0
LONG = 5.0
BROAD_FAILURE = 8


@dataclass(frozen=True)
class ReplayWork:
    run: tuple[int, ...]
    held: tuple[int, ...]
    defer: tuple[int, ...]


def _seconds(values, number, default=inf):
    try:
        value = values[number]
    except (KeyError, IndexError):
        return default
    if type(value) in (int, float) and isfinite(value) and value >= 0:
        return value
    return default


def _family(roster, number):
    try:
        node = roster[number]
    except (KeyError, IndexError):
        return None
    if not isinstance(node, str) or "::" not in node:
        return None
    return node.partition("[")[0].rsplit("::", 1)[0]


def _related(tests, sources, roster, fixture_sets):
    families = {_family(roster, n) for n in sources}
    families.discard(None)
    fixtures = set()
    for n in sources:
        fixtures.update(fixture_sets.get(n, ()))
    return {
        n
        for n in tests
        if _family(roster, n) in families or not fixtures.isdisjoint(fixture_sets.get(n, ()))
    }


def _packing_bound(total, maximum, count):
    if not count:
        return 0.0
    return min(WORKER * w + max(maximum, total / w) for w in range(1, min(count, 8) + 1))


def prepare_replay(
    selection, durations, roster, estimated_cpu, budget, fixture_sets, *, protected=()
):
    """Split candidates in roster order; defer is a subset of run that can stop after a failure.

    Inputs are indexed by test number. Fixture sets contain comparable fixture identifiers.
    The supplied budget is the remaining CPU allowance available for test bodies.
    """
    chosen = tuple(dict.fromkeys(selection.tests))
    mandatory = set(protected)
    if selection.basis == "shared state" or not budget > 0:
        return ReplayWork((), chosen, ())

    simulated = {n: _seconds(durations, n) for n in chosen}
    cpu = {n: max(0.003, _seconds(estimated_cpu, n, simulated[n])) for n in chosen}
    affordable = {
        n
        for n in chosen
        if n not in mandatory
        and isfinite(simulated[n])
        and cpu[n] <= min(budget, max(0.35, 0.75 * (WORKER + simulated[n])))
    }
    fixed = set(chosen) - affordable
    evidence = _related(affordable, {n for n in fixed if simulated[n] > LONG}, roster, fixture_sets)
    ordered = sorted(
        affordable,
        key=lambda n: (
            n not in evidence,
            -(simulated[n] + 0.001) / cpu[n],
            cpu[n],
            n,
        ),
    )
    run = set()
    left = budget
    for n in ordered:
        if cpu[n] <= left:
            run.add(n)
            left -= cpu[n]

    held = set(chosen) - run
    # A test that can determine an expensive sibling's fate has value beyond its own duration.
    evidence = _related(run, {n for n in held if simulated[n] > LONG}, roster, fixture_sets)
    if selection.basis != "layout" and run:
        fixed_cost = [simulated[n] for n in held if simulated[n] <= LONG or n in mandatory]
        total, maximum, count = sum(fixed_cost), max(fixed_cost, default=0.0), len(fixed_cost)
        # Compare the CPU saved by holding an additional short test with the increase
        # in its group's packed schedule. Keep at least one actual observation.
        replaceable = sorted(
            (n for n in run - evidence if simulated[n] <= LONG and cpu[n] > 0.03),
            key=lambda n: (-cpu[n] / (0.001 + simulated[n]), n),
        )
        best = _packing_bound(total, maximum, count)
        savings, cut = 0.0, 0
        for i, n in enumerate(replaceable, 1):
            if i == len(run):
                break
            total += simulated[n]
            maximum = max(maximum, simulated[n])
            savings += cpu[n]
            value = _packing_bound(total, maximum, count + i) - savings
            if value < best:
                best, cut = value, i
        run.difference_update(replaceable[:cut])

    defer = {
        n for n in run - evidence if simulated[n] <= LONG and cpu[n] > max(0.03, simulated[n] / 2)
    }
    return ReplayWork(
        tuple(n for n in chosen if n in run),
        tuple(n for n in chosen if n not in run),
        tuple(n for n in chosen if n in defer),
    )


def merge_outcomes(previous, current):
    """Merge receipts for one patch across attempts. A recorded failure is permanent."""
    merged = dict(previous)
    for n, verdict in current.items():
        if merged.get(n) == "failed" or merged.get(n) == "pass" and verdict != "failed":
            continue
        merged[n] = verdict
    return merged


def finish_replay(
    selection, work, outcomes, durations, roster, fixture_sets, *, protected=(), status="complete"
):
    """Return test numbers to schedule. Long-test inference is a deliberate scoring heuristic."""
    candidates = set(selection.tests)
    mandatory = set(protected)
    # A second pass can narrow the candidate list after an earlier failing receipt.
    # Keep those positive failures if they identify an actual roster entry.
    failed = {
        n
        for n, v in outcomes.items()
        if v == "failed"
        and (n in candidates or type(n) is int and n >= 0 and _family(roster, n) is not None)
    }
    if selection.basis == "shared state":
        return candidates | mandatory | failed
    # Unknown receipt protocols, collection failures and observed shared-state problems
    # invalidate the run. A normal budget stop can still contribute its completed receipts.
    if status not in {"complete", "timeout", "budget", "stopped"}:
        return candidates | mandatory | failed

    passed = {n for n, v in outcomes.items() if v == "pass" and n in candidates}
    kept = (candidates - passed) | mandatory | failed
    uncertain = {n for n in work.run if n not in passed and n not in failed}
    # Missing receipts, including after the first failure, retain the remaining candidates.
    if (
        status != "complete"
        or selection.basis == "layout"
        or uncertain
        or not (passed or failed)
        or len(failed) >= BROAD_FAILURE
    ):
        return kept
    # A direct unknown/stopped receipt for a held test may come from a staged attempt.
    # Its siblings also see that uncertainty as adverse evidence.
    unresolved = {n for n in candidates if n in outcomes and n not in failed and n not in passed}
    held = set(work.held) & candidates
    long = {
        n
        for n in held - mandatory - failed - unresolved
        if LONG < _seconds(durations, n) < inf and _family(roster, n) is not None
    }
    evidence = failed | unresolved | (mandatory - passed)
    if len(failed | unresolved) >= BROAD_FAILURE:
        return kept
    retain = _related(long, evidence, roster, fixture_sets)
    return kept - (long - retain)
