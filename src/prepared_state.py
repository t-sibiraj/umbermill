"""Check a qualified warm replay against an independently imported patched namespace."""

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from .state_fingerprint import digest, fingerprint, runtime


IMPACT_DIGEST = "f57c85894a47dac97762308b2be807bbf312206db182d4c33c067c5934ff4514"
ARTIFACT = Path(__file__).resolve().parent.parent / "maps" / "namespace-inventory.json"
WORKER = Path(__file__).with_name("state_worker.py")


def fresh(root, relative):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen([sys.executable, "-B", "-P", "-s", str(WORKER), str(root), relative],
                              stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                              cwd=root, env=env, start_new_session=True) as child:
            try:
                child.wait(timeout=3.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
            output.seek(0)
            blob = output.read(200001)
            if child.returncode or len(blob) > 200000:
                return None
        return json.loads(blob)


@lru_cache(1)
def inventory():
    try:
        data = ARTIFACT.read_bytes()
        if len(data) > 1024 * 1024:
            return None
        envelope = json.loads(data)
        payload = envelope["payload"]
        if (digest(payload) == envelope["digest"] and payload["runtime"] == runtime()
                and payload["impact"] == IMPACT_DIGEST and payload["runtime"][-2] == "0"
                and isinstance(payload["entries"], dict)):
            return payload["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _actual(module, base, overlay):
    # Reading CPython annotations or function dictionaries can materialize lazy
    # state. Keep that observation out of the interpreter that will run tests.
    if not hasattr(os, "fork"):
        return None
    reader, writer = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(reader)
        os.close(writer)
        return None
    if pid == 0:
        try:
            os.close(reader)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(2)
            value = fingerprint(module, (base, overlay))["whole"]
            if value is not None:
                os.write(writer, value.encode("ascii"))
        finally:
            os._exit(0)
    os.close(writer)
    try:
        output = os.read(reader, 65)
    finally:
        os.close(reader)
        _, status = os.waitpid(pid, 0)
    if status == 0 and len(output) == 64:
        return output.decode("ascii")
    return None


def before(modules, sources, base, overlay):
    """Return a check for an inventoried module; None leaves existing activation rules in control."""
    if len(sources) != 1:
        return None
    records = inventory()
    if not records:
        return None
    name, (left, right) = next(iter(sources.items()))
    path = name.replace(".", "/") + ".py"
    row = records.get(path)
    if not row or row["fingerprint"]["whole"] is None:
        return None
    raw = hashlib.sha256(left.encode()).hexdigest()
    if raw != row["source"]:
        return False
    actual = _actual(modules[name], base, overlay)
    if actual != row["fingerprint"]["whole"]:
        return False
    return name, path, hashlib.sha256(right.encode()).hexdigest()


def after(check, modules, base, overlay):
    if check is None:
        return True
    if check is False:
        return False
    name, path, source = check
    try:
        actual = _actual(modules[name], base, overlay)
        probe = fresh(overlay, path) if actual else None
        return bool(probe and probe["runtime"] == runtime() and probe["source"] == source
                    and probe["fingerprint"]["whole"] == actual)
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError):
        return False
