# -*- coding: utf-8 -*-
"""Restart recovery at a real between-round screen.

These tests are offline.  They read preserved 1600x900 frames and never call
ADB or operate an emulator.
"""
from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import cv2
import numpy as np

from assist_window import TeaAssistWindow
from round_transition import RoundTransitionExecutor


BOT_DIR = Path(__file__).resolve().parent.parent
TEA_ROOT = BOT_DIR.parent
REPO_ROOT = TEA_ROOT.parent
REAL_SUMMARY = (
    TEA_ROOT / "采集" / "dev_auto_play" /
    "ui_20261025_165932_emulator5558_拾则" / "frames" /
    "20261025_170009_431341_before_5b9e69.png"
)
REAL_TABLE = (
    TEA_ROOT / "采集" / "dev_auto_play" /
    "ui_20261025_165115_emulator5556_玉姐" / "frames" /
    "20261025_165416_996466_before_29c49d.png"
)
REAL_STOPPED_MIDHAND = (
    TEA_ROOT / "采集" / "dev_auto_play" /
    "ui_20261027_101013_emulator5554_玉姐" / "frames" /
    "20261027_103931_593362_after_claim_b6d2f2.png"
)
REAL_DISSOLVED = (
    TEA_ROOT / "采集" / "dev_auto_play" /
    "ui_20261026_170909_emulator5556_烘熊瞎" / "frames" /
    "20261026_181402_326482_before_e49f08.png"
)
REAL_MATCH_END = REPO_ROOT / "_takeover" / "match_end_probe.png"
REAL_STARTUP_PROMO = (
    TEA_ROOT / "采集" / "dev_auto_play" /
    "ui_20261027_204836_emulator5556_烘熊瞎" / "frames" /
    "20261027_204933_930116_before_22792b.png"
)


def _read(path: Path) -> np.ndarray:
    assert path.is_file(), path
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2] == (900, 1600)
    return image


class Sequence:
    def __init__(self, frames):
        self.frames = list(frames)

    def __call__(self):
        assert self.frames, "unexpected extra capture"
        return self.frames.pop(0).copy()


def test_real_promo_at_restart_closes_then_rechecks_summary(monkeypatch, tmp_path):
    import tap_executor
    promo, summary, table = map(_read, (REAL_STARTUP_PROMO, REAL_SUMMARY, REAL_TABLE))
    taps, closes = [], []
    class FakeTapExecutor:
        def __init__(self, **kwargs):
            assert kwargs["serial"] == "emulator-5556"
        def execute_close_popup(self, point, **kwargs):
            assert self.authorization_check()
            closes.append(point)
            return {"executed": True, "verified": True}
    monkeypatch.setattr(tap_executor, "TapExecutor", FakeTapExecutor)
    executor = RoundTransitionExecutor(
        capture=Sequence([promo, summary, summary, summary, table]),
        tap=lambda x, y: taps.append((x, y)), sleep=lambda _s: None,
        report_dir=tmp_path, post_polls=1, post_interval_s=.01)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "advanced"
    assert closes == [(1466, 190)]
    assert len(taps) == 1


def test_startup_promo_cannot_tap_without_authority_or_cancelled(monkeypatch, tmp_path):
    import tap_executor
    promo = _read(REAL_STARTUP_PROMO)
    monkeypatch.setattr(tap_executor, "TapExecutor", lambda **kwargs:
        (_ for _ in ()).throw(AssertionError("no authority means no executor")))
    for token in (None, Event()):
        if token is not None:
            token.set()
        executor = RoundTransitionExecutor(capture=Sequence([promo]),
                                           tap=lambda *args: None,
                                           report_dir=tmp_path)
        executor._serial = "emulator-5556"
        result = TeaAssistWindow._recover_startup_round_transition(executor, token)
        assert result.status in ("startup_blocked", "cancelled")
        assert not result.clicked


def test_startup_promo_unverified_never_attempts_next_round(monkeypatch, tmp_path):
    import tap_executor
    promo = _read(REAL_STARTUP_PROMO)
    class FakeTapExecutor:
        def __init__(self, **kwargs): pass
        def execute_close_popup(self, *args, **kwargs):
            raise tap_executor.TapUnverified("尚未证明消失")
    monkeypatch.setattr(tap_executor, "TapExecutor", FakeTapExecutor)
    taps = []
    executor = RoundTransitionExecutor(capture=Sequence([promo]),
                                       tap=lambda *args: taps.append(args),
                                       report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "startup_blocked"
    assert "尚未证明消失" in result.reason
    assert taps == []


def test_startup_promo_uses_real_executor_with_fresh_frames(monkeypatch, tmp_path):
    import tap_executor
    promo, summary, table = map(_read, (REAL_STARTUP_PROMO, REAL_SUMMARY, REAL_TABLE))
    frames = Sequence([promo, summary, summary])
    commands = []
    monkeypatch.setattr(tap_executor.TapExecutor, "_require_game_foreground", lambda self: None)
    monkeypatch.setattr(tap_executor.TapExecutor, "_screenshot", lambda self: frames())
    monkeypatch.setattr(tap_executor.TapExecutor, "_adb",
                        lambda self, *args, **kwargs: commands.append(args))
    monkeypatch.setattr(tap_executor.time, "sleep", lambda _s: None)
    executor = RoundTransitionExecutor(
        capture=Sequence([promo, summary, summary, summary, table]),
        tap=lambda *args: None, sleep=lambda _s: None,
        report_dir=tmp_path, post_polls=1, post_interval_s=.01)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "advanced"
    assert commands == [("shell", "input", "tap", "1466", "190")]
    evidence = list((tmp_path / "tap_probe").glob("*/evidence.json"))
    assert len(evidence) == 1


def test_stop_during_promo_fresh_check_never_taps(monkeypatch, tmp_path):
    import tap_executor
    promo = _read(REAL_STARTUP_PROMO)
    token, foreground_calls, commands = Event(), [], []
    def foreground(_self):
        foreground_calls.append(True)
        if len(foreground_calls) == 2:
            token.set()
    monkeypatch.setattr(tap_executor.TapExecutor, "_require_game_foreground", foreground)
    monkeypatch.setattr(tap_executor.TapExecutor, "_screenshot", lambda self: promo)
    monkeypatch.setattr(tap_executor.TapExecutor, "_adb",
                        lambda self, *args, **kwargs: commands.append(args))
    executor = RoundTransitionExecutor(capture=Sequence([promo]),
                                       tap=lambda *args: commands.append(args),
                                       report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, token)
    assert result.status == "startup_blocked"
    assert "权限已撤销" in result.reason
    assert commands == []


def test_promo_disappearing_before_fresh_recheck_does_not_tap(monkeypatch, tmp_path):
    import tap_executor
    promo, summary = map(_read, (REAL_STARTUP_PROMO, REAL_SUMMARY))
    commands = []
    monkeypatch.setattr(tap_executor.TapExecutor, "_require_game_foreground", lambda self: None)
    monkeypatch.setattr(tap_executor.TapExecutor, "_screenshot", lambda self: summary)
    monkeypatch.setattr(tap_executor.TapExecutor, "_adb",
                        lambda self, *args, **kwargs: commands.append(args))
    executor = RoundTransitionExecutor(capture=Sequence([promo]),
                                       tap=lambda *args: commands.append(args),
                                       report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "startup_blocked"
    assert commands == []


def test_startup_partial_promo_never_constructs_tap_executor(monkeypatch, tmp_path):
    import promo_popup
    import tap_executor
    monkeypatch.setattr(promo_popup, "read_promo_popup", lambda _frame:
                        promo_popup.PromoPopupState(present=True, close_center=None))
    monkeypatch.setattr(tap_executor, "TapExecutor", lambda **kwargs:
        (_ for _ in ()).throw(AssertionError("incomplete popup must not tap")))
    executor = RoundTransitionExecutor(capture=Sequence([_read(REAL_STARTUP_PROMO)]),
                                       tap=lambda *args: None,
                                       report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "startup_blocked"
    assert "证据不完整" in result.reason


def test_missing_promo_template_never_falls_through_to_other_actions(monkeypatch, tmp_path):
    import promo_popup
    monkeypatch.setattr(promo_popup, "read_promo_popup", lambda _frame:
                        promo_popup.PromoPopupState(False, reason="关闭按钮模板缺失"))
    executor = RoundTransitionExecutor(
        capture=Sequence([_read(REAL_SUMMARY)]),
        tap=lambda *args: (_ for _ in ()).throw(AssertionError("must not tap")),
        report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "startup_blocked"
    assert "模板缺失" in result.reason


def test_startup_promo_dismissal_does_not_bypass_match_end(monkeypatch, tmp_path):
    import tap_executor
    promo, match_end = map(_read, (REAL_STARTUP_PROMO, REAL_MATCH_END))
    class FakeTapExecutor:
        def __init__(self, **kwargs): pass
        def execute_close_popup(self, *args, **kwargs):
            return {"executed": True, "verified": True}
    monkeypatch.setattr(tap_executor, "TapExecutor", FakeTapExecutor)
    taps = []
    executor = RoundTransitionExecutor(capture=Sequence([promo, match_end]),
                                       tap=lambda *args: taps.append(args),
                                       report_dir=tmp_path)
    executor._serial = "emulator-5556"
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == "startup_blocked"
    assert "本场结束页" in result.reason
    assert taps == []


def test_real_old_summary_restarts_through_existing_fresh_gates_once(tmp_path):
    """Preflight is read-only; the existing executor owns the only tap."""
    summary = _read(REAL_SUMMARY)
    table = _read(REAL_TABLE)
    taps = []
    executor = RoundTransitionExecutor(
        capture=Sequence([summary, summary, summary, table]),
        tap=lambda x, y: taps.append((x, y)),
        sleep=lambda _seconds: None,
        report_dir=tmp_path,
        post_polls=1,
        post_interval_s=0.01,
    )

    result = TeaAssistWindow._recover_startup_round_transition(executor)

    assert result.status == "advanced"
    assert result.clicked is True
    assert len(taps) == 1
    assert executor.clicks == 1


def test_real_dissolved_popup_is_blocked_without_lobby_or_tap():
    """The server's timeout/dissolve modal may never reach any click channel."""
    frame = _read(REAL_DISSOLVED)
    taps = []
    executor = RoundTransitionExecutor(
        capture=Sequence([frame]),
        tap=lambda x, y: taps.append((x, y)),
        sleep=lambda _seconds: None,
    )

    result = TeaAssistWindow._recover_startup_round_transition(executor)

    assert result.status == "startup_blocked"
    assert "结算/模态" in result.reason
    assert taps == []
    assert executor.clicks == 0


def test_real_match_end_refuses_return_share_and_continue_buttons():
    frame = _read(REAL_MATCH_END)
    taps = []
    executor = RoundTransitionExecutor(
        capture=Sequence([frame]),
        tap=lambda x, y: taps.append((x, y)),
        sleep=lambda _seconds: None,
    )

    result = TeaAssistWindow._recover_startup_round_transition(executor)

    assert result.status == "startup_blocked"
    assert "返回大厅、分享战绩和末局续局" in result.reason
    assert taps == []


def test_real_table_without_visible_two_player_text_does_not_midhand_reattach():
    frame = _read(REAL_TABLE)
    taps = []
    executor = RoundTransitionExecutor(
        capture=Sequence([frame]),
        tap=lambda x, y: taps.append((x, y)),
        sleep=lambda _seconds: None,
    )

    result = TeaAssistWindow._recover_startup_round_transition(executor)

    assert result.status == "startup_not_settlement"
    assert result.evidence["active_table"]["mode"] is None
    assert taps == []


def test_real_stopped_midhand_can_reattach_without_lobby_clicks():
    frame = _read(REAL_STOPPED_MIDHAND)
    taps = []
    executor = RoundTransitionExecutor(
        capture=Sequence([frame]),
        tap=lambda x, y: taps.append((x, y)),
        sleep=lambda _seconds: None,
    )

    result = TeaAssistWindow._recover_startup_round_transition(executor)

    assert result.status == "startup_in_game"
    assert result.evidence["active_table"]["mode"] == "tea_2p"
    assert result.evidence["active_table"]["caishen"] is not None
    assert result.evidence["active_table"]["concealed_count"] == 11
    assert result.evidence["active_table"]["exposed_box_count"] == 6
    assert result.evidence["active_table"]["physical_hand_count"] == 17
    assert taps == []


def test_startup_in_game_bypasses_lobby_and_reenters_normal_start_gates():
    calls = []
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._round_transition_resume_mode = "full_auto"
    window._transition_follow_pending = False
    window._lobby_in_game_verified = False
    window.full_auto_status = SimpleNamespace(set=lambda value: calls.append(value))
    window.start = lambda: calls.append("start")
    window._begin_lobby_follow = lambda: calls.append("lobby")

    window._on_round_transition_result(SimpleNamespace(
        status="startup_in_game", reason="midhand"))

    assert window._lobby_in_game_verified is True
    assert "start" in calls
    assert "lobby" not in calls


def test_repeated_explicit_start_submits_only_one_recovery(tmp_path):
    class Pending:
        def done(self):
            return False

    class Pool:
        submissions = 0

        def submit(self, fn, *args, **kwargs):
            self.submissions += 1
            self.call = (fn, args, kwargs)
            return Pending()

    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._round_transition_future = None
    window._round_transition_resume_mode = None
    window._transition_follow_pending = False
    window._ensure_round_transition_executor = lambda: object()
    window.auto_executor = Pool()
    window.full_auto_status = SimpleNamespace(set=lambda value: None)
    window.root = SimpleNamespace(after=lambda delay, callback: "timer")

    window._begin_startup_round_transition("follow")
    first = window._round_transition_future
    window._begin_startup_round_transition("follow")

    assert window.auto_executor.submissions == 1
    assert window._round_transition_future is first


def test_live_full_auto_transition_keeps_resume_authority_while_in_flight():
    class Pending:
        def done(self):
            return False

    class Pool:
        def submit(self, fn, *args, **kwargs):
            self.call = (fn, args, kwargs)
            return Pending()

    executor = SimpleNamespace(attempt=lambda **kwargs: None)
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._round_transition_future = None
    window._round_transition_resume_mode = None
    window._transition_follow_pending = False
    window._full_auto_wanted = True
    window._auto_resume_pending = False
    window.round_no = 5
    window._ensure_round_transition_executor = lambda: executor
    window.auto_executor = Pool()
    window.full_auto_status = SimpleNamespace(set=lambda value: None)
    window.root = SimpleNamespace(after=lambda delay, callback: "timer")

    window._begin_round_transition({"stop_gate": "gate_turn"})

    assert window._round_transition_resume_mode == "full_auto"
    assert window._auto_resume_pending is True
    assert window._round_transition_authorized() is True


def test_revoked_follow_discards_a_late_transition_result():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._round_transition_future = SimpleNamespace(
        done=lambda: True,
        result=lambda: SimpleNamespace(
            status="advanced", reason="late", clicked=True),
    )
    window._round_transition_future_mode = "follow"
    window._round_transition_resume_mode = None
    window._transition_follow_pending = False
    window._accepted_round_observing = False
    window._accepted_round_future_generation = None
    window.root = SimpleNamespace(after=lambda delay, callback: "timer")
    window.next_round = lambda: (_ for _ in ()).throw(
        AssertionError("revoked result must not advance bookkeeping"))

    window._poll_round_transition()

    assert window._round_transition_future is None


def test_stop_revokes_running_transition_without_dropping_its_future():
    class RunningFuture:
        def __init__(self):
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1
            return False

    token = Event()
    future = RunningFuture()
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._handoff_to_auto = False
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._auto_start_pending = False
    window._round_transition_cancel_event = token
    window._round_transition_future = future
    window._round_transition_future_mode = "follow"
    window._round_transition_resume_mode = "follow"
    window._transition_follow_pending = True
    window._accepted_round_observe_generation = 0
    window._accepted_round_cancel_event = None
    window._accepted_round_observe_timer = None
    window._accepted_round_observing = False
    window._lobby_active = False
    window._lobby_in_game_verified = True
    window.full_auto_running = False
    window.continuous = False
    window.continuous_round = object()
    window.continuous_caishen = "w1"
    window._follow_pipeline = None
    window.start_button = None
    window.display_source = "live"
    window.live_info = SimpleNamespace(set=lambda value: None)
    window.timer = None
    window._refresh_binding_views = lambda: None
    window._style_start_button = lambda running: None
    window._set_hud_expanded = lambda running: None
    window._apply_preview_layout = lambda running: None

    window.stop()

    assert token.is_set()
    assert future.cancel_calls == 1
    assert window._round_transition_future is future
    assert window._round_transition_resume_mode is None
    assert window._transition_follow_pending is False


def test_follow_only_accepted_waiting_never_gains_autoplay_or_second_tap():
    calls = []
    probes = iter([(False, "transition_table"), (True, "table_dealt")])

    class Var:
        def __init__(self):
            self.value = ""

        def set(self, value):
            self.value = value

    class Root:
        def after(self, delay, callback):
            self.callback = callback
            return "timer"

    class ImmediatePool:
        def submit(self, fn):
            result = fn()
            return SimpleNamespace(done=lambda: True, result=lambda: result)

    class Transition:
        clicks = 1

        @staticmethod
        def _capture():
            calls.append("capture")
            return object()

        @staticmethod
        def _probe(_frame):
            return next(probes)

        @staticmethod
        def _sleep(_seconds):
            return None

    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._round_transition = Transition()
    window._round_transition_future = None
    window._round_transition_resume_mode = "follow"
    window._transition_follow_pending = True
    window._accepted_round_observing = False
    window._accepted_round_observe_timer = None
    window._accepted_round_observe_generation = 0
    window._accepted_round_future_generation = None
    window._accepted_round_cancel_event = None
    window._full_auto_wanted = False
    window._auto_resume_pending = False
    window._quitting = False
    window._lobby_in_game_verified = False
    window.continuous = False
    window.round_no = 7
    window.auto_executor = ImmediatePool()
    window.root = Root()
    window.full_auto_status = Var()
    window._set_operational_status = lambda *args, **kwargs: None
    window.next_round = lambda: calls.append("next_round")
    window.start = lambda: calls.append("start_gates")

    window._on_round_transition_result(SimpleNamespace(
        status="accepted_waiting", reason="本端已接受"))
    window.root.callback()

    assert window._round_transition.clicks == 1
    assert calls.count("next_round") == 1
    assert calls.count("start_gates") == 1
    assert window._full_auto_wanted is False
    assert window._auto_resume_pending is False
    assert window._lobby_in_game_verified is True

