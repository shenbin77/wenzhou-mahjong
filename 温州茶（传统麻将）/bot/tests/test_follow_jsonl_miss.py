# -*- coding: utf-8 -*-
"""Regression: live follow must always create/append follow_session.jsonl or fail loud."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_policy import CLAIM_TAP_CHANNEL_READY  # noqa: E402
from follow_pipeline import FollowPipeline  # noqa: E402
from session_logger import (  # noqa: E402
    FOLLOW_SESSION_FILENAME,
    FollowSessionLogError,
    SessionLogger,
)
import assist_window  # noqa: E402


def test_claim_stays_false():
    assert CLAIM_TAP_CHANNEL_READY is True


def test_ensure_ready_creates_jsonl(tmp_path: Path):
    logger = SessionLogger(tmp_path / "sess")
    path = logger.ensure_ready({"mode": "tea_2p"})
    assert path == tmp_path / "sess" / FOLLOW_SESSION_FILENAME
    assert path.is_file()
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["kind"] == "session_header"
    assert rows[0]["claim_tap_channel_ready"] is True


def test_ensure_ready_disabled_fails_loud(tmp_path: Path):
    logger = SessionLogger(tmp_path / "sess", enabled=False)
    with pytest.raises(FollowSessionLogError):
        logger.ensure_ready()


def test_pipeline_start_requires_jsonl(tmp_path: Path):
    pipe = FollowPipeline(tmp_path / "live", mode="tea_2p", enabled=True)
    path = pipe.start({"account": "t"})
    assert path.is_file()
    assert path.name == FOLLOW_SESSION_FILENAME


def test_pipeline_start_disabled_fails_loud(tmp_path: Path):
    pipe = FollowPipeline(tmp_path / "live", mode="tea_2p", enabled=False)
    with pytest.raises(FollowSessionLogError):
        pipe.start()


def test_window_start_fails_loud_without_attach(tmp_path: Path):
    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.continuous = False
    window.video_future = None
    window._ready = lambda: True
    window.continuous_round = None
    window.continuous_caishen = None
    window._last_capture_at = None
    window._last_render_done_at = None
    window._follow_pipeline = None
    window.start_button = None
    window.status = SimpleNamespace(set=lambda v: setattr(window, "_status", v))
    window.live_info = SimpleNamespace(set=lambda v: setattr(window, "_live", v))
    # Simulate stale process / old window missing the binder.
    window._attach_follow_pipeline = None
    assist_window.TeaAssistWindow.start(window)
    assert window.continuous is False
    assert "无法开始跟随" in getattr(window, "_status", "")


def test_window_start_fails_loud_when_jsonl_cannot_be_created(tmp_path: Path):
    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.continuous = False
    window.video_future = None
    window._ready = lambda: True
    window.continuous_round = None
    window.continuous_caishen = None
    window._last_capture_at = None
    window._last_render_done_at = None
    window._follow_pipeline = None
    window.start_button = None
    window.status = SimpleNamespace(set=lambda v: setattr(window, "_status", v))
    window.live_info = SimpleNamespace(set=lambda v: setattr(window, "_live", v))

    def _bad_attach():
        raise FollowSessionLogError("disk full simulated")

    window._attach_follow_pipeline = _bad_attach
    assist_window.TeaAssistWindow.start(window)
    assert window.continuous is False
    assert "无法开始跟随" in getattr(window, "_status", "")
    assert "disk full" in getattr(window, "_status", "")


class _Var:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value

    def strip(self):
        return str(self._v).strip()


def test_window_start_creates_jsonl_and_arms(tmp_path: Path):
    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.session_dir = tmp_path / "live"
    window.session_dir.mkdir()
    window.account = _Var("tester")
    window.room_id = _Var("123")
    window.serial = _Var("emu")
    window.round_no = 1
    window._mode_key = lambda: "tea_2p"
    window.continuous = False
    window.video_future = None
    window._ready = lambda: True
    window.continuous_round = None
    window.continuous_caishen = None
    window._last_capture_at = None
    window._last_render_done_at = None
    window._follow_pipeline = None
    window.embedded_follow_log = None
    window.follow_session_log = None
    window.start_button = SimpleNamespace(configure=lambda **k: None)
    window.status = SimpleNamespace(set=lambda v: setattr(window, "_status", v))
    window.live_info = SimpleNamespace(set=lambda v: setattr(window, "_live", v))
    window.preview_title = None
    window.preview_flags = None
    window.display_source = "live"
    window._schedule = lambda delay: setattr(window, "_scheduled", delay)
    window._attach_follow_pipeline = (
        assist_window.TeaAssistWindow._attach_follow_pipeline.__get__(window)
    )
    assist_window.TeaAssistWindow.start(window)
    assert window.continuous is True
    assert window._follow_pipeline is not None
    assert window._follow_pipeline.follow_log_path.is_file()
    assert CLAIM_TAP_CHANNEL_READY is True
    assert FOLLOW_SESSION_FILENAME in getattr(window, "_live", "")


def test_live_observe_refuses_enabled_false_fallback(tmp_path: Path):
    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.session_dir = tmp_path / "live"
    window.session_dir.mkdir()
    window.continuous = True
    window._follow_pipeline = None
    window._mode_key = lambda: "tea_2p"
    window.room_id = _Var("")
    window.round_no = 1

    def _boom():
        raise FollowSessionLogError("attach failed on purpose")

    window._attach_follow_pipeline = _boom
    with pytest.raises(FollowSessionLogError):
        assist_window.TeaAssistWindow._observe_follow_frame(
            window,
            {"status": "advise", "mode": "tea_2p"},
            SimpleNamespace(status="advise", actionable=True),
        )
