"""Read-only stacked-kong evidence survives an otherwise incomplete hand."""

import contextlib
import io
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import _visible_stacked_kongs, analyze_snapshot  # noqa: E402
from assist_window import stacked_kong_summary  # noqa: E402
from tile_vision import TileVision  # noqa: E402

SESSION = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_005400"
           / "live" / "tea_2p")


def test_west_kong_remains_visible_evidence_while_waiting_for_draw(tmp_path):
    frame = SESSION / "20261024_005700_684588_0ae39a.png"
    if not frame.is_file():
        pytest.skip("西杠实战帧缺失")
    with contextlib.redirect_stdout(io.StringIO()):
        result = analyze_snapshot(
            frame, caishen_label="s8", confirmed_two_player=True,
            log_dir=tmp_path / "logs")
    assert result["status"] == "wait"
    assert "等待摸牌" in result["reason"]
    assert not result.get("suggested_boxes")
    assert result["stacked_kongs"][0]["label"] == "xi"
    assert result["stacked_kongs"][0]["logical_count"] == 4
    assert len(result["stacked_kongs"][0]["visible_boxes"]) == 3
    assert stacked_kong_summary(result) == "西杠（4张）"


def test_flat_west_peng_is_not_reported_as_kong():
    frame = SESSION / "20261024_005413_959184_662f3d.png"
    if not frame.is_file():
        pytest.skip("西碰实战帧缺失")
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    vision = TileVision(template_dir=BOT_DIR.parent / "采集" / "templates" / "tea_tiles")
    vision.recognize_hand(image)
    assert _visible_stacked_kongs(vision) == []
    assert stacked_kong_summary({"stacked_kongs": []}) == ""
