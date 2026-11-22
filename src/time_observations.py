"""Project observed converter operands onto freshly constructed SQLGlot dialects."""

import gzip
import hashlib
import json


FIELDS = ("TIME_MAPPING", "INVERSE_TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_FORMAT_MAPPING")


def fingerprint(pairs):
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:20]


def direct_readers(data, field, keys):
    row = data["observed_keys"].get(field)
    if row is None or row["opaque"]:
        raise ValueError("unknown attributed mapping readers")
    reads = row["reads"]
    affected = set(reads.get("field", ())) | set(reads.get("reflection", ())) | set(reads.get("*", ()))
    if keys is None:
        for readers in reads.values():
            affected.update(readers)
    else:
        for key in keys:
            affected.update(reads.get("=" + key, ()))
    return affected


def changed_inputs(dialects, families, evidence):
    from sqlglot.time import format_time
    from sqlglot.trie import new_trie

    data = json.loads(gzip.decompress(evidence.read_bytes()))
    affected = set()
    retained = int(data["always"], 16)
    checked = set()
    for name in families:
        dialect = dialects[name]
        for field in FIELDS:
            before = data["original"][name][field]
            mapping = getattr(dialect, field)
            trie = getattr(dialect, field.replace("MAPPING", "TRIE"))
            if type(mapping) is not dict or not all(type(k) is str and type(v) is str for k, v in mapping.items()):
                raise ValueError("unrecognized time mapping")
            # An empty FORMAT_MAPPING can inherit its sibling TIME_TRIE.
            # Its direct converter has no substitutions to make on any string.
            if mapping and trie != new_trie(mapping):
                raise ValueError("unrecognized converter trie")
            old_items = tuple(map(tuple, before))
            new_items = tuple(mapping.items())
            if old_items == new_items:
                continue
            original = dict(old_items)
            keys = {k for k in original.keys() | mapping.keys() if original.get(k) != mapping.get(k)}
            qualified = data["owners"][name] + ":" + field
            if qualified in data["observed_keys"]:
                # Apply class-specific readers even if another dialect uses the same
                # converter contents. A missing lookup is a dependency on future insertions.
                affected.update(direct_readers(data, qualified, keys))
            else:
                affected.update(data["unknown_family"][name])
            if (old_items, new_items) in checked:
                continue
            # This cache is scoped to one patch and to two full ordered mappings.
            # A caller's identity or a passing assertion cannot establish equivalence.
            checked.add((old_items, new_items))
            same_trie = tuple(original) == tuple(mapping)
            observed = data["operands"].get(fingerprint(before))
            if observed is None:
                continue
            if observed["items"] != before:
                raise ValueError("ambiguous baseline mapping")
            base_trie = new_trie(original)
            for value, tests in observed["inputs"].items():
                if same_trie and keys.isdisjoint(observed["lookups"][value]):
                    continue
                unresolved = [n for n in tests if not (retained & (1 << n)) and n not in affected]
                if unresolved and format_time(value, original, base_trie) != format_time(value, mapping, trie):
                    affected.update(unresolved)
    return {"state": "ready", "affected": sorted(affected)}
