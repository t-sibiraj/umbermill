"""Private test-source checks using pinned pytest collection and raw-reader observations."""

import ast
from pathlib import PurePosixPath
from .patches import apply_change, parse_patch


def assert_added(before, after):
    """One block of independent subtests or final primitive assertions in an existing test method."""
    if len(after) <= len(before):
        return False
    before_text = [ast.dump(s) for s in before]
    after_text = [ast.dump(s) for s in after]
    delta = len(after) - len(before)
    for i in range(len(before) + 1):
        if before_text != after_text[:i] + after_text[i + delta :]:
            continue

        def literal(n):
            try:
                ast.literal_eval(n)
                return True
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return False

        def call(n, names):
            return (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self"
                and n.func.attr in names
                and all(literal(a) for a in n.args)
                and all(k.arg and literal(k.value) for k in n.keywords)
            )

        def assertion(stmt):
            return isinstance(stmt, ast.Expr) and call(
                stmt.value,
                {
                    "assertEqual",
                    "assertNotEqual",
                    "assertTrue",
                    "assertFalse",
                    "assertIs",
                    "assertIsNot",
                    "assertIsNone",
                    "assertIsNotNone",
                    "fail",
                },
            )

        def isolated(stmt):
            if isinstance(stmt, ast.With) and len(stmt.items) == 1 and stmt.items[0].optional_vars is None:
                return (
                    call(stmt.items[0].context_expr, {"subTest"})
                    and bool(stmt.body)
                    and all(assertion(s) for s in stmt.body)
                )
            return i == len(before) and assertion(stmt)

        return all(isolated(s) for s in after[i : i + delta])
    return False


def assertion_owner(before, after):
    tree = ast.parse(before)
    fresh = ast.parse(after)
    if len(tree.body) != len(fresh.body):
        return None
    modified = []
    for old_cls, new_cls in zip(tree.body, fresh.body):
        if ast.dump(old_cls) == ast.dump(new_cls):
            continue
        if (
            not isinstance(old_cls, ast.ClassDef)
            or not isinstance(new_cls, ast.ClassDef)
            or len(old_cls.body) != len(new_cls.body)
        ):
            return None
        if (
            old_cls.decorator_list
            or len(old_cls.bases) != 1
            or ast.unparse(old_cls.bases[0]) != "unittest.TestCase"
            or not any(
                isinstance(n, ast.Import) and any(a.name == "unittest" and a.asname is None for a in n.names)
                for n in tree.body
            )
            or any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in {"subTest", "fail", "__getattribute__", "__getattr__"}
                for n in old_cls.body
            )
        ):
            return None
        for old, new in zip(old_cls.body, new_cls.body):
            if ast.dump(old) == ast.dump(new):
                continue
            if (
                not isinstance(old, ast.FunctionDef)
                or not isinstance(new, ast.FunctionDef)
                or not old.name.startswith("test_")
                or not assert_added(old.body, new.body)
            ):
                return None
            modified.append((old_cls.name, old.name, old.lineno))
            new.body = old.body
        if ast.dump(old_cls) != ast.dump(new_cls):
            return None
    return modified[0] if len(modified) == 1 else None


def trailing_comment_only(old, new):
    if not old.endswith("\n") or not new.startswith(old):
        return False
    suffix = new[len(old) :]
    return bool(
        suffix
        and all(not line.strip() or line.lstrip().startswith("#") for line in suffix.splitlines())
        and ast.dump(ast.parse(old, type_comments=True), include_attributes=True)
        == ast.dump(ast.parse(new, type_comments=True), include_attributes=True)
    )


def import_stop_only(old, new):
    if not old.endswith("\n") or not new.startswith(old):
        return False
    tail = ast.parse(new[len(old) :])
    if len(tail.body) != 1 or not isinstance(tail.body[0], ast.Raise):
        return False
    node = tail.body[0]
    if not isinstance(node.exc, ast.Call) or not isinstance(node.exc.func, ast.Name):
        return False
    exc_name = node.exc.func.id
    if any(
        isinstance(n, ast.Name)
        and n.id == exc_name
        and isinstance(n.ctx, (ast.Store, ast.Del))
        or isinstance(n, ast.alias)
        and (n.asname or n.name) == exc_name
        or isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
        and n.name == exc_name
        for n in ast.walk(ast.parse(old))
    ):
        return False
    return bool(
        node.cause is None
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id in {"ImportError", "ModuleNotFoundError", "RuntimeError"}
        and not node.exc.keywords
        and len(node.exc.args) <= 1
        and all(type(a) is ast.Constant and type(a.value) is str for a in node.exc.args)
    )


def pick(selector, patch, table):
    changes = parse_patch(patch)
    if len(changes) != 1:
        return None
    c = changes[0]
    p = c.old_path
    if (
        not p.startswith("tests/")
        or not PurePosixPath(p).name.startswith("test_")
        or not p.endswith(".py")
        or p != c.new_path
        or c.deleted
        or c.added
    ):
        return None
    row = table.get(p)
    if row is None or row["raw"] is None:
        return None
    old = selector._source(p)
    new = apply_change(old, c)
    guards = selector._unmapped | sum(1 << n for n in row["raw"])
    if trailing_comment_only(old, new):
        return guards, "identical compiled test and original assertions; observed raw readers"
    if not row["leaf"]:
        return None
    # Explicit fixture hooks/importers in another module disqualify the file.
    if import_stop_only(old, new) and row.get("foreign") is not None:
        return (
            guards | selector._tests_by_file.get(p, 0) | sum(1 << n for n in row["foreign"]),
            "leaf test module terminates at EOF",
        )
    owner = assertion_owner(old, new)
    if owner is None or row.get("metadata") is None:
        return None
    cls, name, line = owner
    ident = cls + "." + name + "|" + str(line)
    nodes = row.get("entries", {}).get(ident)
    if not nodes:
        return None
    return (
        guards | sum(1 << n for n in set(nodes) | set(row["metadata"])),
        "single test method adds isolated primitive assertions",
    )
