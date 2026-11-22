"""Read immutable, build-prepared JSON maps, with the portable gzip input as fallback."""

import gzip
import hashlib
import json
import marshal
from pathlib import Path
import sys


def load_map(path):
    path = Path(path)
    compressed = path.read_bytes()
    try:
        from ._map_manifest import INTERPRETER, RECORDS
    except ImportError:
        record = None
    else:
        current = sys.implementation.cache_tag, sys.version_info[:3]
        record = RECORDS.get(path.name) if INTERPRETER == current else None
    if record and hashlib.sha256(compressed).hexdigest() == record[0]:
        # The generated package contains the trusted digests; never deserialize a sidecar
        # supplied by a custom map directory unless both digests match the build artifacts.
        try:
            decoded = path.with_name(path.name + ".decoded").read_bytes()
        except OSError:
            decoded = b""
        if decoded and hashlib.sha256(decoded).hexdigest() == record[1]:
            return marshal.loads(decoded)
    return json.loads(gzip.decompress(compressed))
