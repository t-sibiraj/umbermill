"""Move deterministic baseline JSON decoding into the image preparation step."""

import argparse
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import sys

SOURCES = ("fields", "conditions", "isolated", "readers", "fresh-process")


def build(directory, package):
    records = {}
    for name in SOURCES:
        path = directory / (name + ".json.gz")
        buf = path.read_bytes()
        data = json.loads(gzip.decompress(buf))
        decoded = marshal.dumps(data, 4)
        if marshal.loads(decoded) != data:
            raise ValueError(f"Prepared map differs from {path.name}")
        path.with_name(path.name + ".decoded").write_bytes(decoded)
        records[path.name] = hashlib.sha256(buf).hexdigest(), hashlib.sha256(decoded).hexdigest()
    identity = sys.implementation.cache_tag, sys.version_info[:3]
    (package / "_map_manifest.py").write_text(f"INTERPRETER = {identity!r}\nRECORDS = {records!r}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("maps", type=Path)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    build(args.maps, args.package)
