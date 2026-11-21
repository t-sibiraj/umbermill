"""Cross-check baseline input snapshots against the documented pristine optimizer outputs."""

import gzip
import json
from pathlib import Path
from rule_witness import run

assets = Path("/app/rule-evidence")
import tarfile

with tarfile.open("/recipe/rule-inputs.tar.gz", "r:gz") as archive:
    archive.extractall(assets, filter="data")
triples = json.loads(gzip.decompress((assets / "controls.json.gz").read_bytes()))
result = run(Path("/app/base"), [[0, triples]], assets / "blobs", None)
qualified = result["controls"]
(assets / "qualified.json.gz").write_bytes(
    gzip.compress(json.dumps(qualified, separators=(",", ":")).encode(), mtime=0)
)
print(
    "inputs", len(triples), "qualified", len(qualified), "checked", result["replayed"], flush=True
)
