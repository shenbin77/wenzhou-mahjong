"""Historical-frame checks for the discard action guard."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_guard import detect_action_ui  # noqa: E402

CAPTURE = BOT_DIR.parent / "采集" / "assist_sessions"


def load(relative: str):
    path = CAPTURE / relative
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


@pytest.mark.parametrize("relative", [
    "20261023_235121/live/tea_2p/20261024_001334_883101_e40ece.png",
    "20261023_223209/live/tea_2p/20261023_223514_816068_ad1347.png",
    "20261023_235121/live/tea_2p/20261024_001041_972605_40a23d.png",
    "20261023_201640/live/tea_2p/20261023_205442_192224_8bef75.png",
    "20261023_235121/live/tea_2p/20261024_001052_355565_28411b.png",
    "20261024_002903/live/tea_2p/20261024_004731_660563_152d48.png",
])
def test_action_frames_block_discard(relative):
    result = detect_action_ui(load(relative))
    assert result.state == "action_visible"
    assert result.blocks_discard and result.boxes


@pytest.mark.parametrize("relative", [
    "20261023_235121/live/tea_2p/20261024_001132_169189_1b9a51.png",
    "20261023_235121/live/tea_2p/20261023_235126_427878_8ef13c.png",
])
def test_waiting_frames_allow_discard_pipeline(relative):
    result = detect_action_ui(load(relative))
    assert result.state == "clear"
    assert not result.blocks_discard


def test_invalid_frame_is_uncertain_and_blocks():
    result = detect_action_ui(np.zeros((10, 10, 3), dtype=np.uint8))
    assert result.state == "uncertain"
    assert result.blocks_discard

