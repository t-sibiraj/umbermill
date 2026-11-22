"""Run original pytest lifecycles in a disposable patched interpreter."""

import gc
import contextlib
import importlib
import json
import linecache
import os
from pathlib import Path
import sys
import signal
import warnings
from types import CodeType, FunctionType

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT))


def extension(name):
    target = "umbermill." + name
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as error:
        if error.name != target:
            raise
        return None


def settings():
    environment = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    dialect = sys.modules.get('sqlglot.dialects.dialect')
    meta = vars(dialect).get('_Dialect') if dialect is not None else None
    registry = vars(meta).get('_classes') if type(meta) is type else None
    registrations = (id(registry), tuple((id(k),id(v)) for k,v in registry.items())) if type(registry) is dict else None
    return (environment, tuple(sys.path), tuple(sys.meta_path), sys.getrecursionlimit(),
            sys.getswitchinterval(), os.getcwd(), tuple(warnings.filters), registrations)


def direct_or_pytest(job, job_path, numbers, event, clock):
    from umbermill import direct_runner

    tree = Path(job["tree"])
    if not direct_runner.candidate(job, tree):
        return False
    tasks = Path("/proc/self/task")
    if not tasks.is_dir() or len(os.listdir(tasks)) != 1:
        return False
    # Collection imports production code. A rejected child may already have
    # modified its Python state or its files, so restart pytest from a clean tree.
    pid = os.fork()
    if pid == 0:
        try:
            completed = direct_runner.run(job, numbers, event, clock, settings)
            os._exit(0 if completed else 10)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) == 0 or Path(job["journal"]).stat().st_size:
        return True
    from umbermill.verification_process import prepare_tree
    from umbermill.patches import parse_patch

    clean = tree.with_name(tree.name + "-pytest")
    if not prepare_tree(Path(job["base"]), clean, parse_patch(job["patch"])):
        return True
    cold_job = job_path.with_suffix(".pytest-job")
    cold_job.write_text(json.dumps(dict(job, tree=str(clean), direct=False)))
    os.execv(sys.executable, [sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(cold_job)])


def replay(job_path):
    job = json.loads(job_path.read_bytes())
    tree = Path(job["tree"])
    base = Path(job["base"])
    os.chdir(tree)
    sys.path.insert(0, str(tree))
    os.environ["PYTHONPATH"] = str(tree)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    import pytest
    from umbermill.probe_worker import cpu

    expected = job['collection']
    files = list(dict.fromkeys(name.split('::', 1)[0] for name in expected))
    numbers = {node: n for n, node in job["members"]}
    if (not numbers or len(numbers) != len(job['members'])
            or [name for name in expected if name in numbers] != list(numbers)):
        return
    paths = tuple(job["changes"])
    native_only = job.get("native_only", False)
    original = importlib.util.find_spec("sqlglot")
    if original is None or Path(original.origin).resolve() != tree / "sqlglot/__init__.py":
        return
    lazy_health = None
    if (not native_only and os.environ.get("UMBERMILL_LAZY_IMPORTS") == "1"
            and all(p.startswith("sqlglot/") for p in paths)):
        from umbermill.deferred_libraries import defer_libraries

        lazy_health = defer_libraries()
    cache = None if native_only else extension("executor_cache")
    cache_installed = cache is not None and cache.install_executor_cache(tree, paths, tuple(numbers))
    unit = extension("verification_units") if job["units"] else None
    reducer = unit.replay_plugin(base, tree, job["patch"], tuple(numbers), Path(job["maps"])) if unit else None
    tokens = None if native_only else extension("token_replay")

    with open(job["journal"], "w", buffering=1) as journal:
        def event(*values):
            journal.write(json.dumps(values, separators=(",", ":")) + "\n")

        if (job.get("direct") and not cache_installed and not lazy_health and not reducer
                and not (tokens and any(tokens.eligible(node, paths) for node in numbers))
                and direct_or_pytest(job, job_path, numbers, event, cpu)):
            return

        class Reporter:
            def __init__(self):
                self.seen = {}
                self.bad = False
                self.matched = False

            @pytest.hookimpl(trylast=True)
            def pytest_collection_modifyitems(self, session, config, items):
                if [item.nodeid for item in items] != expected:
                    pytest.exit("changed or incomplete roster", returncode=3)
                self.matched = True
                others = [item for item in items if item.nodeid not in numbers]
                items[:] = [item for item in items if item.nodeid in numbers]
                config.hook.pytest_deselected(items=others)

            @pytest.hookimpl(trylast=True)
            def pytest_collection_finish(self, session):
                if not self.matched or session.testsfailed or [i.nodeid for i in session.items] != list(numbers):
                    pytest.exit('incomplete selected collection',returncode=3)
                event("ready")

            def pytest_runtest_logstart(self, nodeid, location):
                self.seen[nodeid] = [set(), set(), cpu(), settings()]
                event("start", numbers[nodeid])

            @pytest.hookimpl(tryfirst=True)
            def pytest_runtest_setup(self, item):
                if self.bad and numbers[item.nodeid] in job["after_failure"]:
                    pytest.skip("remaining short check deferred")

            @pytest.hookimpl(wrapper=True)
            def pytest_runtest_call(self, item):
                if tokens is None:
                    return (yield)
                with tokens.accelerate(item.nodeid, paths):
                    return (yield)

            def pytest_runtest_logreport(self, report):
                row = self.seen.get(report.nodeid)
                if row is None:
                    if report.failed:
                        event('unattributed_failure')
                    return
                row[0].add(report.when)
                if report.failed and 'fail' not in row[1]:
                    # The next subtest or teardown may never return.
                    event('failure', numbers[report.nodeid], report.when)
                if report.failed or report.skipped or getattr(report, "wasxfail", None):
                    row[1].add("fail" if report.failed else "unknown")
                self.bad |= report.failed

            def pytest_runtest_logfinish(self, nodeid, location):
                stages, results, began, before = self.seen.pop(nodeid)
                snapshot = settings()
                if snapshot != before:
                    event("state", numbers[nodeid])
                if "fail" in results:
                    verdict = "failed"
                elif results or stages != {"setup", "call", "teardown"}:
                    verdict = "unknown"
                elif reducer is not None and nodeid in getattr(reducer, "partial", ()):
                    verdict = "partial"
                else:
                    verdict = "pass"
                event("end", numbers[nodeid], verdict, max(0.,cpu() - began))

        plugins = [Reporter()] if reducer is None else [reducer, Reporter()]
        rc = pytest.main(["-q", "--maxfail=0", "-p", "no:cacheprovider", "--disable-warnings", *files], plugins=plugins)
        if lazy_health and not lazy_health():
            event("state", min(numbers.values()))
        event("complete", int(rc))


def repoint_modules(modules, base, tree):
    def replace(name):
        if not isinstance(name, str):
            return name
        p = Path(name)
        return str(tree / p.relative_to(base)) if p.is_relative_to(base) else name
    for module in modules.values():
        if module is None:
            continue
        spec = getattr(module, "__spec__", None)
        source = getattr(module, "__file__", None)
        if not source or replace(source) == source:
            continue
        module.__file__ = replace(source)
        if getattr(module, "__cached__", None):
            module.__cached__ = replace(module.__cached__)
        if spec:
            spec.origin = replace(spec.origin)
            if hasattr(spec.loader, "path"):
                spec.loader.path = replace(spec.loader.path)
            if spec.submodule_search_locations is not None:
                spec.submodule_search_locations = list(map(replace, spec.submodule_search_locations))
                module.__path__ = spec.submodule_search_locations
    sys.path[:] = [replace(p) for p in sys.path]
    sys.path_importer_cache.clear()
    importlib.invalidate_caches()
    linecache.clearcache()


def repoint_code(base, tree):
    cache = {}
    def moved(code):
        if id(code) not in cache:
            here = Path(code.co_filename)
            filename = str(tree / here.relative_to(base)) if here.is_relative_to(base) else code.co_filename
            children = tuple(moved(c) if isinstance(c, CodeType) else c for c in code.co_consts)
            cache[id(code)] = code, code.replace(co_filename=filename, co_consts=children)
        return cache[id(code)][1]
    for obj in gc.get_objects():
        if type(obj) is FunctionType and Path(obj.__code__.co_filename).is_relative_to(base):
            obj.__code__ = moved(obj.__code__)


def has_old_paths(values, base, seen):
    if isinstance(values, (str, Path)):
        return str(values).startswith(str(base) + os.sep) or str(values) == str(base)
    if id(values) in seen or type(values) not in (tuple, list, dict, frozenset, set):
        return False
    seen.add(id(values))
    if isinstance(values, dict):
        return any(has_old_paths(k,base,seen) or has_old_paths(v,base,seen) for k,v in values.items())
    return any(has_old_paths(item,base,seen) for item in values)


def checkpoint(base):
    activation = extension("patch_activation")
    if activation is None or "sqlglot" in sys.modules:
        return
    sys.path.insert(0, str(base))
    os.chdir(base)
    importlib.import_module('pytest')
    with activation.ImportActivity() as observations:
        importlib.import_module("sqlglot")
    if len(os.listdir("/proc/self/task")) != 1:
        return
    print("ready", flush=True)
    for line in sys.stdin:
        job_path = Path(json.loads(line))
        process = os.fork()
        if process == 0:
            try:
                os.setsid()
                job = json.loads(job_path.read_bytes())
                tree = Path(job["tree"])
                replacements = {p: (tree / p).read_text() for p in job["changes"]}
                repoint_modules(dict(sys.modules), base, tree)
                os.chdir(tree)
                os.environ['PYTHONPATH'] = str(tree)
                if not activation.activate(sys.modules, replacements, base_root=base, overlay_root=tree, observed=observations):
                    os._exit(10)
                repoint_code(base, tree)
                seen = set()
                if any(has_old_paths(vars(m),base,seen) for k,m in tuple(sys.modules.items())
                       if m is not None and (k == 'sqlglot' or k.startswith('sqlglot.'))):
                    os._exit(10)
                with open(os.devnull, "w") as quiet:
                    os.dup2(quiet.fileno(), 1)
                    os.dup2(quiet.fileno(), 2)
                    replay(job_path)
                os._exit(0)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(process, 0)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process, signal.SIGKILL)
        job_path.with_suffix(".status").write_text(str(os.waitstatus_to_exitcode(status)))


if __name__ == "__main__":
    if sys.argv[1] == "checkpoint":
        checkpoint(Path(sys.argv[2]).resolve())
    else:
        replay(Path(sys.argv[1]))
