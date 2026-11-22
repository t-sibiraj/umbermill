"""Defer optional numerical packages in dedicated verification processes."""

import importlib
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
import sys
import threading
from types import ModuleType


_LIBRARIES = ("numpy", "pandas", "duckdb")
_TESTING = "pandas.testing"


def defer_libraries():
    """Install the real module objects and initialize them on first use.

    Call only in a disposable replay child before it imports test code.
    The caller can retry with ordinary imports when lazy replay is incomplete.
    """
    if threading.active_count() != 1 or any(
        name in sys.modules for name in (*_LIBRARIES, _TESTING)
    ):
        return False
    specs = {name: PathFinder.find_spec(name) for name in _LIBRARIES}
    if any(spec is None or not hasattr(spec.loader, "exec_module") for spec in specs.values()):
        return False
    modules = {name: module_from_spec(spec) for name, spec in specs.items()}
    testing = ModuleType(_TESTING)
    testing.__package__ = "pandas"
    lock = threading.RLock()
    pending = set(modules)
    poison = {}

    def load(name):
        with lock:
            if name in poison:
                raise ImportError(f"deferred import of {name} failed") from poison[name]
            module = modules[name]
            if name not in pending:
                return module
            spec = specs[name]
            if sys.modules.get(name) is not module:
                raise ImportError(f"module replaced before deferred import of {name}")
            if name == "pandas" and sys.modules.get(_TESTING) is testing:
                del sys.modules[_TESTING]
            pending.remove(name)
            vars(module).pop("__getattr__", None)
            if spec.submodule_search_locations is not None:
                module.__path__ = spec.submodule_search_locations
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:
                poison[name] = exc
                # A failed lazy load cannot certify a replay. Keep a marker
                # even if the tests themselves subsequently catch the error.
                vars(module)["__getattr__"] = lookup(name)
                raise
            if sys.modules.get(name) is not module:
                error = ImportError(f"deferred import changed module identity for {name}")
                poison[name] = error
                raise error
            return module

    def lookup(name):
        def read(attribute):
            return getattr(load(name), attribute)
        return read

    def assertion(*args, **kwargs):
        load("pandas")
        return importlib.import_module(_TESTING).assert_frame_equal(*args, **kwargs)

    def testing_lookup(attribute):
        if attribute.startswith("__") and attribute.endswith("__"):
            raise AttributeError(attribute)
        load("pandas")
        return getattr(importlib.import_module(_TESTING), attribute)

    testing.assert_frame_equal = assertion
    testing.__getattr__ = testing_lookup
    for name, module in modules.items():
        # A submodule import must initialize its parent package first.
        vars(module).pop("__path__", None)
        module.__getattr__ = lookup(name)
    sys.modules.update(modules)
    sys.modules[_TESTING] = testing
    return lambda: not poison
