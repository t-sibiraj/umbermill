"""Record stable TPC-DS step recipes and rehearse the original spawned unittest before enabling them."""

from contextlib import closing
from pathlib import Path
import argparse
import hashlib
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, "/app")
from umbermill import executor_cache, executor_queries, executor_steps


class Verdict(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.subtests = []
        self.passed = 0

    def addSubTest(self, case, subtest, error):
        super().addSubTest(case, subtest, error)
        self.subtests.append(subtest.id())

    def addSuccess(self, case):
        super().addSuccess(case)
        self.passed += 1


def run(args):
    root = args.root.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    from tests import test_executor as module

    # Exercise exactly the fixture initialization used by the runtime worker.
    with closing(
        sqlite3.connect(f"file:{executor_cache.ASSET}?mode=ro&immutable=1", uri=True)
    ) as db:
        info = json.loads(db.execute("SELECT data FROM metadata").fetchone()[0])
        if executor_steps.NODE not in info["costs"]:
            raise ValueError("original TPC-DS method was not qualified")
        executor_cache.configure(root, db, info, "tests/test_executor.py")
        if args.mode == "query":
            executor_queries.install(module, db, info, executor_cache.ASSET, args.asset)
        elif args.mode != "native":
            executor_steps.enable_workers(module, root, args.asset, args.mode)
        verdict = Verdict()
        case = module.TestExecutor("test_execute_tpcds")
        unittest.TestSuite([case]).run(verdict)
        if (
            not verdict.wasSuccessful()
            or verdict.passed != 1
            or not verdict.subtests
            or verdict.skipped
        ):
            raise ValueError(f"TPC-DS rehearsal did not pass: {verdict.failures + verdict.errors}")
        cost = sum(
            u.ru_stime + u.ru_utime
            for u in (
                resource.getrusage(resource.RUSAGE_SELF),
                resource.getrusage(resource.RUSAGE_CHILDREN),
            )
        )
        result = {
            "cpu": cost,
            "assertions": len(verdict.subtests),
            "digest": hashlib.sha256(executor_steps.encode(verdict.subtests)).hexdigest(),
        }
        args.receipt.write_text(json.dumps(result))


def records(scratch):
    recipes = {}
    for part in sorted(scratch.glob("steps-*.sqlite")):
        with closing(sqlite3.connect(part)) as db:
            for tag, key, action, cost in db.execute("SELECT * FROM steps"):
                before = recipes.get((tag, key))
                if before is not None and action != before[0]:
                    action = None
                recipes[tag, key] = (action, min(before[1], cost) if before else cost)
    return {k: v for k, v in recipes.items() if v[0] is not None}


def build(args):
    if os.getenv("PYTHONHASHSEED") != "0":
        raise ValueError("a fixed interpreter seed is required")
    with tempfile.TemporaryDirectory(dir=args.asset.parent) as name:
        tmp = Path(name)

        def invoke(mode, target, run):
            receipt = tmp / f"{run}.json"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    __file__,
                    "--root",
                    str(args.root),
                    "--mode",
                    mode,
                    "--asset",
                    str(target),
                    "--receipt",
                    str(receipt),
                ],
                cwd=args.root,
                check=True,
            )
            return json.loads(receipt.read_bytes())

        baseline = invoke("native", tmp, "native")
        # Measure whole-test CPU independently of physical step replay. These
        # caches serve disjoint patch families and can qualify independently.
        marker = tmp / "optimizer-hit"
        try:
            query = invoke("query", marker, "query")
        except subprocess.CalledProcessError:
            query = {}
            print(
                "Optional optimizer rehearsal failed; continuing physical step qualification",
                flush=True,
            )
        if (
            query
            and marker.exists()
            and query["digest"] == baseline["digest"]
            and query["assertions"] == baseline["assertions"]
            and query["cpu"] < baseline["cpu"]
        ):
            certified = tmp / "query-cost.json"
            certified.write_text(
                json.dumps({"cpu": query["cpu"], "identity": executor_cache.identity()})
            )
            os.chmod(certified, 0o644)
            certified.replace(executor_queries.cost_path(args.asset))
            print(
                f"Qualified SQL and optimizer stages: {query['cpu']:.3f}s vs {baseline['cpu']:.3f}s",
                flush=True,
            )
        elif query:
            print(
                f"Query rehearsal hit={marker.exists()}, CPU={query['cpu']:.3f}s, native={baseline['cpu']:.3f}s; retaining native queries",
                flush=True,
            )
        sets = []
        for n in range(2):
            folder = tmp / str(n)
            folder.mkdir()
            observed = invoke("record", folder, n)
            if (
                observed["digest"] != baseline["digest"]
                or observed["assertions"] != baseline["assertions"]
            ):
                raise ValueError("instrumentation changed pristine executor assertions")
            sets.append(records(folder))
        dbfile = tmp / "qualified.sqlite"
        with closing(sqlite3.connect(dbfile)) as db:
            executor_steps.create_tables(db)
            for k in sets[0].keys() & sets[1].keys():
                (left, t1), (right, t2) = sets[0][k], sets[1][k]
                if left == right:
                    db.execute("INSERT INTO steps VALUES(?,?,?,?)", (*k, left, min(t1, t2)))
            count = db.execute("SELECT count(*) FROM steps").fetchone()[0]
            if not count:
                raise ValueError("no reproducible executor steps")
            db.commit()
        replay = invoke("rehearse", dbfile, "replay")
        replay["workers_with_hits"] = sum(1 for _ in tmp.glob("replay-hit-*"))
        if not replay["workers_with_hits"]:
            raise ValueError("fixture rehearsal did not reuse any recorded steps")
        if replay["digest"] != baseline["digest"] or replay["assertions"] != baseline["assertions"]:
            raise ValueError("cache replay changed pristine executor assertions")
        if replay["cpu"] >= baseline["cpu"]:
            print(
                f"Original {baseline['cpu']:.3f}s, replay {replay['cpu']:.3f}s; retaining native worker",
                flush=True,
            )
            return
        # Import the same source tree used in each forked rehearsal.
        sys.path.insert(0, str(args.root))
        info = {
            "version": 1,
            "identity": executor_cache.identity(),
            "evaluators": executor_steps.kernel_bindings(),
            "native": baseline,
            "replay": replay,
            "steps": count,
            "sources": {p: executor_cache.digest(args.root / p) for p in executor_steps.SOURCES},
        }
        with closing(sqlite3.connect(dbfile)) as db:
            db.execute("CREATE TABLE metadata(data TEXT)")
            db.execute("INSERT INTO metadata VALUES(?)", (json.dumps(info),))
            db.commit()
        os.chmod(dbfile, 0o644)
        dbfile.replace(args.asset)
        print(
            f"{count} stable steps; native {baseline['cpu']:.3f}s, replay {replay['cpu']:.3f}s",
            flush=True,
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=executor_cache.BASE)
    p.add_argument("--asset", type=Path, default=executor_steps.ASSET)
    p.add_argument("--mode", choices=("native", "query", "record", "rehearse"))
    p.add_argument("--receipt", type=Path)
    args = p.parse_args()
    run(args) if args.mode else build(args)
