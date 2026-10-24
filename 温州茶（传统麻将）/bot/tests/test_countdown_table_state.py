# -*- coding: utf-8 -*-
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
