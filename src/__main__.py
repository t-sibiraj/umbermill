"""One JSON output record per independent patch, logs only on stderr."""

import argparse
import json
import os
from pathlib import Path
import sys

from .impact import ImpactSelector
from .patches import UnsupportedPatch, parse_patch
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
        durations, nodeids = load_tests(args.data_dir / "tests.json")
        if args.run_all:
            selector = Selector(nodeids)
        elif args.portable:
            selector = PortableSelector(nodeids, args.source_root)
        elif args.maps.suffix == ".json" and not args.require_impact and args.granularity != "statement":
            selector = Selector(nodeids, args.maps, args.source_root, args.granularity or "file")
        else:
            granularity = args.granularity or os.environ.get("IMPACT_GRANULARITY", "file")
            selector = ImpactSelector(nodeids, args.maps, args.source_root, granularity=granularity)
        if args.require_impact and selector.unavailable:
            raise ValueError(selector.unavailable)
        field_selector = None
        if isinstance(selector, ImpactSelector) and not selector.unavailable and selector.granularity == "function":
            try:
                field_selector = FieldSelector(selector, args.maps.with_name("fields.json.gz"), args.data_dir / "tests.json")
            except (OSError, ValueError, TypeError, KeyError):
                pass
        commits = args.data_dir / "commits"
        if not commits.is_dir():
            raise ValueError("commits directory is missing")
        slack = SlackPacker(durations) if isinstance(selector, ImpactSelector) and not selector.unavailable else None
        output = []
        plain_output = []
        schedules = {}
        for path in sorted(commits.glob("*.patch")):
            patch = path.read_text(encoding="utf-8", errors="replace")
            selection = selector.select(patch)
            if field_selector and selection.fallback:
                try:
                    narrower = field_selector.narrow(patch)
                    if narrower is not None:
                        mask, _, _ = narrower
                        selection = Selection([n for n in selection.tests if (mask >> n) & 1],
                                              "observed class field consumers", False)
                except (OSError, ValueError, TypeError, KeyError, SyntaxError, RecursionError, UnsupportedPatch):
                    pass
            key = tuple(selection.tests)
            if key not in schedules:
                schedules[key] = schedule_tests(key, durations)
            workers = schedules[key]
            if slack:
                plain_output.append(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))
                try:
                    possible = 0
                    for change in parse_patch(patch):
                        relpath = change.old_path
                        possible |= (selector._files.get(relpath, 0) | selector._reads.get(relpath, 0)
                                     | selector._collection.get(relpath, 0) | selector._tests_by_file.get(relpath, 0))
                    workers = slack.fill(workers, possible)
                except (ValueError, UnsupportedPatch, RecursionError):
                    pass
            output.append(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))
            print(f"{path.stem}: {len(selection.tests)}/{len(durations)} tests; {selection.reason}", file=sys.stderr)
        payload = "\n".join(output) + ("\n" if output else "")
        if slack and len(payload.encode()) > 2_000_000:
            payload = "\n".join(plain_output) + ("\n" if plain_output else "")
        if len(payload.encode()) > 2_000_000:
            raise ValueError("output exceeds conservative 2 MB limit")
        sys.stdout.write(payload)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"selector error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
