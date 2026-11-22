"""Pack the existing original converter census, source reader sites and dialect hierarchy."""

import ast
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from time_observations import FIELDS, fingerprint
from time_field_reads import collect as collect_field_reads


FIELDS_AND_TRIES = set(FIELDS) | {f.replace("MAPPING", "TRIE") for f in FIELDS}


def source_sites(source):
    conversion, direct, fixtures = {}, {}, set()
    for folder in ("sqlglot", "tests"):
        for path in sorted((source / folder).rglob("*.py")):
            raw = path.read_bytes()
            rel = path.relative_to(source).as_posix()
            if not any(f.encode() in raw for f in FIELDS_AND_TRIES | {"_classes"}) and rel != "sqlglot/time.py":
                continue
            tree = ast.parse(raw)
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if rel == "sqlglot/time.py" and isinstance(node, ast.FunctionDef) and node.name == "format_time":
                    first = next(st for st in node.body if not isinstance(st, ast.Expr))
                    conversion[rel] = [first.lineno]
                attribute = isinstance(node, ast.Attribute) and node.attr in FIELDS_AND_TRIES
                literal = isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in FIELDS_AND_TRIES
                registry = folder == "tests" and (
                    isinstance(node, ast.Attribute) and node.attr == "_classes"
                    or isinstance(node, ast.Constant) and isinstance(node.value, str) and "_classes" in node.value
                )
                if not (attribute or literal or registry):
                    continue
                parent = parents.get(node)
                delegated = False
                while parent is not None and not isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if isinstance(parent, ast.Call):
                        name = getattr(parent.func, "attr", getattr(parent.func, "id", ""))
                        delegated = name == "format_time"
                        break
                    parent = parents.get(parent)
                if delegated and attribute and isinstance(node.ctx, ast.Load):
                    continue
                owner = node
                while owner in parents and not isinstance(owner, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents[owner]
                if folder == "tests":
                    # Collection, unittest setup and pytest fixture activity can precede
                    # a method's recorded context. Retain all locally affected methods.
                    fixture_names = {"setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule"}
                    if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) or owner.name in fixture_names or any(
                        (isinstance(x, ast.Name) and x.id == "fixture" or isinstance(x, ast.Attribute) and x.attr == "fixture")
                        for deco in owner.decorator_list for x in ast.walk(deco)
                    ):
                        fixtures.add(rel)
                if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # The registered production descendants are reconstructed before narrowing.
                    continue
                if rel == "sqlglot/dialects/dialect.py" and owner.name == "__new__":
                    continue
                direct.setdefault(rel, set()).add((owner.lineno, owner.end_lineno))
    return conversion, direct, fixtures


def declaring_dialects(source, registry, hashes):
    """Find concrete class-owned declarations and unrelated eager map consumers."""
    from collections import Counter, defaultdict

    concrete = {name: cls for name, cls in registry.items() if name and name.isidentifier()}
    names = Counter(cls.__name__ for cls in concrete.values())
    per_path = Counter(cls.__module__ for cls in concrete.values())
    possible, eager = [], defaultdict(set)
    for filename in sorted((source / "sqlglot").rglob("*.py")):
        raw = filename.read_bytes()
        if not any(field.encode() in raw for field in FIELDS_AND_TRIES):
            continue
        rel = filename.relative_to(source).as_posix()
        module = rel[:-3].replace("/", ".")
        tree = ast.parse(raw)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        imports = {a.asname or a.name: a.name for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in FIELDS_AND_TRIES:
                continue
            receiver = getattr(node.value, "id", getattr(node.value, "attr", ""))
            receiver = imports.get(receiver, receiver)
            if not names[receiver]:
                continue
            child, parent = node, parents.get(node)
            while parent and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child, parent = parent, parents.get(parent)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and child in parent.body:
                continue
            target = parent.name if isinstance(parent, ast.ClassDef) else None
            eager[receiver].add((module, target))
        if per_path[module] != 1 or hashlib.sha256(raw).hexdigest() != hashes.get(rel):
            continue
        base, = (c for c in concrete.values() if c.__module__ == module)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == base.__qualname__]
        if len(classes) != 1 or names[base.__name__] != 1:
            continue
        declarations = []
        for stmt in classes[0].body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id in FIELDS and target.id in vars(base):
                declarations.append(target.id)
        if not declarations or len(set(declarations)) != len(declarations):
            continue
        # A native mapping referenced by another class-body expression may
        # initialize a cached value outside the four materialized time fields.
        if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in FIELDS_AND_TRIES
               for stmt in classes[0].body if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               for n in ast.walk(stmt)):
            continue
        possible.append((rel, base, declarations))
    for path, base, declarations in possible:
        children = {k: c for k, c in concrete.items() if issubclass(c, base)}
        registered = {(c.__module__, c.__qualname__) for c in children.values()}
        # A reference kept by a different declaration, such as a sibling dialect's
        # forward or inverse alias, can survive rebuilding the inheritance family.
        if any(names[c.__name__] != 1 or eager[c.__name__] - registered for c in children.values()):
            continue
        if 0 < len(children) <= 12:
            yield path, base, children, declarations


def original_snapshot(cls):
    from sqlglot.trie import new_trie

    snapshot = {}
    for f in FIELDS:
        mapping = getattr(cls, f, None)
        trie = getattr(cls, f.replace("MAPPING", "TRIE"), None)
        if (type(mapping) is not dict or type(trie) is not dict
                or not all(type(k) is str and type(v) is str for k, v in mapping.items())
                or mapping and trie != new_trie(mapping)):
            return None
        snapshot[f] = list(mapping.items())
    return snapshot


def pack(source, maps, census_file, output, capture):
    sys.path.insert(0, str(source.resolve()))
    from sqlglot import Dialect

    impact = json.loads(gzip.decompress((maps / "impact.json.gz").read_bytes()))
    cold = json.loads(gzip.decompress((maps / "isolated.json.gz").read_bytes()))
    fields = json.loads(gzip.decompress((maps / "fields.json.gz").read_bytes()))
    census = json.loads(census_file.read_bytes())
    roster = {node: n for n, node in enumerate(impact["roster"])}
    assert len(roster) == len(set(roster.values())) and impact["complete"]
    assert census["scope"] == "original parent interpreter"
    runner_sources = {}
    families = {}
    field_readers = {}
    original = {}
    observed_keys, opaque_children, process_receipts = collect_field_reads(capture, roster, FIELDS_AND_TRIES)
    for path, base, children, declared in declaring_dialects(source, Dialect.classes, impact["source_hashes"]):
        snapshots = {name: original_snapshot(cls) for name, cls in children.items()}
        child_sources = {c.__module__.replace(".", "/") + ".py" for c in children.values()}
        if any(s is None for s in snapshots.values()) or not child_sources <= impact["source_hashes"].keys():
            continue
        prefix = f"{base.__module__}:{base.__qualname__}:"
        tracked = [f for f in declared if prefix+f in fields["fields"] and prefix+f in fields["dicts"]]
        if not tracked:
            continue
        direct_fields = 0
        for child, cls in children.items():
            owner = f"{cls.__module__}:{cls.__qualname__}"
            reflected = [fields[k].get(owner, "") for k in ("reflection", "dict_reflection")]
            if "eager" in reflected:
                break
            reads = list(reflected)
            canonical = f"{owner}:TIME_MAPPING"
            if canonical not in fields["fields"] or canonical not in fields["dicts"]:
                file = "tests/dialects/test_" + child + ".py"
                direct_fields |= sum(1 << n for node, n in roster.items() if node.split("::")[0] == file)
            for f in FIELDS_AND_TRIES:
                key = f"{owner}:{f}"
                reads.append(fields["fields"].get(key, ""))
                reads.extend(fields["dicts"].get(key, {}).values())
            for encoded in reads:
                # Eager reads during class creation are handled by the fresh descendant
                # constructor and by materializing all four effective maps and their tries.
                if encoded != "eager":
                    direct_fields |= int.from_bytes(base64.b64decode(encoded, validate=True), "little")
        else:
            families[path] = [base.__name__, sorted(children), sorted(tracked)]
            field_readers[path] = hex(direct_fields)
            original.update(snapshots)
            runner_sources.update((p, impact["source_hashes"][p]) for p in child_sources)
    # Each previous operand observation carries the complete original ordered mapping.
    samples = {}
    for node, row in census["calls"].items():
        if node not in roster or node in census["unknown"]:
            continue
        n = roster[node]
        for key, values in row.items():
            items = census["mapping_items"][key]
            assert fingerprint(items) == key
            entry = samples.setdefault(key, {"items": items, "inputs": {}})
            for value in values:
                entry["inputs"].setdefault(value, []).append(n)

    # Recover the actual dictionary keys reached by the unchanged converter on
    # original recorded operands. No tests, patches or answers enter this replay.
    from sqlglot.time import format_time
    from sqlglot.trie import new_trie

    class MappingReads(dict):
        def get(self, key, default=None):
            self.lookups.add(key)
            return super().get(key, default)

    for record in samples.values():
        original_mapping = MappingReads(record["items"])
        trie = new_trie(original_mapping)
        lookups = {}
        for text in record["inputs"]:
            original_mapping.lookups = set()
            format_time(text, original_mapping, trie)
            lookups[text] = sorted(original_mapping.lookups)
        record["lookups"] = lookups

    def mask(value):
        if isinstance(value, int):
            value = impact["mask_table"][value]
        if isinstance(value, list):
            return sum(1 << i for i in value)
        return int.from_bytes(base64.b64decode(value), "little")

    conversion, direct, fixtures = source_sites(source)
    assert conversion and direct
    guards = sum(1 << n for n in set(fields["guards"] + fields["uninstrumented_tests"] + [r["test"] for r in fields["isolated_readers"]]))
    guards |= sum(1 << n for n in opaque_children)
    guards |= mask(cold["guard"])
    guards |= sum(1 << n for name, n in roster.items() if name.split("::")[0] in fixtures)
    converter_mask = 0
    for path, spans in [(p, [(n, n) for n in ns]) for p, ns in conversion.items()] + list(direct.items()):
        runner_sources[path] = impact["source_hashes"][path]
        is_converter = path in conversion
        guards |= mask(cold.get("untraced", {}).get(path, ""))
        warm = 0
        for mode in ("sites", "collection_sites"):
            for line, encoded in impact[mode].get(path, {}).items():
                if any(lo <= int(line) <= hi for lo, hi in spans):
                    warm |= mask(encoded)
        for n, lines in cold["lines"].get(path, {}).items():
            if any(lo <= line <= hi for lo, hi in spans for line in lines):
                guards |= 1 << int(n)
        if is_converter:
            converter_mask |= warm
        else:
            guards |= warm
    # This census is from a separate dialect-directory run. A recorded method may
    # receive additional inputs under full-suite ordering or inside a subprocess.
    # Replay can add positive evidence; it must never erase a recorded caller.
    guards |= converter_mask
    for path in ("sqlglot/trie.py", "sqlglot/dialects/dialect.py", *fixtures):
        runner_sources[path] = impact["source_hashes"][path]
    for path, expected in runner_sources.items():
        if hashlib.sha256((source / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"source differs from recorded coverage: {path}")
    artifacts = {f: hashlib.sha256((maps / f).read_bytes()).hexdigest()
                 for f in ("impact.json.gz", "isolated.json.gz", "fields.json.gz")}
    artifacts["converter_census"] = hashlib.sha256(census_file.read_bytes()).hexdigest()
    roster_bytes = json.dumps(impact["roster"], separators=(",", ":")).encode()
    result = {
        "provenance": {
            "inputs": artifacts,
            "field_processes": process_receipts,
            "operand_scope": census["scope"],
            "cross_run_operand_completeness": False,
        },
        "base": impact["base_commit"],
        "roster_sha256": hashlib.sha256(roster_bytes).hexdigest(),
        "families": families,
        "field_readers": field_readers,
        "observed_keys": observed_keys,
        "unknown_family": {name: [n for node, n in roster.items() if node.split("::")[0] == f"tests/dialects/test_{name}.py"]
                           for name in original},
        "owners": {name: f"{Dialect[name].__module__}:{Dialect[name].__qualname__}" for name in original},
        "source_hashes": runner_sources,
        "original": original,
        "operands": samples,
        "always": hex(guards),
        "size": len(roster),
    }
    output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0))


if __name__ == "__main__":
    pack(*(Path(arg).resolve() for arg in sys.argv[1:]))
