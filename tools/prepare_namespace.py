"""Build a bounded inventory of real imports from the pinned SQLGlot tree."""

import argparse
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("root", type=Path)
    cli.add_argument("package", type=Path)
    cli.add_argument("maps", type=Path)
    cli.add_argument("--limit", type=int, default=24)
    args = cli.parse_args()
    root, package, maps = (p.resolve() for p in (args.root, args.package, args.maps))
    sys.path.insert(0, str(package.parent))
    model = importlib.import_module(package.name + ".state_fingerprint")
    worker = importlib.import_module(package.name + ".prepared_state")
    if (Path(model.__file__).resolve() != package / "state_fingerprint.py"
            or Path(worker.__file__).resolve() != package / "prepared_state.py"):
        raise ValueError("namespace producer imported the wrong package")
    archive = (maps / "impact.json.gz").read_bytes()
    if model.runtime()[-2] != "0":
        raise ValueError("namespace inventory requires PYTHONHASHSEED=0")
    actual_digest = hashlib.sha256(archive).hexdigest()
    if actual_digest != worker.IMPACT_DIGEST:
        raise ValueError(f"impact.json.gz has SHA-256 {actual_digest}, expected {worker.IMPACT_DIGEST}")
    index = json.loads(gzip.decompress(archive))
    if index["base_commit"] != model.BASE_COMMIT:
        raise ValueError("different SQLGlot revision")
    sys.path.insert(0, str(root))
    sqlglot = importlib.import_module("sqlglot")
    if Path(sqlglot.__file__).resolve() != root / "sqlglot/__init__.py":
        raise ValueError("wrong preload root")
    warm = set(sys.modules)
    groups = {}
    for relative, checksum in sorted(index["source_hashes"].items()):
        if not relative.startswith("sqlglot/") or not relative.endswith(".py") or relative.endswith("/__init__.py"):
            continue
        source = (root / relative).read_bytes()
        if hashlib.sha256(source).hexdigest() != checksum:
            raise ValueError(f"source tree differs from source map at {relative}")
        if model.eligible(source):
            dirname = str(Path(relative).parent)
            groups.setdefault(dirname, []).append((relative[:-3].replace("/", ".") not in warm, len(source), relative, checksum))
    groups = [sorted(v, reverse=True) for _, v in sorted(groups.items())]
    entries = {}
    attempted = 0
    limit = max(0, min(96, args.limit))
    while attempted < limit and any(groups):
        for group in groups:
            if not group or attempted >= limit:
                continue
            attempted += 1
            _, _, relative, expected = group.pop()
            try:
                result = worker.fresh(root, relative)
            except (OSError, ValueError, subprocess.SubprocessError):
                result = None
            row = result if result and result.get("runtime") == model.runtime() and result.get("source") == expected else None
            entries[relative] = {"source": expected, "fingerprint": (
                row["fingerprint"] if row else {"whole": None, "owners": {}, "unclear": ["fresh import unavailable"]})}
    payload = {"runtime": model.runtime(), "impact": worker.IMPACT_DIGEST, "entries": entries}
    out = maps / "namespace-inventory.json"
    temporary = out.with_suffix(".tmp")
    temporary.write_bytes(model.packed({"digest": model.digest(payload), "payload": payload}))
    temporary.replace(out)
    complete = sum(row["fingerprint"]["whole"] is not None for row in entries.values())
    print(f"Namespace inventory captured {complete} complete and {len(entries) - complete} unclear imports")


if __name__ == "__main__":
    main()
