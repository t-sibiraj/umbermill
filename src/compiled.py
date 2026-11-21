"""Reuse trusted build-time code objects for the unmodified baseline program."""
from functools import lru_cache
import hashlib
import marshal
from pathlib import Path
import sys


def fingerprint():
    # Include the actual code-location metadata mode as well as bytecode ABI.
    probe = compile("def f(x):\n    return x + 1\n", "probe", "exec", dont_inherit=True)
    return (sys.implementation.cache_tag, sys.version_info[:3], sys.flags.optimize,
            probe.co_consts[0].co_linetable.hex())


@lru_cache(maxsize=1)
def catalog():
    try:
        from ._compiled_catalog import INTERPRETER, SOURCES
    except ImportError:
        return {}
    return SOURCES if INTERPRETER == fingerprint() else {}


def baseline_code(path, source):
    record = catalog().get(path)
    if record is None:
        return None
    source_hash, artifact_hash = record
    if hashlib.sha256(source).hexdigest() != source_hash:
        return None
    content = Path(__file__).resolve().parent.parent / "maps" / "compiled" / artifact_hash
    if not content.is_file():
        return None
    data = content.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact_hash:
        return None
    code, clean = marshal.loads(data)
    return code, clean
