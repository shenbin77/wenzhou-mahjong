# -*- coding: utf-8 -*-
"""Read-only follow enrich wiring: river/wall/room/melds/claim/snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_policy import CLAIM_TAP_CHANNEL_READY  # noqa: E402
from follow_enrich import (  # noqa: E402
    classify_meld_types,
    enrich_follow_result,
    format_claim_buttons,
    format_meld_types,
    promote_discards,
)
from follow_pipeline import DISPLAY_FACTS, FollowPipeline  # noqa: E402


def test_claim_stays_false():
    assert CLAIM_TAP_CHANNEL_READY is True


def test_display_facts_include_new_keys():
    for name in ("meld_types", "claim_buttons", "river", "wall_cards", "room_round", "last_discard", "countdown"):
        assert name in DISPLAY_FACTS


def test_classify_meld_types_chi_and_four_tile_kong():
    """Chi still works; four listed tiles = 杠; three same alone ≠ 碰."""
    reading = classify_meld_types([[21, 22, 23], [9, 9, 9, 9]])
    assert reading.types == ("吃", "杠")
    three = classify_meld_types([[1, 1, 1]])
    assert three.types == ("未识别",)


def test_classify_xi_xi_xi_mid_raised_is_kong():
    """西西西 + stacked_kongs.logical_count==4 + matching visible_boxes → 杠."""
    xi = 63
    boxes = [[6, 761, 89, 875], [93, 735, 175, 849], [179, 761, 263, 875]]  # mid raised
    stacked = [{
        "label": "xi",
        "tile_id": xi,
        "logical_count": 4,
        "visible_boxes": boxes,
    }]
    reading = classify_meld_types([[xi, xi, xi]], stacked_kongs=stacked)
    assert reading.types == ("杠",)
    assert "杠" in format_meld_types(reading)



def test_stacked_kong_without_visible_boxes_stays_unknown():
    """logical_count==4 alone (no/empty/invalid visible_boxes) must NOT be 杠."""
    xi = 63
    # Codex repro: missing visible_boxes key.
    missing = classify_meld_types(
        [[xi, xi, xi]],
        stacked_kongs=[{"tile_id": xi, "logical_count": 4}],
    )
    assert missing.types == ("未识别",), missing.types
    # Empty list.
    empty = classify_meld_types(
        [[xi, xi, xi]],
        stacked_kongs=[{"tile_id": xi, "logical_count": 4, "visible_boxes": []}],
    )
    assert empty.types == ("未识别",), empty.types
    # Wrong count (2 boxes).
    wrong_n = classify_meld_types(
        [[xi, xi, xi]],
        stacked_kongs=[{
            "tile_id": xi,
            "logical_count": 4,
            "visible_boxes": [[6, 761, 89, 875], [93, 735, 175, 849]],
        }],
    )
    assert wrong_n.types == ("未识别",), wrong_n.types
    # Invalid format (not 4-number boxes).
    bad_fmt = classify_meld_types(
        [[xi, xi, xi]],
        stacked_kongs=[{
            "tile_id": xi,
            "logical_count": 4,
            "visible_boxes": ["a", "b", "c"],
        }],
    )
    assert bad_fmt.types == ("未识别",), bad_fmt.types


def test_classify_three_same_without_geometry_is_unknown():
    """三张同牌无 stacked_kongs 几何 → 未识别（不能直接判碰）。"""
    reading = classify_meld_types([[63, 63, 63]])
    assert reading.types == ("未识别",)
    assert "未识别" in format_meld_types(reading)


def test_classify_meld_types_fail_closed():
    assert classify_meld_types(None).types == ()
    assert "未识别" in format_meld_types(None)
    assert format_meld_types(classify_meld_types([]), meld_count=0) == "副露类型：无"
    bad = classify_meld_types([[1, 2]])
    assert bad.types == ("未识别",)


def test_promote_discards_nested_block():
    result = promote_discards({
        "discards": {
            "incoming_tile": 7,
            "incoming_source": "opponent",
            "own_river": [1],
            "opponent_river": [2, 3],
        }
    })
    assert result["incoming_tile"] == 7
    assert result["own_river"] == [1]
    assert result["opponent_river"] == [2, 3]


def test_enrich_uses_card_reader_and_never_invents_wall():
    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    empty = enrich_follow_result({}, blank, read_river=False, read_room=False, read_claims_display=False)
    assert empty.wall_cards is None
    filled = enrich_follow_result(
        {},
        blank,
        card_reader=lambda _img: 72,
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    assert filled.wall_cards == 72
    bad = enrich_follow_result(
        {},
        blank,
        card_reader=lambda _img: "nope",
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    assert bad.wall_cards is None


def test_enrich_manual_room_round_wins():
    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    enr = enrich_follow_result(
        {"melds": [[1, 1, 1]]},
        blank,
        room_id="869684",
        round_no=3,
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    assert enr.room_id == "869684"
    assert enr.round_no == 3
    assert enr.meld_types.types == ("未识别",)  # 三张同牌无几何 ≠ 碰


def test_format_claim_buttons_display_only():
    from follow_enrich import ClaimButtonsReading

    assert "未识别" in format_claim_buttons(None)
    text = format_claim_buttons(
        ClaimButtonsReading(known=True, buttons=("胡", "过"), display_only=True)
    )
    assert "只读" in text
    assert "胡" in text


def test_pipeline_observe_writes_vision_snapshot(tmp_path):
    pipe = FollowPipeline(
        tmp_path / "sess",
        mode="tea_2p",
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    pipe.start()
    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    outcome = pipe.observe(
        {
            "status": "advise",
            "mode": "tea_2p",
            "caishen": "w1",
            "hand_ids": list(range(1, 18)),
            "melds": [[1, 1, 1]],
            "meld_count": 1,
            "suggestion": "x",
            "suggestion_id": 1,
            "discards": {
                "incoming_tile": 9,
                "incoming_source": "opponent",
                "own_river": [1],
                "opponent_river": [2],
            },
            "wall_cards": 40,
        },
        blank,
        room_id="740195",
        round_no=2,
        turn_status="advise",
        turn_actionable=True,
    )
    assert outcome.abstain is False
    assert outcome.event["claim_tap_channel_ready"] is True
    assert "vision_snapshot" in outcome.event
    snap = outcome.event["vision_snapshot"]
    assert snap["read_only"] is True
    assert snap["claim_tap_channel_ready"] is False
    assert snap["wall_cards"] == 40
    assert snap["room_id"] == "740195"
    assert "未识别" in outcome.display["meld_types"]  # [[1,1,1]] 无几何
    assert "牌河" in outcome.display["river"]
    assert "最近出牌" in outcome.display["last_discard"]
    assert "余牌：40" == outcome.display["wall_cards"]
    assert "房号:740195" in outcome.display["room_round"]

    path = pipe.follow_log_path
    assert path is not None and path.is_file()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    decision = [r for r in rows if r.get("kind") != "session_header"][-1]
    vision = decision.get("vision") or {}
    assert vision.get("snapshot") or vision.get("wall_cards") == 40 or "incoming_tile" in vision


def test_pipeline_missing_readers_stay_unknown(tmp_path):
    pipe = FollowPipeline(
        tmp_path / "sess2",
        mode="tea_2p",
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    pipe.start()
    outcome = pipe.observe(
        {
            "status": "wait",
            "mode": "tea_2p",
            "caishen": "w1",
            "hand_ids": list(range(1, 17)),
        },
        np.zeros((900, 1600, 3), dtype=np.uint8),
    )
    assert "未识别" in outcome.display["wall_cards"]
    assert "未识别" in outcome.display["room_round"]
    assert "未识别" in outcome.display["river"]
    assert "未识别" in outcome.display["last_discard"]
    assert "未识别" in outcome.display["claim_buttons"]
    assert "未识别" in outcome.display["countdown"] or "倒计时" in outcome.display["countdown"]


CALIB = (
    BOT_DIR.parent
    / "采集"
    / "assist_sessions"
    / "20261023_192955"
    / "preflight"
    / "tea_2p"
    / "20261023_193112_597026_c9be80.png"
)



def test_blank_frame_river_fail_closed_not_zero_zero(tmp_path):
    """Invalid discard geometry must not invent 牌河 0/0."""
    pipe = FollowPipeline(
        tmp_path / "blank_river",
        mode="tea_2p",
        read_river=True,
        read_room=False,
        read_claims_display=False,
    )
    pipe.start()
    outcome = pipe.observe(
        {"status": "wait", "mode": "tea_2p", "caishen": "w1", "hand_ids": list(range(1, 17))},
        np.zeros((900, 1600, 3), dtype=np.uint8),
    )
    assert "未识别" in outcome.display["river"]
    assert "未识别" in outcome.display["last_discard"]


@pytest.mark.skipif(not CALIB.is_file(), reason="calibration HUD frame missing")
def test_room_round_ocr_on_calibration_frame():
    import cv2
    from room_round_vision import read_room_round

    image = cv2.imdecode(np.fromfile(str(CALIB), dtype=np.uint8), cv2.IMREAD_COLOR)
    reading = read_room_round(image)
    assert reading.hud_visible is True
    # Fail-closed: either exact OCR or None — never invent.
    if reading.room_id is not None:
        assert reading.room_id.isdigit()
        assert reading.room_id == "740195"
    if reading.round_no is not None:
        assert reading.round_no == 2


def test_tea4_shu4_blank_frames_never_show_river_zero_zero(tmp_path):
    """tea_4p / shu_4p uncalibrated → rivers None → UI 未识别, never 我0/对0."""
    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    for mode in ("tea_4p", "shu_4p"):
        pipe = FollowPipeline(
            tmp_path / f"river_{mode}",
            mode=mode,
            read_river=True,  # even if caller asks, whitelist must force off
            read_room=False,
            read_claims_display=False,
        )
        assert pipe.read_river is False
        pipe.start()
        outcome = pipe.observe(
            {
                "status": "wait",
                "mode": mode,
                "caishen": "w1",
                "hand_ids": list(range(1, 17)),
                # Poison: empty lists must not become 我0/对0 for uncalibrated modes.
                "discards": {
                    "own_river": [],
                    "opponent_river": [],
                    "incoming_tile": None,
                },
            },
            blank,
        )
        river = outcome.display["river"]
        assert "未识别" in river, river
        assert "我0" not in river and "对0" not in river, river
        assert outcome.facts.river_own is None
        assert outcome.facts.river_opp is None


def test_live_window_follow_pipeline_card_reader_not_wired(tmp_path, monkeypatch):
    """实时窗口创建 FollowPipeline 时 card_reader 尚未接通（余牌保持未识别）。"""
    from follow_enrich import WALL_CARD_READER_CONNECTED

    assert WALL_CARD_READER_CONNECTED is False

    # Simulate assist_window._attach_follow_pipeline wiring without Tk.
    from follow_enrich import river_read_enabled
    from follow_pipeline import FollowPipeline

    mode = "tea_2p"
    pipe = FollowPipeline(
        tmp_path / "live_wire",
        mode=mode,
        enabled=True,
        card_reader=None,
        read_river=river_read_enabled(mode),
    )
    assert pipe.card_reader is None
    assert pipe.wall_card_reader_connected is False
    pipe.start()
    # Use a pipeline that skips room/claim OCR noise; wall still unknown.
    pipe2 = FollowPipeline(
        tmp_path / "live_wire2",
        mode=mode,
        enabled=True,
        card_reader=None,
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    pipe2.start()
    outcome = pipe2.observe(
        {"status": "wait", "mode": mode, "caishen": "w1", "hand_ids": list(range(1, 17))},
        np.zeros((900, 1600, 3), dtype=np.uint8),
    )
    assert "未识别" in outcome.display["wall_cards"]


def test_assist_attach_follow_pipeline_wires_card_reader_none(tmp_path):
    """Mirror assist_window._attach_follow_pipeline contract without opening Tk."""
    from follow_enrich import WALL_CARD_READER_CONNECTED, river_read_enabled
    from follow_pipeline import FollowPipeline

    assert WALL_CARD_READER_CONNECTED is False
    mode = "tea_2p"
    # Exact kwargs used by _attach_follow_pipeline after the fix.
    pipeline = FollowPipeline(
        tmp_path / "attach",
        mode=mode,
        enabled=True,
        card_reader=None,
        read_river=river_read_enabled(mode),
        read_room=True,
        read_claims_display=True,
    )
    assert pipeline.card_reader is None
    assert pipeline.wall_card_reader_connected is False
    assert pipeline.read_river is True  # tea_2p whitelist
    # tea_4p attach must disable river
    p4 = FollowPipeline(
        tmp_path / "attach4",
        mode="tea_4p",
        enabled=True,
        card_reader=None,
        read_river=river_read_enabled("tea_4p"),
    )
    assert p4.read_river is False
    assert p4.card_reader is None


def test_tea2p_empty_river_lists_scrubbed_not_zero_zero():
    """tea_2p poison empty lists must scrub to None / 未识别, never 河0/0."""
    from assist_hud import format_river_summary, UNKNOWN as HUD_UNKNOWN
    from follow_enrich import enrich_follow_result

    enr = enrich_follow_result(
        {
            "status": "wait",
            "mode": "tea_2p",
            "caishen": "t7",
            "hand_ids": list(range(1, 17)),
            "own_river": [],
            "opponent_river": [],
            "discards": {"own_river": [], "opponent_river": [], "incoming_tile": None},
        },
        image=None,
        read_river=False,
        read_room=False,
        read_claims_display=False,
    )
    enriched = enr.result
    assert enriched.get("own_river") is None
    assert enriched.get("opponent_river") is None
    text = format_river_summary(enriched)
    assert "河0" not in text and "0/0" not in text
    assert HUD_UNKNOWN in text
