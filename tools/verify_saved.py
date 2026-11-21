"""Run a single qualified query against patched optimizer input in a fresh process."""

import gzip
import json
from pathlib import Path
import sys
from rule_witness import run

root = Path("/app/rule-evidence")
args = json.loads(Path(sys.argv[2]).read_bytes())
qualified = json.loads(gzip.decompress((root / "qualified.json.gz").read_bytes()))
result = run(Path(sys.argv[1]), args["refs"], root / "blobs", qualified)
result.pop("controls", None)
print(json.dumps(result))
