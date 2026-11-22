"""Execute complete unittest lifecycle checks in an isolated child."""

import contextlib, importlib, json, os, resource, signal, subprocess, sys, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Outcomes(unittest.TestResult):
    def __init__(self, known):
        super().__init__()
        self.known = known
        self.good = set()
        self.bad = set()
        self.unknown = False

    def addSuccess(self, test):
        super().addSuccess(test)
        self.good.add(test.id())

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad.add(test.id())

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.bad.add(getattr(test, "test_case", test).id())

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.bad.add(test.id())

    def addError(self, test, err):
        super().addError(test, err)
        if test.id() not in self.known:
            self.unknown = True
        self.bad.add(test.id())


def run_requested(tree, members, changed_paths=()):
    tree = tree.resolve()
    os.chdir(tree)
    sys.path.insert(0, str(tree))
    # Spawned interpreters inherit cwd and this entry through multiprocessing.
    os.environ["PYTHONPATH"] = str(tree)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    from importlib.util import find_spec

    if Path(find_spec("sqlglot").origin).resolve() != tree / "sqlglot/__init__.py":
        return {"reason": "wrong source origin"}
    if changed_paths:
        if __package__:
            from .executor_cache import install_executor_cache
        else:
            from executor_cache import install_executor_cache
        if not install_executor_cache(tree, changed_paths, tuple(node for _, node in members)):
            return {"reason": "fixture cache unavailable"}
    tests, known = [], {}
    for number, nodeid in members:
        pieces = nodeid.split("::")
        if len(pieces) != 3:
            continue
        path, cls, method = pieces
        fullname = path.removesuffix(".py").replace("/", ".")
        module = importlib.import_module(fullname)
        if Path(module.__file__).resolve() != tree / path:
            return {"reason": "wrong test origin"}
        typ = getattr(module, cls)
        if not issubclass(typ, unittest.TestCase):
            continue
        instance = typ(method)
        if (
            getattr(module, "pytestmark", None)
            or getattr(typ, "pytestmark", None)
            or getattr(getattr(typ, method), "pytestmark", None)
        ):
            continue
        tests.append(instance)
        known[instance.id()] = number
    result = Outcomes(known)
    unittest.TestSuite(tests).run(result)
    if result.unknown or result.testsRun != len(tests):
        return {"reason": "suite fixture failed or test missing"}
    good = result.good - result.bad
    return {
        "reason": "complete",
        "passed": sorted(known[name] for name in good if name in known),
        "unresolved": sorted(known[name] for name in result.bad if name in known),
        "ran": result.testsRun,
        "failed_or_unknown": len(tests) - len(good),
    }


def child_body(tree, members, receipt, changed_paths=()):
    try:
        result = run_requested(tree, members, changed_paths)
    except BaseException as exc:
        result = {"reason": "worker " + type(exc).__name__}
    receipt.write_text(json.dumps(result))


def bounded_worker(tree, members, scratch, wall, use_fork, baseline_threads=1, max_cpu=None, changed_paths=()):
    if __package__:
        from .replay_limits import ReplayMemory
    else:
        from replay_limits import ReplayMemory

    memory = ReplayMemory()
    end_cpu = cpu() + max_cpu if max_cpu is not None else None
    receipt = scratch / "receipt.json"
    receipt.unlink(missing_ok=True)
    if (
        use_fork
        and sys.platform == "linux"
        and len(list(Path("/proc/self/task").iterdir())) == baseline_threads
        and threading.active_count() == 1
    ):
        pid = os.fork()
        if pid == 0:
            os.setsid()
            with open(os.devnull, "w") as nil:
                os.dup2(nil.fileno(), 1)
                os.dup2(nil.fileno(), 2)
            child_body(tree, members, receipt, changed_paths)
            os._exit(0)
        end = time.monotonic() + wall
        while time.monotonic() < end and (end_cpu is None or cpu() < end_cpu) and not memory.over(pid):
            child, status = os.waitpid(pid, os.WNOHANG)
            if child:
                break
            time.sleep(0.012)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                # The child can still be waiting to create its process group.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return {
                "reason": ("memory budget" if memory.exceeded else
                           "cpu budget" if end_cpu is not None and cpu() >= end_cpu else "timeout")
            }
        if status:
            return {"reason": "worker exit"}
    else:
        job = scratch / "job.json"
        job.write_text(json.dumps(members))
        with subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(HERE / "probe_worker.py"),
                "worker",
                str(tree),
                str(job),
                str(receipt),
                *changed_paths,
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as proc:
            end = time.monotonic() + wall
            while (
                proc.poll() is None
                and time.monotonic() < end
                and (end_cpu is None or cpu() < end_cpu)
                and not memory.over(proc.pid)
            ):
                time.sleep(0.012)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return {
                    "reason": (
                        "memory budget" if memory.exceeded else
                        "cpu budget" if end_cpu is not None and cpu() >= end_cpu else "timeout"
                    )
                }
            if proc.returncode:
                return {"reason": "worker exit"}
    if not receipt.is_file():
        return {"reason": "no receipt"}
    return json.loads(receipt.read_bytes())


def cpu():
    cg = Path("/sys/fs/cgroup/cpu.stat")
    if cg.exists():
        return int(dict(row.split() for row in cg.read_text().splitlines())["usage_usec"]) / 1e6
    us = [resource.getrusage(x) for x in [resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN]]
    return sum(x.ru_stime + x.ru_utime for x in us)


if __name__ == "__main__" and sys.argv[1] == "worker":
    child_body(Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_bytes()), Path(sys.argv[4]), sys.argv[5:])
