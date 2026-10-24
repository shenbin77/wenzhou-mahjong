"""``discard_vision`` 的离线测试：读不出来必须弃权，真帧必须读对，归属必须判对。

两组证据，分工明确：

* **真帧测试**（``采集/assist_sessions/20261023_201640/live/tea_2p``，真局语料，
  不是合成帧）证明这一层在真实像素上确实能读出"谁刚打了哪张"；
* **纯函数测试**（手工构造 :class:`TableSnapshot`）把每一条弃权分支都走一遍 ——
  弃权是这一层的主要安全属性，光靠真帧覆盖不全（有些分支一年才遇到一次）。

这里不点击任何东西，也不写任何生产目录：``read_discards`` 是纯函数。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from discard_vision import (  # noqa: E402
    SOURCE_OPPONENT,
    SOURCE_SELF,
    RiverReading,
    RiverTile,
    TableSnapshot,
    evaluate_change,
    forget_discards,
    read_discards,
    read_discards_sequential,
    snapshot_table,
)
from tile_vision import TILE_IDS  # noqa: E402

SESSION = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
           / "live" / "tea_2p")
VISION_IDS = set(TILE_IDS.values())


# ---------------------------------------------------------------------------
# 真帧工具
# ---------------------------------------------------------------------------


def load_frame(name: str) -> np.ndarray:
    """按文件名读一帧真机画面（中文路径必须走 imdecode）。"""
    path = SESSION / name
    if not path.is_file():
        pytest.skip(f"真局语料缺失：{path}")
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2] == (900, 1600), name
    return image


def read_pair(before: str, after: str):
    """用 ``before`` 那帧当基线，读 ``after`` 那帧的来牌。"""
    return read_discards(load_frame(after), load_frame(before))


# ---------------------------------------------------------------------------
# Fail-closed：读不出来就是 None
# ---------------------------------------------------------------------------


def test_blank_frame_abstains_instead_of_guessing() -> None:
    blank = np.zeros((900, 1600, 3), np.uint8)
    state = read_discards(blank)
    assert state.incoming_tile is None
    assert state.incoming_source is None
    assert state.river_tiles == ()
    assert state.reason, "弃权必须说明原因，供人工复核"


def test_wrong_frame_size_abstains() -> None:
    small = np.zeros((600, 800, 3), np.uint8)
    state = read_discards(small)
    assert state.incoming_tile is None
    assert "帧尺寸" in state.reason


def test_none_image_abstains() -> None:
    state = read_discards(None)  # type: ignore[arg-type]
    assert state.incoming_tile is None
    assert state.reason


def test_ad_or_lobby_frame_is_rejected_before_river_parsing() -> None:
    """不是牌桌画面就不许进牌河解析（广告动画曾被当成倒计时那次事故）。"""
    image = np.zeros((900, 1600, 3), np.uint8)
    image[:, :] = (60, 120, 60)
    table = snapshot_table(image)
    assert table.opponent.valid is False
    assert table.own.valid is False
    assert "牌桌" in table.opponent.reason


def test_single_frame_reads_river_but_withholds_incoming() -> None:
    """单帧只知"谁是最新"，不知"是不是刚打出"，所以来牌必须留空。"""
    image = load_frame("20261023_205429_571102_99209e.png")
    state = read_discards(image)
    assert state.incoming_tile is None
    assert "前后两帧" in state.reason
    # 牌河读数本身仍然给出来，供人工复核：这一帧对手牌河有 3 张。
    assert state.table is not None
    assert state.table.opponent.valid is True
    assert state.table.opponent.count == 3
    assert state.table.opponent.newest is not None


# ---------------------------------------------------------------------------
# 真帧：已知的那一张必须读对
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "before, after, expected_label, expected_id",
    [
        # 对手第一张弃牌：牌河从 0 张变 1 张，落在最右一格。
        ("20261023_205407_139587_491a9f.png",
         "20261023_205409_557426_31fea1.png", "xi", 63),
        # 对手第 4 张弃牌，落在第 3 格。
        ("20261023_205431_651989_577fbe.png",
         "20261023_205433_718038_eae185.png", "w9", 9),
        # 中间隔着好几格的弃牌，证明不是"位置碰巧"。
        ("20261023_205600_608275_b1ebee.png",
         "20261023_205602_653365_521506.png", "nan", 62),
    ],
)
def test_real_discard_is_read_with_label_and_id(before: str, after: str,
                                                expected_label: str,
                                                expected_id: int) -> None:
    state = read_pair(before, after)
    assert state.incoming_source == SOURCE_OPPONENT
    assert state.incoming_tile == expected_id
    assert state.evidence["newest"]["label"] == expected_label
    # 认对的来牌分数都很高：实测 0.88–0.98。
    assert state.evidence["newest"]["score"] >= 0.85


def test_incoming_tile_is_in_vision_vocabulary() -> None:
    """来牌编码必须与 ``hand_ids`` 同口径，否则上层转码会静默出错。"""
    state = read_pair("20261023_205407_139587_491a9f.png",
                      "20261023_205409_557426_31fea1.png")
    assert state.incoming_tile in VISION_IDS


def test_own_discard_is_attributed_to_self_and_never_claims() -> None:
    """自己牌河长大 = 我刚出牌，不是可鸣的来牌。"""
    state = read_pair("20261023_205413_976425_687dac.png",
                      "20261023_205416_235541_2051cd.png")
    assert state.incoming_tile is None
    assert state.incoming_source == SOURCE_SELF
    assert "你自己刚打出" in state.reason


def test_discard_removed_by_claim_is_not_reported_as_incoming() -> None:
    """对手那张被鸣走后从牌河消失，此时不能把它当成来牌。"""
    state = read_pair("20261023_205532_504131_8ffe2e.png",
                      "20261023_205534_475580_f4a34e.png")
    assert state.incoming_tile is None
    assert "鸣走" in state.reason


def test_river_tiles_expand_to_both_rivers_and_stay_in_vocabulary() -> None:
    state = read_pair("20261023_205431_651989_577fbe.png",
                      "20261023_205433_718038_eae185.png")
    assert state.opponent_river, "对手牌河应该读出牌来"
    assert set(state.opponent_river) <= VISION_IDS
    assert set(state.own_river) <= VISION_IDS
    assert state.river_tiles == state.opponent_river + state.own_river


def test_evidence_is_jsonable() -> None:
    """台账要能直接落盘，所以证据里不许有 numpy 标量 / ndarray。"""
    import json

    state = read_pair("20261023_205431_651989_577fbe.png",
                      "20261023_205433_718038_eae185.png")
    json.dumps(state.as_dict(), ensure_ascii=False)
    assert state.table is not None
    json.dumps(state.table.as_dict(), ensure_ascii=False)


def test_previous_state_can_be_reused_as_baseline() -> None:
    """活循环里每帧只解一次像素：上一帧的 DiscardState 直接当基线。"""
    before = load_frame("20261023_205431_651989_577fbe.png")
    after = load_frame("20261023_205433_718038_eae185.png")
    state = read_discards(after, read_discards(before))
    assert state.incoming_tile == 9
    assert state.evidence["newest"]["label"] == "w9"


# ---------------------------------------------------------------------------
# 纯函数分支：手工构造读数，把每条弃权都走一遍
# ---------------------------------------------------------------------------


def tile(index: int, label: str, tile_id: int | None, *, x: int = 0,
         accepted: bool = True, score: float = 0.95,
         margin: float = 0.30) -> RiverTile:
    return RiverTile(index=index, box=(x, 231, x + 55, 306), tile_id=tile_id,
                     label=label, score=score, margin=margin,
                     accepted=accepted, reason="accepted" if accepted else "low_score")


def river(name: str, tiles: tuple[RiverTile, ...], *, far_x: int = 900,
          valid: bool = True, reason: str = "") -> RiverReading:
    anchor = 1249 if name == SOURCE_OPPONENT else 351
    ordered = tuple(sorted(tiles, key=lambda t: t.box[0]))
    return RiverReading(name=name, tiles=ordered, slots=len(ordered) + 1,
                        pitch=56.15, anchor_x=anchor, far_x=far_x,
                        valid=valid, reason=reason)


def table(opponent: RiverReading, own: RiverReading,
          sparkles: tuple[int, ...] = ()) -> TableSnapshot:
    return TableSnapshot(opponent=opponent, own=own, player_count=2,
                         sparkles=sparkles)


def opponent_three() -> RiverReading:
    return river(SOURCE_OPPONENT, (
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1082)


def own_empty() -> RiverReading:
    return RiverReading(name=SOURCE_SELF, slots=0, anchor_x=351, valid=True)


def test_append_reports_the_outermost_tile() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(3, "w9", 9, x=1026),
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1026), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile == 9
    assert state.incoming_source == SOURCE_OPPONENT


def test_both_sides_discarding_is_ambiguous_and_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(
        river(SOURCE_OPPONENT, (
            tile(3, "w9", 9, x=1026),
            tile(0, "xi", 63, x=1194),
            tile(1, "dong", 61, x=1138),
            tile(2, "t9", 29, x=1082),
        ), far_x=1026),
        river(SOURCE_SELF, (tile(0, "w1", 1, x=351),), far_x=406),
    )
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "归属无法确认" in state.reason


def test_filling_a_hole_is_not_an_append() -> None:
    """张数变多但外框没往外伸 = 补了空洞，最新一张不在端点，报出去就是错的。"""
    previous = table(river(SOURCE_OPPONENT, (
        tile(0, "xi", 63, x=1194),
        tile(2, "t9", 29, x=1082),
    ), far_x=1082), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1082), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "空洞" in state.reason


def test_unrecognised_newest_tile_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(3, "??", None, x=1026, accepted=False, score=0.40, margin=0.02),
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1026), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert state.incoming_source == SOURCE_OPPONENT, "认不出牌也要记下是谁打的"
    assert "没认出来" in state.reason


def test_low_confidence_newest_tile_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(3, "w9", 9, x=1026, score=0.84, margin=0.30),
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1026), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "分数不够稳" in state.reason


def test_invalid_geometry_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (tile(0, "xi", 63, x=1194),),
                          far_x=1194, valid=False, reason="牌河中间断了 2 格"),
                    own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "牌河几何不可信" in state.reason


def test_missing_baseline_never_guesses() -> None:
    state = evaluate_change(table(opponent_three(), own_empty()), None)
    assert state.incoming_tile is None
    assert "前后两帧" in state.reason


def test_sparkle_marker_must_agree_with_the_grid() -> None:
    """闪光标是游戏画的"这张刚落下"，与网格结论冲突时必须弃权。"""
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(3, "w9", 9, x=1026),
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1026), own_empty(), sparkles=(1000,))
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "闪光标" in state.reason


def test_sparkle_marker_that_matches_is_accepted() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(river(SOURCE_OPPONENT, (
        tile(3, "w9", 9, x=1026),
        tile(0, "xi", 63, x=1194),
        tile(1, "dong", 61, x=1138),
        tile(2, "t9", 29, x=1082),
    ), far_x=1026), own_empty(), sparkles=(1053,))
    state = evaluate_change(current, previous)
    assert state.incoming_tile == 9
    assert state.evidence["sparkles"] == [1053]


def test_no_change_between_frames_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(opponent_three(), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "没有新弃牌" in state.reason


def test_row_cleared_between_frames_abstains() -> None:
    previous = table(opponent_three(), own_empty())
    current = table(RiverReading(name=SOURCE_OPPONENT, slots=0, anchor_x=1249,
                                 valid=True), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "清空" in state.evidence["opponent_change"]


def test_unusable_baseline_row_abstains() -> None:
    previous = table(river(SOURCE_OPPONENT, (), valid=False, reason="空带"),
                     own_empty())
    current = table(opponent_three(), own_empty())
    state = evaluate_change(current, previous)
    assert state.incoming_tile is None
    assert "不可比对" in state.reason


# ---------------------------------------------------------------------------
# 活循环用的"上一帧"登记处
# ---------------------------------------------------------------------------
# ``read_discards_sequential`` 是唯一带状态的函数（登记"上一帧"）。每个用例
# 都用自己的 key 并在结尾清掉，避免用例之间互相喂基线。


@pytest.fixture()
def registry_key() -> str:
    """给每个用例一个独立 key，用完即清。"""
    key = f"test-{uuid.uuid4().hex}"
    yield key
    forget_discards(key)


def test_sequential_first_frame_withholds_second_reads(registry_key: str) -> None:
    """登记处的核心承诺：调用方一行不改，第二帧就拿到来牌。"""
    before = load_frame("20261023_205407_139587_491a9f.png")
    after = load_frame("20261023_205409_557426_31fea1.png")
    first = read_discards_sequential(before, key=registry_key)
    assert first.incoming_tile is None
    assert "前后两帧" in first.reason, "首帧没有基线，只能给牌河不能给来牌"
    second = read_discards_sequential(after, key=registry_key)
    assert second.incoming_tile == 63
    assert second.incoming_source == SOURCE_OPPONENT


def test_sequential_does_not_register_unreadable_frames(registry_key: str) -> None:
    """广告/结算帧不写进登记处，否则会把下一帧的差分带偏。"""
    before = load_frame("20261023_205407_139587_491a9f.png")
    after = load_frame("20261023_205409_557426_31fea1.png")
    read_discards_sequential(before, key=registry_key)
    junk = read_discards_sequential(np.zeros((900, 1600, 3), np.uint8),
                                    key=registry_key)
    assert junk.incoming_tile is None
    assert junk.table is not None and not junk.table.opponent.valid
    # 垃圾帧之后仍与上一张牌桌帧比对，所以来牌照样读得出来。
    good = read_discards_sequential(after, key=registry_key)
    assert good.incoming_tile == 63


def test_sequential_keys_are_independent(registry_key: str) -> None:
    """两条流水线（两个会话/两台设备）不能互相喂基线。"""
    other = f"{registry_key}-other"
    try:
        before = load_frame("20261023_205407_139587_491a9f.png")
        read_discards_sequential(before, key=registry_key)
        state = read_discards_sequential(
            load_frame("20261023_205409_557426_31fea1.png"), key=other)
        assert state.incoming_tile is None, "另一个 key 没有基线，绝不能猜"
        assert "前后两帧" in state.reason
    finally:
        forget_discards(other)


def test_forget_discards_clears_the_registry(registry_key: str) -> None:
    before = load_frame("20261023_205407_139587_491a9f.png")
    read_discards_sequential(before, key=registry_key)
    forget_discards(registry_key)
    state = read_discards_sequential(
        load_frame("20261023_205409_557426_31fea1.png"), key=registry_key)
    assert state.incoming_tile is None
    assert "前后两帧" in state.reason


def test_explicit_previous_is_used_instead_of_the_registry(registry_key: str) -> None:
    """调用方自己管基线时，传进来的那个说话。"""
    baseline = snapshot_table(load_frame("20261023_205431_651989_577fbe.png"))
    state = read_discards_sequential(
        load_frame("20261023_205433_718038_eae185.png"),
        key=registry_key, previous=baseline)
    assert state.incoming_tile == 9


# ---------------------------------------------------------------------------
# 端到端：analyze_snapshot -> HandState.incoming_tile
# ---------------------------------------------------------------------------


def test_analyze_snapshot_feeds_hand_state_incoming(tmp_path) -> None:
    """最后一公里：快照返回体里的 ``discards`` 必须真能填进 ``HandState``。"""
    assist_tea = pytest.importorskip("assist_tea")
    action_policy = pytest.importorskip("action_policy")

    frame_dir = tmp_path / "logs"
    before = assist_tea.analyze_snapshot(
        SESSION / "20261023_205407_139587_491a9f.png", caishen_label="",
        confirmed_two_player=True, log_dir=frame_dir)
    assert "discards" in before, "每个返回分支都必须带 discards"
    assert before["discards"]["incoming_tile"] is None

    after = assist_tea.analyze_snapshot(
        SESSION / "20261023_205409_557426_31fea1.png", caishen_label="",
        confirmed_two_player=True, log_dir=frame_dir)
    assert after["discards"]["incoming_tile"] == 63
    assert after["discards"]["incoming_source"] == SOURCE_OPPONENT

    hand = action_policy.HandState.from_analysis(after)
    assert hand.incoming_tile is not None, "来牌必须真的传到决策闸门，不能是 None"
    assert hand.incoming_tile in VISION_IDS or hand.incoming_tile >= 61


def test_analyze_snapshot_discards_are_jsonable(tmp_path) -> None:
    """快照结果要落盘成台账（``json.dump``），所以不能夹带 numpy 标量。"""
    import json

    assist_tea = pytest.importorskip("assist_tea")
    result = assist_tea.analyze_snapshot(
        SESSION / "20261023_205431_651989_577fbe.png", caishen_label="",
        confirmed_two_player=True, log_dir=tmp_path / "logs")
    json.dumps(result, ensure_ascii=False)
    assert set(result["discards"]) >= {"incoming_tile", "incoming_source",
                                       "river_tiles", "reason", "evidence"}


def test_four_player_snapshot_does_not_read_the_river(tmp_path) -> None:
    """牌河几何只在 2 人布局上标定过，4 人局不许假装有来牌。"""
    assist_tea = pytest.importorskip("assist_tea")
    result = assist_tea.analyze_snapshot(
        SESSION / "20261023_205431_651989_577fbe.png", caishen_label="",
        confirmed_four_player=True, log_dir=tmp_path / "logs")
    assert result["discards"]["incoming_tile"] is None
    assert "2 人" in result["discards"]["reason"]
