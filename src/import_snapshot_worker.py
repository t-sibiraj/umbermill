"""Inspect one import in a clean interpreter, with one source override."""

import contextlib
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import types


class Override(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, name, path, source):
        self.name, self.path, self.source = name, path, source

    def find_spec(self, fullname, path, target=None):
        if fullname != self.name:
            return None
        if self.source is None:
            raise ModuleNotFoundError(name=fullname)
        return importlib.util.spec_from_file_location(fullname, self.path, loader=self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, self.path, "exec", dont_inherit=True), vars(module))


def binding(value, depth=0):
    if depth > 12:
        raise ValueError("unbounded binding")
    typ = type(value)
    if value is None or typ in (str, int, bool, float):
        return [typ.__name__, value]
    if typ is bytes:
        return ["bytes", value.hex()]
    if typ in (tuple, list, set, frozenset):
        items = [binding(v, depth + 1) for v in value]
        if typ in (set, frozenset):
            items.sort(key=repr)
        return [typ.__name__, items]
    if typ is dict:
        return [
            "dict",
            [[binding(k, depth + 1), binding(v, depth + 1)] for k, v in value.items()],
        ]
    if isinstance(value, type):
        return [
            "class",
            type.__getattribute__(value, "__module__"),
            type.__getattribute__(value, "__qualname__"),
        ]
    if typ is types.CodeType:
        return [
            "code",
            value.co_code.hex(),
            value.co_exceptiontable.hex(),
            value.co_names,
            value.co_varnames,
            value.co_cellvars,
            value.co_freevars,
            value.co_argcount,
            value.co_posonlyargcount,
            value.co_kwonlyargcount,
            value.co_flags,
            value.co_stacksize,
            value.co_name,
            value.co_qualname,
            [binding(v, depth + 1) for v in value.co_consts],
        ]
    if value is Ellipsis:
        return ["ellipsis"]
    if typ is types.FunctionType:
        cells = [binding(c.cell_contents, depth + 1) for c in value.__closure__ or ()]
        return [
            "function",
            value.__module__,
            value.__qualname__,
            binding(value.__code__, depth + 1),
            binding(value.__defaults__, depth + 1),
            binding(value.__kwdefaults__, depth + 1),
            cells,
        ]
    if typ is types.MethodType:
        return [
            "method",
            binding(value.__self__, depth + 1),
            binding(value.__func__, depth + 1),
        ]
    if isinstance(value, types.BuiltinFunctionType):
        return ["builtin", value.__module__, value.__qualname__]
    import enum

    if isinstance(value, enum.Enum):
        return ["enum", binding(type(value)), value.name]
    raise ValueError("opaque binding")


def digest(value):
    try:
        buf = json.dumps(
            binding(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(buf).hexdigest()
    except (ValueError, TypeError, RecursionError, OverflowError, AttributeError):
        return None


def construct_dialects(families):
    from sqlglot import Dialect

    if not 1 <= len(families) <= 12 or not all(name.isidentifier() for name in families):
        raise ValueError("invalid dialect family")
    for family in families:
        instance = Dialect[family]()
        instance.tokenizer()
        instance.parser()
        instance.generator()
    return Dialect


def snapshot(plan):
    root = Path(plan["root"]).resolve()
    if "overrides" in plan:
        edits = plan["overrides"]
        if plan["kind"] != "collection" or not 1 <= len(edits) <= 6:
            raise ValueError("invalid collection overrides")
        os.chdir(root)
        sys.path.insert(0, str(root))
        sys.dont_write_bytecode = True
        for relative, source in edits.items():
            path = root / relative
            parts = Path(relative).with_suffix("").parts
            if (
                len(parts) < 2
                or parts[0] != "sqlglot"
                or not all(part.isidentifier() for part in parts)
                or path.suffix != ".py"
                or path.name == "__init__.py"
                or path.resolve() != path
                or not path.is_file()
                or not isinstance(source, str)
            ):
                raise ValueError("unexpected collection override")
            name = ".".join(parts)
            if name in sys.modules:
                raise ValueError("unexpected prior module")
            sys.meta_path.insert(0, Override(name, str(path), source))
        receipt = collect(root)
        if families := plan.get("families"):
            construct_dialects(families)
        return receipt
    path = (root / plan["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("source outside baseline")
    name = plan["name"]
    if name in sys.modules or not name.startswith("sqlglot."):
        raise ValueError("unexpected prior module")
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    if "source" in plan:
        sys.meta_path.insert(0, Override(name, str(path), plan["source"]))
    if plan["kind"] == "collection":
        return collect(root)
    try:
        mod = importlib.import_module(name)
    except (ImportError, SyntaxError, NameError) as exc:
        last = exc.__traceback__
        while last.tb_next:
            last = last.tb_next
        filename = last.tb_frame.f_code.co_filename
        provider = sys.modules.get(getattr(exc, "name", None))
        ordinary = (
            type(provider) is types.ModuleType
            and "__getattr__" not in vars(provider)
            and "__path__" not in vars(provider)
        )
        return {
            "ordinary_provider": ordinary,
            "state": "failed",
            "error": type(exc).__name__,
            "name": getattr(exc, "name", None),
            "origin": (
                os.path.relpath(filename, root) if os.path.isabs(filename) else None
            ),
        }
    if Path(mod.__file__).resolve() != path:
        raise ValueError("different source origin")
    if plan["kind"] in {"dialects", "time_maps"}:
        if plan["kind"] == "dialects" and plan["field"]:
            owner, field = plan["field"].split(":")
            table = vars(vars(mod)[owner])[field]
            if type(table) is not dict or not 0 < len(table) <= 4096 or not all(map(callable, table.values())):
                return {"state": "opaque"}
        families = plan["owner"].split(",")
        Dialect = construct_dialects(families)
        if plan["kind"] == "time_maps":
            # Replay immutable observations using the original SQLGlot converter.
            import importlib.util as util
            helper = Path(__file__).with_name("time_observations.py")
            spec = util.spec_from_file_location("time_observations", helper)
            module = util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.changed_inputs(Dialect, families, Path(plan["field"]))
        return {"state": "ready"}
    if plan["kind"] == "load":
        return {"state": "loaded"}
    owner = mod
    for part in plan["owner"].split("."):
        if not part:
            continue
        owner = vars(owner)[part]
        if not isinstance(owner, type):
            raise ValueError("not an ordinary class namespace")
    value = vars(owner).get(plan["field"])
    if type(value) is not dict or len(value) > 4096:
        return {"state": "opaque"}
    # Full contents, including inherited **spreads and actual aliases, determine
    # the observed delta. A symbol can have different values in two namespaces.
    entries = []
    for k, v in value.items():
        key = digest(k)
        if key is None:
            return {"state": "opaque"}
        entries.append([key, k if type(k) is str else None, digest(v)])
    return {"state": "mapping", "entries": entries}


def collect(root):
    import pytest

    attempted = set()
    opened = set()

    class Attempted(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "sqlglot" or fullname.startswith("sqlglot."):
                attempted.add(fullname.replace(".", "/") + ".py")
            return None

    sys.meta_path.insert(0, Attempted())

    def read(event, args):
        if event != "open" or not args or not isinstance(args[0], (str, bytes)):
            return
        name = os.path.abspath(os.fsdecode(args[0]))
        if name.startswith(str(root) + "/sqlglot/"):
            if name.endswith(".pyc") and "/__pycache__/" in name:
                name = importlib.util.source_from_cache(name)
            opened.add(os.path.relpath(name, root))

    sys.addaudithook(read)

    monitor = sys.monitoring
    monitor.use_tool_id(monitor.COVERAGE_ID, "import snapshot")
    handled = set()
    prefix = str(root) + os.sep

    def catch(code, offset, error):
        if isinstance(
            error, (ImportError, SyntaxError, NameError)
        ) and code.co_filename.startswith(prefix):
            handled.add(
                (
                    code.co_filename[len(prefix) :],
                    code.co_qualname,
                    type(error).__name__,
                )
            )

    monitor.register_callback(
        monitor.COVERAGE_ID, monitor.events.EXCEPTION_HANDLED, catch
    )
    monitor.set_events(monitor.COVERAGE_ID, monitor.events.EXCEPTION_HANDLED)

    items, errors = [], []

    class Observer:
        def pytest_collectreport(self, report):
            if report.failed or report.skipped:
                errors.append(report.nodeid)

        def pytest_collection_finish(self, session):
            items.extend(it.nodeid for it in session.items)

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(
        quiet
    ), contextlib.redirect_stderr(quiet):
        code = pytest.main(
            [
                "-q",
                "--collect-only",
                "--continue-on-collection-errors",
                "-p",
                "no:cacheprovider",
            ],
            plugins=[Observer()],
        )
    if int(code) not in (0, 1, 2) or not items:
        raise ValueError("incomplete collection")
    monitor.set_events(monitor.COVERAGE_ID, 0)
    modules = {}
    for name, module in sys.modules.copy().items():
        if name == "sqlglot" or name.startswith("sqlglot."):
            filename = (
                vars(module).get("__file__")
                if type(module) is types.ModuleType
                else None
            )
            if (
                not filename
                or not filename.startswith(prefix)
                or not filename.endswith(".py")
            ):
                raise ValueError("unbounded import origin")
            # A failed import may leave a parent package alive while suppressing
            # a cached export or publishing a different child module. Preserve
            # lexical keys and qualified origins for each public binding.
            public = []
            for key, obj in vars(module).items():
                if key.startswith("__"):
                    continue
                if type(obj) is types.ModuleType:
                    origin = ["module", vars(obj).get("__name__")]
                elif type(obj) is types.FunctionType:
                    origin = ["function", obj.__module__, obj.__qualname__]
                elif isinstance(obj, type):
                    origin = [
                        "class",
                        type.__getattribute__(obj, "__module__"),
                        type.__getattribute__(obj, "__qualname__"),
                    ]
                else:
                    origin = ["value", type(obj).__module__, type(obj).__qualname__]
                public.append([key, origin])
            modules[filename[len(prefix) :]] = hashlib.sha256(
                json.dumps(
                    sorted(public), separators=(",", ":"), ensure_ascii=True
                ).encode()
            ).hexdigest()
    return {
        "state": "collected",
        "items": items,
        "errors": errors,
        "code": int(code),
        "handled": sorted(handled),
        "modules": modules,
        "attempted": sorted(attempted),
        "opened": sorted(opened),
    }


if __name__ == "__main__":
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU, (30, 30) if len(sys.argv) == 3 else (6, 6)
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        # No patched module may escape the worker's measured process or supply a
        # result through an external process. The parent owns the receipt pipe.
        def audit(event, args):
            if event in {
                "subprocess.Popen",
                "os.exec",
                "os.fork",
                "os.posix_spawn",
                "os.system",
                "socket.connect",
                "socket.bind",
            }:
                raise RuntimeError("unqualified import effects")

        sys.addaudithook(audit)
        if len(sys.argv) == 3 and sys.argv[1] == "--prepare":
            # This build step runs once inside each target image on its pristine
            # source. Prediction only reads the artifact from that same image.
            req = dict(
                root=sys.argv[2],
                name="sqlglot.optimizer",
                path="sqlglot/optimizer/__init__.py",
                kind="collection",
            )

        else:
            req = json.load(sys.stdin)
        result = snapshot(req)
        print(json.dumps(result, separators=(",", ":")))
    except BaseException:
        sys.exit(2)
