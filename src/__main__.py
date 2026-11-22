"""One JSON output record per independent patch, logs only on stderr."""

import argparse
import json
import os
from pathlib import Path
import sys

from .batch import prepare
from .doc_readers import source_doc_readers
from .execution_plan import changed_comparisons
from .fixture_batch import passing as fixture_passes
from .impact import ImpactSelector
from .map_cache import load_map
from .run_cpu import CpuPlacement
from .patches import UnsupportedPatch, parse_patch, apply_change
from .slack import SlackPacker
from .precision import FieldSelector
from .selector import Selection
from .portable import PortableSelector
from .scheduler import schedule_tests
from .selector import Selector, load_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", "/data")))
    parser.add_argument("--maps", type=Path, default=Path(os.environ.get("COVERAGE_MAP", "/app/maps/impact.json.gz")))
    parser.add_argument("--source-root", type=Path, default=Path(os.environ.get("SQLGLOT_ROOT", "/app/base")))
    parser.add_argument("--granularity", choices=("file", "function", "statement"), default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-all", action="store_true", help="benchmark the full roster using the worker scheduler")
    mode.add_argument("--portable", action="store_true", help="compare selection using the pinned source without a trace map")
    mode.add_argument("--require-impact", action="store_true", help="fail if the verified impact map cannot be loaded")
    args = parser.parse_args()
    try:
        placement = CpuPlacement.start()
        durations, nodeids = load_tests(args.data_dir / "tests.json")
        if args.run_all:
            selector = Selector(nodeids)
        elif args.portable:
            selector = PortableSelector(nodeids, args.source_root)
        elif args.maps.suffix == ".json" and not args.require_impact and args.granularity != "statement":
            selector = Selector(nodeids, args.maps, args.source_root, args.granularity or "file")
        else:
            granularity = args.granularity or os.environ.get("IMPACT_GRANULARITY", "function")
            selector = ImpactSelector(nodeids, args.maps, args.source_root, granularity=granularity)
        if args.require_impact and selector.unavailable:
            raise ValueError(selector.unavailable)
        field_selector = None
        if isinstance(selector, ImpactSelector) and not selector.unavailable and selector.granularity == "function":
            try:
                field_selector = FieldSelector(selector, args.maps.with_name("fields.json.gz"), args.data_dir / "tests.json")
            except (OSError, ValueError, TypeError, KeyError):
                pass
        guard_index = None
        guard_map = args.maps.with_name("conditions.json.gz")
        if isinstance(selector,ImpactSelector) and not selector.unavailable and selector._reader_index and guard_map.is_file():
            from .guards import GuardMasks, match_hunk
            from . import BASE_COMMIT
            guard_data = load_map(guard_map)
            if guard_data.get("base_commit")==BASE_COMMIT:
                guard_index=GuardMasks(guard_data,selector._reader_index.guard)
                selector._composition_guards = guard_index
        commits = args.data_dir / "commits"
        if not commits.is_dir():
            raise ValueError("commits directory is missing")
        slack = SlackPacker(durations) if isinstance(selector, ImpactSelector) and not selector.unavailable else None
        output = {}
        plain_output = {}
        schedules = {}
        child_test = nodeids.get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
        child_proof = None
        from .runtime_checks import RuntimeChecks
        checks=RuntimeChecks(args.source_root,durations,nodeids) if isinstance(selector,ImpactSelector) and not selector.unavailable else None
        from .verification_plan import VerificationPlan
        verifier = VerificationPlan(checks) if checks is not None else None
        allow_inferred = os.environ.get("UMBERMILL_INFER_PASSES") == "1"
        primitive = None
        if checks is not None and args.maps.with_name('primitives.json.gz').is_file():
            from .primitive import PrimitiveObservations
            try:
                primitive = PrimitiveObservations(args.maps.with_name('primitives.json.gz'), nodeids)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        executor_phases = None
        if checks is not None and args.maps.with_name('executor-phases.json.gz').is_file():
            from .executor_phases import ExecutorPhases
            try:
                executor_phases = ExecutorPhases(args.maps.with_name('executor-phases.json.gz'), nodeids)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        jobs = prepare(commits, selector, durations)
        completed_fixtures = fixture_passes(jobs, selector, checks)
        for job in jobs:
            placement.release_if_delayed()
            path, patch, selection = job.path, job.text, job.selection
            mandatory = set()
            if checks is not None and selection.basis != "shared state":
                mandatory = source_doc_readers(selector, patch, nodeids)
                mandatory.update(changed_comparisons(selector, patch))
                if mandatory:
                    selection.tests = sorted(mandatory.union(selection.tests))
            if selection.basis == "coverage" and field_selector and selection.fallback:
                try:
                    narrower = field_selector.narrow(patch)
                    if narrower is not None:
                        mask, _, _ = narrower
                        selection = Selection([n for n in selection.tests if (mask >> n) & 1],
                                              "observed class field consumers", False)
                except (OSError, ValueError, TypeError, KeyError, SyntaxError, RecursionError, UnsupportedPatch):
                    pass
            if selection.basis == "coverage" and guard_index and (not selection.fallback or selection.reason ==
                                "uncertain impact: changed function can run during module loading or test collection"):
                try:
                    changes=parse_patch(patch)
                    if len(changes)==1:
                        c=changes[0]
                        eligible=guard_index.by_path.get(c.old_path, set())
                        if c.old_path==c.new_path and not c.added and not c.deleted and len(c.old_lines)==1 and c.old_lines & eligible:
                            item=match_hunk(c)
                            if item is not None:
                                old=selector._source(c.old_path)
                                new=apply_change(old,c)
                                readers=selector._reader_index.changed(c.old_path,old,new)
                                if readers is not None:
                                    current=sum(1<<i for i in selection.tests)
                                    mask=guard_index.select(c,item,readers,selector._collection_sites.get(c.old_path,{}),current)
                                    if mask is not None:
                                        mask|=selector._unmapped | (current & selector._sites.get(c.old_path,{}).get(next(iter(c.old_lines)),0))
                                        selection=Selection([n for n in selection.tests if (mask>>n)&1],"observed condition and worker dependencies",False)
                except (OSError,ValueError,KeyError,TypeError,UnsupportedPatch,SyntaxError,RecursionError):
                    pass
            passed, protected = completed_fixtures.get(path.stem, (set(), set()))
            mandatory.update(protected)
            shared_state = selection.basis == "shared state"
            native_only = selection.basis == "layout"
            if not shared_state and checks is not None:
                if selection.basis == "coverage":
                    try:
                        passed.update(checks.passing(patch, selection, selector))
                    except (OSError, ValueError, RuntimeError):
                        checks.usable = False
                    from .executor_stage import passing as executor_passing
                    try:
                        passed.update(executor_passing(checks, patch, selection, passed, selector))
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                        pass
                if not native_only and primitive is not None and primitive.passing(selector, patch, selection):
                    passed.add(primitive.number)
                if not native_only and executor_phases is not None:
                    passed.update(executor_phases.passing(selector, patch, selection))
                if selection.basis in {"dependencies", "layout"} or selection.fallback:
                    passed.update(verifier.omit(patch, selection, selector, passed, must=mandatory,
                                                allow_inferred=allow_inferred))
                    mandatory.update(verifier.failures)
                    if allow_inferred:
                        passed.update(verifier.inferred)
            passed.difference_update(mandatory)
            if mandatory:
                selection.tests = sorted(mandatory.union(selection.tests))
            key=tuple(n for n in selection.tests if n not in passed)
            if key not in schedules:
                schedules[key] = schedule_tests(key, durations)
            workers = schedules[key]
            # On a single occupied worker each proved pass saves its full duration.
            if (not shared_state and not native_only and checks is not None and len(workers) == 1
                    and child_test in workers[0] and child_test not in mandatory and not selection.fallback):
                try:
                    if child_proof is None:
                        from .process_proof import FreshProcess
                        child_proof = FreshProcess(args.maps.with_name("fresh-process.json.gz"), nodeids)
                    if child_proof.passing(selector, patch, selection):
                        passed.add(child_test)
                        key = tuple(n for n in key if n != child_test)
                        if key not in schedules:
                            schedules[key] = schedule_tests(key, durations)
                        workers = schedules[key]
                except (OSError, ValueError, KeyError, TypeError):
                    child_test = None
            if slack:
                plain_output[path.stem] = json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":"))
                try:
                    possible = 0
                    for change in parse_patch(patch):
                        relpath = change.old_path
                        possible |= (selector._files.get(relpath, 0) | selector._reads.get(relpath, 0)
                                     | selector._collection.get(relpath, 0) | selector._tests_by_file.get(relpath, 0))
                    possible &= ~sum(1 << n for n in passed)
                    workers = slack.fill(workers, possible)
                except (ValueError, UnsupportedPatch, RecursionError):
                    pass
            output[path.stem] = json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":"))
            print(f"{path.stem}: {len(selection.tests)}/{len(durations)} tests; {selection.reason}", file=sys.stderr)
        payload = "\n".join(output[key] for key in sorted(output)) + ("\n" if output else "")
        if slack and len(payload.encode()) > 2_000_000:
            payload = "\n".join(plain_output[key] for key in sorted(plain_output)) + ("\n" if plain_output else "")
        if len(payload.encode()) > 2_000_000:
            raise ValueError("output exceeds conservative 2 MB limit")
        sys.stdout.write(payload)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"selector error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
