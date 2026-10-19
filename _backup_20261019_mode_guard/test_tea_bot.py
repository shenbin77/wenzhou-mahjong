"""温州茶 Bot 的离线接线与拒绝路径。"""
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tea_bot import TeaBot, offline_replay_test  # noqa: E402


WINNING_HAND = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61, 61]
DISCARD_HAND = WINNING_HAND[:-1] + [62]


def _png():
    ok, encoded = cv2.imencode(".png", np.zeros((32, 32, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def test_engine_import_and_winning_hand_stay_offline(tmp_path):
    bot = TeaBot(vision_fn=lambda _: WINNING_HAND, log_dir=str(tmp_path))
    summary = bot.play_one_round(screenshot=b"offline")
    assert summary["hu"] is True
    assert summary["final_shanten"] == -1
    assert summary["decisions"][0]["action"] == "hu"


def test_wait_then_discard_is_advice_only(tmp_path):
    bot = TeaBot(vision_fn=lambda _: WINNING_HAND[:-1], log_dir=str(tmp_path))
    wait = bot.play_one_round(screenshot=b"offline")
    assert wait["decisions"][0]["action"] == "wait"
    bot.vision_fn = lambda _: DISCARD_HAND
    discard = bot.play_one_round(screenshot=b"offline")
    assert discard["decisions"][0]["action"] == "discard"
    assert discard["decisions"][0]["tile"] in DISCARD_HAND


def test_white_tile_is_converted_to_engine_id(tmp_path):
    visual_hand = WINNING_HAND[:-2] + [67]
    bot = TeaBot(vision_fn=lambda _: visual_hand, log_dir=str(tmp_path))
    assert bot.see(b"offline")[-1] == 73


def test_rejects_live_mode_and_missing_offline_screenshot(tmp_path):
    with pytest.raises(ValueError, match="dry_run"):
        TeaBot(dry_run=False, log_dir=str(tmp_path))
    bot = TeaBot(vision_fn=lambda _: WINNING_HAND, log_dir=str(tmp_path))
    summary = bot.play_one_round()
    assert "未提供离线截图" in summary["error"]
    assert summary["decisions"] == []


@pytest.mark.parametrize("hand", [WINNING_HAND[:-2], [1] * 5 + WINNING_HAND[5:],
                                        [55] + WINNING_HAND[1:]])
def test_invalid_or_incomplete_hand_never_reaches_decision(tmp_path, hand):
    bot = TeaBot(vision_fn=lambda _: hand, log_dir=str(tmp_path))
    summary = bot.play_one_round(screenshot=b"offline")
    assert "error" in summary
    assert summary["decisions"] == []


@pytest.mark.parametrize("complete, melds, allowed", [
    (True, [], True), (False, [], False), (True, [(0, 0, 1, 1)], False),
])
def test_default_vision_checks_completeness_and_melds(
    tmp_path, monkeypatch, complete, melds, allowed
):
    seen = {}

    class FakeVision:
        def __init__(self, template_dir):
            seen["template_dir"] = Path(template_dir)
            self.last_complete = complete
            self.last_meld_boxes = melds

        def recognize_hand(self, image):
            assert image.shape == (32, 32, 3)
            return [(tile, 0, 0, 1, 1) for tile in WINNING_HAND]

    monkeypatch.setitem(sys.modules, "tile_vision", types.SimpleNamespace(TileVision=FakeVision))
    bot = TeaBot(log_dir=str(tmp_path))
    if allowed:
        assert bot.see(_png()) == WINNING_HAND
    else:
        with pytest.raises(ValueError, match="识牌不完整|明牌组"):
            bot.see(_png())
    assert seen["template_dir"].parts[-3:] == ("采集", "templates", "tea_tiles")


def test_offline_replay_counts_rejected_frame_as_failure(tmp_path):
    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir()
    (screenshot_dir / "frame.png").write_bytes(_png())
    report = offline_replay_test(
        str(screenshot_dir), vision_fn=lambda _: WINNING_HAND[:-2],
        log_dir=str(tmp_path / "logs"),
    )
    assert report["total"] == 1
    assert report["ok"] == 0
    assert report["fail"] == 1
    assert report["sample_errors"]


def test_tea_templates_keep_real_suit_labels_on_archived_two_player_frame():
    from tile_vision import TileVision

    tea_root = BOT_DIR.parent
    vision = TileVision(template_dir=tea_root / "采集" / "templates" / "tea_tiles")
    expected = {
        "frame_02.png": ["w2", "w4", "w5", "w7", "s1", "s1", "s3",
                         "s6", "s6", "s7", "s7", "t1", "t2", "t8",
                         "t8", "t8", "t5"],
        "frame_03.png": ["w4", "w5", "w7", "s1", "s1", "s3", "s6",
                         "s6", "s7", "s7", "t1", "t2", "t5", "t8",
                         "t8", "t8", "s4"],
    }
    for filename, labels in expected.items():
        frame = tea_root / "采集" / "2026-10-16" / "740195" / "r08" / filename
        image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        recognized = vision.recognize_hand(image)
        assert vision.detection_diagnostics["concealed_count"] == 17
        assert len(recognized) == 17 and vision.last_complete
        assert [d["label"] for d in vision.diagnostics] == labels


@pytest.mark.parametrize(
    ("round_name", "expected"),
    [
        ("r05", "w1 w2 w4 w4 w5 s2 s2 s5 s6 s6 s7 s8 s9 t1 t7 t8 t2"),
        ("r10", "w1 w2 w2 s1 s3 s6 s6 s7 s8 s9 bai t5 t7 dong xi xi dong"),
    ],
)
def test_other_two_player_rounds_match_visually_checked_hand(round_name, expected):
    """Independent 2-player replay hands, read left-to-right from saved pixels."""
    from tile_vision import TileVision

    tea_root = BOT_DIR.parent
    frame = tea_root / "采集" / "2026-10-15" / "424821" / round_name / "frame_02.png"
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    vision = TileVision(template_dir=tea_root / "采集" / "templates" / "tea_tiles")
    vision.recognize_hand(image)
    assert vision.detection_diagnostics["concealed_count"] == 17
    assert vision.last_complete
    assert [d["label"] for d in vision.diagnostics] == expected.split()


def test_dimmed_two_player_replay_hand_abstains():
    """A 2-player hand on the opponent's turn must not become a confident hand."""
    from tile_vision import TileVision

    tea_root = BOT_DIR.parent
    frame = tea_root / "采集" / "2026-10-15" / "424821" / "r08" / "frame_02.png"
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    vision = TileVision(template_dir=tea_root / "采集" / "templates" / "tea_tiles")
    vision.recognize_hand(image)
    assert vision.detection_diagnostics["concealed_count"] == 17
    assert not vision.last_complete
    assert any(not d["accepted"] for d in vision.diagnostics)


def test_detect_hand_uses_tea_templates_and_abstains_on_dimmed_frame():
    from tile_vision import TILE_IDS, detect_hand

    tea_root = BOT_DIR.parent
    bright = tea_root / "采集" / "2026-10-15" / "424821" / "r05" / "frame_02.png"
    dimmed = tea_root / "采集" / "2026-10-15" / "424821" / "r08" / "frame_02.png"
    labels = "w1 w2 w4 w4 w5 s2 s2 s5 s6 s6 s7 s8 s9 t1 t7 t8 t2".split()

    assert detect_hand(bright.read_bytes()) == [TILE_IDS[label] for label in labels]
    assert detect_hand(dimmed.read_bytes()) == []
    assert detect_hand(b"") == []


def test_detect_hand_abstains_on_replay_hand_with_multiple_exposed_groups():
    """A raised left group must not be counted as a complete closed tea hand."""
    from tile_vision import TileVision, detect_hand

    tea_root = BOT_DIR.parent
    frame = tea_root / "采集" / "2026-10-16" / "740195" / "r10" / "frame_01.png"
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    vision = TileVision(template_dir=tea_root / "采集" / "templates" / "tea_tiles")
    recognized = vision.recognize_hand(image)
    assert len(vision.last_meld_boxes) >= 3
    assert len(recognized) < 16
    assert detect_hand(frame.read_bytes()) == []


def test_detect_hand_requires_sixteen_or_seventeen_concealed_tiles(monkeypatch):
    """A complete classifier result can still be an invalid tea hand size."""
    import tile_vision

    class FifteenTileVision:
        def __init__(self, template_dir):
            self.last_complete = True
            self.last_meld_boxes = []

        def recognize_hand(self, image):
            return [(1, 0, 0, 1, 1)] * 15

    monkeypatch.setattr(tile_vision, "TileVision", FifteenTileVision)
    assert tile_vision.detect_hand(_png()) == []
