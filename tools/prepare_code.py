"""Prepare exact old-program code for the small subset of files the selector can inspect."""
import argparse
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import sys
import warnings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("maps", type=Path)
    parser.add_argument("package", type=Path)
    parser.add_argument("base", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.package.resolve().parent))
    from umbermill.compiled import fingerprint
    map_path = args.maps.resolve()
    base = args.base.resolve()
    source_hashes = json.loads(gzip.decompress(map_path.read_bytes()))["source_hashes"]
    folder = map_path.parent / "compiled"
    folder.mkdir(exist_ok=True)
    index = {}
    for name, digest in sorted(source_hashes.items()):
        if not name.endswith(".py") or not name.startswith(("sqlglot/", "tests/")):
            continue
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            raise ValueError("baseline file outside verified source tree")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"build-time source differs: {name}")
        with warnings.catch_warnings(record=True) as diagnostics:
            warnings.simplefilter("always")
            code = compile(data, name, "exec", dont_inherit=True)
        blob = marshal.dumps((code, not diagnostics), 4)
        artifact = hashlib.sha256(blob).hexdigest()
        (folder / artifact).write_bytes(blob)
        index[name] = (digest, artifact)
    (args.package / "_compiled_catalog.py").write_text(
        f"INTERPRETER = {fingerprint()!r}\nSOURCES = {index!r}\n"
    )
    print(f"{len(index)} verified sources")


if __name__ == "__main__":
    main()
