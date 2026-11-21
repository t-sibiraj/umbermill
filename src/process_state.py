"""Keep the full roster when a changed Python scope can alter inherited process state."""

import ast
from functools import lru_cache

from .patches import UnsupportedPatch, apply_change, parse_patch

_MAPPINGS = {"environ", "environb"}
_MUTATORS = {"putenv", "unsetenv", "setenv", "delenv", "chdir", "fchdir", "chroot"}
_MODULES = {"os", "posix", "nt"}
_READS = {"get", "__getitem__", "__contains__", "copy"}
_MARKERS = (
    *_MAPPINGS,
    *_MUTATORS,
    "__import__",
    "import_module",
    "import os",
    "import posix",
    "from os",
    "from posix",
    "import nt",
    "from nt",
)


@lru_cache(maxsize=64)
def _sensitive(source):
    return any(marker in source for marker in _MARKERS)


def _mapping_read(node, parent, parents):
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return isinstance(parent.ctx, ast.Load)
    if (
        isinstance(parent, ast.Attribute)
        and parent.value is node
        and parent.attr in _READS
    ):
        call = parents.get(parent)
        return isinstance(call, ast.Call) and call.func is parent
    return False


def _can_change_process(source, changed):
    if not _sensitive(source):
        return False
    tree = ast.parse(source)
    parents = {
        child: owner
        for owner in ast.walk(tree)
        for child in ast.iter_child_nodes(owner)
    }
    aliases = set(_MODULES)
    dynamic_importers = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                name.asname or name.name for name in node.names if name.name in _MODULES
            )
        if isinstance(node, ast.ImportFrom):
            if node.module in _MODULES and any(
                name.name in _MAPPINGS | _MUTATORS | {"*"} for name in node.names
            ):
                # A direct or renamed export can escape this scope through another file.
                return True
            if node.module == "importlib":
                dynamic_importers.update(
                    n.asname or n.name for n in node.names if n.name == "import_module"
                )
    changed_import = any(
        name in changed for name in dynamic_importers | {"import_module"}
    )
    changed_os_name = any(
        repr(name) in changed or ('"' + name + '"') in changed for name in _MODULES
    )
    for node in ast.walk(tree):
        parent = parents.get(node)
        if isinstance(node, ast.Attribute):
            if node.attr in _MUTATORS:
                return True
            if node.attr in _MAPPINGS and not _mapping_read(node, parent, parents):
                return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _MAPPINGS | _MUTATORS
        ):
            if isinstance(parent, (ast.Call, ast.Subscript)):
                return True
        if isinstance(node, ast.Name) and node.id in aliases:
            # Ordinary qualified calls retain their existing behavior. Passing the
            # module itself to reflection, an assignment, or a callback is an escape.
            if not (isinstance(parent, ast.Attribute) and parent.value is node):
                return True
            if parent.attr in {"__dict__", "__getattribute__"}:
                return True
        if isinstance(node, ast.Call):
            func = node.func
            imports = (
                isinstance(func, ast.Name)
                and func.id in dynamic_importers
                or isinstance(func, ast.Attribute)
                and func.attr == "import_module"
            )
            if imports and (changed_import or changed_os_name):
                arg = node.args[0] if node.args else None
                if (
                    isinstance(arg, ast.JoinedStr)
                    and arg.values
                    and isinstance(arg.values[0], ast.Constant)
                ):
                    prefix = arg.values[0].value
                    if isinstance(prefix, str) and prefix.startswith(
                        ("sqlglot.", "tests.")
                    ):
                        continue
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    # A dynamic import followed by an opaque lookup may return an OS module.
                    if not (
                        isinstance(parent, ast.Attribute)
                        and parent.attr
                        not in _MAPPINGS | _MUTATORS | {"__dict__", "__getattribute__"}
                    ):
                        return True
                elif arg.value in _MODULES and not isinstance(parent, ast.Attribute):
                    return True
    return False


def uncertain_process_state(patch, selector):
    try:
        changes = parse_patch(patch)
    except (ValueError, UnsupportedPatch):
        return False
    for change in changes:
        paths = (change.old_path, change.new_path)
        if not any(
            path.startswith(("sqlglot/", "tests/")) and path.endswith(".py")
            for path in paths
        ):
            continue
        changed = "\n".join(
            line[1:]
            for hunk in change.hunks
            for line in hunk.lines
            if line[:1] in {"+", "-"}
        )
        try:
            old = "" if change.added else selector._source(change.old_path)
            if not (_sensitive(changed) or _sensitive(old)):
                continue
            if _can_change_process(old, changed):
                return True
            if not change.deleted and _can_change_process(
                apply_change(old, change), changed
            ):
                return True
        except (OSError, UnsupportedPatch, SyntaxError, ValueError, RecursionError):
            if _sensitive(changed):
                return True
    return False
