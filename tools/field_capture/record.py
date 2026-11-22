"""Record original dialect field and callback dependency receipts without loading a candidate.

Usage:
  python tools/field_capture/record.py --source /path/to/pinned/sqlglot \
      --roster /path/to/tests.json --output /path/to/new-receipts \
      --python /path/to/sqlglot-test-env/bin/python --phase suite

The interpreter must already contain the pinned project's test dependencies. Choose
collection, suite, or isolated (one interpreter per numbered method). The source is
copied to a disposable directory. The output contains observations and process status.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    flags = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "roster", "output", "python"):
        flags.add_argument("--" + name, required=True, type=Path)
    flags.add_argument("--phase", required=True, choices=("collection", "suite", "isolated"))
    args = flags.parse_args()
    source, roster = args.source.resolve(), args.roster.resolve()
    python = args.python.absolute()
    output = args.output.resolve()
    if not (source / "sqlglot/__init__.py").is_file() or not (source / "tests").is_dir():
        flags.error("the source path must contain the pinned sqlglot and tests directories")
    if output == source or source in output.parents or output.exists():
        flags.error("use a new output directory outside the source tree")
    rows = json.loads(roster.read_bytes())["tests"]
    ordered = [row["id"] for row in sorted(rows, key=lambda row: row["number"])]
    source_digest = hashlib.sha256()
    for base in ("sqlglot", "tests"):
        for path in sorted((source / base).rglob("*.py")):
            source_digest.update(path.relative_to(source).as_posix().encode() + b"\0")
            source_digest.update(bytes.fromhex(digest(path.read_bytes())))
    output.mkdir(parents=True)
    plugin = Path(__file__).resolve().parent
    seen = {}
    with tempfile.TemporaryDirectory(prefix="field-record-") as tmp:
        work = Path(tmp) / "source"
        shutil.copytree(source, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv", ".tox"))
        env = {**os.environ, "FIELD_TRACE_DIR": str(output), "FIELD_CAPTURE_SOURCE": str(work),
               "PYTHONPATH": os.pathsep.join((str(plugin), str(work))), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTEST_ADDOPTS": "", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        units = ordered if args.phase == "isolated" else [args.phase]
        for item in units:
            command = [str(python), "-B", "-m", "pytest", "-p", "field_capture_plugin", "-p", "no:cacheprovider", "-q"]
            if args.phase == "isolated":
                command.append(item)
            else:
                if args.phase == "collection":
                    command.append("--collect-only")
                command.append("tests")
            result = subprocess.run(command, cwd=work, env=env)
            seen[item] = result.returncode
            info = {"phase": args.phase, "source_python_sha256": source_digest.hexdigest(),
                    "roster_sha256": digest(roster.read_bytes()), "exits": seen,
                    "observer_sha256": {p.name: digest(p.read_bytes()) for p in plugin.glob("*.py")}}
            (output / "capture.json").write_text(json.dumps(info, indent=2) + "\n")
    return int(any(seen.values()))


if __name__ == "__main__":
    raise SystemExit(main())
