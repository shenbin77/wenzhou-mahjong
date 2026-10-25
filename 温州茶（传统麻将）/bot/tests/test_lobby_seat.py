"""Guarded tea-2p lobby seating: positive evidence before every tap."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lobby_seat
from lobby_seat import execute_auto_seat, plan_auto_seat
from session_watch import WatchState


def _load_gray(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    assert image is not None
    return image


def _frame(*, seats=((115, 267), (411, 467))) -> np.ndarray:
    """Synthetic full frame using the reviewed real table template."""
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    x0, y0, x1, y1 = lobby_seat.TABLE_ROI
    tpl = _load_gray(lobby_seat.TABLE_TEMPLATE)
    assert tpl.shape == (y1 - y0, x1 - x0)
    image[y0:y1, x0:x1] = cv2.cvtColor(tpl, cv2.COLOR_GRAY2BGR)

    # White 36x36 plus, 6px bars -> reviewed component geometry/area gate.
    for cx, cy in seats:
        x, y = cx - 18, cy - 18
        image[y:y + 36, x:x + 36] = (110, 145, 175)  # non-white seat card
        image[cy - 3:cy + 3, x:x + 36] = 255
        image[y:y + 36, cx - 3:cx + 3] = 255
    return image


def _watch(state: WatchState):
    return SimpleNamespace(state=state)


def test_plan_requires_reviewed_mode_size_and_table():
    assert plan_auto_seat(_frame(), "emulator-5556", "tea_4p").status == "blocked"
    assert plan_auto_seat(np.zeros((720, 1280, 3), np.uint8), "emulator-5556").status == "blocked"
    blank = np.zeros((900, 1600, 3), np.uint8)
    plan = plan_auto_seat(blank, "emulator-5556")
    assert plan.status == "blocked"
    assert "证据不足" in plan.detail


def test_plan_detects_two_empty_seats_and_pins_by_serial():
    frame = _frame()
    a = plan_auto_seat(frame, "emulator-5556")
    b = plan_auto_seat(frame, "emulator-5558")
    assert a.status == b.status == "ready"
    assert a.table_score >= lobby_seat.TABLE_MIN_SCORE
    assert [x.point for x in a.candidates] == [(115, 267), (411, 467)]
    assert a.target is not None and b.target is not None
    assert a.target.point != b.target.point


def test_missing_or_excess_seat_candidates_fail_closed():
    assert plan_auto_seat(_frame(seats=()), "emulator-5556").status == "blocked"
    # Third plus is outside reviewed seat zones, so it must not become a candidate.
    plan = plan_auto_seat(_frame(seats=((115, 267), (411, 467), (250, 550))), "emulator-5556")
    assert plan.status == "ready"
    assert len(plan.candidates) == 2


def test_execute_seats_only_after_fresh_frame_and_waiting_room(monkeypatch):
    initial = _frame()
    frames = iter([_frame(), np.zeros((900, 1600, 3), np.uint8)])
    taps = []
    monkeypatch.setattr(lobby_seat, "classify_watch",
                        lambda _img: _watch(WatchState.WAITING_ROOM))
    result = execute_auto_seat(
        initial, serial="emulator-5556",
        capture_image=lambda: next(frames),
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _s: None,
    )
    assert result.status == "seated"
    assert result.clicked is True
    assert len(taps) == 1
    assert taps[0] in ((115, 267), (411, 467))


def test_fresh_target_drift_blocks_before_any_tap():
    initial = _frame(seats=((115, 267),))
    # Still inside the reviewed zone, but >12 px drift from the initial target.
    moved = _frame(seats=((135, 267),))
    taps = []
    result = execute_auto_seat(
        initial, serial="emulator-5556",
        capture_image=lambda: moved,
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _s: None,
    )
    assert result.status == "blocked"
    assert "漂移" in result.detail
    assert taps == []


def test_rules_modal_requires_fresh_recheck_before_close(monkeypatch):
    initial = _frame(seats=((115, 267),))
    fresh = _frame(seats=((115, 267),))
    after_seat = np.zeros((900, 1600, 3), np.uint8)
    fresh_modal = after_seat.copy()
    final = after_seat.copy()
    frames = iter([fresh, after_seat, fresh_modal, final])
    taps = []
    watch_states = iter([WatchState.LOBBY, WatchState.WAITING_ROOM])
    monkeypatch.setattr(lobby_seat, "classify_watch",
                        lambda _img: _watch(next(watch_states)))
    calls = {"n": 0}
    def modal(_img):
        calls["n"] += 1
        return True, 0.999, (1126, 254)
    monkeypatch.setattr(lobby_seat, "detect_target_rules_modal", modal)

    result = execute_auto_seat(
        initial, serial="emulator-5556",
        capture_image=lambda: next(frames),
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _s: None,
    )
    assert result.status == "seated"
    assert len(taps) == 2
    assert taps[1] == (1126, 254)
    assert calls["n"] == 2


def test_unknown_post_click_scene_never_generates_extra_click(monkeypatch):
    initial = _frame(seats=((115, 267),))
    fresh = _frame(seats=((115, 267),))
    after = np.zeros((900, 1600, 3), np.uint8)
    frames = iter([fresh] + [after] * 8)
    taps = []
    monkeypatch.setattr(lobby_seat, "classify_watch",
                        lambda _img: _watch(WatchState.UNKNOWN))
    monkeypatch.setattr(lobby_seat, "detect_target_rules_modal",
                        lambda _img: (False, 0.2, None))
    result = execute_auto_seat(
        initial, serial="emulator-5556",
        capture_image=lambda: next(frames),
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _s: None,
    )
    assert result.status == "failed"
    assert len(taps) == 1
    assert "不再点击" in result.detail


def test_delayed_in_game_after_transient_unknown_is_success(monkeypatch):
    initial = _frame(seats=((115, 267),))
    fresh = _frame(seats=((115, 267),))
    transition = np.zeros((900, 1600, 3), np.uint8)
    in_game = transition.copy()
    frames = iter([fresh, transition, transition, in_game])
    states = iter([WatchState.UNKNOWN, WatchState.UNKNOWN, WatchState.IN_GAME])
    taps = []
    monkeypatch.setattr(
        lobby_seat, "classify_watch", lambda _img: _watch(next(states)))
    monkeypatch.setattr(
        lobby_seat, "detect_target_rules_modal",
        lambda _img: (False, 0.1, None))
    result = execute_auto_seat(
        initial, serial="emulator-5556",
        capture_image=lambda: next(frames),
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _s: None,
    )
    assert result.status == "seated"
    assert result.final_state == "in_game"
    assert len(taps) == 1
