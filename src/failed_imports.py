"""Recognize edits that make a previously importable module fail to load."""
import ast
from functools import lru_cache


@lru_cache(maxsize=16)
def mentioned_in_baseline(root, name):
    needle = name.encode("ascii")
    return any(
        needle in path.read_bytes()
        for folder in ("sqlglot", "tests")
        for path in (root / folder).rglob("*.py")
    )


def immediate_failure(path, before, after, root, root_names):
    try:
        ast.parse(after, path)
    except SyntaxError:
        return True

    # Require the original source through its final line, including comments.
    # Parse the suffix independently to exclude edits inside a trailing suite.
    prefix = before if before.endswith("\n") else before + "\n"
    if not after.startswith(prefix):
        return False
    try:
        suffix = ast.parse(after[len(prefix):], path)
    except SyntaxError:
        return False
    if len(suffix.body) != 1:
        return False
    statement = suffix.body[0]
    if isinstance(statement, ast.ImportFrom):
        if (not root_names or statement.module != "sqlglot" or statement.level
                or len(statement.names) != 1):
            return False
        item = statement.names[0]
        name = item.name
        # Account for existing runtime exports, importable child modules and
        # names referenced by the pinned tests, including literal monkeypatches.
        return (not item.asname and name.isascii() and name.isidentifier()
                and not name.startswith("_") and name not in root_names
                and not mentioned_in_baseline(root, name))
    if not isinstance(statement, ast.Raise):
        return False
    call = statement.exc
    return (
        statement.cause is None
        and isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in {"ImportError", "RuntimeError"}
        and not call.keywords
        and all(isinstance(arg, ast.Constant) and type(arg.value) is str for arg in call.args)
    )
