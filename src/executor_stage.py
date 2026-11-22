"""Choose complete SQL fixture methods whose measured replay fits the marginal worker saving."""

from contextlib import closing
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import time

from .executor_cache import ASSET, MODULES, scalar_body_edit
from .patches import UnsupportedPatch, apply_change, parse_patch
from .scheduler import evaluate_schedule, schedule_tests


@lru_cache(maxsize=1)
def method_costs():
    if not ASSET.is_file():
        return {}
    try:
        with closing(sqlite3.connect(f"file:{ASSET}?mode=ro&immutable=1", uri=True)) as store:
            return json.loads(store.execute("SELECT data FROM metadata").fetchone()[0])["costs"]
    except (sqlite3.Error, KeyError, ValueError, TypeError):
        return {}


@lru_cache(maxsize=8)
def stage_change(root, patch):
    try:
        changes = parse_patch(patch)
        if len(changes) != 1:
            return None
        ch = changes[0]
        path = ch.old_path
        if (ch.added or ch.deleted or path != ch.new_path or not path.startswith("sqlglot/")
                or not path.endswith(".py") or path == "sqlglot/executor/table.py"):
            return None
        source = root / path
        if source.resolve() != source or not source.is_file():
            return None
        old = source.read_text()
        new = apply_change(old, ch)
        if scalar_body_edit(old, new):
            return path, old, new
    except (KeyError, OSError, SyntaxError, UnicodeError, UnsupportedPatch, ValueError, RecursionError):
        pass
    return None


def stage_costs(path, old, new):
    costs = method_costs()
    from .executor_steps import NODE, metadata, upstream_only
    from . import executor_queries
    if NODE in costs and path in executor_queries.OWNERS:
        proof = executor_queries.cost()
        if proof and executor_queries.eligible(path, old, new):
            return {**costs, NODE: min(costs[NODE], proof["cpu"])}
    if NODE in costs and upstream_only(path):
        proof = metadata()
        if proof:
            return {**costs, NODE:min(costs[NODE],proof["replay"]["cpu"])}
    return costs


def stage_candidates(root, patch, members):
    costs = method_costs()
    nodes = [n for n in members if n in costs]
    if not nodes:
        return ()
    change = stage_change(Path(root).resolve(), patch)
    if change is None:
        return ()
    costs = stage_costs(*change)
    return tuple(sorted(nodes, key=lambda n: (costs[n], n)))


def passing(checks, patch, selection, passed, selector):
    if (not checks.usable or selection.fallback or not ASSET.is_file()
            or checks.worker_cpu >= checks.budget or time.monotonic() >= checks.deadline):
        return set()
    remaining = set(selection.tests) - passed
    costs = method_costs()
    nodes = {checks.nodeids[n]: n for n in remaining if checks.nodeids[n] in costs}
    if not nodes:
        return set()
    start = checks.cpu()
    additions = set()
    try:
        ordered = stage_candidates(checks.root, patch, nodes)
        if not ordered:
            return additions
        path, old, updated = stage_change(checks.root, patch)
        costs = stage_costs(path, old, updated)
        # Keep raw readers and collection-/process-sensitive candidates on the schedule.
        readers = selector._reader_index.changed(path, old, updated)
        if readers is None:
            return additions
        veto = readers | selector._unmapped | selector._reader_index.guard
        for line in parse_patch(patch)[0].old_lines:
            veto |= selector._collection_sites.get(path, {}).get(line, 0)

        def cost(ids):
            return evaluate_schedule(schedule_tests(ids, checks.durations), checks.durations)

        # Recalculate marginal benefit after each receipt. Keep later stages optional.
        for group in MODULES:
            numbers = sorted(nodes[node] for node in ordered if node.startswith(group + "::")
                             and not veto >> nodes[node] & 1)
            if not numbers:
                continue
            choices = [(n,) for n in numbers]
            if len(numbers) > 1:
                choices.append(tuple(numbers))
            best = None
            initial = cost(remaining)
            for members in choices:
                saving = initial - cost(remaining.difference(members))
                available = min(checks.budget - checks.worker_cpu - (checks.cpu() - start),
                                saving * checks.ratio)
                # The rehearsal records complete method and lifecycle CPU. Reserve a
                # margin for a fresh interpreter and the patched code's own running time.
                estimate = 0.4 + sum(costs[checks.nodeids[n]] for n in members)
                if available > estimate and (best is None or saving - estimate > best[0]):
                    best = saving - estimate, members, available
            if best is None or time.monotonic() >= checks.deadline:
                continue
            _, members, available = best
            started = checks.cpu()
            if checks.tree is None:
                checks.prepare()
            dest = checks.tree / path
            if dest.read_bytes() != old.encode():
                return additions
            from .probe_worker import bounded_worker

            try:
                dest.write_bytes(updated.encode())
                limit = min(available - (checks.cpu() - started),
                            checks.budget - checks.worker_cpu - (checks.cpu() - start))
                wall = min(10.0, checks.deadline - time.monotonic())
                if limit <= 0.08 or wall <= 0:
                    continue
                receipt = bounded_worker(checks.tree, [(n, checks.nodeids[n]) for n in members],
                                         Path(checks.temp.name), wall, False, max_cpu=limit,
                                         changed_paths=(path,))
            finally:
                dest.write_bytes(old.encode())
            good = set(receipt.get("passed", ())) if receipt.get("reason") == "complete" else set()
            if good <= set(members):
                additions.update(good)
                remaining.difference_update(good)
        return additions
    finally:
        checks.worker_cpu += checks.cpu() - start
