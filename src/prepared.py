"""Load the build's verified, decoded baseline index on the matching interpreter."""

import hashlib
import marshal
import sys
from pathlib import Path

# All entries are inert builtins. Runtime source lookup and incoming roster remain separate.
INDEX_FIELDS = (
    "_unmapped",
    "_files",
    "_collection",
    "_reads",
    "_filtered_fixture_reads",
    "_paired_fixture_reads",
    "_sites",
    "_collection_sites",
    "_source_hashes",
    "_relation_tokens",
    "_root_documents",
    "_length_sites",
    "_relation_sites",
    "_position_readers",
    "_test_sources",
    "_deferred",
)


def install(target, maps, nodeids):
    try:
        from ._prepared_identity import SOURCE_DIGEST, INDEX_DIGEST, INTERPRETER
    except ImportError:
        return False
    if INTERPRETER != (sys.implementation.cache_tag, sys.version_info[:3]):
        return False
    file = maps.with_name("impact.decoded")
    if not file.is_file() or hashlib.sha256(maps.read_bytes()).hexdigest() != SOURCE_DIGEST:
        return False
    data = file.read_bytes()
    if hashlib.sha256(data).hexdigest() != INDEX_DIGEST:
        return False
    roster, fields = marshal.loads(data)
    if sorted(nodeids, key=nodeids.get) != roster or sorted(nodeids.values()) != list(range(len(roster))):
        raise ValueError("runtime roster differs from the build")
    if len(fields) != len(INDEX_FIELDS):
        return False
    for key, value in zip(INDEX_FIELDS, fields):
        setattr(target, key, value)
    return True
