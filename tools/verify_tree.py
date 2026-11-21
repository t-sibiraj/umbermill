"""Calculate a Git tree ID directly from a materialized checkout."""

import argparse
import hashlib
import os
from pathlib import Path
import stat


SQLGLOT_TREE = "6606fc5229c882bd0cf8f9ba79503e9e10d084f7"
SQLGLOT_GITLINKS = {
    "sqlglot-integration-tests": "3000152841de3ae0c0fce2e6b383d61396363521",
}


def object_id(kind, contents):
    header = kind + b" " + str(len(contents)).encode("ascii") + b"\0"
    return hashlib.sha1(header + contents).digest()


def tree_id(directory, gitlinks=None):
    gitlinks = gitlinks or {}
    records = []
    for entry in directory.iterdir():
        if entry.name in gitlinks:
            if entry.is_symlink() or not entry.is_dir() or any(entry.iterdir()):
                raise ValueError(f"expected an empty exported Git submodule: {entry}")
            continue
        name = os.fsencode(entry.name)
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            records.append((name + b"/", b"40000", name, tree_id(entry)))
        elif stat.S_ISLNK(mode):
            contents = os.fsencode(os.readlink(entry))
            records.append((name, b"120000", name, object_id(b"blob", contents)))
        elif stat.S_ISREG(mode):
            git_mode = b"100755" if mode & stat.S_IXUSR else b"100644"
            records.append((name, git_mode, name, object_id(b"blob", entry.read_bytes())))
        else:
            raise ValueError(f"unsupported Git tree entry: {entry}")
    for name, digest in gitlinks.items():
        encoded = os.fsencode(name)
        records.append((encoded, b"160000", encoded, bytes.fromhex(digest)))
    records.sort(key=lambda record: record[0])
    contents = b"".join(mode + b" " + name + b"\0" + digest
                        for _, mode, name, digest in records)
    return object_id(b"tree", contents)


def verify_tree(directory, expected):
    gitlinks = SQLGLOT_GITLINKS if expected == SQLGLOT_TREE else None
    actual = tree_id(directory, gitlinks).hex()
    if actual != expected:
        raise ValueError(f"Git tree mismatch: expected {expected}, got {actual}")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("expected")
    args = parser.parse_args()
    try:
        print(verify_tree(args.directory, args.expected))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
