"""Melded win advice uses the same validated shape as claim L2, without clicks."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from action_policy import ActionPolicyConfig, HandState, evaluate_claim_candidates
from assist_tea import analyze_snapshot
from discard_vision import snapshot_table
from tea_bot import TeaBot

FIXTURES = Path(__file__).parent / "fixtures" / "tea2_melded_hu"
RON = "20261027_020226_207971_before_f7e6b4.png"
PREVIOUS = "20261027_020223_972223_before_9e5984.png"
SELF_DRAW = "20261027_020712_698703_before_8b941d.png"
RON_HAND = [28, 6, 7, 8, 43, 43, 47, 48, 24, 25]
RON_MELDS = [[3, 4, 5], [25, 26, 27]]


def image(name):
    path = FIXTURES / name
    assert path.is_file(), f"Required native regression fixture missing: {path}"
    return cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)


@pytest.fixture
def bot(tmp_path):
    instance = TeaBot(caishen_id=28, log_dir=str(tmp_path))
    instance.meld_groups = [list(group) for group in RON_MELDS]
    yield instance
    for handler in list(instance.logger.handlers):
        instance.logger.removeHandler(handler)
        handler.close()


def test_native_melded_self_draw_reaches_hu_advice(tmp_path):
    result = analyze_snapshot(
        FIXTURES / SELF_DRAW, caishen_label="", confirmed_two_player=True,
        log_dir=tmp_path)
    assert result["status"] == "hu"
    assert result["shanten"] == -1
    assert result["suggestion_id"] is None
    assert result["melds"] == [[43, 44, 45], [41, 42, 43]]
    assert result["dry_run"] is True
    assert "计分未验证" in result["reason"]
    assert result["hu_info"]["scoring_validated"] is False


def test_native_melded_ron_requires_real_two_frame_discard_evidence(tmp_path):
    previous = snapshot_table(image(PREVIOUS))
    assert previous.opponent.valid and previous.own.valid
    result = analyze_snapshot(
        FIXTURES / RON, caishen_label="", confirmed_two_player=True,
        previous_discards=previous, log_dir=tmp_path)
    assert result["discards"]["incoming_tile"] == 46
    assert result["discards"]["incoming_source"] == "opponent"
    assert result["status"] == "hu"
    assert result["suggestion_id"] is None
    assert "已确认对手来牌" in result["reason"]
    hand = HandState.from_analysis(result)
    assert evaluate_claim_candidates(hand, ActionPolicyConfig())["hu"]["feasible"]


@pytest.mark.parametrize("source,tile", [(None, 46), ("self", 46),
                                        ("opponent", None), ("opponent", 61)])
def test_unconfirmed_or_nonwinning_incoming_keeps_wait(bot, source, tile):
    thought = bot.think(RON_HAND, incoming_tile=tile, incoming_source=source)
    assert not thought.is_hu
    assert thought.discard is None
    assert thought.shanten == 0


def test_melded_ron_does_not_invent_pattern_score(bot):
    thought = bot.think(RON_HAND, incoming_tile=46, incoming_source="opponent")
    assert thought.is_hu
    assert thought.hu_info["win_source"] == "opponent"
    assert thought.hu_info["scoring_validated"] is False
    assert "multiplier" not in thought.hu_info
    assert thought.hand == RON_HAND
    assert bot.act(thought)["dry_run"] is True


def test_incoming_cannot_fill_a_missing_concealed_tile(bot):
    with pytest.raises(ValueError, match="暗手应为"):
        bot.think(RON_HAND[:-1], incoming_tile=46, incoming_source="opponent")


def test_white_kong_keeps_engine_encoding_and_logical_slot_contract(bot):
    bot.meld_groups = [[67, 67, 67, 67]]
    hand = [1, 2, 3, 4, 5, 6, 21, 22, 23, 41, 41, 41, 61, 61]
    thought = bot.think(hand)
    assert thought.is_hu
    assert bot.meld_groups == [[67, 67, 67, 67]]
    invalid = list(hand)
    invalid[-1] = 73
    with pytest.raises(ValueError, match="最多4张"):
        bot.think(invalid)


def test_confirmed_incoming_still_cannot_make_a_fifth_copy(bot):
    bot.meld_groups = [[67, 67, 67]]
    hand = [73, 1, 2, 3, 4, 5, 6, 21, 22, 23, 41, 41, 61]
    with pytest.raises(ValueError, match="最多4张"):
        bot.think(hand, incoming_tile=73, incoming_source="opponent")


@pytest.mark.parametrize("same_frame", [False, True])
def test_native_ron_without_a_new_discard_stays_wait(tmp_path, same_frame):
    previous = snapshot_table(image(RON)) if same_frame else None
    result = analyze_snapshot(
        FIXTURES / RON, caishen_label="", confirmed_two_player=True,
        previous_discards=previous, log_dir=tmp_path)
    assert result["discards"]["incoming_tile"] is None
    assert result["status"] == "wait"
    assert result["suggestion_id"] is None
