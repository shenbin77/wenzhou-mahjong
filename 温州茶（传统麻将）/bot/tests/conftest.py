"""Collect destroyed Tk reference cycles on their creating pytest thread.

Only tests that really create a Tk interpreter opt in with real_tk.
This autouse fixture starts before ordinary test fixtures, so its teardown
runs after their finalizers have released references to the destroyed UI.
Native tray startup and message handling remain real in the UI smoke tests.
"""
from __future__ import annotations

import gc
import threading

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_tk: creates a real Tk interpreter on the pytest main thread")


@pytest.fixture(autouse=True)
def _collect_destroyed_tk_on_owner_thread(request):
    if request.node.get_closest_marker("real_tk") is None:
        yield
        return
    owner = threading.get_ident()
    assert threading.current_thread() is threading.main_thread(), (
        "real Tk smoke tests must create and dispose Tk on the pytest main thread")
    yield
    assert threading.get_ident() == owner, "Tk teardown changed threads"
    # destroy() closes the native window but Python widget/callback cycles may
    # still retain its Tcl interpreter. Reclaim them here before another
    # native worker's allocation can trigger GC on the wrong thread.
    gc.collect()
