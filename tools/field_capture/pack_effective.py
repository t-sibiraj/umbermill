"""Pack a complete first-party pristine suite recording and its identified child receipts.

On Linux with the pinned Python, test dependencies, and a fresh SQLGlot checkout::

  mkdir -p /tmp/dispatch-capture/children
  export DISPATCH_ROSTER=/absolute/official/tests.json
  export DISPATCH_OUTPUT=/tmp/dispatch-capture/raw.json.gz
  export DISPATCH_RECEIPTS=/tmp/dispatch-capture/children
  PYTHONPATH=/absolute/tools/field_capture:$PWD python -B -m pytest -p effective_dispatch -q tests
  python /absolute/tools/field_capture/pack_effective.py /tmp/dispatch-capture /tmp/effective-dispatch.json.gz

Use a new empty capture directory. Both program and test source must be pristine.
The pristine process instrumentation changes class dictionary types, so completion
of the unchanged roster is required. Missing child receipts keep their owner guarded.
"""
import argparse
import gzip
import json
from pathlib import Path


def read(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def pack(directory):
    data = read(directory / "raw.json.gz")
    if data["passed"] != data["count"] or data["outside_spawn"]:
        raise ValueError("incomplete original roster or unowned process activity")
    guards = int(data["guard"], 16)
    audit = data.pop("child_audit")
    documented = set()
    for index, launches in audit.items():
        test = int(index)
        bit = 1 << test
        if not guards & bit:
            continue
        if launches["uncertain"]:
            continue
        children = []
        for pid in launches["pids"]:
            path = directory / "children" / (str(pid) + ".json.gz")
            if not path.exists():
                break
            child = read(path)
            if child["owner"] != index or child["unobserved"]:
                break
            children.append(child)
        else:
            documented.update(launches["pids"])
            guards &= ~bit
            for child in children:
                for category in ("tables", "writes"):
                    for table, events in child[category].items():
                        target = data[category].setdefault(table, {})
                        for key in events:
                            target[key] = hex(int(target.get(key, "0"),16) | bit)
                for base, derived in child["ancestry"].items():
                    existing = data["ancestry"].setdefault(base, [])
                    existing[:] = sorted(set(existing) | set(derived))
    for path in (directory / "children").glob("*.json.gz"):
        if int(path.name.split(".")[0]) not in documented:
            owner = int(read(path)["owner"])
            if not 0 <= owner < data["count"]:
                raise ValueError("unowned child process")
            guards |= 1 << owner
    data["guard"] = hex(guards)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = pack(args.directory)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    args.output.write_bytes(gzip.compress(raw, mtime=0))


if __name__ == "__main__":
    main()
