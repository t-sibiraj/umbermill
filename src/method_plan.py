"""Bound inherited SQL generator edits with observations of the original consumers."""

import ast
import re
from functools import lru_cache

from .deferred import warning_free
from .module_cold import cold_mask
from .patches import apply_change
from .readers import number


def _generator_file(path):
    parts = path.split("/")
    return (len(parts) == 3 and parts[:2] == ["sqlglot", "generators"]
            and parts[2].endswith(".py") and parts[2][:-3].isidentifier())


def _plain_method(node):
    return (isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
            and node.name.endswith("_sql") and not node.decorator_list
            and not node.args.defaults and not any(node.args.kw_defaults)
            and not getattr(node, "type_params", ()) and not ast.get_docstring(node))


@lru_cache(48)
def _tree(path, source):
    return ast.parse(source, path, type_comments=True)


def _imports(tree):
    return {alias.asname or alias.name: node.module + "." + alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom) and node.module and not node.level
            for alias in node.names}


def _target_base(tree, cls):
    if cls.decorator_list or cls.keywords or len(cls.bases) != 1:
        return None
    base = cls.bases[0]
    roots = _imports(tree)
    if isinstance(base, ast.Name):
        return roots.get(base.id)
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        module = roots.get(base.value.id)
        return module + "." + base.attr if module else None
    return None


def _same_node(left, right):
    if left is None or right is None:
        return left is right
    return ast.dump(left) == ast.dump(right)


def _signature(node):
    return ([n.arg for n in node.args.posonlyargs], [n.arg for n in node.args.args],
            [n.arg for n in node.args.kwonlyargs], bool(node.args.vararg), bool(node.args.kwarg))


def _has_base(selector, tree, cls, method):
    base = _target_base(tree, cls)
    seen = set()
    while base and base not in seen:
        seen.add(base)
        module, _, name = base.rpartition(".")
        path = module.replace(".", "/") + ".py"
        if path != "sqlglot/generator.py" and not _generator_file(path):
            return False
        tree = _tree(path, selector._source(path))
        owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
        if len(owners) != 1:
            return False
        cls = owners[0]
        if any(isinstance(n, ast.Name) and n.id == method.name for n in ast.walk(cls)):
            return False
        definitions = [n for n in cls.body if getattr(n, "name", None) == method.name]
        if definitions and (len(definitions) != 1 or not isinstance(definitions[0], ast.FunctionDef)
                            or definitions[0].decorator_list or _signature(definitions[0]) != _signature(method)
                            or definitions[0].args.defaults or any(definitions[0].args.kw_defaults)):
            return False
        if path == "sqlglot/generator.py":
            return name == "Generator" and bool(definitions)
        base = _target_base(tree, cls)
    return False


def _base_import(node):
    return (isinstance(node, ast.ImportFrom) and not node.level and node.module == "sqlglot.generator"
            and len(node.names) == 1 and node.names[0].name == "Generator")


def _literal_edits_only(old, new):
    def shape(node):
        if isinstance(node, ast.Constant):
            return ("literal", type(node.value), node.kind)
        if isinstance(node, ast.AST):
            return (type(node), tuple((k, shape(v)) for k, v in ast.iter_fields(node)))
        if isinstance(node, list):
            return tuple(shape(item) for item in node)
        return node

    return shape(old.body) == shape(new.body)


def _edits(selector, path, before, after):
    old = _tree(path, before)
    new = _tree(path, after)
    if not any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(alias.name == "annotations" for alias in node.names) for node in old.body):
        return None
    if old.type_ignores or new.type_ignores:
        # Ignore directives are metadata and usually occur outside the changed code.
        if [n.tag for n in old.type_ignores] != [n.tag for n in new.type_ignores]:
            return None
    remaining = iter(new.body)
    current = next(remaining, None)
    methods = []
    export_changed = False
    for node in old.body:
        if current is not None and ast.dump(node) == ast.dump(current):
            current = next(remaining, None)
            continue
        if _base_import(node):
            alias = node.names[0].asname or node.names[0].name
            if any(isinstance(n, ast.Name) and n.id == alias for n in ast.walk(new)):
                return None
            export_changed = True
            continue
        if not isinstance(node, ast.ClassDef) or not isinstance(current, ast.ClassDef):
            return None
        # Compare the entire class header and every non-method statement in order.
        if node.name != current.name or any(
            [ast.dump(x) for x in getattr(node, k, ())] != [ast.dump(x) for x in getattr(current, k, ())]
            for k in ("bases", "keywords", "decorator_list", "type_params")
        ):
            return None
        new_members = iter(current.body)
        member = next(new_members, None)
        for original in node.body:
            if member is not None and ast.dump(original) == ast.dump(member):
                member = next(new_members, None)
                continue
            if not _plain_method(original) or not _has_base(selector, old, node, original):
                return None
            # A direct class-body reference can publish the old function while
            # the module imports, before any handler call is observed.
            if any(isinstance(n, ast.Name) and n.id == original.name for n in ast.walk(node)):
                return None
            if sum(getattr(n, "name", None) == original.name for n in node.body) != 1:
                return None
            if getattr(member, "name", None) == original.name:
                if not _plain_method(member) or ast.dump(original.args) != ast.dump(member.args):
                    return None
                # Changes to defaults, annotations and the docstring can be observed without a call.
                if not _same_node(original.returns, member.returns) or original.type_comment != member.type_comment:
                    return None
                # Retain the existing control flow and call targets. Arbitrary new
                # code could persist state for a later, unrelated test.
                if not _literal_edits_only(original, member):
                    return None
                member = next(new_members, None)
            methods.append(original)
        if member is not None:
            return None
        current = next(remaining, None)
    return (methods, export_changed) if current is None and methods else None


def _line_mask(selector, path, ranges):
    bits = selector._isolated_index.executed_ranges(path, ranges)
    for first, last in ranges:
        for line in range(first, last + 1):
            bits |= selector._sites.get(path, {}).get(line, 0)
            bits |= selector._collection_sites.get(path, {}).get(line, 0)
    return bits


_REFLECT = re.compile(r"\b(?:vars|dir|getattr|getmembers|getmembers_static|getattr_static|DocTestSuite)\s*\(|\b__dict__\b")


@lru_cache(24)
def _other_consumers(selector, names):
    """Include tests reflecting on source or class data, and non-call handler references."""
    mask = 0
    for path in selector._source_hashes:
        if not path.endswith(".py") or not path.startswith(("tests/", "sqlglot/")):
            continue
        source = selector._source(path)
        test_file = path.startswith("tests/")
        if not any(name in source for name in names) and not (test_file and _REFLECT.search(source)):
            continue
        tree = _tree(path, source)
        calls = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        positions = []
        for node in ast.walk(tree):
            named_ref = (isinstance(node, ast.Attribute) and node.attr in names and id(node) not in calls
                         or isinstance(node, ast.Constant) and type(node.value) is str and node.value in names)
            if named_ref:
                positions.append((node.lineno, node.end_lineno))
        if test_file and (_REFLECT.search(source) or positions):
            mask |= selector._tests_by_file.get(path, 0)
        if positions:
            mask |= _line_mask(selector, path, positions)
    return mask


def select(selector, changes, patch):
    """Return a roster subset for supported overrides, or defer to the existing planner."""
    if (not changes or selector.unavailable or selector.granularity != "function"
            or not selector._reader_index or not selector._isolated_index
            or len({change.old_path for change in changes}) != len(changes)
            or any(c.old_path != c.new_path or not _generator_file(c.old_path) for c in changes)):
        return None
    bits = selector._unmapped
    names = set()
    try:
        for change in changes:
            path = change.old_path
            before = selector._source(path)
            after = apply_change(before, change)
            edits = _edits(selector, path, before, after)
            if not edits or not (warning_free(path, before) and warning_free(path, after)):
                return None
            methods, export_changed = edits
            warm = selector._reader_index.changed(path, before, after)
            cold = selector._isolated_index.changed(path, before, after)
            if warm is None or cold is None:
                return None
            names.update(m.name for m in methods)
            ranges = [(m.body[0].lineno, m.end_lineno) for m in methods]
            bits |= warm | cold | _line_mask(selector, path, ranges)
            bits |= number(selector._isolated_index.untraced.get(path, ""), len(selector.all_tests))
            bits |= (selector._position_readers or {}).get(path, 0)
            if export_changed:
                isolated_importers = cold_mask(selector, path)
                if isolated_importers is None:
                    return None
                bits |= (isolated_importers | selector._files.get(path, 0)
                         | selector._collection.get(path, 0) | selector._reads.get(path, 0))
        bits |= _other_consumers(selector, frozenset(names))
    except (OSError, SyntaxError, ValueError, UnicodeError, RecursionError, TypeError, KeyError):
        return None
    return {number for number in selector.all_tests if bits >> number & 1}
