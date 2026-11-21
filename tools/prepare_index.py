"""Perform invariant baseline decoding once during the image build."""

import argparse
import gzip
import hashlib
import json
import marshal
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("maps", type=Path)
    p.add_argument("package", type=Path)
    a = p.parse_args()
    maps = a.maps.resolve()
    package = a.package.resolve()
    sys.path.insert(0, str(package.parent))
    from umbermill.impact import ImpactSelector
    from umbermill.prepared import INDEX_FIELDS

    payload = json.loads(gzip.decompress(maps.read_bytes()))
    roster = payload["roster"]
    base = ImpactSelector(
        {s: i for i, s in enumerate(roster)}, maps, package.parent / "base", granularity="function"
    )
    if base.unavailable:
        raise RuntimeError(base.unavailable)
    data = marshal.dumps((roster, [getattr(base, key) for key in INDEX_FIELDS]), 4)
    maps.with_name("impact.decoded").write_bytes(data)
    version = (sys.implementation.cache_tag, sys.version_info[:3])
    source_hash = hashlib.sha256(maps.read_bytes()).hexdigest()
    cache_hash = hashlib.sha256(data).hexdigest()
    (package / "_prepared_identity.py").write_text(
        f"SOURCE_DIGEST={source_hash!r}\nINDEX_DIGEST={cache_hash!r}\nINTERPRETER={version!r}\n"
    )


if __name__ == "__main__":
    main()
