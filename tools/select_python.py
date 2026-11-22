"""Choose a compatible, checksum-pinned standalone CPython for the Linux image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

RELEASE = "20261110"
VERSION = "3.13.18"
ARCHIVES = {
    "x86_64": ("f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb", 35596130),
    "aarch64": ("55c00325f7c1fddf325dfc621aabdfbbac4dc54adda224af88ec411dc12db239", 29683231),
}
ROOT = Path("/opt/umbermill")
BASE = Path(sys.executable).resolve()


def describe():
    import decimal
    import importlib.util
    import math
    import sqlite3
    import struct
    import sysconfig

    connection = sqlite3.connect(":memory:")
    try:
        options = sorted(v for v, in connection.execute("PRAGMA compile_options"))
        values = connection.execute("SELECT sqlite_source_id(), typeof(1.0), hex(?), round(2.675,2)", (b"\x00data",)).fetchone()
    finally:
        connection.close()
    code = compile("def length(x):\n    return len(x) + 1\n", "probe.py", "exec").co_consts[0]
    print(json.dumps({
        "version": list(sys.version_info), "abi": sysconfig.get_config_var("SOABI"),
        "interpreter": sys.implementation.name, "bytecode": importlib.util.MAGIC_NUMBER.hex(),
        "hash_abi": list(sys.hash_info), "long": list(sys.int_info), "float": list(sys.float_info), "word_size": struct.calcsize("P"),
        "decimal": str(decimal.getcontext()), "float_cases": [math.log1p(0.25).hex(), math.sqrt(2).hex()],
        "program": [code.co_code.hex(), code.co_linetable.hex(), code.co_exceptiontable.hex()],
        "sqlite": [sqlite3.sqlite_version, options, values],
    }, sort_keys=True))


def profile(python):
    return subprocess.check_output([python, "-I", "-B", __file__, "--describe"], timeout=8)


def fetch(archive, machine):
    digest, size = ARCHIVES[machine]
    name = f"cpython-{VERSION}%2B{RELEASE}-{machine}-unknown-linux-gnu-install_only_stripped.tar.gz"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}"
    checksum = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=9) as response, archive.open("wb") as output:
        for chunk in iter(lambda: response.read(256 * 1024), b""):
            if output.tell() + len(chunk) > size:
                raise ValueError("standalone archive exceeds the published size")
            checksum.update(chunk)
            output.write(chunk)
    if archive.stat().st_size != size or checksum.hexdigest() != digest:
        raise ValueError("standalone archive differs from publisher digest")


def standalone():
    machine = platform.machine()
    if machine not in ARCHIVES or platform.python_version() != VERSION:
        return BASE
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        staging = Path(directory)
        archive = staging / "interpreter.tar.gz"
        fetch(archive, machine)
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if any(not Path(member.name).is_relative_to("python") for member in members):
                raise ValueError("unexpected standalone archive layout")
            bundle.extractall(staging, members=members, filter="data")
        unpacked = staging / "python"
        candidate = unpacked / "bin/python3.13"
        reference = profile(BASE)
        if profile(candidate) != reference:
            import _sqlite3

            # Isolated mode still loads sitecustomize from this interpreter's site-packages.
            # Only replace its SQLite extension; all other standard-library imports stay local.
            native_sqlite = Path(_sqlite3.__file__).resolve()
            site = unpacked / "lib/python3.13/site-packages"
            (site / "_umbermill_sqlite_path.py").write_text(f"EXTENSION = {str(native_sqlite)!r}\n")
            shutil.copyfile(Path(__file__).with_name("sqlite_site.py"), site / "sitecustomize.py")
            if profile(candidate) != reference:
                raise ValueError("standalone interpreter differs from base image")
        target = ROOT / "python"
        unpacked.rename(target)
    return target / "bin/python3.13"


def install(preference):
    ROOT.mkdir(parents=True, exist_ok=True)
    selected = BASE
    if preference == "auto":
        try:
            selected = standalone()
        except Exception as exc:
            print(f"Using image CPython ({type(exc).__name__}: {exc})", file=sys.stderr)
    cmd = [selected, "-S", "-c", "pass"]
    allocator = subprocess.run(cmd, env={**os.environ, "PYTHONMALLOC": "mimalloc"}, capture_output=True, timeout=8)
    directory = ROOT / "bin"
    directory.mkdir(exist_ok=True)
    wrapper = directory / "python"
    setup = 'export PYTHONMALLOC="${PYTHONMALLOC:-mimalloc}"\n' if allocator.returncode == 0 else ""
    wrapper.write_text(f'#!/bin/sh\n{setup}exec {shlex.quote(str(selected))} "$@"\n')
    wrapper.chmod(0o755)
    print(f"Selected interpreter {selected}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--preference", choices=("auto", "base"), default="auto")
    args = parser.parse_args()
    if args.describe:
        describe()
    else:
        install(args.preference)
