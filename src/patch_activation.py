"""Activate a source overlay in a disposable child of an import-only checkpoint."""

import ast
import dis
import importlib
import inspect
from pathlib import Path, PurePosixPath
import sys
import threading
from types import CodeType, FunctionType, ModuleType

from .position import constant_key
from .readers import same_code


def _project(name):
    return isinstance(name, str) and (name == "sqlglot" or name.startswith("sqlglot."))


class ImportActivity:
    """Observe an entire, single-threaded SQLGlot preload, before any test imports."""

    def __init__(self):
        self.entered = set()
        self.loaded_order = []
        self.imported_by = {}
        self.opaque_readers = set()
        self._registry_ops = {}
        self._call_log = []
        self._direct_ends = {}
        self.root = None
        self.overlay = None
        self.last_leaf = None
        self.complete = False
        self._modules = []
        self._usable = False

    def after_direct(self, name):
        end = self._direct_ends.get(name)
        return None if end is None else self._call_log[end:]

    def _registry_reads(self, frame, module):
        code = frame.f_code
        key = id(code)
        if key not in self._registry_ops:
            accesses = []
            if any(name in code.co_names for name in ("get_objects", "get_referrers", "walk_packages", "__subclasses__")):
                accesses.append((None, None))
            if "modules" in code.co_names and "sys" in code.co_names:
                ops = list(dis.get_instructions(code))
                for i, op in enumerate(ops):
                    if op.opname != "LOAD_ATTR" or op.argval != "modules":
                        continue
                    following = ops[i + 1:i + 3]
                    if following and following[0].opname == "CONTAINS_OP":
                        continue  # Membership is unchanged after reimporting the same group.
                    if (len(following) == 2 and following[1].opname == "BINARY_SUBSCR"
                            and following[0].opname in {"LOAD_CONST", "LOAD_FAST", "LOAD_FAST_CHECK"}):
                        accesses.append((following[0].opname, following[0].argval))
                    else:
                        accesses.append((None, None))
            self._registry_ops[key] = code, tuple(accesses)
        for op, value in self._registry_ops[key][1]:
            if op is None:
                self.opaque_readers.add(module)
                continue
            target = value if op == "LOAD_CONST" else frame.f_locals.get(value)
            if _project(target):
                self.imported_by.setdefault(module, set()).add(target)
            elif not isinstance(target, str):
                self.opaque_readers.add(module)

    def _event(self, frame, event, arg):
        module = frame.f_globals.get("__name__")
        if not _project(module):
            if (event == "call" and module in {"importlib._bootstrap", "_frozen_importlib", "importlib"}
                    and frame.f_code.co_name in {"_find_and_load", "import_module"}):
                target = frame.f_locals.get("name")
                if _project(target):
                    caller = frame.f_back
                    while caller is not None:
                        origin = caller.f_globals.get("__name__")
                        if _project(origin):
                            self.imported_by.setdefault(origin, set()).add(target)
                            break
                        caller = caller.f_back
            if event == "call" and isinstance(module, str) and (module == "tests" or module.startswith("tests.")):
                self._usable = False
            return
        if event == "call":
            self.last_leaf = None
            code = frame.f_code
            self.entered.add((module, code.co_qualname))
            self._registry_reads(frame, module)
            if self._direct_ends:
                self._call_log.append((module, code))
            if frame.f_code.co_name == "<module>":
                self._modules.append(module)
        elif event == "return" and frame.f_code.co_name == "<module>":
            if not self._modules or self._modules.pop() != module:
                self._usable = False
            self.loaded_order.append(module)
            if not self._modules:
                # The final directly imported leaf can be reimported only when no
                # SQLGlot frame was suspended across its import and resumed after.
                caller = frame.f_back
                while caller is not None and not _project(caller.f_globals.get("__name__")):
                    caller = caller.f_back
                if caller is None:
                    self.last_leaf = module
                    self._direct_ends[module] = len(self._call_log)

    def _other_thread(self, frame, event, arg):
        self._usable = False

    def __enter__(self):
        self._hook = self._event
        self._thread_hook = self._other_thread
        self._usable = (threading.active_count() == 1 and sys.getprofile() is None
                        and threading.getprofile() is None and not any(_project(k) for k in sys.modules))
        if self._usable:
            threading.setprofile(self._thread_hook)
            sys.setprofile(self._hook)
        return self

    def __exit__(self, typ, value, traceback):
        kept = sys.getprofile() is self._hook
        if kept:
            sys.setprofile(None)
        other_kept = threading.getprofile() is self._thread_hook
        if other_kept:
            threading.setprofile(None)
        self.complete = bool(self._usable and kept and other_kept and typ is None and not self._modules
                             and threading.active_count() == 1)


def _module_name(relative):
    path = PurePosixPath(relative)
    if (path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "sqlglot"
            or path.suffix != ".py" or not all(p.isidentifier() for p in path.with_suffix("").parts)):
        return None
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _qualified_function(module, code):
    owner = module
    for name in code.co_qualname.split(".")[:-1]:
        owner = vars(owner).get(name)
        if not isinstance(owner, type):
            return None
    value = vars(owner).get(code.co_name)
    if type(value) in (staticmethod, classmethod):
        value = value.__func__
    if (type(value) is not FunctionType or value.__globals__ is not vars(module)
            or value.__qualname__ != code.co_qualname or not same_code(value.__code__, code)):
        return None
    return value


def _compatible(left, right):
    n = left.co_argcount + left.co_kwonlyargcount
    n += bool(left.co_flags & inspect.CO_VARARGS) + bool(left.co_flags & inspect.CO_VARKEYWORDS)
    return ((left.co_qualname, left.co_freevars, left.co_cellvars, left.co_flags,
             left.co_argcount, left.co_posonlyargcount, left.co_kwonlyargcount, left.co_varnames[:n])
            == (right.co_qualname, right.co_freevars, right.co_cellvars, right.co_flags,
                right.co_argcount, right.co_posonlyargcount, right.co_kwonlyargcount, right.co_varnames[:n])
            and (left.co_consts[:1] == right.co_consts[:1]))


def _function_swaps(module, before, after, observed, swaps):
    if same_code(before, after):
        return True
    if before.co_flags & inspect.CO_OPTIMIZED:
        if ((module.__name__, before.co_qualname) in observed.entered or not _compatible(before, after)):
            return False
        live = _qualified_function(module, before)
        if live is None or len(live.__closure__ or ()) != len(after.co_freevars):
            return False
        swaps.append((live, after))
        return True

    # Replacing the children's code objects must restore the entire surrounding
    # module/class code, including constants, definition headers and line tables.
    if len(before.co_consts) != len(after.co_consts):
        return False
    normalized = list(after.co_consts)
    for i, (old, new) in enumerate(zip(before.co_consts, after.co_consts)):
        if isinstance(old, CodeType) or isinstance(new, CodeType):
            if (not isinstance(old, CodeType) or not isinstance(new, CodeType)
                    or old.co_qualname != new.co_qualname or not _function_swaps(module, old, new, observed, swaps)):
                return False
            normalized[i] = old
    return same_code(before, after.replace(co_consts=tuple(normalized)))


def _literal(node):
    return isinstance(node, ast.Constant) or isinstance(node, ast.Tuple) and all(_literal(item) for item in node.elts)


def _declarative(statements, *, in_class=False):
    """Only namespace-local declarations, with no decorators, hooks or evaluated defaults."""
    for stmt in statements:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and type(stmt.value.value) is str:
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            if not stmt.targets[0].id.startswith("__") and _literal(stmt.value):
                continue
        if isinstance(stmt, ast.FunctionDef):
            if (not stmt.name.startswith("__") and not stmt.decorator_list and not stmt.args.defaults
                    and not any(stmt.args.kw_defaults) and not getattr(stmt, "type_params", ())
                    and stmt.returns is None and all(a.annotation is None for a in ast.walk(stmt.args)
                                                      if isinstance(a, ast.arg))):
                continue
        if (not in_class and isinstance(stmt, ast.ClassDef) and not stmt.bases and not stmt.keywords
                and not stmt.decorator_list and not getattr(stmt, "type_params", ()) and _declarative(stmt.body, in_class=True)):
            continue
        if isinstance(stmt, ast.Pass):
            continue
        return False
    return True


def _matches_namespace(owner, statements, code, module_name):
    """Compare the actual initialized namespace with the declarative source."""
    is_module = type(owner) is ModuleType
    namespace = vars(owner)
    compiler_names = ({"__name__", "__doc__", "__package__", "__loader__", "__spec__", "__file__",
                       "__cached__", "__builtins__"} if is_module else
                      {"__module__", "__doc__", "__qualname__", "__dict__", "__weakref__",
                       "__firstlineno__", "__static_attributes__"})
    children = {c.co_name: c for c in code.co_consts if isinstance(c, CodeType)}
    expected = set()
    if statements and isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant):
        if namespace.get("__doc__") != statements[0].value.value:
            return False
    elif namespace.get("__doc__") is not None:
        return False
    for statement in statements:
        if isinstance(statement, ast.Assign):
            name = statement.targets[0].id
            if name not in namespace or constant_key(namespace[name]) != constant_key(ast.literal_eval(statement.value)):
                return False
        elif isinstance(statement, (ast.FunctionDef, ast.ClassDef)):
            name = statement.name
            child = children.get(name)
            value = namespace.get(name)
            if child is None or getattr(value, "__module__", None) != module_name:
                return False
            if isinstance(statement, ast.FunctionDef):
                if (type(value) is not FunctionType or value.__qualname__ != child.co_qualname
                        or value.__defaults__ or value.__kwdefaults__ or vars(value)
                        or not same_code(value.__code__, child)):
                    return False
            elif (type(value) is not type or value.__bases__ != (object,) or value.__qualname__ != child.co_qualname
                    or not _matches_namespace(value, statement.body, child, module_name)):
                return False
        else:
            continue
        if name in expected:
            return False
        expected.add(name)
    return set(namespace) - compiler_names == expected


def _no_aliases(imported, module):
    # A leaf can still be held by the code which explicitly requested its
    # import. Only the standard parent-package attribute may already publish it.
    parent, _, child = module.__name__.rpartition(".")
    objects = {id(module)}
    for value in vars(module).values():
        if isinstance(value, (type, FunctionType)) and getattr(value, "__module__", None) == module.__name__:
            objects.add(id(value))
    for name, other in imported.items():
        if other is module or not isinstance(other, ModuleType):
            continue
        for key, value in vars(other).items():
            if id(value) in objects and not (name == parent and key == child and value is module):
                return False
    return True


def _overlay_filename(code, filename):
    children = tuple(_overlay_filename(item, filename) if isinstance(item, CodeType) else item
                     for item in code.co_consts)
    return code.replace(co_filename=filename, co_consts=children)


def _read(path):
    return path.read_bytes().decode("utf-8")


def activate(imported, replacements, *, base_root, overlay_root, observed):
    """Return True on complete child activation. Discard this child on any False.

    'imported' must be the live module table or a current copy of it. The caller
    must redirect later imports, source readers and subprocesses to overlay_root
    before replay. A preload may only import modules and discard its references.
    """
    if not isinstance(observed, ImportActivity) or not observed.complete or threading.active_count() != 1:
        return False
    root = Path(base_root).resolve()
    overlay = Path(overlay_root).resolve()
    observed.root = root
    observed.overlay = overlay
    if root == overlay or any(imported.get(k) is not v for k, v in sys.modules.items() if _project(k)):
        return False
    swaps, leaves = [], []
    live_sources = {}
    unresolved = set()
    try:
        module_names = set()
        for relative, replacement in replacements.items():
            name = _module_name(relative)
            if not name or name in module_names or not isinstance(replacement, str):
                return False
            module_names.add(name)
            path = (root / relative).resolve()
            changed = (overlay / relative).resolve()
            if not path.is_relative_to(root) or not changed.is_relative_to(overlay) or _read(changed) != replacement:
                return False
            from .slots_plan import requires_fresh
            original = _read(path)
            if requires_fresh(original, replacement):
                return False
            module = imported.get(name)
            if name not in sys.modules:
                # Replaced files which are still cold will import off the child's overlay.
                continue
            if (type(module) is not ModuleType or module is not sys.modules[name] or module.__name__ != name
                    or Path(module.__file__).resolve() not in (path, changed)
                    or (name, "<module>") not in observed.entered):
                return False
            live_sources[name] = original, replacement
            old = compile(original, str(path), "exec", dont_inherit=True)
            new = compile(replacement, str(path), "exec", dont_inherit=True)
            proposed = []
            if _function_swaps(module, old, new, observed, proposed):
                swaps.extend((fn, _overlay_filename(code, str(changed))) for fn, code in proposed)
                continue
            original_body = ast.parse(original).body
            replacement_body = ast.parse(replacement).body
            if (name == observed.last_leaf and relative.rsplit("/", 1)[-1] != "__init__.py"
                    and _declarative(original_body) and _declarative(replacement_body)
                    and _matches_namespace(module, original_body, old, name)
                    and _no_aliases(sys.modules.copy(), module)):
                leaves.append((name, module, changed, replacement_body, _overlay_filename(new, str(changed))))
                continue
            unresolved.add(name)

        state_check = None
        if len(live_sources) == 1 and (leaves or unresolved):
            from .prepared_state import before, after
            state_check = before(imported, live_sources, root, overlay)
            if state_check is False:
                return False
            from .class_reexecution import plan
            update = plan(imported, live_sources, observed)
            if update:
                cls, fields = update
                for field, value in fields.items():
                    type.__setattr__(cls, field, value)
                return state_check is None or after(state_check, sys.modules, root, overlay)
        if unresolved:
            # A cone can contain another changed module. Reconstruct all live
            # changes together instead of updating an obsolete function object.
            from .import_cone import reimport
            if not reimport(live_sources, replacements, root, overlay, observed):
                return False
            return state_check is None or after(state_check, sys.modules, root, overlay)

        for name, module, changed, body, replacement_code in leaves:
            # Import the class bodies and module declarations under their actual name.
            parent_name, _, member = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if not parent or vars(parent).get(member) is not module:
                return False
            delattr(parent, member)
            del sys.modules[name]
            importlib.invalidate_caches()
            fresh = importlib.import_module(name)
            if (Path(fresh.__file__).resolve() != changed
                    or not _matches_namespace(fresh, body, replacement_code, name)):
                return False
        for fn, code in swaps:
            fn.__code__ = code  # aliases and cached dispatch tables retain their identity
        return state_check is None or after(state_check, sys.modules, root, overlay)
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, SyntaxError, TypeError, ValueError):
        return False
