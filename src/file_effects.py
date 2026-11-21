"""Reconstruct plain Git file operations against the immutable source tree."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat

from .patches import UnsupportedPatch, apply_change, parse_patch


@dataclass(frozen=True)
class FileEdit:
    old: str | None
    new: str | None
    before: str
    after: str
    patch: str


# Quoted/escaped paths, nonregular modes, submodules, copies and combined diffs
# go through the selector's ordinary broad fallback.
_PATH = r"[A-Za-z0-9_.+@/-]+"
_HEADER = re.compile(rf"diff --git a/({_PATH}) b/({_PATH})")
_INDEX = re.compile(r"index ([a-f0-9]{7,40})\.\.([a-f0-9]{7,40})(?: (100644|100755))?")
_MODE = re.compile(r"(new file|deleted file) mode (100644|100755)")


def _fail():
    raise UnsupportedPatch("cannot reconstruct this file operation")


def _location(root, name, *, exists):
    parts = name.split("/")
    if any(p in {"", ".", ".."} or p.casefold() == ".git" for p in parts):
        _fail()
    path = root
    for part in parts[:-1]:
        path = path / part
        if not stat.S_ISDIR(path.lstat().st_mode):
            _fail()
    path = path / parts[-1]
    if exists:
        if not stat.S_ISREG(path.lstat().st_mode):
            _fail()
    elif os.path.lexists(path):
        _fail()
    return path


def _match_hash(text, digest, exists):
    if not exists:
        if digest.strip("0"):
            _fail()
        return
    data = text.encode("utf-8")
    actual = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
    if not actual.startswith(digest):
        _fail()


def _one(root, block):
    lines = block.split("\n")
    if lines[-1] == "":
        lines.pop()
    head = _HEADER.fullmatch(lines[0])
    if not head:
        _fail()
    src, dst = head.groups()
    if src != dst:
        if lines[1:] != ["similarity index 100%", "rename from " + src, "rename to " + dst]:
            _fail()
        before = _location(root, src, exists=True).read_bytes().decode("utf-8")
        _location(root, dst, exists=False)
        return FileEdit(src, dst, before, before, block)
    idx = 1
    mode = _MODE.fullmatch(lines[idx]) if idx < len(lines) else None
    if mode:
        idx += 1
    sha = _INDEX.fullmatch(lines[idx]) if idx < len(lines) else None
    if sha is None:
        _fail()
    idx += 1
    adding = bool(mode and mode[1] == "new file")
    removing = bool(mode and mode[1] == "deleted file")
    if mode and sha[3]:
        _fail()
    file = _location(root, src, exists=not adding)
    before = "" if adding else file.read_bytes().decode("utf-8")
    if not adding and (perm := (mode[2] if mode else sha[3])):
        if bool(file.stat().st_mode & stat.S_IXUSR) != (perm == "100755"):
            _fail()
    if mode and idx == len(lines):
        # Git represents an empty regular-file addition/deletion with mode and
        # blob headers alone. A nonempty missing hunk remains unsupported.
        if before:
            _fail()
        _match_hash("", sha[1], not adding)
        _match_hash("", sha[2], not removing)
        return FileEdit(None if adding else src, None if removing else dst, "", "", block)
    old_header = "--- " + ("/dev/null" if adding else "a/" + src)
    new_header = "+++ " + ("/dev/null" if removing else "b/" + dst)
    if lines[idx:idx + 2] != [old_header, new_header]:
        _fail()
    hunks = "\n".join(lines[idx + 2:]) + "\n"
    same_path = f"--- a/{src}\n+++ b/{src}\n" + hunks
    changes = parse_patch(same_path)
    if len(changes) != 1:
        _fail()
    after = apply_change(before, changes[0])
    if removing and after:
        _fail()
    _match_hash(before, sha[1], not adding)
    _match_hash(after, sha[2], not removing)
    return FileEdit(None if adding else src, None if removing else dst, before, after, block)


def materialize(root: Path, patch: str) -> list[FileEdit]:
    """Validate all hunks, blob IDs, paths and destinations without writing files."""
    if not patch.startswith("diff --git ") or "\0" in patch or "\r" in patch:
        _fail()
    blocks = re.split(r"(?m)(?=^diff --git )", patch)[1:]
    edits = [_one(root, block) for block in blocks]
    occupied = set()
    for edit in edits:
        paths = {name.casefold() for name in (edit.old, edit.new) if name is not None}
        if occupied & paths:
            _fail()
        occupied.update(paths)
    return edits
