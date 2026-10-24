"""Frozen tea_2p gold frames and the first offline vision baseline."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BOT_DIR.parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tile_vision import TILE_IDS, TileVision  # noqa: E402

GOLD_DIR = BOT_DIR / "gold"
GOLD = json.loads((GOLD_DIR / "tea_2p_manifest.json").read_text(encoding="utf-8"))
BASELINE = json.loads(
    (GOLD_DIR / "tea_2p_vision_baseline.json").read_text(encoding="utf-8")
)
ID_TO_LABEL = {tile_id: label for label, tile_id in TILE_IDS.items()}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _frame_by_path(document: dict, frame_path: str) -> dict:
    return next(frame for frame in document["frames"] if frame["frame_path"] == frame_path)


@pytest.mark.parametrize(
    ("frame", "expected_count"), list(zip(GOLD["frames"], (16, 17, 16)))
)
def test_frozen_gold_frame_hash_mode_and_count(frame, expected_count):
    path = PROJECT_ROOT / Path(frame["frame_path"])
    assert frame["mode"] == "tea_2p"
    assert path.is_file()
    assert _sha256(path) == frame["sha256"]
    assert sum(frame["annotated_hand_multiset"].values()) == expected_count


@pytest.mark.parametrize("frame", GOLD["frames"])
def test_offline_vision_matches_recorded_baseline(frame):
    path = PROJECT_ROOT / Path(frame["frame_path"])
    expected = _frame_by_path(BASELINE, frame["frame_path"])
    vision = TileVision(
        template_dir=PROJECT_ROOT / "温州茶（传统麻将）" / "采集" / "templates" / "tea_tiles"
    )
    result = vision.recognize_hand(_read(path))
    predicted = [ID_TO_LABEL[tile_id] for tile_id, *_ in result]

    gold = Counter(frame["annotated_hand_multiset"])
    observed = Counter(predicted)
    correct = sum((gold & observed).values())
    difference = {
        "correct": correct,
        "wrong": len(predicted) - correct,
        "missed": sum(gold.values()) - correct,
    }

    assert len(vision.diagnostics) == expected["detected_boxes"]
    assert predicted == expected["predicted_hand"]
    assert len(predicted) == expected["accepted"]
    assert vision.last_complete is expected["complete"]
    assert difference == expected["difference"]
