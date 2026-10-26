# -*- coding: utf-8 -*-
"""Geometry math for the "收起到右下角" floating bar (no Tk instance needed)."""
from __future__ import annotations

import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from assist_window import (  # noqa: E402
    FLOATING_BAR_MARGIN,
    FLOATING_BAR_SIZE,
    calculate_bottom_right_position,
    parse_window_geometry,
    restore_window_geometry,
)


def test_floating_bar_keeps_eight_pixel_margin_on_1600_by_900():
    width, height = FLOATING_BAR_SIZE
    x, y = calculate_bottom_right_position(1600, 900)
    assert (x, y) == (1460, 856)
    assert 1600 - (x + width) == FLOATING_BAR_MARGIN
    assert 900 - (y + height) == FLOATING_BAR_MARGIN


def test_floating_bar_keeps_eight_pixel_margin_on_1920_by_1080():
    """1920×1080 with a Windows taskbar: bottom margin stays eight pixels."""
    width, height = FLOATING_BAR_SIZE
    x, y = calculate_bottom_right_position(1920, 1080)
    assert (x, y) == (1780, 1036)
    assert 1920 - (x + width) == FLOATING_BAR_MARGIN
    assert 1080 - (y + height) == FLOATING_BAR_MARGIN


def test_bottom_right_position_never_returns_negative_coordinates():
    x, y = calculate_bottom_right_position(64, 20)
    assert x >= 0 and y >= 0


def test_parse_window_geometry_accepts_signed_offsets():
    assert parse_window_geometry("1180x860-20+40") == (1180, 860, -20, 40)
    assert parse_window_geometry("1180x860+200+50") == (1180, 860, 200, 50)


def test_parse_window_geometry_rejects_partial_or_invalid_strings():
    assert parse_window_geometry("1180x860") is None
    assert parse_window_geometry("") is None
    assert parse_window_geometry("1180x860+abc+40") is None


def test_restore_window_geometry_keeps_window_on_screen():
    assert restore_window_geometry("1180x860-20+40", (1600, 900)) == "1180x860+0+40"
    assert restore_window_geometry("1180x860+900+900", (1600, 900)) == "1180x860+420+40"
    assert restore_window_geometry("invalid", (1600, 900)) is None


def test_restore_window_geometry_clamps_too_small_sizes():
    assert restore_window_geometry("100x100+10+10", (1600, 900)) == "900x760+10+10"
