"""Collect bounded verdicts for one planned patch and finalize its schedule."""

import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time

from . import executor_stage, token_replay
from .patches import parse_patch
from .probe_worker import cpu
from .replay_policy import finish_replay, merge_outcomes, prepare_replay
from .replay_schedule import pack
from .scheduler import evaluate_schedule
from .verification_process import Processes, prepare_tree
from .verification_units import eligible as has_units


def _collection(maps, roster):
    try:
        data = json.loads((maps / "pristine-collection.json").read_bytes())
        if data["state"] != "collected" or data["code"] or data["errors"]:
            return ()
        files = {node.partition("::")[0] for node in roster.values()}
        names = tuple(node for node in data["items"] if node.partition("::")[0] in files)
        return names if set(roster.values()) <= set(names) and len(set(names)) == len(names) else ()
    except (OSError, ValueError, KeyError, TypeError):
        return ()


def _outcomes(events, numbers, complete):
    """Retain negative phase receipts even after the child has stopped."""
    admitted = set(numbers)
    failed, observed, timings, active = set(), {}, {}, None
    unstable = False
    for row in events:
        kind, *record = row
        if kind in {"state", "unattributed_failure"}:
            unstable = True
        elif kind == "start":
            test, = record
            unstable |= test not in admitted or active is not None or test in observed
            active = test
        elif kind == "failure":
            test, _phase = record
            if test in admitted:
                failed.add(test)
            unstable |= test != active
        elif kind == "end":
            test, verdict, cost = record
            unstable |= (test != active or test in observed
                         or verdict not in {"pass", "failed", "unknown", "partial"})
            if verdict == "failed" and test in admitted:
                failed.add(test)
            if test == active and test in admitted:
                observed[test] = verdict
                timings[test] = max(0.0, float(cost))
            active = None
    if complete and (active is not None or set(observed) != admitted):
        unstable = True
    observed.update({test: "failed" for test in failed})
    if unstable:
        return {test: "failed" for test in failed}, {}, True
    return observed, timings, False


class ReplaySession:
    def __init__(self, evidence, roster, durations, deadline):
        self.root = evidence.source_root
        self.maps = evidence._maps_path.parent
        self.roster = roster
        self.durations = durations
        self.deadline = deadline
        self.collection = _collection(self.maps, roster) if sys.platform == "linux" else ()
        positions = {node: rank for rank, node in enumerate(self.collection)}
        self.order = {number: positions[node] for number, node in roster.items() if node in positions}
        self.times = {test: max(.002, seconds) for test, seconds in durations.items()}
        self.processes = Processes(self.root)
        self.fixture_sets = {}
        for path, mask in getattr(evidence, "_reads", {}).items():
            if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                while mask:
                    bit = mask & -mask
                    mask ^= bit
                    self.fixture_sets.setdefault(bit.bit_length() - 1, set()).add(path)

    def close(self):
        self.processes.close()

    def _saving(self, tests, probe):
        original = pack(tests, self.durations)
        reduced = pack(tests - set(probe), self.durations)
        return evaluate_schedule(original, self.durations) - evaluate_schedule(reduced, self.durations)

    def _followup(self, work, selected, protected, observed, timings, budget):
        pending = selected - {n for n, verdict in observed.items() if verdict == "pass"}
        ready = {n for n in work.defer if observed.get(n) in (None, "unknown")}
        ready.update(n for n in work.held
                     if n not in protected and n not in observed and self.durations[n] <= 5)
        reference = sum(self.durations[n] for n in timings)
        speed = max(.1, min(1.0, sum(timings.values()) / reference)) if reference else 1.0
        costs = {n: max(.003, self.times[n] * speed) for n in ready}
        ranked = sorted(ready, key=lambda n: (-self.durations[n] / costs[n], n))
        proposed, charge = [], .35
        for n in ranked:
            if charge + costs[n] <= budget:
                proposed.append(n)
                charge += costs[n]
        if proposed and self._saving(pending, proposed) >= max(2.0, charge):
            return tuple(proposed)
        return ()

    def _abbreviated(self, selection, work, patch, paths, still_selected, remaining):
        if selection.basis == "layout" or remaining < .3:
            return None
        options = [test for test in work.held if test in still_selected and self.durations[test] >= 5]
        if not options:
            return None
        reverse = {self.roster[test]: test for test in options}
        costs = executor_stage.method_costs()
        executor = executor_stage.stage_candidates(self.root, patch, tuple(reverse))
        opportunities = []
        for node, test in reverse.items():
            if node in executor:
                charge, units = costs[node], False
            elif token_replay.eligible(node, paths):
                charge, units = min(.8, self.times[test]), False
            elif has_units(node, paths, self.maps):
                charge, units = self.times[test], True
            else:
                continue
            gain = self._saving(still_selected, (test,))
            if gain > charge and charge < remaining:
                opportunities.append((charge - gain, test, units, min(gain, remaining)))
        return min(opportunities, default=None)

    def _run(self, scratch, index, changes, patch, numbers, deferred, available, hot, units, native,
             *, scoped=False, stage=False):
        tree = scratch / f"source-{index}"
        if not prepare_tree(self.root, tree, changes):
            return "uncertain", None, {}, {}, False
        try:
            members = [(test, self.roster[test]) for test in sorted(numbers, key=self.order.__getitem__)]
            collection = self.collection
            if scoped:
                files = {node.partition("::")[0] for _, node in members}
                collection = tuple(node for node in collection if node.partition("::")[0] in files)
            job = {
                "tree": str(tree),
                "base": str(self.root),
                "changes": [change.old_path for change in changes],
                "collection": collection,
                "patch": patch,
                "maps": str(self.maps),
                "members": members,
                "after_failure": sorted(deferred),
                "units": units,
                "native_only": native,
                "direct": not (stage or native),
            }
            limits = {n: min(available, max(.4, 6 * self.times[n] + .25)) for n in numbers}
            status, stopped, events = self.processes.run(job, scratch / f"job-{index}.json", limits,
                                                         available, self.deadline, hot)
            observed, timings, unstable = _outcomes(events, numbers, status == "complete")
            state = next(((i, row[1]) for i, row in enumerate(events)
                          if row[0] == "state" and row[1] in numbers), None)
            if state is not None and not any(row[0] == "unattributed_failure" for row in events):
                offset, first_affected = state
                prefix, costs, broken = _outcomes(events[:offset], numbers, complete=False)
                if not broken:
                    status, stopped = "state", first_affected
                    observed, timings = merge_outcomes(prefix, observed), costs
                    unstable = False
            if os.environ.get("UMBERMILL_REPLAY_TRACE") == "1":
                print(f"attempt {index} status={status} warm={hot} scoped={scoped} "
                      f"cases={len(numbers)} receipts={len(observed)} unstable={unstable}",
                      file=sys.stderr, flush=True)
            return status, stopped, observed, timings, unstable
        finally:
            # Each attempt owns a fresh filesystem. The runtime tmpfs has room for
            # one backing tree, so clean it before trying a different interpreter.
            shutil.rmtree(tree, ignore_errors=True)

    def _after_state(self, selected, protected, observed, first_affected):
        boundary = self.order[first_affected]
        prefix_passes = {n for n, verdict in observed.items() if verdict == "pass" and n not in protected}
        later = {n for n in self.roster if self.order.get(n, boundary) >= boundary}
        return (selected - prefix_passes) | protected | later

    def choose(self, selection, patch, protected):
        selected = set(selection.tests) | protected
        if (not self.collection or not selection.tests or selection.basis in {"static", "shared state"}
                or time.monotonic() >= self.deadline):
            return selected
        native = selection.basis == "layout"
        warm = selection.basis in {"hot", "fixture"}
        budget = 8.0 if warm else 3.0
        started = cpu()
        observed = {}
        try:
            changes = parse_patch(patch)
            paths = tuple(change.old_path for change in changes)
            if not (0 < len(changes) <= 12 and all(
                not change.added and not change.deleted and change.old_path == change.new_path
                and change.old_path.startswith(("sqlglot/", "tests/")) for change in changes
            )):
                return selected
            scoped = not (native or selection.fallback) and all(
                path.startswith("sqlglot/") and path.endswith(".py")
                or path.startswith("tests/fixtures/") and path.endswith(".sql") for path in paths)
            unknown = selected - self.order.keys()
            protected = protected | unknown
            work = prepare_replay(selection, self.durations, self.roster, self.times,
                                  budget, self.fixture_sets, protected=protected)
            if not work.run:
                return selected
            with TemporaryDirectory(prefix="umbermill-replay-") as name:
                scratch = Path(name)
                hot = warm and all(path.startswith("sqlglot/") for path in paths) and self.processes.hot()
                status, stopped, observed, timings, unstable = self._run(
                    scratch, 0, changes, patch, work.run, work.defer, budget, hot, False, native,
                    scoped=scoped)
                if status == "state":
                    return self._after_state(selected, protected, observed, stopped)
                if unstable:
                    return selected
                for test, spent in timings.items():
                    self.times[test] = .65 * self.times[test] + .35 * max(.002, spent)
                # A bracketed activation decline can be retried cold without
                # inheriting provisional success from the abandoned process.
                if status in {"fresh", "uncertain"} and hot and budget - (cpu() - started) >= .25:
                    status, stopped, cold, _, unstable = self._run(
                        scratch, 1, changes, patch, work.run, work.defer,
                        budget - (cpu() - started), False, False, native, scoped=scoped)
                    if status == "state":
                        return self._after_state(selected, protected, cold, stopped)
                    if unstable:
                        return selected
                    observed = merge_outcomes({n: v for n, v in observed.items() if v == "failed"}, cold)
                if stopped is not None and observed.get(stopped) != "failed":
                    observed[stopped] = "stopped"
                if status == "stop" and stopped is not None:
                    pending = selected - {n for n, verdict in observed.items() if verdict == "pass"}
                    unrun = tuple(n for n in work.run if n not in observed)
                    balance = budget - (cpu() - started)
                    charge = .35 + sum(self.times[n] for n in unrun)
                    if unrun and charge < balance and self._saving(pending, unrun) > charge:
                        retry_status, other_stop, later, _, unsafe = self._run(
                            scratch, 2, changes, patch, unrun, (), balance, False, False, native,
                            scoped=scoped)
                        if retry_status == "state":
                            return self._after_state(selected, protected, later, other_stop)
                        if unsafe:
                            return selected
                        if other_stop is not None and later.get(other_stop) != "failed":
                            later[other_stop] = "stopped"
                        observed = merge_outcomes(observed, later)
                if status == "complete":
                    balance = budget - (cpu() - started)
                    followup = self._followup(work, selected, protected, observed, timings, balance)
                    if followup:
                        retry_status, follow_state, later, _, still_safe = self._run(
                            scratch, 2, changes, patch, followup, (), balance, False, False, native,
                            scoped=scoped)
                        if retry_status == "state":
                            return self._after_state(selected, protected, later, follow_state)
                        if not still_safe and retry_status == "complete":
                            observed = merge_outcomes(observed, later)
                    balance = budget - (cpu() - started)
                    preliminary = finish_replay(selection, work, observed, self.durations, self.roster,
                                               self.fixture_sets, protected=protected, status=status)
                    abbr = self._abbreviated(selection, work, patch, paths, preliminary, balance)
                    if abbr:
                        _, test, units, cost = abbr
                        extra_status, stage_state, extra, _, unstable = self._run(
                            scratch, 3, changes, patch, (test,), (), cost, False, units, native, stage=True)
                        if extra_status == "state":
                            return self._after_state(selected, protected, extra, stage_state)
                        if not unstable and extra_status == "complete":
                            observed = merge_outcomes(observed, extra)
            return finish_replay(selection, work, observed, self.durations, self.roster,
                                 self.fixture_sets, protected=protected,
                                 status="stopped" if status == "stop" else status)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            return selected
