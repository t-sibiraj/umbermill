"""Strict unified-diff parsing. Unsupported input must trigger a broad fallback."""

from dataclasses import dataclass, field
import re


class UnsupportedPatch(ValueError):
    pass


NO_NEWLINE = "\\ No newline at end of file"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Change:
    old_path: str
    new_path: str
    old_lines: set[int] = field(default_factory=set)
    inserted_after: set[int] = field(default_factory=set)
    deleted: bool = False
    added: bool = False
    hunks: list[Hunk] = field(default_factory=list)


HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(value: str) -> str:
    value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return value
    if not value.startswith(("a/", "b/")):
        raise UnsupportedPatch("unrecognized diff path")
    value = value[2:]
    if not value or any(p in ("", ".", "..") for p in value.split("/")) or "\\" in value:
        raise UnsupportedPatch("unsafe diff path")
    return value


def parse_patch(text: str) -> list[Change]:
    lines = text.split("\n")
    changes: list[Change] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old = _path(line[4:])
            i += 1
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise UnsupportedPatch("missing new path")
            new = _path(lines[i][4:])
            change = Change(old, new, deleted=new == "/dev/null", added=old == "/dev/null")
            changes.append(change)
            i += 1
            while i < len(lines) and lines[i].startswith("@@ "):
                match = HEADER.match(lines[i])
                if not match:
                    raise UnsupportedPatch("bad hunk header")
                old_at, old_count, new_at, new_count = match.groups()
                old_at, old_count = int(old_at), int(old_count or 1)
                new_at, new_count = int(new_at), int(new_count or 1)
                hunk = Hunk(old_at, old_count, new_at, new_count)
                change.hunks.append(hunk)
                old_seen = new_seen = 0
                replacement = False
                i += 1
                while old_seen < old_count or new_seen < new_count:
                    if i >= len(lines):
                        raise UnsupportedPatch("truncated hunk")
                    body = lines[i]
                    if body == NO_NEWLINE:
                        hunk.lines.append(body)
                        i += 1
                        continue
                    if not body or body[0] not in " +-":
                        raise UnsupportedPatch("bad hunk line")
                    if body[0] == "-":
                        change.old_lines.add(old_at + old_seen)
                        old_seen += 1
                        replacement = True
                    elif body[0] == "+":
                        if not replacement:
                            # With no old lines, unified diff names the line
                            # *before* the insertion, rather than the first
                            # line in an old-side range.
                            anchor = old_at + old_seen - (1 if old_count else 0)
                            change.inserted_after.add(max(0, anchor))
                        new_seen += 1
                    else:
                        old_seen += 1
                        new_seen += 1
                        replacement = False
                    if old_seen > old_count or new_seen > new_count:
                        raise UnsupportedPatch("hunk length mismatch")
                    hunk.lines.append(body)
                    i += 1
                if i < len(lines) and lines[i] == NO_NEWLINE:
                    hunk.lines.append(lines[i])
                    i += 1
            if not change.old_lines and not change.inserted_after:
                raise UnsupportedPatch("no changed text")
            continue
        if line.startswith(("Binary files ", "GIT binary patch", "rename from ", "rename to ", "copy from ", "copy to ", "old mode ", "new mode ", "new file mode ", "deleted file mode ")):
            raise UnsupportedPatch("structural diff needs broad selection")
        if line and not line.startswith(("diff --git ", "index ")):
            raise UnsupportedPatch("unexpected patch content")
        i += 1
    if not changes:
        raise UnsupportedPatch("empty or unsupported patch")
    return changes


def apply_change(source: str, change: Change) -> str:
    """Reconstruct a modified file only if every hunk is exact for the base."""
    if change.added or change.deleted or change.old_path != change.new_path or not change.hunks:
        raise UnsupportedPatch("reconstruction requires an existing file")
    body = source.split("\n")
    original = [line + "\n" for line in body[:-1]]
    if body[-1]:
        original.append(body[-1])
    result: list[str] = []
    cursor = 0
    for hunk in change.hunks:
        old_index = hunk.old_start - bool(hunk.old_count)
        new_index = hunk.new_start - bool(hunk.new_count)
        if old_index < cursor or old_index + hunk.old_count > len(original):
            raise UnsupportedPatch("hunk range does not match the baseline")
        result.extend(original[cursor:old_index])
        if len(result) != new_index:
            raise UnsupportedPatch("hunk result range is inconsistent")
        cursor = old_index
        lines = iter(hunk.lines)
        line = next(lines, None)
        while line is not None:
            if line == NO_NEWLINE:
                raise UnsupportedPatch("orphaned no-newline marker")
            following = next(lines, None)
            content = line[1:] + "\n"
            if following == NO_NEWLINE:
                content = content[:-1]
                following = next(lines, None)
            if line[0] in " -":
                if cursor >= len(original) or original[cursor] != content:
                    raise UnsupportedPatch("hunk context does not match the baseline")
                cursor += 1
            if line[0] in " +":
                result.append(content)
            line = following
    result.extend(original[cursor:])
    if any(not line.endswith("\n") for line in result[:-1]):
        raise UnsupportedPatch("unexpected content after end of file")
    return "".join(result)
