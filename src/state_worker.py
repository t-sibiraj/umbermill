"""Import one source module in a separate interpreter and report its observable state."""

import argparse
import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import resource
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE.parent))
model = importlib.import_module(PACKAGE.name + ".state_fingerprint")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("relative")
    args = parser.parse_args()
    root = args.root.resolve()
    parts = args.relative.removesuffix(".py").split("/")
    if (not args.relative.endswith(".py") or parts[0] != "sqlglot" or parts[-1] == "__init__"
            or not all(part.isidentifier() for part in parts)):
        return
    path = (root / args.relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or "sqlglot" in sys.modules:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    code = path.read_bytes()
    os.chdir(root)
    sys.path.insert(0, str(root))
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        module = importlib.import_module(".".join(parts))
        if Path(module.__file__).resolve() != path:
            return
        report = model.fingerprint(module, [root])
    print(json.dumps({"runtime": model.runtime(), "source": hashlib.sha256(code).hexdigest(),
                      "fingerprint": report}, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
