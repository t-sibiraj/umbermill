"""Replay ordinary unittest cases after checking their original pytest collection order."""

import importlib
import inspect
import json
import os
import re
import unittest
from pathlib import Path
from types import FunctionType

from .replay_policy import ReplayDeferral


class Unsupported(Exception):
    pass


# These edits can observe the test runner or its process-wide configuration.
RUNNER_OBSERVER = re.compile(
    r"\b(?:pytest\w*|PYTEST_\w+|unittest|inspect|traceback|warnings|sys|logging|"
    r"environ\w*|getenv|stdout|stderr|_outcome|_getframe|currentframe|f_back|"
    r"stacklevel|exceptiongroup|atexit|settrace|setprofile)\b", re.I
)
XUNIT_HOOKS = {"setup_module", "teardown_module", "setup_class", "teardown_class",
               "setup_method", "teardown_method", "setup_function", "teardown_function",
               "pytestmark", "pytest_plugins", "load_tests"}
CASE_INTERNALS = {"__call__", "__getattribute__", "__getattr__", "__init__", "__iter__", "__repr__", "__str__",
                  "run", "subTest", "_callSetUp", "_callTestMethod", "_callTearDown", "_callCleanup",
                  "doCleanups", "id", "shortDescription", "defaultTestResult"}


def candidate(job, tree):
    if not job.get("direct") or job.get("native_only") or job.get("units") or job.get("stage"):
        return False
    if not job["changes"] or any(
        not (p.startswith("sqlglot/") and p.endswith(".py")
             or p.startswith("tests/fixtures/") and p.endswith(".sql"))
        for p in job["changes"]
    ):
        return False
    if any(RUNNER_OBSERVER.search(line[1:]) for line in job["patch"].splitlines()
           if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))):
        return False
    if any(os.environ.get(k) for k in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_TIMEOUT")):
        return False
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1":
        from importlib.metadata import entry_points

        if any(ep.name not in {"timeout", "xdist", "xdist.looponfail"} for ep in entry_points(group="pytest11")):
            return False
    for file in ("pytest.ini", "pytest.toml", ".pytest.ini", ".pytest.toml"):
        if (tree / file).exists():
            return False
    for file in ("setup.cfg", "pyproject.toml", "tox.ini"):
        config = tree / file
        if config.exists() and b"pytest" in config.read_bytes().lower():
            return False
    return True


def fixture_marked(value):
    return type(value).__module__.startswith("_pytest.") or (
        type(value) is FunctionType and any(
            key in vars(value) for key in ("pytestmark", "_fixture_function_marker", "_pytestfixturefunction")
        )
    )


def plain_function(fn, tree, args):
    if type(fn) is not FunctionType or hasattr(fn, "pytestmark") or fn.__annotations__:
        return False
    code = fn.__code__
    return (not hasattr(fn, "__wrapped__") and code.co_argcount == args and not code.co_kwonlyargcount
            and not code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS |
                                     inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR)
            and Path(code.co_filename).is_relative_to(tree / "tests")
            and not any(RUNNER_OBSERVER.fullmatch(name) for name in code.co_names))


def ordinary_class(cls, tree):
    if type(cls) is not type or cls.__new__ is not object.__new__:
        return False
    for base in cls.__mro__:
        if base is unittest.TestCase:
            break
        scope = vars(base)
        if (not base.__module__.startswith("tests.") or CASE_INTERNALS.intersection(scope)
                or XUNIT_HOOKS.intersection(scope) or "__test__" in scope):
            return False
        if any(map(fixture_marked, scope.values())):
            return False
        for name in ("setUp", "tearDown", "setUpClass", "tearDownClass"):
            if name in scope:
                fn = scope[name]
                if not plain_function(fn.__func__ if type(fn) is classmethod else fn, tree, 1):
                    return False
    else:
        return False
    return True


def collect(job, numbers):
    tree, base = Path(job["tree"]), Path(job["base"])
    expected = job["collection"]
    files = list(dict.fromkeys(node.split("::", 1)[0] for node in expected))
    file_set = set(files)
    manifest = json.loads((Path(job["maps"]) / "pristine-collection.json").read_bytes())
    if (manifest.get("state") != "collected" or manifest.get("code") != 0 or manifest.get("errors")
            or [node for node in manifest["items"] if node.split("::", 1)[0] in file_set] != expected):
        raise Unsupported
    cases, actual = {}, []
    vetted = set()
    for file in files:
        relative = Path(file)
        if (not relative.parts or relative.parts[0] != "tests" or relative.suffix != ".py"
                or any(not p.isidentifier() for p in relative.with_suffix("").parts)):
            raise Unsupported
        source = tree / relative
        text = source.read_bytes()
        parents = (p for p in source.parents if p.is_relative_to(tree))
        if (source.resolve() != source or text != (base / relative).read_bytes()
                or any((p / "conftest.py").exists() for p in parents) or b"pytest" in text.lower()):
            raise Unsupported
        module = importlib.import_module(".".join(relative.with_suffix("").parts))
        scope = vars(module)
        if (Path(module.__file__).resolve() != source or XUNIT_HOOKS.intersection(scope)
                or any(k.startswith("pytest_") for k in scope) or any(map(fixture_marked, scope.values()))):
            raise Unsupported
        for name in ("setUpModule", "tearDownModule"):
            if name in scope and not plain_function(scope[name], tree, 0):
                raise Unsupported
        for name, obj in scope.items():
            if not isinstance(obj, type) or not issubclass(obj, unittest.TestCase):
                if name.startswith("test") and callable(obj):
                    raise Unsupported
                continue
            if obj is unittest.TestCase:
                continue
            methods = unittest.defaultTestLoader.getTestCaseNames(obj)
            if not methods and hasattr(obj, "runTest"):
                raise Unsupported
            for method in methods:
                node = f"{file}::{name}::{method}"
                actual.append(node)
                if node in numbers:
                    if obj not in vetted:
                        if obj.__module__ != module.__name__ or not ordinary_class(obj, tree):
                            raise Unsupported
                        vetted.add(obj)
                    fn = inspect.getattr_static(obj, method)
                    if not (type(fn) is FunctionType and getattr(fn, "__unittest_skip__", False)
                            and not fn.__annotations__) and not plain_function(fn, tree, 1):
                        raise Unsupported
                    cases[node] = obj(method)
    if actual != expected or list(cases) != list(numbers):
        raise Unsupported
    return cases


class Records(unittest.TestResult):
    def __init__(self, numbers, cases, event, clock, snapshot):
        super().__init__()
        self.labels = {id(case): numbers[node] for node, case in cases.items()}
        self.event, self.clock, self.snapshot = event, clock, snapshot
        self.active = None
        self.bad = False
        self.unattributed = False
        self.finished = 0
        self.between = snapshot()

    def startTest(self, case):
        super().startTest(case)
        self.active = self.labels[id(case)]
        self.event("start", self.active)
        self.began, self.before, self.answer = self.clock(), self.snapshot(), None
        if self.before != self.between:
            self.event("state", self.active)

    def stopTest(self, case):
        self.between = self.snapshot()
        if self.between != self.before:
            self.event("state", self.active)
        self.event("end", self.active, self.answer or "unknown", max(0., self.clock() - self.began))
        self.active = None
        self.finished += 1
        super().stopTest(case)

    def failure(self, case, err=None):
        if err and err[0].__module__ == "_pytest.outcomes" and err[0].__name__ in ("Skipped", "XFailed"):
            self.addSkip(case, "")
            return
        self.bad = True
        if self.active is None or self.labels.get(id(case)) != self.active:
            self.event("unattributed_failure")
            self.unattributed = True
            return
        if self.answer == "failed":
            return
        phase = "call"
        if err:
            tb = err[2]
            while tb:
                frame = tb.tb_frame
                if frame.f_globals.get("__name__") == "unittest.case":
                    phase = {"_callSetUp": "setup", "_callTearDown": "teardown",
                             "_callCleanup": "teardown"}.get(frame.f_code.co_name, phase)
                tb = tb.tb_next
        self.event("failure", self.active, phase)
        self.answer = "failed"

    addError = addFailure = failure
    addUnexpectedSuccess = failure

    def addSuccess(self, case):
        if self.answer is None:
            self.answer = "pass"

    def addSkip(self, case, reason):
        if self.active is None:
            self.unattributed = True
        elif self.answer != "failed":
            self.answer = "unknown"

    addExpectedFailure = addSkip

    def addSubTest(self, case, subtest, err):
        if err is not None:
            self.failure(case, err)


def run(job, numbers, event, clock, snapshot):
    """Return False only before the first journal record; a partial replay is inconclusive."""
    try:
        cases = collect(job, numbers)
    except Exception:
        return False
    result = Records(numbers, cases, event, clock, snapshot)
    deferral = ReplayDeferral(job)
    stop_after_failure = job.get("stop_after_failure", False)

    class Suite(unittest.TestSuite):
        _cleanup = False

        def __iter__(self):
            for case in super().__iter__():
                label = result.labels[id(case)]
                if deferral.should_defer(label) or result.bad and stop_after_failure:
                    result.startTest(case)
                    result.answer = "deferred"
                    result.stopTest(case)
                else:
                    yield case
                    if result.answer == "failed":
                        deferral.observe_failure(label)

    event("ready")
    Suite(list(cases.values())).run(result)
    if (result.unattributed or result.finished != len(cases)
            or result.between != snapshot()):
        event("unattributed_failure")
        event("complete", 3)
    else:
        event("complete", int(result.bad))
    return True
