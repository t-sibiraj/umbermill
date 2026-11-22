"""Plan, replay and schedule independent patches against pinned SQLGlot."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import BASE_COMMIT
from .doc_readers import source_doc_readers
from .execution_plan import changed_comparisons
from .guards import GuardMasks
from .impact import ImpactSelector
from .map_cache import load_map
from .portable import PortableSelector
from .precision import FieldSelector
from .replay_planner import plan_patch
from .replay_schedule import pack
from .replay_session import ReplaySession
from .replay_trace import trace_decision
from .run_cpu import CpuPlacement
from .scheduler import schedule_tests
from .selector import Selection, Selector, load_tests


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", "/data")))
    parser.add_argument("--maps", type=Path, default=Path(os.environ.get("COVERAGE_MAP", "/app/maps/impact.json.gz")))
    parser.add_argument("--source-root", type=Path, default=Path(os.environ.get("SQLGLOT_ROOT", "/app/base")))
    parser.add_argument("--granularity", choices=("file", "function", "statement"), default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-all", action="store_true", help="schedule the full roster for comparison")
    mode.add_argument("--portable", action="store_true", help="use source-only selection for comparison")
    mode.add_argument("--require-impact", action="store_true", help="fail if the dependency map cannot be loaded")
    return parser.parse_args()


def _configure(evidence, args):
    evidence._field_selector = None
    evidence._composition_guards = None
    try:
        evidence._field_selector = FieldSelector(evidence, args.maps.with_name("fields.json.gz"),
                                                args.data_dir / "tests.json")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if evidence._reader_index is None:
        return
    try:
        conditions = load_map(args.maps.with_name("conditions.json.gz"))
        if conditions.get("base_commit") == BASE_COMMIT:
            evidence._composition_guards = GuardMasks(conditions, evidence._reader_index.guard)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _whole_roster(evidence, reason):
    return Selection(list(evidence.all_tests), reason, True, "static")


def _cases(paths, evidence, deadline):
    for path in paths:
        if time.monotonic() >= deadline:
            yield path.stem, "", _whole_roster(evidence, "batch planning limit")
            continue
        try:
            patch = path.read_bytes().decode("utf-8")
            selection = plan_patch(evidence, patch)
            if selection is None:
                selection = _whole_roster(evidence, "unresolved patch plan")
        except (OSError, ValueError, TypeError, KeyError, RecursionError, SyntaxError):
            patch = ""
            selection = _whole_roster(evidence, "unresolved patch input")
        yield path.stem, patch, selection


def _emit_schedule(cases, number_of_tests, durations, schedule=pack):
    assignments = {}
    output = []
    for name, selected, reason in sorted(cases):
        key = tuple(sorted(set(selected)))
        if key not in assignments:
            assignments[key] = schedule(key, durations)
        output.append(json.dumps({"commit": name, "workers": assignments[key]}, separators=(",", ":")))
        print(f"{name}: {len(key)}/{number_of_tests} tests, {reason}", file=sys.stderr)
    if output:
        payload = ("\n".join(output) + "\n").encode("utf-8")
        if len(payload) > 2_000_000:
            raise ValueError("output exceeds the 2 MB limit")
        sys.stdout.buffer.write(payload)


def main():
    args = _arguments()
    start = time.monotonic()
    placement = CpuPlacement.start()
    try:
        durations, nodes = load_tests(args.data_dir / "tests.json")
        roster = {number: node for node, number in nodes.items()}
        directory = args.data_dir / "commits"
        if not directory.is_dir():
            raise ValueError("commits directory is missing")
        paths = sorted(directory.glob("*.patch"))
        level = args.granularity or os.environ.get("IMPACT_GRANULARITY", "function")
        diagnostic = None
        if args.run_all:
            diagnostic = Selector(nodes)
        elif args.portable:
            diagnostic = PortableSelector(nodes, args.source_root)
        elif args.maps.suffix == ".json" and not args.require_impact and level != "statement":
            diagnostic = Selector(nodes, args.maps, args.source_root, args.granularity or "file")
        elif level in {"file", "statement"}:
            diagnostic = ImpactSelector(nodes, args.maps, args.source_root, granularity=level)
        if diagnostic is not None:
            schedules = [(path.stem, choice.tests, choice.reason) for path in paths
                         if (choice := diagnostic.select(path.read_text(encoding="utf-8"))) is not None]
            _emit_schedule(schedules, len(roster), durations, schedule_tests)
            return 0

        evidence = ImpactSelector(nodes, args.maps, args.source_root, granularity="function")
        if evidence.unavailable:
            if args.require_impact:
                raise ValueError(evidence.unavailable)
            _emit_schedule(((path.stem, durations, evidence.unavailable) for path in paths), len(roster), durations)
            return 0
        _configure(evidence, args)
        deadline = start + min(210.0, 150.0 + max(0, len(paths) - 45) * 4.0)
        cases = list(_cases(paths, evidence, deadline - 3.0))
        stage = {"static": 0, "shared state": 0, "hot": 1, "fixture": 2, "fresh": 3, "layout": 4}
        cases.sort(key=lambda case: (stage.get(case[2].basis, 3), sum(durations[n] for n in case[2].tests), case[0]))
        always = {n for n in roster if evidence._unmapped & (1 << n)}
        session = ReplaySession(evidence, roster, durations, deadline - 2.0)
        schedules = []
        trace = os.environ.get("UMBERMILL_REPLAY_TRACE") == "1"
        try:
            for name, patch, selection in cases:
                placement.release_if_delayed()
                needed = always | source_doc_readers(evidence, patch, nodes) | changed_comparisons(evidence, patch)
                if trace:
                    began = time.monotonic()
                    print(f"replay {name} {selection.basis} candidates={len(selection.tests)}", file=sys.stderr, flush=True)
                trace_decision("candidates", commit=name, mode=selection.basis, reason=selection.reason,
                               tests=selection.tests)
                chosen = session.choose(selection, patch, needed)
                trace_decision("selection", commit=name, tests=sorted(chosen))
                if trace:
                    print(f"replay {name} selected={len(chosen)} elapsed={time.monotonic() - began:.3f}",
                          file=sys.stderr, flush=True)
                schedules.append((name, chosen, selection.reason))
        finally:
            session.close()
        _emit_schedule(schedules, len(roster), durations)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"selector error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
