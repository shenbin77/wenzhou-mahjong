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
