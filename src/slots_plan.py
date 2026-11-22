"""Treat instance layout as a dependency of every original class or subclass consumer."""

import ast
from functools import lru_cache
import re

from .module_cold import cold_mask
from .patches import apply_change


_UNKNOWN = object()
_BINDING = re.compile(r"\b__slots__\b")


def _assignment(stmt):
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
    elif isinstance(stmt, ast.AnnAssign):
        target = stmt.target
    else:
        return None
    if isinstance(target, ast.Name):
        return target.id, stmt.value
    return None


def _literal(node):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return "str", (node.value,)
    if isinstance(node, (ast.List, ast.Tuple)) and all(
            isinstance(x, ast.Constant) and type(x.value) is str for x in node.elts):
        return type(node).__name__, tuple(x.value for x in node.elts)
    return _UNKNOWN


def _layout(source):
    tree = ast.parse(source, type_comments=True)
    layouts = {}
    marks = {}
    covered_stores = set()

    def walk(body, prefix):
        for cls in body:
            if not isinstance(cls, ast.ClassDef):
                continue
            owner = (*prefix, cls.name)
            declared = {}
            for stmt in cls.body:
                entry = _assignment(stmt)
                if not entry:
                    continue
                name, value = entry
                declared.setdefault(name, []).append((stmt, value))
                if name != "__slots__":
                    continue
                if owner in layouts:
                    raise ValueError("repeated class layout")
                covered_stores.add(id(stmt))
                data = _literal(value)
                positions = {id(stmt)}
                if isinstance(value, ast.Name):
                    aliases = declared.get(value.id, ())
                    if len(aliases) == 1:
                        alias, original = aliases[0]
                        data = _literal(original)
                        positions.add(id(alias))
                if data is _UNKNOWN:
                    raise ValueError("dynamic class layout")
                layout_type, names = data

                def mangle(n):
                    prefix = cls.name.lstrip("_")
                    return "_" + prefix + n if prefix and n.startswith("__") and not n.endswith("__") else n

                class_bindings = {mangle(n.id) for member in cls.body if isinstance(member, (ast.Assign, ast.AnnAssign))
                                  if member.value is not None
                                  for target in (member.targets if isinstance(member, ast.Assign) else [member.target])
                                  for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                class_bindings.update(mangle(member.name) for member in cls.body
                                      if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                effective = {mangle(n) for n in names}
                if (len(names) != len(effective) or any(not n.isidentifier() for n in names)
                        or class_bindings & effective):
                    raise ValueError("ambiguous or conflicting slot names")
                layouts[owner] = (layout_type, names,
                                  tuple(ast.dump(base) for base in cls.bases),
                                  tuple(ast.dump(k) for k in cls.keywords),
                                  tuple(ast.dump(d) for d in cls.decorator_list))
                marks[owner] = positions
            walk(cls.body, owner)

    walk(tree.body, ())
    for node in ast.walk(tree):
        # Conditional, destructured and otherwise computed slot bindings are not a local proof.
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (any(isinstance(n, ast.Name) and n.id == "__slots__" for target in targets for n in ast.walk(target))
                    and id(node) not in covered_stores):
                raise ValueError("unclassified slot binding")
    return tree, layouts, marks


@lru_cache(16)
def _original(source):
    if "__slots__" not in source:
        return {}, frozenset(), frozenset()
    tree, records, marked = _layout(source)
    rows, aliases = set(), set()
    nodes = set().union(*marked.values())
    for n in ast.walk(tree):
        if id(n) in nodes:
            rows.update(range(n.lineno, n.end_lineno + 1))
            identifier, _ = _assignment(n)
            aliases.add(identifier)
        if isinstance(n, ast.ClassDef):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            rows.update(range(start, n.body[0].lineno + 1))
    return records, frozenset(rows), frozenset(aliases)


def _structural_text(lines, aliases):
    return any("__slots__" in text or text.lstrip().startswith(("class ", "@"))
               or any(delimiter * 3 in text for delimiter in (chr(34), chr(39)))
               or any(word in text for word in aliases) for text in lines)


def requires_fresh(before, after, change=None):
    """A changed or unclassifiable storage layout must never enter an existing interpreter."""
    if before == after or "__slots__" not in before and "__slots__" not in after:
        return False
    try:
        original, rows, aliases = _original(before)
        if change is not None:
            affected = change.old_lines | change.inserted_after
            touched = bool(rows & affected)
            edited = (r[1:] for h in change.hunks for r in h.lines if r.startswith(("+", "-")))
        else:
            # Patches replayed in a fork have materialized source instead of hunks.
            a, b = before.splitlines(), after.splitlines()
            start = 0
            while start < min(len(a), len(b)) and a[start] == b[start]:
                start += 1
            end_a, end_b = len(a), len(b)
            while end_a > start and end_b > start and a[end_a-1] == b[end_b-1]:
                end_a -= 1
                end_b -= 1
            touched = any(start <= row <= end_a for row in rows)
            edited = (line for block in (a[start:end_a], b[start:end_b]) for line in block)
        if not touched and not _structural_text(edited, aliases):
            return False
        return original != _layout(after)[1]
    except (SyntaxError, ValueError, RecursionError):
        return True


def _changed_owners(before, after):
    a, original, a_marks = _layout(before)
    b, patched, b_marks = _layout(after)
    owners = {o for o in original.keys() | patched.keys() if original.get(o) != patched.get(o)}
    if not owners:
        return None
    for tree, marks in ((a, a_marks), (b, b_marks)):
        removed = set().union(*(marks.get(o, ()) for o in owners))
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef):
                cls.body = [n for n in cls.body if id(n) not in removed]
    # Shared aliases have other public consumers, retained by the whole-module bounds.
    if ast.dump(a) != ast.dump(b):
        return None
    for owner in owners:
        old = original.get(owner)
        new = patched.get(owner)
        if old is None or new is None or old[2:] != new[2:] or bool(old[1]) != bool(new[1]):
            return None
        if ({"__dict__", "__weakref__"} & set(old[1])) != ({"__dict__", "__weakref__"} & set(new[1])):
            return None
    return {o[-1] for o in owners}


@lru_cache(12)
def _descendants(selector, initial):
    """Resolve original declared subclasses, including test-local and re-exported class aliases."""
    names = set(initial)
    paths = set()
    pending = {p for p in selector._source_hashes if p.endswith(".py") and p.startswith(("sqlglot/", "tests/"))}
    parsed = {}
    while True:
        before = len(names)
        for path in pending:
            source = selector._source(path)
            if not any(name in source for name in names):
                continue
            if path not in parsed:
                declarations = []
                aliases = []
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.ClassDef):
                        bases = [n.id if isinstance(n, ast.Name) else n.attr
                                 for n in node.bases if isinstance(n, (ast.Name, ast.Attribute))]
                        declarations.append((node.name, bases))
                    elif isinstance(node, ast.ImportFrom):
                        aliases.extend((a.name, a.asname) for a in node.names if a.asname)
                parsed[path] = declarations, aliases
            cls, imports = parsed[path]
            names.update(alias for original, alias in imports if original in names)
            for name, bases in cls:
                if any(base in names for base in bases):
                    paths.add(path)
                    names.add(name)
        if len(names) == before:
            return frozenset(paths)


def patch_requires_fresh(selector, changes):
    """Look at every patch part before approving a replay shortcut."""
    for change in changes or ():
        path = change.old_path
        if path == change.new_path and path.startswith("sqlglot/") and path.endswith(".py"):
            try:
                old = selector._source(path)
                if "__slots__" in old or any("__slots__" in row for h in change.hunks for row in h.lines):
                    if requires_fresh(old, apply_change(old, change), change):
                        return True
            except (OSError, UnicodeError, ValueError, RecursionError):
                return True
    return False


def _module_consumers(selector, path):
    bits = (selector._files.get(path, 0) | selector._collection.get(path, 0)
            | selector._reads.get(path, 0) | selector._cold_file_dependencies.get(path, 0)
            | selector._tests_by_file.get(path, 0) | (selector._position_readers or {}).get(path, 0))
    if path.startswith("sqlglot/"):
        original = cold_mask(selector, path)
        if original is None:
            raise ValueError("missing original process module evidence")
        bits |= original
    return bits


def select(selector, changes, patch):
    """Bound pure slot changes by original module reach. Mixed/opaque edits retain the roster."""
    if not changes or selector.unavailable or not patch_requires_fresh(selector, changes):
        return None
    try:
        if len({c.old_path for c in changes}) != len(changes):
            return set(selector.all_tests)
        affected = set()
        roots = set()
        bits = selector._unmapped
        for change in changes:
            path = change.old_path
            if path != change.new_path or not path.startswith("sqlglot/") or not path.endswith(".py"):
                return set(selector.all_tests)
            before = selector._source(path)
            after = apply_change(before, change)
            owners = _changed_owners(before, after)
            if not owners or not selector._reader_index or not selector._isolated_index:
                return set(selector.all_tests)
            warm = selector._reader_index.changed(path, before, after)
            isolated = selector._isolated_index.changed(path, before, after)
            if warm is None or isolated is None:
                return set(selector.all_tests)
            bits |= warm | isolated
            roots.update(owners)
            affected.add(path)
        for path in affected:
            bits |= _module_consumers(selector, path)
        if bits.bit_count() == len(selector.all_tests):
            return set(selector.all_tests)
        for path in _descendants(selector, frozenset(roots)) - affected:
            bits |= _module_consumers(selector, path)
        return {n for n in selector.all_tests if bits >> n & 1}
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, RecursionError):
        return set(selector.all_tests)
