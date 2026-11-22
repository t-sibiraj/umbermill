"""Distill native Linux observations of dictionary lookups without losing eager/ordinary overlaps."""

from collections import Counter
import hashlib
import json


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(capture, roster, fields):
    truth = json.loads((capture / "truth.json").read_bytes())
    if (not truth["complete"] or truth["pytest_exit"] or truth["collected_count"] != len(roster) + 1
            or not truth["source_import"]["expected_origin"] or any(
                truth[k] for k in ("failing", "missing", "unexpected_skips", "extra_failed_ids", "collection_errors")
            )):
        raise ValueError("incomplete original roster or incorrect source")
    files = sorted(capture.glob("[0-9]*.json"))
    if not files or {p.stem for p in files} != {p.stem for p in capture.glob("*.boot")}:
        raise ValueError("incomplete original process capture")
    workers = [json.loads(p.read_bytes()) for p in files]
    if any(w["errors"] or not w["qualified_threads"] or not w["strict_namespace"] for w in workers):
        raise ValueError("original process has unqualified observations")
    submitted = [(token, n) for w in workers for token, n in w["submitted"]]
    tasks = dict(submitted)
    acks = Counter(token for w in workers for token in w["completed"])
    if len(tasks) != len(submitted) or acks != Counter(tasks.keys()) or any(n not in roster for n in tasks.values()):
        raise ValueError("incomplete child-task lineage")
    root, = (w for w in workers if not w["child"])
    guarded = {roster[n] for n, _ in root["launched"] if n in roster}
    fallback = {f for w in workers for f in w["test_fallbacks"]}
    guarded.update(n for node, n in roster.items() if node.split("::")[0] in fallback)
    rows = {}
    for w in workers:
        inherited = {roster[tasks[k]] for k in w["completed"]}
        if w["startup"] in roster:
            inherited.add(roster[w["startup"]])

        def contexts(scopes):
            ids = set()
            uncertain = False
            for scope in scopes:
                if scope in roster:
                    ids.add(roster[scope])
                elif scope == "<init>" and not w["child"]:
                    # Eager class reads are checked by the dialect construction worker.
                    continue
                elif w["child"] and scope.startswith("<worker-init:") and inherited:
                    ids.update(inherited)
                elif scope == "<pool-transport>":
                    ids.update(roster[n] for n in tasks.values())
                else:
                    uncertain = True
            return ids, uncertain

        def add(key, operation, scopes):
            ids, opaque = contexts(scopes)
            row = rows.setdefault(key, {"opaque": False, "reads": {}})
            row["opaque"] |= opaque
            row["reads"].setdefault(operation, set()).update(ids)

        for key, scopes in w["fields"].items():
            if key.rsplit(":", 1)[-1] in fields:
                add(key, "field", scopes)
        for key, operations in w["dict_edges"].items():
            if key.rsplit(":", 1)[-1] not in fields:
                continue
            for op, scopes in operations.items():
                if op != "*" and not op.startswith("="):
                    raise ValueError("unknown mapping operation")
                add(key, op, scopes)
        for owner, scopes in w["reflection"].items():
            for field in fields:
                add(owner + ":" + field, "reflection", scopes)
    for row in rows.values():
        row["reads"] = {k: sorted(v) for k, v in row["reads"].items() if v}
    receipts = {p.name:digest(p) for p in [*files, capture / "truth.json", capture / "inputs.json",
                                            capture / "capture-environment.json"]}
    return rows, sorted(guarded), receipts
