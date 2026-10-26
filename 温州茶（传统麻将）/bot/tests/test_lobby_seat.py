"""Guarded tea-2p lobby seating: positive evidence before every tap."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lobby_seat
from lobby_autostart import RoomState, SeatCandidate
from lobby_seat import execute_auto_seat, plan_auto_seat
from session_watch import WatchState


REPO = Path(__file__).resolve().parents[3]
ROOM_EMPTY = REPO / "_takeover" / "lobby_probe" / "001_now_5556.png"
ROOM_ONE_SEATED = (
    REPO / "_takeover" / "lobby_probe" / "013_now_5558_after_table_created.png"
)


def _read(path: Path = ROOM_EMPTY) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    assert image is not None
    return image


def _room_state(*points: tuple[int, int]) -> RoomState:
    seats = tuple(
        SeatCandidate(
            box=(x - 45, y - 47, x + 45, y + 65),
            center=(x, y), unit=0,
            slot="upper" if index == 0 else f"extra-{index}",
            join_score=0.999,
        )
        for index, (x, y) in enumerate(points)
    )
    return RoomState(
        True, "测试房间", seats=seats, units=1,
        evidence={"diamonds": [{"score": 0.966}]},
    )


def _watch(state: WatchState):
    return SimpleNamespace(state=state)


def test_plan_requires_reviewed_mode_size_and_table():
    assert plan_auto_seat(_read(), "emulator-5556", "tea_4p").status == "blocked"
    assert plan_auto_seat(np.zeros((720, 1280, 3), np.uint8), "emulator-5556").status == "blocked"
    blank = np.zeros((900, 1600, 3), np.uint8)
    plan = plan_auto_seat(blank, "emulator-5556")
    assert plan.status == "blocked"
    assert "证据不足" in plan.detail


def test_plan_detects_two_empty_seats_and_pins_by_serial():
    frame = _read()
    a = plan_auto_seat(frame, "emulator-5556")
    b = plan_auto_seat(frame, "emulator-5558")
    assert a.status == b.status == "ready"
    assert a.table_score >= 0.85
    assert [x.point for x in a.candidates] == [(115, 267), (411, 467)]
    assert a.target is not None and b.target is not None
    assert a.target.point != b.target.point


def test_live_one_seated_table_uses_shared_lobby_evidence():
    """Regression: the old duplicate 0.98 table gate rejected this at 0.966."""
    plan = plan_auto_seat(_read(ROOM_ONE_SEATED), "emulator-5556")
    assert plan.status == "ready"
    assert [candidate.point for candidate in plan.candidates] == [(411, 467)]
    assert plan.target is not None and plan.target.point == (411, 467)
    assert abs(plan.table_score - 0.9658) < 0.002


def test_missing_or_excess_first_table_seats_fail_closed(monkeypatch):
    blank = np.zeros((900, 1600, 3), np.uint8)
    assert plan_auto_seat(blank, "emulator-5556").status == "blocked"
    monkeypatch.setattr(
        lobby_seat, "read_lobby_room",
        lambda _image: _room_state((115, 267), (411, 467), (250, 550)),
    )
    plan = plan_auto_seat(blank, "emulator-5556")
    assert plan.status == "blocked"
    assert "候选异常" in plan.detail


def test_full_first_table_never_falls_through_to_empty_second_table(monkeypatch):
    blank = np.zeros((900, 1600, 3), np.uint8)
    right_only = RoomState(
        True, "左桌已满、右桌有空座",
        seats=(SeatCandidate(
            box=(603, 221, 693, 333), center=(649, 267),
            unit=1, slot="upper", join_score=0.993,
        ),),
        units=2,
        evidence={"diamonds": [{"score": 0.966}, {"score": 0.973}]},
    )
    monkeypatch.setattr(lobby_seat, "read_lobby_room", lambda _image: right_only)
    plan = plan_auto_seat(blank, "emulator-5556")
    assert plan.status == "blocked"
    assert "最左桌没有" in plan.detail


def test_execute_seats_only_after_fresh_frame_and_waiting_room(monkeypatch):
    initial = _read()
    frames = iter([_read(), np.zeros((900, 1600, 3), np.uint8)])
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


def test_fresh_target_drift_blocks_before_any_tap(monkeypatch):
    initial = np.zeros((900, 1600, 3), np.uint8)
    moved = initial.copy()
    states = iter([_room_state((115, 267)), _room_state((135, 267))])
    monkeypatch.setattr(lobby_seat, "read_lobby_room", lambda _image: next(states))
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
    initial = _read()
    fresh = _read()
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
    initial = _read()
    fresh = _read()
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
    initial = _read()
    fresh = _read()
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
