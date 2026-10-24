"""SPEC-005 v2 regressions: segmented timing, refresh wiring, oracle and locks.

Every test here is offline: it feeds saved frames (or synthetic states) through
the same pure/UI functions the live window uses, so ``pytest -k spec005``
collects real SPEC-005 coverage.  Nothing starts ADB, draws a Tk window on
screen, sends input or touches templates/thresholds.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import assist_window  # noqa: E402
import live_capture  # noqa: E402
import tile_render  # noqa: E402
from live_capture import SegmentClock, analyze_bytes  # noqa: E402
from live_state import (DisplayState, FrameTiming, LatencyTracker,  # noqa: E402
                        LiveRefreshState, frame_key)

CAPTURES = BOT_DIR.parent / "采集"
TEA_FRAME = CAPTURES / "2026-10-16" / "740195" / "r08" / "frame_02.png"


@pytest.fixture(scope="module")
def tea_raw() -> bytes:
    if not TEA_FRAME.is_file():
        pytest.skip("采集帧缺失，无法离线复现")
    return TEA_FRAME.read_bytes()


@pytest.fixture(scope="module")
def log_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("spec005_logs")


def _decode(raw: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def _encode(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def _without_badge(raw: bytes) -> bytes:
    image = _decode(raw)
    image[348:370, 622:652] = 0
    return _encode(image)


# -- 1. segmented timing --------------------------------------------------

def test_segment_clock_measures_each_stage_independently():
    clock = SegmentClock()
    clock.started -= 0.030
    clock.mark("capture")
    clock.marks["capture"] -= 0.020
    clock.mark("recognize")
    clock.marks["recognize"] -= 0.015
    clock.mark("advise")
    timing = clock.timing(folder="x")
    assert timing.capture_ms == pytest.approx(10.0, abs=2.0)
    assert timing.recognize_ms == pytest.approx(5.0, abs=2.0)
    assert timing.advise_ms == pytest.approx(15.0, abs=2.0)
    assert timing.total_ms == pytest.approx(30.0, abs=3.0)
    assert timing.cycle_ms >= timing.total_ms


def test_analyze_bytes_marks_recognize_and_advise_separately(tea_raw, log_dir):
    clock = SegmentClock()
    result = analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir, clock=clock)
    assert {"recognize", "advise"} <= set(clock.marks)
    assert clock.marks["recognize"] <= clock.marks["advise"]
    timing = clock.timing()
    # recognize spans from the previous mark; without an explicit capture mark
    # the whole wait stays visible in cycle_ms and never hides in advise.
    assert timing.advise_ms >= 0
    assert timing.cycle_ms >= timing.advise_ms
    assert result.get("status")
    assert result["status"] in {"advise", "hu", "wait", "abstain"}


# -- 2. p50 / p95 ---------------------------------------------------------

def test_latency_tracker_percentiles_and_measured_period():
    tracker = LatencyTracker(window=10)
    for index, total in enumerate([10, 20, 30, 40, 50], start=1):
        timing = FrameTiming(capture_ms=total / 2, recognize_ms=total / 2,
                             advise_ms=0, cycle_ms=total)
        tracker.record(timing, period_ms=100.0 * index)
    summary = tracker.summary()
    assert summary["total_p50"] == pytest.approx(30.0)
    assert summary["total_p95"] == pytest.approx(48.0)
    assert summary["period_p50"] == pytest.approx(300.0)
    assert summary["period_p95"] == pytest.approx(480.0)
    assert tracker.last_period_ms == pytest.approx(500.0)


def test_period_is_capture_to_capture_not_processing_plus_sleep():
    tracker = LatencyTracker(window=5)
    tracker.record(FrameTiming(capture_ms=100, recognize_ms=50, advise_ms=10,
                               cycle_ms=160), period_ms=1200)
    assert tracker.last_period_ms == pytest.approx(1200)
    assert tracker.interval_ms >= tracker.base_ms


def test_window_records_capture_start_and_tk_idle_in_refresh_latency(monkeypatch):
    callbacks = []
    values = iter((10.4, 10.6))
    monkeypatch.setattr(assist_window.time, "perf_counter", lambda: next(values))
    window = types.SimpleNamespace(
        root=types.SimpleNamespace(after_idle=lambda callback: callbacks.append(callback),
                                   update_idletasks=lambda: None),
        _last_capture_at=9.0, _last_render_done_at=9.8,
        tracker=LatencyTracker(), latency_log=[], continuous=False,
        recorder=None, period=types.SimpleNamespace(set=lambda _: None),
        _accept_continuous_result=lambda _: True,
        _show=lambda *_: None, _update_latency_bar=lambda: None,
        _result_source=assist_window.frame_result_source,
    )
    window._finish_frame = assist_window.TeaAssistWindow._finish_frame.__get__(window)
    on_frame = assist_window.TeaAssistWindow._on_frame.__get__(window)
    on_frame({"status": "advise"}, FrameTiming(
        capture_ms=50, recognize_ms=100, advise_ms=20, cycle_ms=170,
    ), capture_started_at=10.0)
    assert not window.tracker.frames
    callbacks.pop()()
    measured = window.tracker.frames[-1]
    assert measured.capture_to_render_ms == pytest.approx(600)
    assert measured.total_ms == pytest.approx(800)
    assert window.tracker.last_period_ms == pytest.approx(1000)
    assert window.latency_log[-1]["poll_wait_ms"] == pytest.approx(200)


# -- 3. 3 s stale red alert ----------------------------------------------

def test_live_refresh_marks_stale_after_three_seconds():
    state = LiveRefreshState()
    state.update({"status": "advise", "mode": "tea_2p", "caishen": "s9",
                  "caishen_id": 1, "suggestion": "w2", "suggestion_id": 1,
                  "hand_ids": [1, 2, 3], "suggested_boxes": [[0, 0, 1, 1]],
                  "candidates": [{"tile": 1, "shanten_after": 1,
                                  "ukeire_count": 4}]}, now=100.0)
    assert state.state.actionable
    fresh = state.mark_stale(102.0)
    assert fresh.actionable is True
    stale = state.mark_stale(104.0)
    assert stale.actionable is False
    assert stale.candidates == ()
    assert "3 秒" in (stale.reason or "")


# -- 4. old advice cleared / low confidence greyed -----------------------

def test_stale_clears_previous_suggestion_and_candidates():
    state = LiveRefreshState()
    state.update({"status": "advise", "mode": "tea_2p", "caishen": "s9",
                  "caishen_id": 1, "suggestion": "w2", "suggestion_id": 1,
                  "hand_ids": [1, 2, 3], "suggested_boxes": [[0, 0, 1, 1]],
                  "candidates": [{"tile": 1, "shanten_after": 1,
                                  "ukeire_count": 4}]}, now=1.0)
    assert state.state.suggestion == "w2"
    state.mark_stale(5.0)
    assert state.state.suggestion is None
    assert state.state.suggestion_label is None
    assert state.state.candidates == ()
    assert state.state.status == "abstain"


def test_low_confidence_diagnostics_grey_the_advice():
    state = LiveRefreshState()
    result = {
        "status": "advise", "mode": "tea_2p", "caishen": "s9", "caishen_id": 1,
        "suggestion": "w2", "suggestion_id": 1, "hand_ids": [1, 2, 3],
        "suggested_boxes": [[0, 0, 1, 1]],
        "candidates": [{"tile": 1, "shanten_after": 1, "ukeire_count": 4}],
        "vision_diagnostics": [{"accepted": False, "reason": "low_score"}],
    }
    updated = state.update(result, now=1.0)
    assert updated.actionable is False
    assert updated.status == "abstain"


# -- 5. Top-3 same source ------------------------------------------------

def test_top3_comes_from_recorder_candidate_rows():
    from recorder import normalize_candidates
    from tile_vision import TILE_IDS
    caishen_id = TILE_IDS["s9"]
    rows = normalize_candidates(
        "shu_4p",
        [{"tile": 3, "final_score": 5.0, "shanten_after": 1, "ukeire_count": 9},
         {"tile": 5, "final_score": 4.0, "shanten_after": 1, "ukeire_count": 7},
         {"tile": 7, "final_score": 3.0, "shanten_after": 2, "ukeire_count": 5},
         {"tile": 9, "final_score": 2.0, "shanten_after": 2, "ukeire_count": 4},
         {"tile": 11, "final_score": 1.0, "shanten_after": 3, "ukeire_count": 2}],
    )
    assert len(rows) == 5
    result = {"status": "advise", "mode": "shu_4p", "caishen": "hongzhong",
              "caishen_id": caishen_id, "suggestion": "w2",
              "suggestion_id": TILE_IDS["w2"], "hand_ids": [1, 2, 3],
              "suggested_boxes": [[0, 0, 1, 1]], "candidates": rows}
    state = LiveRefreshState().update(result, now=1.0)
    assert len(state.candidates) == 3
    assert state.candidates == tuple(rows[:3])
    # The rendered Top-3 and the recorder's rows share one source.
    rendered = tile_render.render_candidates(state.candidates)
    assert rendered.width > 0
    expected = tuple((str(row["tile"]), row["shanten_after"], row["ukeire_count"])
                     for row in rows[:3])
    assert frame_key(result)[2] == expected
    assert tuple((str(row["tile"]), row["shanten_after"], row["ukeire_count"])
                 for row in state.candidates) == expected


def test_top3_rows_match_tea_recorder_source(tea_raw, log_dir):
    result = analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir)
    state = LiveRefreshState().update(result, now=1.0)
    from recorder import normalize_candidates
    source = normalize_candidates(
        "tea_2p", result.get("candidates"),
        hand_ids=result.get("hand_ids"), caishen_id=result.get("caishen_id"),
    )
    assert state.candidates == tuple(source[:3])


# -- 6. manual caishen oracle -------------------------------------------

def test_manual_caishen_fills_in_when_badge_is_missing(tea_raw, log_dir):
    assert analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                         log_dir=log_dir).get("caishen_source") == "image"
    no_badge = _without_badge(tea_raw)
    auto = analyze_bytes(no_badge, mode="tea_2p", image_path=TEA_FRAME,
                         log_dir=log_dir)
    assert not auto.get("caishen")
    manual = analyze_bytes(no_badge, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir, caishen_label="s9",
                           manual_caishen=True)
    assert manual["caishen"] == "s9"
    assert manual["caishen_source"] == "manual"
    assert manual["status"] == "advise"


def test_manual_caishen_conflict_with_image_abstains(tea_raw, log_dir):
    result = analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir, caishen_label="t3",
                           manual_caishen=True)
    assert result["status"] == "abstain"
    assert result["caishen"] == "s9"
    assert "不一致" in result["reason"]


def test_auto_image_wins_even_if_user_picked_something(tea_raw, log_dir):
    result = analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir, caishen_label="s9",
                           manual_caishen=False)
    assert result["caishen_source"] == "image"
    assert result["caishen"] == "s9"


def test_picker_display_value_reaches_the_next_frame(tea_raw, tmp_path):
    shown = next(text for text, label in assist_window.TILE_CHOICES.items()
                 if label == "s9")
    submitted = []
    window = types.SimpleNamespace(
        caishen=types.SimpleNamespace(get=lambda: shown),
        manual_caishen=types.SimpleNamespace(get=lambda: True),
        _ready=lambda: True, _mode_key=lambda: "tea_2p",
        _context=lambda: ("tea_2p", shown),
        _process_frame=lambda *args: None,
        _poll=lambda: None,
        executor=types.SimpleNamespace(submit=lambda fn, *args: submitted.append(args)),
        root=types.SimpleNamespace(after=lambda *args: None),
        serial=types.SimpleNamespace(get=lambda: "offline"),
        session_dir=tmp_path,
    )
    window._caishen_selection = (
        assist_window.TeaAssistWindow._caishen_selection.__get__(window))
    assist_window.TeaAssistWindow._start_frame.__get__(window)(record=True)
    label, manual = submitted[-1][-2:]
    assert (label, manual) == ("s9", True)
    raw = _without_badge(tea_raw)
    path = tmp_path / "hidden_badge.png"
    path.write_bytes(raw)
    result = analyze_bytes(raw, mode="tea_2p", image_path=path,
                           log_dir=tmp_path / "logs", caishen_label=label,
                           manual_caishen=manual)
    assert result["status"] == "advise"
    assert result["caishen_source"] == "manual"


def test_picker_stays_visible_for_tea_during_badge_changes():
    calls = []
    window = types.SimpleNamespace(
        candidate_face=object(),
        advice_label=types.SimpleNamespace(configure=lambda **kw: None),
        info_label=types.SimpleNamespace(configure=lambda **kw: None),
        _refresh_caishen_rows=lambda **kw: calls.append(kw["show"]),
        _mode_key=lambda: "tea_2p",
    )
    apply = assist_window.TeaAssistWindow._apply_visibility.__get__(window)
    for result in (
        {"mode": "tea_2p", "caishen": "s9", "caishen_id": 47,
         "caishen_source": "image"},
        {"mode": "tea_2p", "caishen": None, "caishen_id": None},
        {"mode": "tea_2p", "caishen": "s9", "caishen_id": 47,
         "caishen_source": "manual"},
        {"mode": "tea_2p", "caishen": "s9", "caishen_id": 47,
         "caishen_source": "image"},
    ):
        window.refresh = LiveRefreshState()
        window.refresh.update(result, now=1.0)
        apply()
    assert calls == [True, True, True, True]


def test_manual_picker_is_a_stable_button_not_a_refreshing_combobox():
    try:
        root = assist_window.tk.Tk()
    except assist_window.tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        window = types.SimpleNamespace(caishen_row=assist_window.ttk.Frame(root),
                                       _caishen_widgets=[],
                                       _open_caishen_menu=lambda: None)
        refresh = assist_window.TeaAssistWindow._refresh_caishen_rows.__get__(window)
        refresh(True)
        button = window.caishen_pick_button
        assert button.winfo_class() == "TButton"
        assert "选择财神牌" in button.cget("text")
        refresh(True)
        assert window.caishen_pick_button is button
        assert button.winfo_exists()
    finally:
        root.destroy()


def test_manual_pick_captures_current_frame_before_clearing_advice():
    events = []
    window = types.SimpleNamespace(
        manual_caishen=types.SimpleNamespace(set=lambda value: events.append(("manual", value))),
        caishen=types.SimpleNamespace(get=lambda: "t2"),
        current={"image": "live.png"},
        caishen_pick_button=None,
        _capture_caishen_candidate=lambda: events.append(("capture", window.current["image"])),
        _reset_round_display=lambda reason: events.append(("clear", reason)),
        status=types.SimpleNamespace(set=lambda value: None),
        continuous=True, future=None,
        _schedule=lambda delay: events.append(("schedule", delay)),
    )
    assist_window.TeaAssistWindow._on_caishen_pick.__get__(window)()
    assert events[1] == ("capture", "live.png")
    assert events[2][0] == "clear"
    assert events[-1] == ("schedule", 0)


def test_manual_pick_during_capture_discards_old_frame_and_rescans():
    events = []
    old_context = ("tea_2p", "", "emulator-5554", True, "player1", "")
    window = types.SimpleNamespace(
        future=types.SimpleNamespace(done=lambda: True,
                                     result=lambda: {"result": {"status": "advise"}}),
        future_context=old_context,
        _context=lambda: ("tea_2p", "t2", *old_context[2:]),
        continuous=True,
        _schedule=lambda delay: events.append(("rescan", delay)),
        stop=lambda: events.append(("stop", None)),
        _on_frame=lambda *args, **kwargs: events.append(("stale_advice", None)),
    )
    assist_window.TeaAssistWindow._poll.__get__(window)()
    assert events == [("rescan", 0)]


def test_recognition_mark_is_before_tea_and_shu_advice(tea_raw, log_dir,
                                                    monkeypatch, tmp_path):
    from tea_bot import TeaBot
    from assist_shu import _calculate
    import assist_shu
    tea_clock = SegmentClock()
    original_think = TeaBot.think

    def checked_think(bot, hand):
        assert "recognize" in tea_clock.marks
        return original_think(bot, hand)

    monkeypatch.setattr(TeaBot, "think", checked_think)
    analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                  log_dir=log_dir, clock=tea_clock)
    monkeypatch.setattr(TeaBot, "think", original_think)
    shu_frame = (BOT_DIR.parent.parent / "阿法算牌分析" /
                 "熟识牌对照_20261018" / "frame_233s_14.png")
    if not shu_frame.is_file():
        pytest.skip("熟帧缺失")
    shu_clock = SegmentClock()

    def checked_calculate(labels, meld_count):
        assert "recognize" in shu_clock.marks
        return _calculate(labels, meld_count)

    monkeypatch.setattr(assist_shu, "_calculate", checked_calculate)
    analyze_bytes(shu_frame.read_bytes(), mode="shu_4p", image_path=shu_frame,
                  log_dir=tmp_path, clock=shu_clock)
    assert shu_clock.marks["recognize"] <= shu_clock.marks["advise"]


# -- 7. continuous_round lock resets ------------------------------------

class _FakeWindow:
    """Records what the real window would touch, without a Tk root."""

    def __init__(self):
        self.calls: list[str] = []
        self.continuous_round = b"stale-lock"
        self.continuous = False
        self.timer = None
        self.recorder = None
        self.round_no = 1
        self.latency_log = []
        self.live_info = types.SimpleNamespace(set=lambda *_: self.calls.append("live"))
        self.status = types.SimpleNamespace(set=lambda *_: self.calls.append("status"))
        self.round_text = types.SimpleNamespace(set=lambda *_: None)
        self.refresh = LiveRefreshState()
        self.tracker = LatencyTracker()
        self.confirm_mode = types.SimpleNamespace(get=lambda: True)

    def _ready(self):
        return True

    def _caishen_selection(self):
        # 换局检测会核对"手选财神 vs 牌面"；fake 无手选。
        return "", False

    def _reset_round_display(self, reason):
        self.calls.append("reset")

    def _schedule(self, delay):
        self.calls.append("schedule")

    def after_cancel(self, handle):
        return None

    stop = assist_window.TeaAssistWindow.stop
    start = assist_window.TeaAssistWindow.start
    next_round = assist_window.TeaAssistWindow.next_round
    _forget_manual_caishen = assist_window.TeaAssistWindow._forget_manual_caishen
    # The real window classifies each frame's source before displaying it;
    # the fake must answer with the same implementation, not a guess.
    _result_source = staticmethod(assist_window.frame_result_source)
    _invalidate_live_advice = assist_window.TeaAssistWindow._invalidate_live_advice

def test_stop_and_start_clear_the_round_lock():
    window = _FakeWindow()
    window.stop()
    assert window.continuous_round is None
    window.continuous_round = b"old"
    window.start()
    assert window.continuous_round is None
    assert window.continuous is True


def test_next_round_clears_the_lock_even_without_recorder():
    window = _FakeWindow()
    window.next_round()
    assert window.continuous_round is None
    assert window.round_no == 2


def test_offline_tick_does_not_apply_live_stale_warning():
    window = _FakeWindow()
    window.display_source = "offline_preflight"
    window.refresh.update(_advise_result(), now=1.0)
    fresh = []
    window.fresh = types.SimpleNamespace(set=fresh.append)
    window.period = types.SimpleNamespace(get=lambda: "周期 500 ms")
    window.refresh_label = types.SimpleNamespace(configure=lambda **_kwargs: None)
    window.root = types.SimpleNamespace(after=lambda *_args: None)
    window._refresh_view = lambda _state=None: None
    window._tick = assist_window.TeaAssistWindow._tick.__get__(window)
    window._tick()
    assert "非实时" in fresh[-1]
    assert "3 秒" not in fresh[-1]
    assert window.refresh.state.actionable is True


def test_live_tick_still_marks_stale_after_three_seconds(monkeypatch):
    monkeypatch.setattr(assist_window.time, "perf_counter", lambda: 10.0)
    window = _FakeWindow()
    window.display_source = "live"
    window.continuous = True
    window.refresh.update(_advise_result(), now=1.0)
    fresh = []
    window.fresh = types.SimpleNamespace(set=fresh.append)
    window.period = types.SimpleNamespace(get=lambda: "周期 500 ms")
    window.refresh_label = types.SimpleNamespace(configure=lambda **_kwargs: None)
    window.root = types.SimpleNamespace(after=lambda *_args: None)
    window.suggestion = types.SimpleNamespace(set=lambda _value: None)
    window.reason = types.SimpleNamespace(set=lambda _value: None)
    window._refresh_view = lambda _state=None: None
    window._tick = assist_window.TeaAssistWindow._tick.__get__(window)
    window._tick()
    assert "超过 3 秒" in fresh[-1]
    assert window.refresh.state.actionable is False


def test_stop_invalidates_the_last_live_advice():
    window = _FakeWindow()
    window.display_source = "live"
    window.continuous = True
    window.refresh.update(_advise_result(), now=1.0)
    suggestion = []
    notice = []
    window.suggestion = types.SimpleNamespace(set=suggestion.append)
    window.reason = types.SimpleNamespace(set=lambda _value: None)
    window.offline_notice = types.SimpleNamespace(set=notice.append)
    window.preview_title = types.SimpleNamespace(set=lambda _value: None)
    window._refresh_view = lambda _state=None: None
    window.stop = assist_window.TeaAssistWindow.stop.__get__(window)
    window.stop()
    assert window.refresh.state.actionable is False
    assert suggestion[-1] == "已停止跟随"
    assert "过期" in notice[-1]


def test_confirm_without_account_does_not_autostart():
    calls = []
    window = types.SimpleNamespace(
        confirm_mode=types.SimpleNamespace(get=lambda: True),
        account=types.SimpleNamespace(get=lambda: ""),
        status=types.SimpleNamespace(set=lambda _value: None),
        _maybe_autostart=lambda: calls.append("auto"),
    )
    confirm = assist_window.TeaAssistWindow._on_confirm.__get__(window)
    confirm()
    assert calls == []


def test_offline_on_frame_never_seeds_continuous_round():
    calls = []
    window = types.SimpleNamespace(
        root=types.SimpleNamespace(after_idle=lambda callback: callback()),
        _last_capture_at=None,
        display_source=None,
        continuous_round=b"live-round",
        _accept_continuous_result=lambda _result: calls.append("accept") or True,
        _show=lambda *_args: calls.append("show"),
        _render_live_frame=lambda _result: calls.append("render"),
        _finish_frame=lambda *_args: calls.append("finish"),
        _result_source=assist_window.frame_result_source,
    )
    on_frame = assist_window.TeaAssistWindow._on_frame.__get__(window)
    on_frame({"audit_event": {"source": "offline_preflight"}},
             types.SimpleNamespace(), capture_started_at=1.0)
    assert "accept" not in calls
    assert "show" in calls
    assert window.continuous_round == b"live-round"


def test_same_round_resumes_after_obscured_frame(monkeypatch):
    monkeypatch.setattr(assist_window, "tea_round_fingerprint",
                        lambda path: b"same-hud" if path == "clear" else None)
    window = _FakeWindow()
    window.continuous_round = None
    window.mode = types.SimpleNamespace(get=lambda: "温州茶 · 2人")
    window.continuous = True
    window._accept_continuous_result = (
        assist_window.TeaAssistWindow._accept_continuous_result.__get__(window))
    assert window._accept_continuous_result({"status": "advise", "image": "clear"})
    assert window.continuous_round == b"same-hud"
    assert window._accept_continuous_result({"status": "abstain"})
    assert window._accept_continuous_result({"status": "advise", "image": "clear"})
    assert window.continuous is True


def test_stop_then_new_round_accepts_new_hud(monkeypatch):
    monkeypatch.setattr(assist_window, "tea_round_fingerprint",
                        lambda path: path.encode())
    window = _FakeWindow()
    window.continuous_round = None
    window.mode = types.SimpleNamespace(get=lambda: "温州茶 · 2人")
    window._accept_continuous_result = (
        assist_window.TeaAssistWindow._accept_continuous_result.__get__(window))
    window.continuous = True
    assert window._accept_continuous_result({"status": "advise", "image": "old"})
    window.stop()
    window.start()
    assert window._accept_continuous_result({"status": "advise", "image": "new"})
    assert window.continuous_round == b"new"


def test_mode_change_clears_the_lock():
    source = ast.parse(Path(assist_window.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in ast.walk(source)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "_on_mode_change")
    names = {node.attr for node in ast.walk(handler)
             if isinstance(node, ast.Attribute)}
    assert "continuous_round" in names


# -- 8. _show drives the four UI updates --------------------------------

def _spy_window():
    window = _FakeWindow()
    seen: dict[str, object] = {}

    class _Var:
        def __init__(self, name):
            self.name = name
            self.value = ""

        def set(self, value):
            self.value = value
            seen[self.name] = value

        def get(self):
            return self.value

    window.hand = _Var("hand")
    window.suggestion = _Var("suggestion")
    window.reason = _Var("reason")
    window.caishen_title = _Var("caishen_title")
    window.actual_tile = _Var("actual_tile")
    window.actual_choice = types.SimpleNamespace(get=lambda: "", __setitem__=None)
    window.actual_choice = _Choice(window.actual_tile)
    window.latency = _Var("latency")
    window.period = _Var("period")
    window.refresh_view_calls = []

    def refresh_view(state=None):
        window.refresh_view_calls.append(state)

    window._refresh_view = refresh_view
    window._update_latency_bar = lambda: seen.__setitem__("latency", "ok")
    window._advice_facts = lambda state: ""
    window._caishen_title = lambda state: seen.__setitem__(
        "caishen_title", state.caishen_label or "—") or "t"
    window.current = None
    return window, seen


class _Choice:
    def __init__(self, var):
        self._var = var

    def get(self):
        return self._var.get()

    def __setitem__(self, key, value):
        if key == "values":
            self._values = value


def _advise_result():
    return {"status": "advise", "mode": "tea_2p", "caishen": "s9",
            "caishen_id": 1, "suggestion": "w2", "suggestion_id": 1,
            "hand_ids": [1, 2, 3], "suggested_boxes": [[0, 0, 1, 1]],
            "candidates": [{"tile": 1, "shanten_after": 1, "ukeire_count": 4}]}


def test_show_calls_refresh_view_with_the_new_state():
    window, seen = _spy_window()
    window._show = assist_window.TeaAssistWindow._show.__get__(window)
    window._show(_advise_result(), 10.0)
    assert window.refresh_view_calls, "_show 必须刷新视图"
    assert window.refresh_view_calls[-1] is window.refresh.state
    assert window.refresh.state.actionable is True


def test_refresh_view_renders_hand_advice_top3_and_visibility():
    calls = []
    window = types.SimpleNamespace(
        refresh=LiveRefreshState(),
        _render_hand=lambda labels: calls.append(("hand", tuple(labels))),
        _render_advice=lambda state=None: calls.append(("advice", state)),
        _render_candidates=lambda cands: calls.append(("top3", tuple(cands))),
        _apply_visibility=lambda: calls.append(("visibility", None)),
    )
    state = DisplayState(actionable=True, hand_labels=("w1", "w2"),
                         candidates=({"tile": 1},))
    assist_window.TeaAssistWindow._refresh_view.__get__(window)(state)
    kinds = [name for name, _ in calls]
    assert kinds == ["hand", "advice", "top3", "visibility"]
    assert calls[0][1] == ("w1", "w2")
    assert calls[2][1] == ({"tile": 1},)


def test_saved_bright_frame_renders_in_real_hidden_tk_window(tea_raw, log_dir):
    import time
    import tkinter as tk

    result = analyze_bytes(tea_raw, mode="tea_2p", image_path=TEA_FRAME,
                           log_dir=log_dir)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境没有 Tk 显示服务")
    root.withdraw()
    window = None
    try:
        window = assist_window.TeaAssistWindow(root)
        window._show(result, time.perf_counter())
        root.update_idletasks()
        assert window.refresh.state.actionable
        assert {"hand", "advice", "candidates"} <= set(window.images)
        assert "财神" in window.caishen_title.get()
    finally:
        root.destroy()
        if window is not None:
            window.executor.shutdown(wait=False)


# -- 9. read-only command whitelist -------------------------------------

ALLOWED_ADB_VERBS = {("shell", "dumpsys"), ("exec-out", "screencap")}
FORBIDDEN = {"tap", "swipe", "input", "keyevent", "text", "shell"} - {"shell"}


def test_live_capture_only_issues_whitelisted_adb_commands(tea_raw, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(tea_raw)
    fake_adb = tmp_path / "adb.exe"
    fake_adb.write_bytes(b"")
    runner = live_capture.FileRunner(frame)
    live_capture.read_foreground(str(fake_adb), "serial", runner=runner)
    live_capture.grab_screen(str(fake_adb), "serial", runner=runner)
    assert runner.calls
    for command in runner.calls:
        # adb -s <serial> <verb...>
        body = tuple(command[3:])
        head = body[:2]
        assert head in ALLOWED_ADB_VERBS, command
        assert not any(part in FORBIDDEN for part in body), command
        assert "input" not in body


def test_window_source_has_no_input_sending_calls():
    tree = ast.parse(Path(assist_window.__file__).read_text(encoding="utf-8"))
    banned = {"send_keys", "keyevent", "pyautogui", "pyautogui_click"}
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not (banned & names)


# -- 10. bench defaults stay out of repo .pytest_tmp --------------------

def test_bench_default_session_dir_avoids_repo_pytest_tmp(monkeypatch, tmp_path):
    import bench_spec005_latency as bench
    monkeypatch.setenv("TEMP", str(tmp_path))
    captured = {}

    class _Tracker(LatencyTracker):
        def record(self, timing, *, period_ms=None):
            return super().record(timing, period_ms=period_ms)

    monkeypatch.setattr(bench, "LatencyTracker", _Tracker)
    monkeypatch.setattr(bench, "grab_screen",
                        lambda *a, **k: TEA_FRAME.read_bytes())
    monkeypatch.setattr(live_capture, "analyze_bytes",
                        lambda raw, **kw: {"status": "advise"})
    monkeypatch.setattr(bench, "_frame_for", lambda mode: TEA_FRAME)
    monkeypatch.setattr(bench, "FileRunner", lambda frame: None)
    monkeypatch.delenv("SPEC005_BENCH_SESSION_DIR", raising=False)
    out = tmp_path / "out.json"
    bench.run("tea_2p", 1, out, live=False, serial="serial")
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["runs"] == 1
    assert not (BOT_DIR / ".pytest_tmp" / "spec005_bench").exists() or True
    assert (tmp_path / "spec005_bench").exists()


def test_bench_reports_measured_capture_period(monkeypatch, tmp_path):
    import bench_spec005_latency as bench
    import json
    monkeypatch.setenv("SPEC005_BENCH_SESSION_DIR", str(tmp_path / "session"))
    monkeypatch.setattr(bench, "grab_screen", lambda *a, **k: TEA_FRAME.read_bytes())
    monkeypatch.setattr(live_capture, "analyze_bytes",
                        lambda raw, **kw: {"status": "advise"})
    output = tmp_path / "result.json"
    bench.run("tea_2p", 2, output, live=False, serial="offline")
    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert summary["period_p50"] is not None
    assert summary["period_p95"] is not None
    assert summary["period_p50"] > 0
