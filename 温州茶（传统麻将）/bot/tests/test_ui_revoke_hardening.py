"""UI unbind request must revoke a live runner before losing identity."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import assist_window
from assist_window import TeaAssistWindow


class Var:
    def __init__(self):
        self.value = ""
    def set(self, value):
        self.value = value


def test_unbind_requests_stop_before_clear(monkeypatch):
    win = TeaAssistWindow.__new__(TeaAssistWindow)
    win.full_auto_running = True
    win.continuous = False
    win._binding = {"account": "test"}
    win.status = Var()
    steps = []
    win._stop_full_auto = lambda: steps.append("stop")
    win._set_operational_status = lambda *a, **k: None
    win._refresh_binding_views = lambda: None
    monkeypatch.setattr(assist_window, "clear_binding", lambda: steps.append("clear"))
    win._unbind()
    assert steps == ["stop", "clear"]
    assert win._binding is None
    assert win._check_state == "unchecked"
