"""Check each selected test against all its qualified optimizer invocations."""

import gzip, hashlib, importlib, json, os, sys
from pathlib import Path
from rule_graph import digest, thaw


def run(source, refs, blobs, qualified):
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise ValueError("different hash seed")
    source = source.resolve()
    sys.path.insert(0, str(source))
    fn_cache = {}
    outputs = {}
    verified = []
    changed = []
    unknown = []
    controls = {}
    used = 0
    for number, cases in refs:
        status = verified
        for rule, sha, expected in cases:
            k = rule + " " + sha
            if qualified is not None and expected not in qualified.get(k, []):
                status = unknown
                break
            if k not in outputs:
                try:
                    fn = fn_cache.get(rule)
                    if fn is None:
                        module, name = rule.rsplit(".", 1)
                        mod = importlib.import_module(module)
                        if not Path(mod.__file__).resolve().is_relative_to(source):
                            raise ValueError("wrong checkout")
                        fn = fn_cache[rule] = getattr(mod, name)
                    raw = gzip.decompress((blobs / (sha + ".gz")).read_bytes())
                    if hashlib.sha256(raw).hexdigest() != sha:
                        raise ValueError("snapshot hash")
                    roots, initial = thaw(json.loads(raw))
                    result = fn(roots[0])
                    outputs[k] = digest(roots[0], result, initial)
                    used += len(raw)
                except Exception:
                    outputs[k] = None
            got = outputs[k]
            if got != expected:
                status = unknown if got is None else changed
                if qualified is not None:
                    break
            elif qualified is None:
                controls.setdefault(k, set()).add(expected)
        status.append(number)
    return dict(
        verified=verified,
        changed=changed,
        unknown=unknown,
        replayed=len(outputs),
        raw_bytes=used,
        controls={k: sorted(v) for k, v in controls.items()},
    )
