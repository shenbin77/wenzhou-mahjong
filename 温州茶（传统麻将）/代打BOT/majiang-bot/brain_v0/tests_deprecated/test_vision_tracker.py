# -*- coding: utf-8 -*-
import os
import sys

import cv2
import numpy as np
from PIL import Image

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from capture import capture_region
from recognizer import TileRecognizer
from tracker import TileTracker


def test_legacy_templates_are_loaded():
    from config import TILE_TYPES
    recognizer = TileRecognizer()
    assert len(recognizer.templates) == len(TILE_TYPES)
    assert {"1m", "1s", "1p", "red"} <= set(recognizer.templates)


def test_recognizer_detects_duplicate_tiles():
    recognizer = TileRecognizer()
    template = recognizer.templates["1m"]
    h, w = template.shape[:2]
    canvas = np.zeros((h + 20, w * 2 + 60, 3), dtype=np.uint8)
    canvas[10:10+h, 10:10+w] = template
    canvas[10:10+h, 40+w:40+2*w] = template
    matches = recognizer.recognize_region(canvas)
    ones = [match for match in matches if match[0] == "1m"]
    assert len(ones) == 2


def test_tracker_separates_players_and_counts_duplicates():
    tracker = TileTracker()
    tracker.reset()
    tracker.update_river([("1m", .9), ("1m", .88)], "top")
    tracker.update_river([("1m", .91)], "left")
    assert tracker.played_counts["1m"] == 3
    tracker.update_river([], "top")
    tracker.update_river([("1m", .9), ("1m", .88)], "top")
    assert tracker.played_counts["1m"] == 3


def test_capture_regions_scale_with_image():
    image = Image.new("RGB", (2560, 1252))
    hand = capture_region(image, "hand_tiles")
    assert hand.size == (1760, 160)


def test_capture_regions_auto_select_competition_layout(monkeypatch):
    """未显式配置时，16:9 竞赛截图必须使用 1600x900 坐标图。"""
    import capture

    monkeypatch.delenv("MVP_CAPTURE_REGIONS", raising=False)
    image = Image.new("RGB", (1600, 900))
    hand = capture_region(image, "hand_tiles")
    assert hand.size == (1600, 190)


# ------------------------------------------------------------------ 问题7
def test_capture_region_warns_on_aspect_ratio_mismatch(capsys):
    """问题7: 画面宽高比明显偏离标定基准时应告警可能形变（只提示不改映射）。"""
    import capture

    capture._warned_aspect_sizes.clear()
    image = Image.new("RGB", (1280, 700))  # 宽高比差 ~10%
    capture_region(image, "hand_tiles")
    out = capsys.readouterr().out
    assert "宽高比" in out and "形变" in out
    # 同一尺寸不应每帧重复刷屏
    capture_region(image, "hand_tiles")
    assert "宽高比" not in capsys.readouterr().out


def test_capture_region_silent_at_reference_ratio(capsys):
    """问题7 反向: 与基准等比的画面不得产生告警。"""
    import capture

    capture._warned_aspect_sizes.clear()
    capture_region(Image.new("RGB", (2560, 1252)), "river_top")
    assert "宽高比" not in capsys.readouterr().out


# ------------------------------------------------------------------ 问题6
def test_capture_window_returns_none_for_minimized_window():
    """问题6: 最小化/隐藏（宽高<=0）时返回 None 表示「本帧跳过」，不抛致命异常。"""
    import capture

    class Minimized:
        left = top = 0
        width = 0
        height = 0
        handle = 1

    assert capture.capture_window(Minimized()) is None
    negative = Minimized()
    negative.width, negative.height = -1, -1
    assert capture.capture_window(negative) is None


# ------------------------------------------------------------------ HiDPI
class _FakeWindll:
    def __init__(self, user32, shcore=None):
        self.user32 = user32
        self.shcore = shcore


def _fake_ctypes(monkeypatch, dpi_for_window=None, dpi_for_system=96):
    import types as _types

    calls = []

    def make_user32():
        class U:
            def GetDpiForWindow(self, hwnd):
                calls.append(("window", hwnd))
                if dpi_for_window is None:
                    raise OSError("GetDpiForWindow 不可用")
                return dpi_for_window

            def GetDpiForSystem(self):
                calls.append(("system",))
                return dpi_for_system

            def MonitorFromWindow(self, hwnd, flag):
                return 0

        return U()

    fake = _types.SimpleNamespace(windll=_FakeWindll(make_user32(), None))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    return fake, calls


def test_dpi_scale_prefers_window_dpi(monkeypatch):
    """HiDPI: per-monitor 感知下必须优先取截图窗口的 DPI，而非主屏系统 DPI。"""
    import capture

    _, calls = _fake_ctypes(monkeypatch, dpi_for_window=144, dpi_for_system=96)

    class W:
        handle = 42

    assert capture._get_dpi_scale(W()) == 1.5
    assert ("window", 42) in calls


def test_dpi_scale_falls_back_to_system_dpi(monkeypatch):
    """HiDPI: 取不到窗口 DPI 时回退 GetDpiForSystem（保持原兜底行为）。"""
    import capture

    _, calls = _fake_ctypes(monkeypatch, dpi_for_window=None,
                            dpi_for_system=192)

    class W:
        handle = 42

    assert capture._get_dpi_scale(W()) == 2.0
    assert ("system",) in calls


def test_dpi_scale_defaults_to_one_without_window(monkeypatch):
    """无窗口对象且系统 DPI 取不到时返回 1.0（不崩）。"""
    import capture
    import types as _types

    fake = _types.SimpleNamespace(
        windll=_FakeWindll(_types.SimpleNamespace(
            GetDpiForWindow=None,
            GetDpiForSystem=None,
            MonitorFromWindow=None,
        ), None))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    assert capture._get_dpi_scale(None) == 1.0
