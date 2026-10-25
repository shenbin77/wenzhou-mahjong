# -*- coding: utf-8 -*-
"""UI hand-off contract: recommendation first, explicit full auto second."""

from __future__ import annotations

from pathlib import Path
from queue import Queue

import action_policy
import auto_play_loop
from assist_window import TeaAssistWindow


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Button:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


class Root:
    def after(self, delay, callback):
        self.delay = delay
        self.callback = callback
        return "timer"


class PendingFuture:
    def done(self):
        return False


class Executor:
    def submit(self, fn):
        self.fn = fn
        return PendingFuture()


def _window(tmp_path: Path):
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._mode_key = lambda: "tea_2p"
    window.account = Var("玉姐")
    window.serial = Var("emulator-5556")
    window.full_auto_status = Var()
    window.status = Var()
    window.live_info = Var()
    window.start_button = Button()
    window.auto_button = Button()
    window._auto_events = Queue()
    window.auto_executor = Executor()
    window.root = Root()
    window.session_dir = tmp_path / "assist"
    window.embedded_follow_log = None
    window.follow_session_log = None
    window.full_auto_running = False
    window._auto_loop = None
    window._auto_future = None
    window._auto_poll_timer = None
    window._ready = lambda: True
    window._identity_gate_ok = lambda **_kwargs: True
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window._set_operational_status = lambda *args, **kwargs: None
    window.stop = lambda: setattr(window, "continuous", False)
    return window


def test_full_auto_refuses_handoff_before_recommendation(tmp_path):
    window = _window(tmp_path)
    window.continuous = False

    window._start_full_auto()

    assert window.full_auto_running is False
    assert "先点“开始跟随”" in window.full_auto_status.get()


def test_full_auto_handoff_enables_guarded_loop(monkeypatch, tmp_path):
    created = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"

        def run(self):
            return {"actions": 0, "stopped": True, "stop_reason": "test"}

        def request_stop(self, reason):
            self.stop_reason = reason

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs),
    )
    window = _window(tmp_path)
    window.continuous = True

    window._start_full_auto()

    assert window.full_auto_running is True
    assert created["enabled"] is True
    assert created["claim_auto_click"] is True
    assert created["claim_policy"][1]["include_chi"] is True
    assert window.start_button.config["text"] == "停止全部"
    assert window.auto_button.config["text"] == "关闭全自动（恢复只读推荐）"
    assert window.root.delay == 150
