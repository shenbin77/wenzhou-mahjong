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

from tea_bot import TeaBot, offline_replay_test, vision_to_engine  # noqa: E402
from tile_vision import TILE_IDS  # noqa: E402
from brain_v0.engine.tea.shanten import (  # noqa: E402
    best_discard_by_efficiency, calc_shanten, calc_ting_tiles,
)


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


def test_sixteen_tile_non_ready_hand_is_not_labelled_ready(tmp_path):
    hand = [9, 21, 22, 22, 24, 25, 29, 41, 43, 46, 48, 48, 63, 63, 64, 73]
    bot = TeaBot(caishen_id=21, log_dir=str(tmp_path))
    thought = bot.think(hand)
    assert thought.shanten > 0
    assert thought.discard is None
    assert "听牌" not in thought.reason
    assert "等待摸牌" in thought.reason


def test_white_tile_is_converted_to_engine_id(tmp_path):
    visual_hand = WINNING_HAND[:-2] + [67]
    bot = TeaBot(vision_fn=lambda _: visual_hand, log_dir=str(tmp_path))
    assert bot.see(b"offline")[-1] == 73


def test_white_exposed_meld_is_translated_at_engine_boundary(tmp_path):
    hand = [5, 2, 2, 7, 9, 42, 44, 45, 47, 48, 25, 25, 28, 29]
    bot = TeaBot(vision_fn=lambda _: hand, caishen_id=5, log_dir=str(tmp_path))
    bot.meld_groups = [[67, 67, 67]]
    assert bot.see(b"offline") == hand
    assert bot.think(hand).discard in hand
    # Public melds stay in vision encoding for the rest of the UI/auto loop.
    assert bot.meld_groups == [[67, 67, 67]]


def test_white_meld_and_concealed_tiles_still_obey_four_tile_limit(tmp_path):
    hand = [67, 67, 2, 7, 9, 42, 44, 45, 47, 48, 25, 25, 28, 29]
    bot = TeaBot(vision_fn=lambda _: hand, caishen_id=5, log_dir=str(tmp_path))
    bot.meld_groups = [[67, 67, 67]]
    with pytest.raises(ValueError, match="最多4张"):
        bot.see(b"offline")


def test_real_white_peng_frame_reaches_advice_with_visual_melds(tmp_path):
    from assist_tea import analyze_snapshot
    frame = (BOT_DIR.parent / "采集" / "dev_auto_play" /
             "ui_20261027_203046_emulator5556_烘熊瞎" / "frames" /
             "20261027_204212_111706_before_cf8a94.png")
    analysis = analyze_snapshot(frame, caishen_label="", confirmed_two_player=True,
                                log_dir=tmp_path)
    assert analysis["status"] == "advise"
    assert analysis["melds"] == [[67, 67, 67]]
    assert len(analysis["hand_ids"]) == 14


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
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _: 2,
        detect_tea_no_zhong_fa=lambda _: True))
    bot = TeaBot(log_dir=str(tmp_path))
    if allowed:
        assert bot.see(_png()) == WINNING_HAND
    else:
        with pytest.raises(ValueError, match="识牌不完整|明牌组"):
            bot.see(_png())
    assert seen["template_dir"].parts[-3:] == ("采集", "templates", "tea_tiles")


def test_one_exposed_triplet_reduces_concealed_hand_size_and_keeps_advice_offline(
        tmp_path, monkeypatch):
    # A confirmed exposed East triplet leaves 13/14 concealed tiles.
    complete = [4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 62, 62]
    turn = complete[:-1] + [63]
    assert calc_shanten(complete, meld_count=1) == -1
    assert calc_shanten(complete[:-1], meld_count=1) == 0
    assert 62 in calc_ting_tiles(complete[:-1], meld_count=1)
    assert best_discard_by_efficiency(turn, meld_count=1)

    class ExposedEastVision:
        def __init__(self, template_dir):
            self.last_complete = True
            self.last_meld_boxes = [(0, 0, 1, 1)] * 3
            self.last_melds = [(61, 0, 0, 1, 1)] * 3

        def recognize_hand(self, image):
            return [(tile, 0, 0, 1, 1) for tile in turn]

    monkeypatch.setitem(sys.modules, "tile_vision", types.SimpleNamespace(
        TileVision=ExposedEastVision, TILE_IDS=TILE_IDS))
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _: 2,
        detect_tea_no_zhong_fa=lambda _: True))
    bot = TeaBot(log_dir=str(tmp_path))
    assert bot.see(_png()) == turn
    assert bot.meld_count == 1
    assert bot.meld_groups == [[61, 61, 61]]
    thought = bot.think(turn)
    assert thought.discard in turn
    assert bot.act(thought)["dry_run"] is True


def test_two_exposed_triplets_leave_ten_or_eleven_concealed_tiles(
        tmp_path, monkeypatch):
    complete = [4, 5, 6, 21, 22, 23, 41, 41, 41, 62, 62]
    turn = complete[:-1] + [63]
    assert calc_shanten(complete, meld_count=2) == -1
    assert calc_shanten(complete[:-1], meld_count=2) == 0

    class TwoMeldVision:
        def __init__(self, template_dir):
            self.last_complete = True
            self.last_meld_boxes = [(0, 0, 1, 1)] * 6
            self.last_melds = [(tile, 0, 0, 1, 1)
                               for tile in [29, 29, 29, 1, 1, 1]]

        def recognize_hand(self, image):
            return [(tile, 0, 0, 1, 1) for tile in turn]

    monkeypatch.setitem(sys.modules, "tile_vision", types.SimpleNamespace(
        TileVision=TwoMeldVision, TILE_IDS=TILE_IDS))
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _: 2,
        detect_tea_no_zhong_fa=lambda _: True))
    bot = TeaBot(log_dir=str(tmp_path))
    assert bot.see(_png()) == turn
    assert bot.meld_count == 2
    assert bot.meld_groups == [[29, 29, 29], [1, 1, 1]]
    assert bot.think(turn).discard in turn


def test_tea_rule_gate_accepts_visible_no_zhong_fa_and_abstains_when_obscured(tmp_path):
    from mode_vision import (
        LATCH_SOURCE_LATCHED,
        detect_tea_no_zhong_fa,
        detect_tea_player_count,
        reset_tea_mode_latch,
    )

    replay = BOT_DIR.parent / "采集" / "2026-10-19"
    clear = [
        replay / "833667" / "r01" / "frame_15s.png",
        replay / "984226" / "r01" / "room984226_round1_3d58eb17_2.png",
    ]
    obscured = replay / "984226" / "r02" / "candidates" / "candidate_054.png"
    for path in clear:
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        assert detect_tea_no_zhong_fa(image)

    for path, players in ((clear[1], 2),
                          (BOT_DIR.parent / "采集" / "2026-10-13" /
                           "323028" / "r01" / "frame_02.png", 4)):
        titled = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        assert detect_tea_player_count(titled) == players
        titled[140:190, 300:470] = 130
        assert detect_tea_player_count(titled) is None
        assert not detect_tea_no_zhong_fa(titled)

    hidden = cv2.imdecode(np.fromfile(str(clear[1]), np.uint8), cv2.IMREAD_COLOR)
    hidden[140:190, 960:1110] = 130
    assert detect_tea_player_count(hidden) == 2
    assert not detect_tea_no_zhong_fa(hidden)
    real_occlusion = cv2.imdecode(np.fromfile(str(obscured), np.uint8), cv2.IMREAD_COLOR)
    assert detect_tea_player_count(real_occlusion) == 2
    assert not detect_tea_no_zhong_fa(real_occlusion)

    # 进程级 DECK_LATCH 会被其它用例弄脏；未确认过时遮挡必须 fail-closed。
    reset_tea_mode_latch(reason="test_tea_rule_gate_fresh")
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="未确认无中发规则"):
        bot.see(cv2.imencode(".png", hidden)[1].tobytes())

    # 玩法/牌组门禁粘滞后：按钮帧遮住「无中发」横幅不得整帧弃权（与 TeaDeckLatch 契约对齐）。
    clear_img = cv2.imdecode(np.fromfile(str(clear[1]), np.uint8), cv2.IMREAD_COLOR)
    reset_tea_mode_latch(reason="test_tea_rule_gate_latch")
    bot2 = TeaBot(log_dir=str(tmp_path / "latched"))
    players, deck, ui = bot2._read_tea_rules(
        clear_img, detect_tea_player_count, detect_tea_no_zhong_fa)
    assert players == 2 and deck is True
    players2, deck2, ui2 = bot2._read_tea_rules(
        hidden, detect_tea_player_count, detect_tea_no_zhong_fa)
    assert players2 == 2 and deck2 is True
    assert ui2.get("deck_source") == LATCH_SOURCE_LATCHED
    assert ui2.get("deck_latched") is True


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


def test_offline_replay_four_player_uses_its_own_mode_gate(tmp_path):
    frame = (BOT_DIR.parent / "采集" / "2026-10-13" / "323028" /
             "r01" / "frame_02.png")
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / frame.name).write_bytes(frame.read_bytes())
    caishen_id = vision_to_engine(TILE_IDS["s6"])
    four = offline_replay_test(
        str(shots), caishen_id=caishen_id, player_count=4,
        log_dir=str(tmp_path / "logs4"),
    )
    two = offline_replay_test(
        str(shots), caishen_id=caishen_id, player_count=2,
        log_dir=str(tmp_path / "logs2"),
    )
    assert four["ok"] == 1 and four["fail"] == 0
    assert two["ok"] == 0 and two["fail"] == 1


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
    import mode_vision
    monkeypatch.setattr(mode_vision, "detect_tea_player_count", lambda _: 2)
    assert tile_vision.detect_hand(_png()) == []


@pytest.mark.parametrize(("path", "players"), [
    ("2026-10-16/740195/r08/frame_01.png", 2),
    ("2026-10-15/room_p2a/r05/frame_02.png", 2),
    ("2026-10-19/833667/r01/frame_20s.png", 2),
    ("2026-10-13/323028/r01/frame_01.png", 4),
    ("2026-10-13/207547/r01/frame_01.png", 4),
])
def test_replay_rule_text_identifies_player_count(path, players):
    from mode_vision import detect_tea_player_count

    image = cv2.imdecode(np.fromfile(str(BOT_DIR.parent / "采集" / path), np.uint8),
                         cv2.IMREAD_COLOR)
    assert image is not None
    assert detect_tea_player_count(image) == players


def test_tea_two_player_entrypoints_reject_four_player_replay(tmp_path):
    from mode_vision import detect_tea_player_count
    from tile_vision import TileVision, detect_hand

    frame = BOT_DIR.parent / "采集" / "2026-10-13" / "207547" / "r05" / "frame_03.png"
    data = frame.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert detect_tea_player_count(image) == 4
    vision = TileVision(template_dir=BOT_DIR.parent / "采集" / "templates" / "tea_tiles")
    assert len(vision.recognize_hand(image)) in (16, 17)
    assert vision.last_complete
    assert detect_hand(data) == []
    with pytest.raises(ValueError, match="4 人局"):
        TeaBot(log_dir=str(tmp_path)).see(data)


def test_unknown_player_count_abstains(tmp_path):
    from mode_vision import detect_tea_player_count
    from tile_vision import detect_hand

    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    ok, png = cv2.imencode(".png", image)
    assert ok and detect_tea_player_count(image) is None
    assert detect_hand(png.tobytes()) == []
    with pytest.raises(ValueError, match="未确认茶 2 人局"):
        TeaBot(log_dir=str(tmp_path)).see(png.tobytes())

    obscured = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r01" / "frame_15s.png"
    image = cv2.imdecode(np.fromfile(str(obscured), np.uint8), cv2.IMREAD_COLOR)
    assert detect_tea_player_count(image) is None
    assert detect_hand(obscured.read_bytes()) == []


def test_new_two_player_replay_round_two_has_complete_bright_hand():
    from tile_vision import detect_hand

    frame = (BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r02"
             / "frame_04s_complete.png")
    assert detect_hand(frame.read_bytes()) == [
        3, 5, 8, 41, 42, 43, 44, 44, 44, 45, 48, 48, 23, 26, 28, 64,
    ]
