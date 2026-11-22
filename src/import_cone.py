"""Reconstruct a bounded group of declarative, already-imported modules in one patch child."""

import ast
import importlib
import importlib.util
from pathlib import Path
import sys
from types import CodeType, FunctionType, ModuleType
import weakref

from .position import constant_key
from .readers import same_code


class Unsupported(ValueError):
    pass


_MISSING = object()
_SCALARS = (type(None), bool, int, float, complex, str, bytes)
_MODULE_METADATA = {"__name__", "__doc__", "__package__", "__loader__", "__spec__", "__file__",
                    "__cached__", "__builtins__"}
_CLASS_METADATA = {"__module__", "__doc__", "__qualname__", "__dict__", "__weakref__",
                   "__firstlineno__", "__static_attributes__"}


def _package(name):
    return isinstance(name, str) and (name == "sqlglot" or name.startswith("sqlglot."))


def _imports(node, package):
    if isinstance(node, ast.Import):
        for a in node.names:
            yield a.asname or a.name.partition(".")[0], a.name, None
    else:
        source = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
        for a in node.names:
            if a.name == "*":
                yield "*", source, None
            else:
                yield a.asname or a.name, source, a.name


def _graph(modules, root, observed, replacements):
    edges = {}
    texts = {}
    for name, module in modules.items():
        if not _package(name) or type(module) is not ModuleType:
            continue
        relative = name.replace(".", "/") + ("/__init__.py" if "__path__" in vars(module) else ".py")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise Unsupported("incomplete warm source tree")
        text = path.read_bytes().decode()
        tree = ast.parse(text, str(path))
        edges[name] = set(observed.imported_by.get(name, ()))
        package = vars(module).get("__package__")
        trees = [tree]
        if relative in replacements:
            trees.append(ast.parse(replacements[relative], str(path)))
        for source_tree in trees:
            for node in ast.walk(source_tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for _, provider, attribute in _imports(node, package):
                        edges[name].add(provider)
                        if attribute:
                            edges[name].add(provider + "." + attribute)
        texts[name] = relative, text, tree
    return edges, texts


def _lookup(node, env):
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise Unsupported("unknown eager name")
        return env[node.id]
    if isinstance(node, ast.Attribute) and not node.attr.startswith("__"):
        owner = _lookup(node.value, env)
        if type(owner) is ModuleType:
            table = vars(owner)
            if node.attr in table:
                return table[node.attr]
        if type(owner) is type:
            for cls in owner.__mro__:
                if node.attr in vars(cls):
                    value = vars(cls)[node.attr]
                    if not hasattr(type(value), "__get__"):
                        return value
                    break
    raise Unsupported("computed eager attribute")


def _value(node, env):
    if isinstance(node, ast.Constant) and type(node.value) in _SCALARS:
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _lookup(node, env)
    if isinstance(node, (ast.Tuple, ast.List)):
        items = [_value(part, env) for part in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if isinstance(node, (ast.Set, ast.Dict)):
        keys = [_value(part, env) for part in (node.elts if isinstance(node, ast.Set) else node.keys)]
        if any(type(k) not in (*_SCALARS, type) for k in keys):
            raise Unsupported("key has a user-defined hash")
        if isinstance(node, ast.Set):
            return set(keys)
        return dict(zip(keys, (_value(part, env) for part in node.values)))
    raise Unsupported("potential eager effect")


def _key(value):
    return (type, id(value)) if type(value) is type else constant_key(value)


def _matches(node, actual, env):
    expected = _value(node, env)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return actual is expected
    if type(actual) is not type(expected):
        return False
    if isinstance(node, (ast.List, ast.Tuple)):
        return len(actual) == len(node.elts) and all(_matches(n, v, env) for n, v in zip(node.elts, actual))
    if isinstance(node, ast.Dict):
        keys = [_value(k, env) for k in node.keys]
        if (len(keys) != len(actual) or len(set(keys)) != len(keys)
                or any(type(k) not in (*_SCALARS, type) for k in actual)):
            return False
        return (tuple(map(_key, keys)) == tuple(map(_key, actual))
                and all(_matches(n, v, env) for n, v in zip(node.values, actual.values())))
    if isinstance(node, ast.Set):
        return (all(type(k) in (*_SCALARS, type) for k in actual)
                and {_key(k) for k in expected} == {_key(k) for k in actual})
    return constant_key(expected) == constant_key(actual)


def _ordinary_base(base):
    return type(base) is type and all("__init_subclass__" not in vars(cls) for cls in base.__mro__ if cls is not object)


def _statements(tree, package, live, code, env, *, postponed, module_name, verify):
    namespace = vars(live)
    is_module = type(live) is ModuleType
    names = set()
    children = {c.co_name: c for c in code.co_consts if isinstance(c, CodeType)}
    if verify:
        first = tree[0] if tree else None
        doc = first.value.value if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                                    and type(first.value.value) is str) else None
        if namespace.get("__doc__") != doc:
            raise Unsupported("actual docstring differs from declaration")
    for stmt in tree:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and type(stmt.value.value) is str:
            continue
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)) and is_module:
            for name, provider, field in _imports(stmt, package):
                if name == "*" or name in names or type(sys.modules.get(provider)) is not ModuleType:
                    raise Unsupported("ambiguous or new import")
                if isinstance(stmt, ast.Import) and not any(a.asname == name for a in stmt.names):
                    obj = sys.modules[name]
                elif field:
                    obj = vars(sys.modules[provider]).get(field, _MISSING)
                else:
                    obj = sys.modules[provider]
                if obj is _MISSING or verify and namespace.get(name, _MISSING) is not obj:
                    raise Unsupported("dynamic import attribute")
                names.add(name)
                env[name] = obj
            continue
        if (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Attribute) and stmt.test.attr == "TYPE_CHECKING"
                and not stmt.orelse and _lookup(stmt.test, env) is False):
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or stmt.value is None:
                raise Unsupported("assignment writes outside the namespace")
            if isinstance(stmt, ast.AnnAssign):
                # __annotations__ is another namespace visible to outsiders.
                raise Unsupported("annotation dictionary would be republished")
            name = targets[0].id
            value = _value(stmt.value, env)
            if not is_module and any("__set_name__" in vars(c) for c in type(value).__mro__):
                raise Unsupported("descriptor would be initialized again")
            if verify and (name not in namespace or not _matches(stmt.value, namespace[name], env)):
                raise Unsupported("eager value differs from declaration")
            env[name] = namespace[name] if verify else value
        elif isinstance(stmt, (ast.FunctionDef, ast.ClassDef)):
            name = stmt.name
            if stmt.decorator_list or getattr(stmt, "type_params", ()):
                raise Unsupported("decorated or generic declaration")
            obj = namespace.get(name, _MISSING)
            subcode = children.get(name)
            if obj is _MISSING or subcode is None or getattr(obj, "__module__", None) != module_name:
                raise Unsupported("ambiguous exported object")
            if isinstance(stmt, ast.FunctionDef):
                if (type(obj) is not FunctionType or stmt.args.defaults or any(stmt.args.kw_defaults)
                        or not postponed and (stmt.returns or any(a.annotation for a in ast.walk(stmt.args)
                                                                  if isinstance(a, ast.arg)))):
                    raise Unsupported("eager function declaration")
                if verify and (not same_code(obj.__code__, subcode) or vars(obj)
                               or obj.__qualname__ != subcode.co_qualname or obj.__defaults__ or obj.__kwdefaults__):
                    raise Unsupported("function was rebound or modified")
            else:
                if not is_module or stmt.keywords or type(obj) is not type:
                    raise Unsupported("custom class creation")
                bases = tuple(_lookup(base, env) for base in stmt.bases) or (object,)
                if (not all(_ordinary_base(base) for base in bases)
                        or verify and (obj.__bases__ != bases or obj.__qualname__ != subcode.co_qualname)):
                    raise Unsupported("new class would run a hook or change an unexpected base")
                _statements(stmt.body, package, obj, subcode, env.copy(), postponed=postponed,
                            module_name=module_name, verify=verify)
            env[name] = obj
        else:
            raise Unsupported("unclassified import-time execution")
        if name.startswith("__") or name in names:
            raise Unsupported("magic or duplicate binding")
        names.add(name)
    if verify and set(namespace) - (_MODULE_METADATA if is_module else _CLASS_METADATA) != names:
        raise Unsupported("actual bindings differ from source")


def _check(name, module, tree, code, verify):
    postponed = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                    and any(a.name == "annotations" for a in n.names) for n in tree.body)
    _statements(tree.body, module.__package__, module, code, {}, postponed=postponed, module_name=name, verify=verify)


def _owned(modules, cone, trees):
    identities = set()
    def include(owner, body):
        identities.add(id(owner))
        ns = vars(owner)
        if type(owner) is ModuleType:
            identities.add(id(ns))
        for node in body:
            if isinstance(node, ast.ClassDef):
                include(ns[node.name], node.body)
            elif isinstance(node, ast.FunctionDef):
                identities.add(id(ns[node.name]))
            elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
                identities.add(id(ns[node.targets[0].id]))
    for name in cone:
        include(modules[name], trees[name].body)
    return identities


def _outside(modules, cone, forbidden):
    """Check container contents, closures, class dictionaries and base identities for stale publications."""
    old = forbidden
    seen = set()
    state = {}

    def follow(obj):
        key = id(obj)
        if key in old:
            raise Unsupported("old module or exported object escaped the import group")
        if key in seen:
            return
        seen.add(key)
        if len(seen) > 350000:
            raise Unsupported("outside-state budget exceeded")
        typ = type(obj)
        if typ is dict:
            children = tuple(x for pair in obj.items() for x in pair)
        elif typ in (tuple, list, set, frozenset):
            children = tuple(obj)
        elif isinstance(obj, type) and _package(type.__getattribute__(obj, "__module__")):
            if type.__getattribute__(obj, "__module__") in cone:
                raise Unsupported("old class escaped the import group")
            table = type.__getattribute__(obj, "__dict__")
            children = tuple(type.__getattribute__(obj, "__bases__")) + tuple(x for pair in table.items() for x in pair)
        elif typ is FunctionType and _package(obj.__module__):
            if obj.__module__ in cone:
                raise Unsupported("old function escaped the import group")
            values = [obj.__defaults__, obj.__kwdefaults__, obj.__dict__, obj.__code__]
            for cell in obj.__closure__ or ():
                try:
                    values.append(cell.cell_contents)
                except ValueError:
                    pass
            children = tuple(values)
        elif typ is weakref.ReferenceType:
            children = (obj(),)
        elif typ in (staticmethod, classmethod):
            children = (obj.__func__,)
        else:
            return
        state[key] = tuple(sorted(map(id, children))) if typ in (set, frozenset) else tuple(map(id, children))
        for item in children:
            follow(item)

    for name, module in modules.items():
        if name in cone or type(module) is not ModuleType:
            continue
        ns = vars(module)
        if not _package(name):
            # The declarative group cannot register objects with non-project
            # modules. Still reject direct references exported there by preload.
            if any(id(v) in old for v in ns.values()):
                raise Unsupported("outside module holds an old export")
            continue
        summary = []
        for k, v in ns.items():
            if k in _MODULE_METADATA:
                continue
            member = name + "." + k
            if member in cone and v is modules[member]:
                summary.append((k, member))
            else:
                summary.append((k, id(v)))
                follow(v)
        state[name] = tuple(summary)
    return state


def reimport(roots, replacements, base, overlay, observed):
    """Reconstruct every loaded static/dynamic importer; return False for unresolved state."""
    modules = dict(sys.modules)
    edges, sources = _graph(modules, base, observed, replacements)
    cone = set(roots)
    while True:
        expanded = cone | {name for name, deps in edges.items() if deps & cone}
        if expanded == cone:
            break
        cone = expanded
    if not cone <= sources.keys() or len(cone) > 32 or observed.opaque_readers - cone:
        return False
    plans = {}
    for name in cone:
        relative, previous, old_tree = sources[name]
        old_path = base / relative
        new_path = overlay / relative
        if (relative.endswith("/__init__.py") or not new_path.is_file()
                or not new_path.resolve().is_relative_to(overlay) or name not in observed.loaded_order):
            return False
        upcoming = replacements.get(relative, previous)
        if new_path.read_bytes().decode() != upcoming:
            return False
        tree = ast.parse(upcoming, str(new_path))
        old_code = compile(previous, str(old_path), "exec", dont_inherit=True)
        new_code = compile(upcoming, str(new_path), "exec", dont_inherit=True)
        _check(name, modules[name], old_tree, old_code, True)
        _check(name, modules[name], tree, new_code, False)
        plans[name] = new_path, tree, new_code

    originals = _owned(modules, cone, {n: sources[n][2] for n in cone})
    before = _outside(modules, cone, originals)
    dependencies = {name: (edges[name] & cone) - {name} for name in cone}
    order = []
    while dependencies:
        ready = [n for n in observed.loaded_order if n in dependencies and not dependencies[n]]
        if not ready:
            return False
        for n in dict.fromkeys(ready):
            order.append(n)
            del dependencies[n]
        for refs in dependencies.values():
            refs.difference_update(ready)
    for name in reversed(order):
        prefix, _, child = name.rpartition(".")
        parent = sys.modules[prefix]
        if vars(parent).get(child) is not modules[name]:
            return False
        delattr(parent, child)
        del sys.modules[name]
    importlib.invalidate_caches()
    for name in order:
        expected_path, tree, code = plans[name]
        fresh = importlib.import_module(name)
        if (type(fresh) is not ModuleType or fresh is modules[name]
                or Path(fresh.__file__).resolve() != expected_path):
            return False
        _check(name, fresh, tree, code, True)
    current = dict(sys.modules)
    created = _owned(current, cone, {n: plans[n][1] for n in cone})
    return before == _outside(current, cone, originals | created)
