"""Intersect successful warm/cold observations of individual original methods."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def build(maps, captures):
    base = json.loads(gzip.decompress((maps / "impact.json.gz").read_bytes()))
    roster = hashlib.sha256("\0".join(base["roster"]).encode()).hexdigest()
    result = {}
    for arch in ("x86_64", "aarch64"):
        warm, cold = {}, {}
        observed_warm = observed_cold = False
        for capture in captures:
            if capture["arch"] != arch:
                continue
            if (capture["roster"] != roster or capture["platform"] != "linux"
                    or capture["python"] != [3, 13, 18] or any(
                        base["source_hashes"].get(p) != value for p, value in capture["sources"].items())):
                raise ValueError("different capture environment or original source")
            active = cold if capture["cold"] else warm
            if capture["cold"]:
                if len(capture["selected"]) != 1:
                    raise ValueError("cold capture contained more than one test")
                observed_cold = True
            else:
                if capture["selected"] != base["roster"][:len(capture["selected"])]:
                    raise ValueError("warm capture did not preserve canonical prefix")
                observed_warm = True
            for key, (shape, samples) in capture["data"].items():
                file = key.rsplit("|", 2)[0]
                if file not in capture["sources"]:
                    raise ValueError("unknown observation origin")
                for n, values in samples.items():
                    if base["roster"][int(n)] not in capture["selected"]:
                        raise ValueError("unowned observation")
                    by_test = active.setdefault(key, {})
                    if n in by_test:
                        raise ValueError("duplicate capture")
                    by_test[n] = (shape, values)
        if not (observed_warm and observed_cold):
            continue
        sites = {}
        for key in warm.keys() & cold.keys():
            pairs = {}
            for n in warm[key].keys() & cold[key].keys():
                shape, a = warm[key][n]
                other, b = cold[key][n]
                if shape != other:
                    raise ValueError("site definitions disagree")
                if shape[0] == "boolean":
                    if (a | b) & ~15:
                        raise ValueError("invalid Boolean outcome")
                    pairs[n] = a | b
                else:
                    pairs[n] = sorted(set(a) | set(b))
            if pairs:
                sites[key] = [shape if shape[0] == "integer" else ["boolean"], pairs]
        if sites:
            result[arch] = sites
    if not result:
        raise ValueError("no independently observed warm/cold pair")
    return dict(base=base["base_commit"], roster=roster, architectures=result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("maps", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    data = build(args.maps, [json.loads(gzip.decompress(c.read_bytes())) for c in args.captures])
    encoded = gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0)
    args.output.write_bytes(encoded)
    print(hashlib.sha256(encoded).hexdigest(), {k: len(v) for k, v in data["architectures"].items()})
