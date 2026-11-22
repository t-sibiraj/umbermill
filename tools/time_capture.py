"""Optional pristine CPython 3.13 pytest plug-in for original format_time operands.

Set UMBERMILL_TIME_CENSUS to an output path and load this module with pytest -p.
Only parent-process inputs are attested. The selector retains independent
full-suite and child coverage when packaging these observations.
"""

import contextvars
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import pytest


context = contextvars.ContextVar("time_test", default="!collection")
calls = {}
mappings = {}
unknown = {}
parent = os.getpid()
tool = 4


def entered(code, offset):
    if code.co_name != "format_time" or not code.co_filename.endswith("/sqlglot/time.py"):
        return sys.monitoring.DISABLE
    if os.getpid() != parent:
        return
    name = context.get()
    if threading.current_thread() is not threading.main_thread():
        unknown.setdefault(name, set()).add("unattributed thread")
        return
    operands = sys._getframe(1).f_locals
    mapping, value = operands["mapping"], operands["string"]
    if type(mapping) is not dict or type(value) is not str or not all(
        type(k) is str and type(v) is str for k, v in mapping.items()
    ):
        unknown.setdefault(name, set()).add("non-native converter input")
        return
    from sqlglot.trie import new_trie

    trie = operands["trie"]
    if trie is not None and trie != new_trie(mapping):
        unknown.setdefault(name, set()).add("nonstandard conversion trie")
        return
    pairs = tuple(mapping.items())
    item = hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:20]
    if mappings.setdefault(item, pairs) != pairs:
        raise ValueError("ordered mapping fingerprint collision")
    calls.setdefault(name, {}).setdefault(item, set()).add(value)


def pytest_configure():
    sys.monitoring.use_tool_id(tool, "time operands")
    event = sys.monitoring.events.PY_START
    sys.monitoring.register_callback(tool, event, entered)
    sys.monitoring.set_events(tool, event)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    token = context.set(item.nodeid)
    try:
        return (yield)
    finally:
        context.reset(token)


def pytest_sessionfinish(session, exitstatus):
    sys.monitoring.set_events(tool, 0)
    sys.monitoring.free_tool_id(tool)
    if exitstatus or os.getpid() != parent:
        return
    result = {
        "scope": "original parent interpreter",
        "mapping_items": mappings,
        "calls": {n: {k: sorted(v) for k,v in row.items()} for n,row in calls.items()},
        "unknown": {n: sorted(v) for n,v in unknown.items()},
    }
    Path(os.environ["UMBERMILL_TIME_CENSUS"]).write_text(json.dumps(result, sort_keys=True) + "\n")
