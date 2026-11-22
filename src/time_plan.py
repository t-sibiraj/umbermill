"""Select consumers of literal dialect time-map edits, including inherited readers."""

import ast
from functools import lru_cache
import gzip
import hashlib
import json

from . import BASE_COMMIT
from .deferred import warning_free
from .import_snapshot import run
from .mapping_value import _strings
from .patches import UnsupportedPatch, apply_change


DATA_SHA256 = "fc1760e960bd701496186675d5faad0c9a993a08547ca6ef714c81556bd7d59d"


@lru_cache(maxsize=1)
def _evidence(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATA_SHA256:
        raise ValueError("time index digest mismatch")
    return json.loads(gzip.decompress(raw))


def _assignments(tree, owner, fields):
    classes = [stmt for stmt in tree.body if isinstance(stmt, ast.ClassDef) and stmt.name == owner]
    if len(classes) != 1:
        raise ValueError("ambiguous dialect class")
    cls = classes[0]
    assignments = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id in fields:
            if target.id in assignments or getattr(stmt, "type_comment", None):
                raise ValueError("ambiguous time declaration")
            assignments[target.id] = stmt
    return cls, assignments


def _literal(assignment):
    value = _strings(assignment.value)
    if len(value) > 512 or any(not k or len(k) > 128 or not v or len(v) > 256 for k, v in value.items()):
        raise ValueError("unbounded time format")
    return value


def _edit(old, new, family):
    trees = [ast.parse(source, type_comments=True) for source in (old, new)]
    (left, a), (right, b) = [_assignments(tree, family[0], family[2]) for tree in trees]
    diff = [f for f in family[2] if (ast.dump(a[f]) if f in a else None) != (ast.dump(b[f]) if f in b else None)]
    if len(diff) != 1 or diff[0] not in a:
        return None
    field = diff[0]
    st = a[field]
    before = _literal(st)
    annotation = ast.dump(st.annotation) if isinstance(st, ast.AnnAssign) else None
    after = None
    if field in b:
        updated = b[field]
        new_annotation = ast.dump(updated.annotation) if isinstance(updated, ast.AnnAssign) else None
        if annotation != new_annotation or left.body.index(st) != right.body.index(updated):
            return None
        after = _literal(updated)
        right.body.remove(updated)
    elif annotation is not None:
        return None
    left.body.remove(st)
    if ast.dump(trees[0]) != ast.dump(trees[1]):
        return None
    keys = None if after is None else {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return field, keys


def select(selector, changes, patch):
    """Return selected test numbers, or defer to the standard selector."""
    if len(changes) != 1 or selector.unavailable or selector._maps_path is None:
        return None
    c = changes[0]
    path = c.old_path
    if c.added or c.deleted or path != c.new_path or not path.startswith("sqlglot/dialects/"):
        return None
    try:
        evidence_path = selector._maps_path.with_name("time-inputs.json.gz").resolve()
        index = _evidence(evidence_path)
        family = index["families"].get(path)
        if not family or not selector._reader_index or not selector._isolated_index:
            return None
        if index["base"] != BASE_COMMIT or index["roster_sha256"] != selector._snapshot_roster_digest:
            return None
        if any(selector._source_hashes.get(p) != h for p, h in index["source_hashes"].items()):
            return None
        old = selector._source(path)
        new = apply_change(old, c)
        edit = _edit(old, new, family)
        if edit is None or not warning_free(path, new):
            return None
        field, keys = edit
        declared = path[:-3].replace("/", ".") + ":" + family[0] + ":" + field
        from .time_observations import direct_readers
        direct = direct_readers(index, declared, keys)
        warm = selector._reader_index.changed(path, old, new)
        cold = selector._isolated_index.changed(path, old, new)
        if warm is None or cold is None:
            return None
        state = run(selector.source_root, path[:-3].replace("/", "."), path, "time_maps",
                    new, ",".join(family[1]), str(evidence_path))
        if not isinstance(state, dict) or state.get("state") != "ready":
            return None
        mask = (int(index["always"], 16) | int(index["field_readers"][path], 16) | warm | cold | selector._unmapped
                | selector._reads.get(path, 0) | selector._collection.get(path, 0)
                | selector._files.get(path, 0) | (selector._position_readers or {}).get(path, 0))
        for n in direct | set(state["affected"]):
            if type(n) is not int or not 0 <= n < index["size"]:
                return None
            mask |= 1 << n
        return {n for n in selector.all_tests if mask & (1 << n)}
    except (AttributeError, OSError, ValueError, TypeError, KeyError, SyntaxError, RecursionError, UnsupportedPatch):
        return None
