# -*- coding: utf-8 -*-
"""Session flag + auto-review wiring for the assist window (Line B).

Product contract: no per-hand correction form on the main window.  The
user may flag the current round with one click; finding the problem is
the offline auto review's job, triggered when a session ends.
"""

from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import auto_review
import assist_window
from assist_window import (
    TeaAssistWindow,
    append_user_flag,
    format_review_summary,
)


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
    def __init__(self):
        self.calls = []

    def after(self, delay, callback, *args):
        self.calls.append((delay, callback, args))
        return f"timer-{len(self.calls)}"

    def after_cancel(self, timer):
        pass


class DoneFuture:
    def done(self):
        return True

    def result(self):
        return {"actions": 7, "stop_reason": "用户在辅助器中关闭全自动代打"}


def _window(tmp_path: Path):
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._mode_key = lambda: "tea_2p"
    window.account = Var("玉姐")
    window.serial = Var("emulator-5556")
    window.room_id = Var("248061")
    window.round_no = 3
    window.status = Var()
    window.live_info = Var()
    window.phase_status = Var("阶段：已停手")
    window.session_dir = tmp_path / "session"
    window.continuous = False
    window.full_auto_running = False
    window._follow_pipeline = None
    window._suppress_review = False
    window._review_thread = None
    window._review_target = None
    window._review_events = Queue()
    window._review_poll_timer = None
    window.root = Root()
    window.ops = []

    def _ops(phase, problem, *, level="wait"):
        window.ops.append((phase, problem, level))

    window._set_operational_status = _ops
    return window


# -- append_user_flag ----------------------------------------------------

def test_append_user_flag_appends_json_lines(tmp_path):
    event = {"ts": "2026-10-25T10:00:00+08:00",
             "serial": "emulator-5556", "source": "assist_window"}
    path = append_user_flag(tmp_path / "s1", event)
    append_user_flag(tmp_path / "s1", event)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["source"] == "assist_window"


# -- mark_round_issue ----------------------------------------------------

def test_mark_round_issue_during_follow_writes_flag(tmp_path):
    window = _window(tmp_path)
    window.continuous = True

    window.mark_round_issue()

    target = window.session_dir / "user_flags.jsonl"
    assert target.is_file()
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["source"] == "assist_window"
    assert event["serial"] == "emulator-5556"
    assert event["account"] == "玉姐"
    assert event["room_id"] == "248061"
    assert event["round_no"] == 3
    assert event["mode"] == "tea_2p"
    assert event["ts"]
    assert "已标记本局有问题" in window.status.get()


def test_mark_round_issue_lands_next_to_follow_log(tmp_path):
    window = _window(tmp_path)
    window.continuous = True
    log_dir = tmp_path / "follow_session"
    window._follow_pipeline = SimpleNamespace(
        follow_log_path=log_dir / "follow_session.jsonl")

    window.mark_round_issue()

    assert (log_dir / "user_flags.jsonl").is_file()


def test_mark_round_issue_during_full_auto_uses_auto_dir(tmp_path):
    window = _window(tmp_path)
    window.full_auto_running = True
    auto_dir = tmp_path / "auto_session"
    window.session_dir = auto_dir

    window.mark_round_issue()

    assert (auto_dir / "user_flags.jsonl").is_file()


def test_mark_round_issue_without_session_writes_nothing(tmp_path):
    window = _window(tmp_path)

    window.mark_round_issue()

    assert window.status.get() == "当前无进行中的会话"
    assert not list(tmp_path.rglob("user_flags.jsonl"))


def test_mark_round_issue_write_failure_is_reported(tmp_path):
    window = _window(tmp_path)
    window.continuous = True
    blocker = window.session_dir / "user_flags.jsonl"
    blocker.mkdir(parents=True)  # a directory: appending must fail loudly

    window.mark_round_issue()

    assert "标记本局失败" in window.status.get()


# -- correction surface is gone ------------------------------------------

def test_correction_surface_removed():
    source = Path(assist_window.__file__).read_text(encoding="utf-8")
    for banned in ("建议不对", "建议可用", "识牌错误", "实际出牌", "复盘纠错",
                   "append_feedback", "feedback.jsonl", "actual_choice",
                   "actual_tile", "def feedback", "训练"):
        assert banned not in source, banned
    assert "标记本局有问题" in source
    assert not hasattr(assist_window, "append_feedback")
    assert not hasattr(TeaAssistWindow, "feedback")
    assert hasattr(TeaAssistWindow, "mark_round_issue")


# -- format_review_summary -------------------------------------------------

def test_format_review_summary_variants():
    text, level = format_review_summary(
        {"issue_count": 0, "by_category": {}, "report_md": "r.md"})
    assert text == "自动复盘：未发现疑似问题 · 报告：r.md"
    assert level == "ok"

    text, level = format_review_summary(
        {"issue_count": 5,
         "by_category": {"b": 1, "a": 3, "c": 1, "d": 9},
         "report_md": ""})
    assert text == "自动复盘：疑似问题 5 个（d×9、a×3、b×1）"
    assert level == "wait"


def test_auto_review_placeholder_branch_is_removed():
    source = Path(assist_window.__file__).read_text(encoding="utf-8")
    assert "复盘核心尚未就绪（占位）" not in source
    assert 'result.get("placeholder")' not in source


# -- session-end review wiring --------------------------------------------

def test_review_runs_in_background_and_applies_summary(monkeypatch, tmp_path):
    seen = {}

    def fake_review(session_dir):
        seen["dir"] = Path(session_dir)
        return {"issue_count": 3,
                "by_category": {"claim验证停手": 1, "财神未知": 2},
                "report_md": str(tmp_path / "review.md")}

    monkeypatch.setattr(auto_review, "review_session", fake_review)
    window = _window(tmp_path)
    session = tmp_path / "auto_session"

    window._schedule_session_review(session)
    assert window._review_thread is not None
    window._review_thread.join(timeout=10)
    assert seen["dir"] == session

    window._poll_review()
    assert window.ops, "review result was not applied"
    phase, problem, level = window.ops[-1]
    assert problem.startswith("自动复盘：疑似问题 3 个")
    assert "财神未知×2" in problem
    assert "报告：" in problem
    assert level == "wait"
    assert phase == "已停手"


def test_review_zero_issues_shows_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        auto_review, "review_session",
        lambda session_dir: {"issue_count": 0, "by_category": {},
                             "report_md": "r.md"})
    window = _window(tmp_path)

    window._schedule_session_review(tmp_path / "s")
    window._review_thread.join(timeout=10)
    window._poll_review()

    _phase, problem, level = window.ops[-1]
    assert problem.startswith("自动复盘：未发现疑似问题")
    assert level == "ok"


def test_review_failure_is_surfaced_not_swallowed(monkeypatch, tmp_path):
    def boom(session_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(auto_review, "review_session", boom)
    window = _window(tmp_path)

    window._schedule_session_review(tmp_path / "s")
    window._review_thread.join(timeout=10)
    window._poll_review()

    _phase, problem, level = window.ops[-1]
    assert "自动复盘失败" in problem
    assert "boom" in problem
    assert level == "error"


def test_follow_stop_schedules_review_for_follow_dir(tmp_path):
    window = _window(tmp_path)
    window.continuous = True
    window.display_source = "offline_preflight"
    window.start_button = Button()
    window.timer = None
    calls = []
    window._schedule_session_review = lambda d: calls.append(Path(d))

    window.stop()

    assert window.continuous is False
    assert calls == [Path(window.session_dir)]


def test_handoff_suppresses_follow_review(tmp_path):
    window = _window(tmp_path)
    window.continuous = True
    window.display_source = "offline_preflight"
    window.start_button = Button()
    window.timer = None
    window._suppress_review = True  # as _start_full_auto sets before stop()

    window.stop()

    assert window._suppress_review is False  # consumed exactly once
    assert window._review_thread is None


def test_auto_loop_end_schedules_review(tmp_path):
    window = _window(tmp_path)
    window.full_auto_running = True
    window._auto_loop = object()
    window._auto_future = DoneFuture()
    window._auto_events = Queue()
    window._auto_poll_timer = None
    window.start_button = Button()
    window.auto_button = Button()
    window.full_auto_status = Var()
    auto_dir = tmp_path / "auto_session"
    window.session_dir = auto_dir
    calls = []
    window._schedule_session_review = lambda d: calls.append(Path(d))

    window._poll_full_auto()

    assert window.full_auto_running is False
    assert calls == [auto_dir]
