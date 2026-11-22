"""Recognize changes confined to inert typing imports in an existing file."""

import ast

from .patches import apply_change


def _shape(source):
    tree = ast.parse(source)
    flags, modules = set(), set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and not statement.level and statement.module == "typing":
            flags.update(alias.asname or alias.name for alias in statement.names
                         if alias.name == "TYPE_CHECKING")
        elif isinstance(statement, ast.Import):
            modules.update(alias.asname or alias.name for alias in statement.names if alias.name == "typing")
    bindings = flags | modules
    if not bindings or flags & modules:
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load) and node.id in bindings:
            return None
        if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING" and not isinstance(node.ctx, ast.Load):
            return None
        if isinstance(node, ast.Global) and bindings.intersection(node.names):
            return None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.partition(".")[0]
                if alias.name == "*":
                    return None
                if name in bindings:
                    if isinstance(node, ast.Import):
                        proper = name in modules and alias.name == "typing"
                    else:
                        proper = (name in flags and not node.level and node.module == "typing"
                                  and alias.name == "TYPE_CHECKING")
                    if not proper:
                        return None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in bindings:
            return None
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name in bindings:
            return None
        if isinstance(node, ast.MatchMapping) and node.rest in bindings:
            return None
    removed = False
    ready = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            ready.update((alias.asname or alias.name) for alias in statement.names)
        if not isinstance(statement, ast.If):
            continue
        predicate = statement.test
        recognized = isinstance(predicate, ast.Name) and predicate.id in flags & ready
        if isinstance(predicate, ast.Attribute):
            recognized = (predicate.attr == "TYPE_CHECKING" and isinstance(predicate.value, ast.Name)
                          and predicate.value.id in modules & ready)
        if not recognized or not all(isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass))
                                     for node in statement.body):
            continue
        # Only module-scope imports are removed from the comparison. Function
        # and class symbol tables may change even for unreachable statements.
        statement.body = [ast.Pass()]
        removed = True
    return ast.dump(tree, include_attributes=False) if removed else None


def select(selector, changes, _patch):
    if not changes or not selector._reader_index or not selector._isolated_index:
        return None
    observers = selector._unmapped
    for change in changes:
        path = change.old_path
        lines = change.old_lines
        if (change.old_path != change.new_path or change.added or change.deleted
                or path not in selector._sites or not lines):
            return None
        warm, collection = selector._sites[path], selector._collection_sites.get(path, {})
        if any(warm.get(line) or collection.get(line) for line in lines):
            return None
        if selector._isolated_index.executed_ranges(path, [(line, line) for line in lines]):
            return None
        before = selector._source(path)
        if "TYPE_CHECKING" not in before:
            return None
        after = apply_change(before, change)
        baseline = _shape(before)
        if baseline is None or baseline != _shape(after):
            return None
        compile(after.encode("utf-8"), path, "exec", dont_inherit=True)
        normal = selector._reader_index.changed(path, before, after)
        isolated = selector._isolated_index.changed(path, before, after)
        if normal is None or isolated is None:
            return None
        observers |= normal | isolated
    return {n for n in selector.all_tests if observers >> n & 1}
