"""Compare a vetted changed module with its original resolved import state."""

import ast
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import sys
import resource
import time

from .module_cold import cold_mask
from .patches import UnsupportedPatch, apply_change

WORKER = Path(__file__).with_name("import_snapshot_worker.py")


def safe_value(node):
    if isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.Attribute):
        return not node.attr.startswith("__") and safe_value(node.value)
    return isinstance(node, (ast.Tuple, ast.List)) and all(
        safe_value(v) for v in node.elts
    )


def class_dicts(tree):
    out = {}

    def walk(body, parent):
        for stmt in body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            name = parent + stmt.name
            for entry in stmt.body:
                targets = entry.targets if isinstance(entry, ast.Assign) else []
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and isinstance(entry.value, ast.Dict)
                ):
                    field = (name, targets[0].id)
                    if field in out:
                        raise ValueError("ambiguous class assignment")
                    out[field] = entry
            walk(stmt.body, name + ".")

    walk(tree.body, "")
    return out


def mapping_edit(before, after):
    a, b = class_dicts(before), class_dicts(after)
    if a.keys() != b.keys():
        return None
    changed = [key for key in a if ast.dump(a[key].value) != ast.dump(b[key].value)]
    if len(changed) != 1:
        return None
    field = changed[0]
    old = a[field].value
    new = b[field].value

    def keyed(node):
        return [
            k.value if isinstance(k, ast.Constant) and type(k.value) is str else None
            for k in node.keys
        ]

    left, right = keyed(old), keyed(new)
    difference = set(left) ^ set(right)
    if not difference or None in difference or len(difference) > 4:
        return None
    values = set()
    for expr, keys in ((old, left), (new, right)):
        values.update(
            ast.unparse(v) for k, v in zip(keys, expr.values) if k in difference
        )
        if any(
            keys.count(k) > 1 or not safe_value(v)
            for k, v in zip(keys, expr.values)
            if k in difference
        ):
            return None
        kept = [
            (k, v)
            for key, k, v in zip(keys, expr.keys, expr.values)
            if key not in difference
        ]
        expr.keys = [k for k, _ in kept]
        expr.values = [v for _, v in kept]
    # Shared spreads, eager constructors, unrelated namespace members and every
    # method body remain byte-for-byte equivalent at the AST level.
    if ast.dump(before) != ast.dump(after):
        return None
    return field, difference, values


def imports_only(before, after):
    """Accept one lookup in the import preamble with the same provider and position."""
    if len(before.body) != len(after.body):
        return None
    changed = [
        i
        for i, (a, b) in enumerate(zip(before.body, after.body))
        if ast.dump(a) != ast.dump(b)
    ]
    if len(changed) != 1:
        return None
    pos = changed[0]
    a, b = before.body[pos], after.body[pos]
    if (
        not isinstance(a, ast.ImportFrom)
        or not isinstance(b, ast.ImportFrom)
        or a.level
        or b.level
    ):
        return None
    if (
        not a.module
        or a.module != b.module
        or not (a.module == "sqlglot" or a.module.startswith("sqlglot."))
    ):
        return None
    if any(i.name == "*" for i in [*a.names, *b.names]):
        return None
    if any(
        not isinstance(st, (ast.Import, ast.ImportFrom))
        and not (
            i == 0
            and isinstance(st, ast.Expr)
            and isinstance(st.value, ast.Constant)
            and type(st.value.value) is str
        )
        for i, st in enumerate(before.body[:pos])
    ):
        return None
    return a.module


# A full input stream shares this budget, including failed and killed children.
# A long process scheduling stall cannot turn many recoverable timeouts into
# an organizer timeout. Each fresh child separately limits its CPU usage.
_SECONDS_LEFT = 9.0
_CPU_LEFT = 7.0


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def run(root, name, path, kind, patch, owner="", field=""):
    plan = dict(
        root=str(root), name=name, path=path, kind=kind, owner=owner, field=field
    )
    if patch is not False:
        plan["source"] = patch
    return _run(plan)


def _run(plan):
    global _SECONDS_LEFT, _CPU_LEFT
    available = min(_SECONDS_LEFT, _CPU_LEFT, 5 if plan["kind"] == "collection" else 1.5)
    if available < 0.12:
        return None
    start, cpu = time.monotonic(), child_cpu()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", str(WORKER)],
            input=json.dumps(plan).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=available,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
        if proc.returncode != 0 or len(proc.stdout) > 800_000:
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    finally:
        _SECONDS_LEFT -= time.monotonic() - start
        _CPU_LEFT -= child_cpu() - cpu


@lru_cache(32)
def clean(root, name, path, kind, owner="", field=""):
    return run(root, name, path, kind, False, owner, field)


@lru_cache(1)
def pristine_collection(root, prepared=None):
    # The chosen name is only a protocol placeholder; the pristine import
    # finder has no override installed while the complete suite is collected.
    if (
        root == Path("/app/base")
        and prepared == Path("/app/maps/pristine-collection.json")
        and prepared.is_file()
    ):
        raw = prepared.read_bytes()
        if len(raw) > 800_000:
            return None
        return json.loads(raw)
    return run(
        root, "sqlglot.optimizer", "sqlglot/optimizer/__init__.py", "collection", False
    )


def collection_unchanged(selector, edits, *, families=()):
    """Qualify all overrides together against the pinned collection receipt."""
    baseline = pristine_collection(
        selector.source_root, selector._maps_path.with_name("pristine-collection.json")
    )
    if (
        not baseline
        or baseline.get("state") != "collected"
        or baseline["code"]
        or baseline["errors"]
        or not set(selector._snapshot_nodes) <= set(baseline["items"])
    ):
        return False
    changed = set(edits)
    observed = _run(dict(
        root=str(selector.source_root), kind="collection", overrides=edits, families=sorted(families)
    ))
    return bool(
        observed
        and all(
            baseline[key] == observed.get(key)
            for key in ("items", "errors", "code", "state", "handled", "modules")
        )
        and all(
            set(baseline[key]) - changed == set(observed[key]) - changed
            for key in ("attempted", "opened")
        )
    )


def failed_collection(selector, name, path, patch):
    before = pristine_collection(
        selector.source_root, selector._maps_path.with_name("pristine-collection.json")
    )
    if (
        not before
        or before.get("state") != "collected"
        or before["errors"]
        or before["code"] != 0
    ):
        return None
    after = run(selector.source_root, name, path, "collection", patch)
    if not after or after.get("state") != "collected":
        return None
    if before["handled"] != after["handled"]:
        return None
    surviving = set(after["modules"])
    original = set(before["modules"])
    if not surviving <= original:
        return None
    # A failed import can remove additional modules from the final interpreter,
    # even when their source did not change. Preserve every direct cold or warm
    # consumer of the complete missing-module frontier.
    frontier = 0
    changed_modules = (original - surviving) | {
        module_file
        for module_file in surviving
        if before["modules"][module_file] != after["modules"][module_file]
    }
    for module_file in changed_modules:
        missing = cold_mask(selector, module_file)
        if missing is None:
            return None
        frontier |= (
            missing
            | selector._files.get(module_file, 0)
            | selector._reads.get(module_file, 0)
        )
    nodes = selector._snapshot_nodes
    old = set(before["items"])
    new = set(after["items"])
    if not set(nodes) <= old or not new <= old or len(after["items"]) != len(new):
        return None
    bits = frontier
    for node in old - new:
        if node in nodes:
            bits |= 1 << nodes[node]
    for failure in after["errors"]:
        file = failure.split("::", 1)[0]
        if file not in selector._tests_by_file:
            return None
        bits |= selector._tests_by_file[file]
    return bits


def module_bounds(
    selector, path, patch, kind, fields=None, expected=None, values=None, provider=None
):
    if (
        not path.startswith("sqlglot/")
        or not path.endswith(".py")
        or path.endswith("/__init__.py")
        or not all(part.isidentifier() for part in path[:-3].split("/"))
        or not selector._reader_index
    ):
        return None
    mask = cold_mask(selector, path)
    if mask is None:
        return None
    mask |= (
        selector._unmapped
        | selector._reader_index.guard
        | selector._files.get(path, 0)
        | selector._reads.get(path, 0)
        | (selector._position_readers or {}).get(path, 0)
    )
    name = path[:-3].replace("/", ".")
    # Imported modules can publish shared aliases during collection. Restrict
    # expensive supplemental probes to originally deferred source when no
    # complete namespace-consumer envelope exists for its eager publications.
    if kind == "load":
        original = pristine_collection(
            selector.source_root,
            selector._maps_path.with_name("pristine-collection.json"),
        )
        if (
            not original
            or original.get("code") != 0
            or original.get("errors")
            or not set(selector._snapshot_nodes) <= set(original.get("items", ()))
            or path in original["modules"]
        ):
            return None
    owner, field = fields or ("", "")
    if kind == "mapping":
        # A real init snapshot cannot enumerate observations of aliases already
        # published by eager metaclasses, descriptors or other test contexts.
        # Only certified whole-owner traces authorize regression exclusions.
        try:
            from .state_readers import state_mask

            readers = state_mask(selector, name + ":" + owner + ":" + field, values)
        except ImportError:
            return None
        if readers is None:
            return None
        mask |= readers | selector._collection.get(path, 0)
    if mask.bit_count() >= len(selector.all_tests) - 3:
        return None
    before = clean(selector.source_root, name, path, kind, owner, field)
    if not before:
        return None
    after = run(selector.source_root, name, path, kind, patch, owner, field)
    if not after:
        return None
    if kind == "load" and before["state"] == "loaded" and after["state"] == "failed":
        if provider is not None and (
            not after["ordinary_provider"]
            or after["error"] != "ImportError"
            or after["name"] != provider
            or after.get("origin") != path
        ):
            return None
        if (
            patch is not None
            and path not in original["attempted"]
            and path not in original["opened"]
        ):
            # An ordinary in-place edit cannot change the original collection
            # through a target nobody attempted to import or read. Preserve all
            # existing collection/metadata observers and actual runtime consumers.
            return (
                mask | selector._collection.get(path, 0),
                "resolved deferred import failure and original consumers",
            )
        additional = failed_collection(selector, name, path, patch)
        if additional is not None:
            return mask | additional, "resolved import failure and changed collection"
    if kind == "mapping" and before["state"] == after["state"] == "mapping":
        if any(
            d is None
            for observation in (before, after)
            for _, _, d in observation["entries"]
        ):
            return None
        old = {k: (s, d) for k, s, d in before["entries"]}
        new = {k: (s, d) for k, s, d in after["entries"]}
        if [k for k in old if k in new] != [k for k in new if k in old]:
            return None
        changed = {
            row[0]
            for k in old.keys() | new.keys()
            if (row := old.get(k) or new[k]) and old.get(k) != new.get(k)
        }
        if changed and None not in changed and changed == expected:
            return mask, "resolved class mapping and cold/warm consumers"
    return None


def select(selector, change):
    path = change.old_path
    if (
        change.added
        or change.deleted
        or path != change.new_path
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
        or not change.hunks
    ):
        return None
    source = selector._source(path)
    patched = apply_change(source, change)
    try:
        old = ast.parse(source)
        new = ast.parse(patched)
    except SyntaxError:
        result = module_bounds(selector, path, patched, "load")
        return result
    provider = imports_only(old, new)
    if provider:
        return module_bounds(selector, path, patched, "load", provider=provider)
    edit = mapping_edit(old, new)
    if edit is not None:
        return module_bounds(selector, path, patched, "mapping", *edit)
    return None


def missing_module_readers(selector, path):
    """Return cold, warm and collection dependents when a real import disappears."""
    try:
        result = module_bounds(selector, path, None, "load")
        return result[0] if result else None
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
