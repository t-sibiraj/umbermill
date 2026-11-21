"""Preserve coverage of code that observes SQLGlot's mutable dialect registry."""

import ast
from functools import lru_cache

from .patches import apply_change, parse_patch


# These are the files in the pristine project that reference the registry.
# New references elsewhere are recognized from the incoming text.
_REFERENCES = {
    "sqlglot/dialects/dialect.py",
    "tests/test_dialect_entry_points.py",
    "tests/dialects/test_dialect.py",
}
_FIELD = "_classes"


def _mentions_registry(node):
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute) and child.attr == _FIELD
            or isinstance(child, ast.Name) and child.id == _FIELD
            or isinstance(child, ast.Constant) and child.value == _FIELD
        ):
            return True
    return False


def _observer_code(statements):
    result = []
    for statement in statements:
        if isinstance(statement, ast.ClassDef):
            members = _observer_code(statement.body)
            header = [
                *statement.decorator_list, *statement.bases, *statement.keywords,
                *getattr(statement, "type_params", ()),
            ]
            if members or any(_mentions_registry(part) for part in header):
                result.append((statement.name, tuple(map(ast.dump, header)), members))
        elif _mentions_registry(statement):
            result.append(ast.dump(statement))
    return tuple(result)


@lru_cache(maxsize=16)
def _observers(source):
    return _observer_code(ast.parse(source).body) if _FIELD in source else ()


def uncertain_registry_state(patch, selector):
    # An isolated test starts with a populated registry. Earlier tests can clear
    # it, changing which recovery code runs and which exceptions are visible.
    if _FIELD not in patch and not any(path in patch for path in _REFERENCES):
        return False
    try:
        for change in parse_patch(patch):
            if not any(p.startswith(("sqlglot/", "tests/")) and p.endswith(".py")
                       for p in (change.old_path, change.new_path)):
                continue
            if change.old_path not in _REFERENCES and change.new_path not in _REFERENCES:
                if not any(_FIELD in line for h in change.hunks for line in h.lines):
                    continue
            before = selector._source(change.old_path)
            after = apply_change(before, change)
            if _observers(before) != _observers(after):
                return True
    except (OSError, ValueError, SyntaxError, RecursionError):
        return True
    return False
