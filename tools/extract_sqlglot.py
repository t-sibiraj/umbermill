#!/usr/bin/env python3
"""Extract the checksum-pinned SQLGlot archive with Git file modes."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import tempfile
from zipfile import BadZipFile, ZipFile


COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
ARCHIVE_SHA256 = "fdfa4469d52fb332d13b680536b44f2607fd3ed37b36294ce88d1fd150a09e5c"
MAX_EXTRACTED_BYTES = 100_000_000


def extract_archive(archive: Path, destination: Path) -> None:
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError("pinned SQLGlot ZIP checksum does not match")
        source.seek(0)
        with ZipFile(source) as contents:
            if contents.comment != COMMIT.encode("ascii"):
                raise ValueError("ZIP does not identify the pinned SQLGlot commit")
            _extract(contents, destination)


def _extract(contents: ZipFile, destination: Path) -> None:
    root = f"sqlglot-{COMMIT}"
    if destination.exists() or destination.is_symlink():
        raise ValueError("refusing to replace an existing source directory")
    if sum(member.file_size for member in contents.infolist()) > MAX_EXTRACTED_BYTES:
        raise ValueError("SQLGlot ZIP exceeds the safe extraction size")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqlglot-", dir=destination.parent) as temporary:
        directory = Path(temporary) / "source"
        directory.mkdir(mode=0o755)
        symlinks: list[tuple[Path, str]] = []
        paths: set[tuple[str, ...]] = set()
        for member in contents.infolist():
            archive_name = member.filename.rstrip("/")
            components = PurePosixPath(archive_name).parts
            if (not components or components[0] != root or ".." in components or "\\" in archive_name
                    or "/".join(components) != archive_name or components in paths):
                raise ValueError(f"unexpected SQLGlot archive path {member.filename!r}")
            paths.add(components)
            relative = PurePosixPath(*components[1:])
            target = directory.joinpath(*relative.parts)
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if member.is_dir() and file_type in (0, stat.S_IFDIR):
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
            elif not relative.parts or member.is_dir():
                raise ValueError(f"invalid archive entry {member.filename!r}")
            elif file_type == stat.S_IFLNK:
                link = os.fsdecode(contents.read(member))
                resolved = posixpath.normpath(posixpath.join(relative.parent.as_posix(), link))
                if (not link or posixpath.isabs(link) or "\\" in link or resolved == ".." or resolved.startswith("../")):
                    raise ValueError(f"archive symlink leaves the source tree: {relative}")
                symlinks.append((target, link))
            elif file_type in (0, stat.S_IFREG):
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with contents.open(member) as original, target.open("xb") as output:
                    shutil.copyfileobj(original, output)
                target.chmod(0o755 if mode & stat.S_IXUSR else 0o644)
            else:
                raise ValueError(f"unsupported archive entry {member.filename!r}")
        for target, link in symlinks:
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.symlink_to(link)
        directory.rename(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        extract_archive(args.archive, args.destination)
    except (OSError, ValueError, BadZipFile) as exc:
        parser.exit(1, f"SQLGlot extraction failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
