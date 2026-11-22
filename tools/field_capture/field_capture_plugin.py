"""Attribute setup, call and teardown, including work spawned by the original method."""
from contextlib import contextmanager
import os
import pytest
from field_capture_observer import current


@contextmanager
def scope(name):
    previous = os.environ.get("FIELD_CAPTURE_ITEM")
    token = current.set(name)
    os.environ["FIELD_CAPTURE_ITEM"] = name
    try:
        yield
    finally:
        current.reset(token)
        if previous is None:
            os.environ.pop("FIELD_CAPTURE_ITEM", None)
        else:
            os.environ["FIELD_CAPTURE_ITEM"] = previous


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    with scope(item.nodeid):
        yield
