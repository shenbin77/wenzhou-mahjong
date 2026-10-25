"""read_opponent_sparkle_tile：claim 圆盘遮挡牌河时，从唯一对手闪光标恢复来牌。"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))
import discard_vision as dv  # noqa: E402

FRAME = (BOT.parent / "采集" / "dev_auto_play" / "ui_20261025_165115_emulator5556_玉姐"
         / "frames" / "20261025_165416_996466_before_29c49d.png")


@pytest.fixture(scope="module")
def real():
    if not FRAME.is_file():
        pytest.skip("real claim frame missing")
    return cv2.imdecode(np.fromfile(str(FRAME), np.uint8), cv2.IMREAD_COLOR)


def test_real_frame_accepts_t6(real):
    ev = dv.read_opponent_sparkle_tile(real, sparkle_x=885)
    assert ev["accepted"] and ev["label"] == "t6"
    assert ev["score"] >= 0.86 and ev["margin"] >= 0.10


def test_no_sparkle_rejects():
    blank = np.zeros((900, 1600, 3), np.uint8)
    assert not dv.read_opponent_sparkle_tile(blank)["accepted"]
    assert not dv.read_opponent_sparkle_tile(blank, sparkle_x=885)["accepted"]


def test_multiple_sparkles_reject(real, monkeypatch):
    monkeypatch.setattr(dv, "find_discard_sparkles", lambda img: (700, 885))
    ev = dv.read_opponent_sparkle_tile(real)
    assert not ev["accepted"] and "count=2" in ev["reason"]


def test_misaligned_x_rejects(real, monkeypatch):
    monkeypatch.setattr(dv, "find_discard_sparkles", lambda img: (913,))
    ev = dv.read_opponent_sparkle_tile(real, sparkle_x=913)
    assert not ev["accepted"]
    assert not dv.read_opponent_sparkle_tile(real, sparkle_x=500)["accepted"]


def test_low_confidence_rejects(real, monkeypatch):
    monkeypatch.setattr(dv, "_classify", lambda patch, clf: {
        "accepted": True, "tile_id": 26, "label": "t6", "score": 0.80, "margin": 0.30})
    ev = dv.read_opponent_sparkle_tile(real, sparkle_x=885)
    assert not ev["accepted"] and ev["reason"].startswith("tile_confidence_low")
    monkeypatch.setattr(dv, "_classify", lambda patch, clf: {
        "accepted": True, "tile_id": 26, "label": "t6", "score": 0.95, "margin": 0.05})
    assert not dv.read_opponent_sparkle_tile(real, sparkle_x=885)["accepted"]
