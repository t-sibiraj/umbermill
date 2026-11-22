"""Apply class-field-only edits to the original class in an import-only checkpoint child."""
import ast
import importlib.util
from pathlib import Path
import sys
from types import CodeType, ModuleType

from .readers import same_code


ABSENT = object()


def _value(node):
    if not isinstance(node, ast.Constant):
        return ABSENT
    value = node.value
    if value is None or type(value) is bool:
        return value
    # Small cached integers have the same identity across both compilations.
    if type(value) is int and -5 <= value <= 256 and int(str(value)) is value:
        return value
    return ABSENT


def _assignments(cls):
    assignments = {}
    for index, stmt in enumerate(cls.body):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            if name in assignments:
                raise ValueError("rebound class attribute")
            assignments[name] = (index, stmt, _value(stmt.value))
    return assignments


def _safe_tail(body):
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            if _value(stmt.value) is not ABSENT:
                continue
        # Defining a plain method only binds its function object. Defaults,
        # annotations, decorators and type parameters can run at definition time.
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = stmt.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if (not stmt.decorator_list and not stmt.returns and not getattr(stmt, "type_params", ())
                    and not args.defaults and not any(args.kw_defaults)
                    and all(arg is None or arg.annotation is None for arg in parameters)):
                continue
        return False
    return True


def _resolve_base(tree, module):
    if not tree.bases:
        return object
    if len(tree.bases) != 1:
        return None
    node = tree.bases[0]
    if isinstance(node, ast.Name):
        return vars(module).get(node.id, object if node.id == "object" else None)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        holder = vars(module).get(node.value.id)
        if type(holder) is ModuleType:
            return vars(holder).get(node.attr)
    return None


def _plain_class(cls, module, tree):
    if (type(cls) is not type or cls.__module__ != module.__name__ or cls.__qualname__ != tree.name
            or tree.keywords or tree.decorator_list or getattr(tree, "type_params", ())
            or cls.__bases__ != (_resolve_base(tree, module),)):
        return False
    # A base can inspect or publish the finished class before the preload returns.
    for base in cls.__mro__[1:]:
        if type(base) is not type or (base is not object and "__init_subclass__" in vars(base)):
            return False
    if type.__subclasses__(cls):
        return False
    # Descriptor initialization can snapshot the field from an otherwise plain class.
    for value in vars(cls).values():
        if any("__set_name__" in vars(base) for base in type(value).__mro__):
            return False
    return True


def _passive_import(tree, module, modules):
    """A later import may publish a class alias without inspecting the class."""
    for stmt in tree.body:
        if isinstance(stmt, (ast.Pass, ast.FunctionDef, ast.AsyncFunctionDef)):
            if _safe_tail([stmt]):
                continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            if _value(stmt.value) is not ABSENT or isinstance(stmt.value, ast.Name):
                continue
        if isinstance(stmt, ast.Import):
            if all(alias.name.startswith("sqlglot.") or alias.name == "sqlglot" for alias in stmt.names):
                if all(type(modules.get(alias.name)) is ModuleType for alias in stmt.names):
                    continue
        if isinstance(stmt, ast.ImportFrom):
            if stmt.module == "__future__" and stmt.level == 0:
                continue
            provider_name = importlib.util.resolve_name("." * stmt.level + (stmt.module or ""), module.__package__)
            if provider_name == "sqlglot" or provider_name.startswith("sqlglot."):
                provider = modules.get(provider_name)
                if (type(provider) is ModuleType and all(alias.name != "*" and alias.name in vars(provider)
                                                        for alias in stmt.names)):
                    continue
        return False
    return True


def _quiescent_since(name, modules, observed):
    if name == observed.last_leaf:
        return True
    history = observed.after_direct(name) if hasattr(observed, "after_direct") else None
    if history is None:
        return False
    root = Path(observed.root).resolve()
    seen = set()
    for module_name, code in history:
        if (module_name == name or not isinstance(code, CodeType) or code.co_name != "<module>"
                or module_name in seen):
            return False
        seen.add(module_name)
        module = modules.get(module_name)
        if type(module) is not ModuleType or module is not sys.modules.get(module_name):
            return False
        source_file = Path(code.co_filename).resolve()
        if source_file.suffix != ".py" or not source_file.is_relative_to(root):
            return False
        redirected = (Path(observed.overlay) / source_file.relative_to(root)).resolve()
        actual = Path(module.__file__).resolve()
        if actual not in (source_file, redirected) or not redirected.is_relative_to(observed.overlay):
            return False
        source = source_file.read_bytes()
        if actual.read_bytes() != source:
            return False
        if (not same_code(compile(source, code.co_filename, "exec", dont_inherit=True), code)
                or not _passive_import(ast.parse(source), module, modules)):
            return False
    return True


def plan(modules, sources, observed):
    """Return verified updates to apply to the existing class without rebinding aliases."""
    if not observed.complete or len(sources) != 1 or sys.gettrace() is not None:
        return False
    name, pair = next(iter(sources.items()))
    mod = modules.get(name)
    if (type(mod) is not ModuleType or mod is not sys.modules.get(name)
            or (name, "<module>") not in observed.entered):
        return False
    try:
        if not _quiescent_since(name, modules, observed):
            return False
        before, after = [ast.parse(source) for source in pair]
        if not before.body or not after.body:
            return False
        a, b = before.body[-1], after.body[-1]
        if not (isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name):
            return False
        cls = vars(mod).get(a.name)
        if not _plain_class(cls, mod, a) or (name, a.name) not in observed.entered:
            return False
        # A prior method invocation can already have observed and cached the old field.
        if any(m == name and fn.startswith(a.name + ".") for m, fn in observed.entered):
            return False
        left, right = _assignments(a), _assignments(b)
        updates = {}
        safe_from = [len(a.body), len(b.body)]
        for field in left.keys() | right.keys():
            x, y = left.get(field), right.get(field)
            if x and y and ast.dump(x[1], include_attributes=True) == ast.dump(y[1], include_attributes=True):
                continue
            if not field.isupper() or (x and x[2] is ABSENT) or (y and y[2] is ABSENT):
                return False
            old, new = x[2] if x else ABSENT, y[2] if y else ABSENT
            # A removed/added field can change class namespace order or inherited descriptor
            # visibility. Preserve only updates to an existing, directly owned attribute.
            if old is ABSENT or new is ABSENT or vars(cls).get(field, ABSENT) is not old:
                return False
            updates[field] = new
            safe_from = [min(safe_from[0], x[0]), min(safe_from[1], y[0])]
        if not updates or not _safe_tail(a.body[safe_from[0]:]) or not _safe_tail(b.body[safe_from[1]:]):
            return False
        for field in updates:
            # Only the original value changes. Source coordinates of unchanged
            # methods and every surrounding statement must match the overlay.
            left[field][1].value = right[field][1].value = ast.Constant(None)
            left[field][1].end_col_offset = right[field][1].end_col_offset = 0
        a.end_col_offset = b.end_col_offset = 0
        if ast.dump(before, include_attributes=True) != ast.dump(after, include_attributes=True):
            return False
        return cls, updates
    except (AttributeError, ImportError, KeyError, OSError, TypeError, ValueError, SyntaxError, RecursionError):
        return False
