"""Read complete cold-process module observations for the matching pinned roster."""
import base64
import gzip
import hashlib
import json

from . import BASE_COMMIT

MAP_DIGEST = "b929c32d109088d4db98f4ee20217b742636ae70b0b212ff1040335dc3e52e95"
ROSTER_DIGEST = "31a019981e19c3d1b18d929066da7e118fcf3d57696a360f7b9d31ce3a87bc89"


def _load(selector):
    impact, numbers = selector._cold_module_input
    if impact is None or selector.unavailable:
        return {}
    roster = sorted(numbers, key=numbers.get)
    identity = hashlib.sha256("\0".join(roster).encode()).hexdigest()
    if identity != ROSTER_DIGEST or any(numbers[node] != i for i, node in enumerate(roster)):
        return {}
    blob = impact.with_name("module-cold.json.gz").read_bytes()
    if hashlib.sha256(blob).hexdigest() != MAP_DIGEST:
        return {}
    data = json.loads(gzip.decompress(blob))
    if data["base_commit"] != BASE_COMMIT:
        return {}
    rows = {path: int.from_bytes(base64.b64decode(value, validate=True), "little")
            for path, value in data["masks"].items()}
    if any(p not in selector._source_hashes or m.bit_length() > len(roster) for p,m in rows.items()):
        return {}
    return rows


def cold_mask(selector, path):
    """Return an observed cold dependency mask, or None if the evidence is unavailable."""
    if not hasattr(selector, "_verified_cold_modules"):
        try:
            selector._verified_cold_modules = _load(selector)
        except (OSError, ValueError, TypeError, KeyError):
            selector._verified_cold_modules = {}
    return selector._verified_cold_modules.get(path)
