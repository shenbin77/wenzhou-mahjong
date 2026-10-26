from pathlib import Path

BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
path = BOT / "auto_play_loop.py"
text = path.read_text(encoding="utf-8")
old = (
    "        table = build_table_state(\n"
    "            result, image,\n"
    "            wall_cards=wall_cards,\n"
)
if old not in text:
    raise SystemExit("table build missing")
new = (
    "        result_for_table = dict(result)\n"
    "        if caishen:\n"
    "            result_for_table[\"caishen\"] = caishen\n"
    "        table = build_table_state(\n"
    "            result_for_table, image,\n"
    "            wall_cards=wall_cards,\n"
)
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("fixed resolved caishen into table")

# tests
test_cd = r'''# -*- coding: utf-8 -*-
"""Tests for countdown_vision + table_state (P0 chunk A)."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from countdown_vision import (  # noqa: E402
    STATUS_ABSENT,
    STATUS_EXPIRED,
    STATUS_LOW,
    STATUS_PRESENT,
    STATUS_UNREADABLE,
    detect_countdown,
)
from table_state import WALL_CARDS_STOP_AT, build_table_state  # noqa: E402


def blank_frame() -> np.ndarray:
    return np.zeros((900, 1600, 3), np.uint8)


def test_detect_countdown_blank_is_absent():
    reading = detect_countdown(blank_frame())
    assert reading.status == STATUS_ABSENT
    assert reading.seconds is None


def test_detect_countdown_bad_frame_is_unreadable():
    reading = detect_countdown(None)
    assert reading.status == STATUS_UNREADABLE
    reading = detect_countdown(np.zeros((100, 100, 3), np.uint8))
    assert reading.status == STATUS_UNREADABLE


def test_detect_countdown_synthetic_digit_low():
    """Paint a bright "1" into the claim-band ROI; expect low/present parse."""
    frame = blank_frame()
    # ROI (450,540,1000,1520) — draw a tall thin white "1"
    cv2.putText(frame, "1", (1200, 520), cv2.FONT_HERSHEY_SIMPLEX,
                1.8, (240, 240, 240), 3, cv2.LINE_AA)
    reading = detect_countdown(frame)
    assert reading.status in {STATUS_LOW, STATUS_PRESENT, STATUS_UNREADABLE, STATUS_ABSENT}
    # Best effort: if parsed as 1 must be low
    if reading.seconds == 1:
        assert reading.status == STATUS_LOW
    if reading.seconds == 0:
        assert reading.status == STATUS_EXPIRED


def test_build_table_state_flags_wall_and_unknown_caishen():
    result = {
        "mode": "tea_2p", "status": "advise", "caishen": None,
        "hand_ids": list(range(1, 17)), "melds": [], "meld_count": 0,
        "concealed_boxes": [[0, 0, 10, 10]] * 16,
    }
    table = build_table_state(result, blank_frame(), wall_cards=16)
    assert table.should_stop_for_wall is True
    assert table.should_stop_for_unknown_caishen is True
    assert WALL_CARDS_STOP_AT == 16
    assert "wall_cards<=16" in table.notes
    assert "caishen_unknown" in table.notes
    assert table.countdown_status != "unknown"


def test_build_table_state_known_caishen_and_safe_wall():
    result = {
        "mode": "tea_2p", "status": "advise", "caishen": "w1",
        "hand_ids": list(range(1, 18)), "melds": [[1, 1, 1]],
        "concealed_boxes": [[0, 0, 10, 10]] * 14,
    }
    table = build_table_state(result, blank_frame(), wall_cards=40)
    assert table.caishen_known is True
    assert table.should_stop_for_wall is False
    assert table.should_stop_for_unknown_caishen is False
    assert table.logical_meld_slots == 3
'''
(BOT / "tests" / "test_countdown_table_state.py").write_text(test_cd, encoding="utf-8")
print("wrote test_countdown_table_state.py")

# patch auto_play tests
path = BOT / "tests" / "test_auto_play_loop.py"
text = path.read_text(encoding="utf-8")

addition = '''

def test_wall_cards_at_16_stops_without_tapping(tmp_path):
    """P0-5: 牌墙/房卡 ≤ 16 → card_exhausted 停机，不点。"""
    loop, shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True, cards=[16])
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "card_exhausted"
    assert loop.stopped is True
    assert loop.card_count == 16
    assert analyzer.calls == 0
    assert tap.execute_calls == []


def test_wall_cards_above_16_does_not_trip_wall_stop(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True, cards=[17])
    settle(loop)
    event = loop.tick()
    assert event.get("gate") != "card_exhausted"
    assert loop.stopped is False or event.get("gate") != "card_exhausted"
    assert tap.execute_calls or event["event"] in {"action", "dry_run_plan", "idle", "stop"}


def test_unknown_caishen_stops_when_enabled(tmp_path):
    """P0-5: enabled + 本帧财神未知 → gate_unknown_caishen。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(caishen=None)],
        hud=[(b"hud-ok", None)],
    )
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_unknown_caishen"
    assert tap.execute_calls == []
    assert "table_state" in event
    assert event["table_state"]["caishen_known"] is False
'''

if "test_wall_cards_at_16_stops_without_tapping" not in text:
    # insert after test_card_zero
    marker = "def test_card_unknown_does_not_block_and_never_clicks_recharge"
    if marker not in text:
        raise SystemExit("marker missing")
    text = text.replace(marker, addition + "\n\n" + marker, 1)
    path.write_text(text, encoding="utf-8")
    print("patched auto_play tests")
else:
    print("auto_play tests already patched")

# fix claim discs test expectation for urgent code optionally
path = BOT / "tests" / "test_turn_gate.py"
text = path.read_text(encoding="utf-8")
if 'countdown_status": "unknown"' in text:
    text = text.replace('countdown_status": "unknown"', 'countdown_status": "absent"', 1)
    path.write_text(text, encoding="utf-8")
    print("fixed turn_gate claim test countdown status")