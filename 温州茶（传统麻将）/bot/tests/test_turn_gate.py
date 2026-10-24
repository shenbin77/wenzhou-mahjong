# -*- coding: utf-8 -*-
"""turn_gate 的离线单元测试：纯函数、零 ADB、零文件写入。

覆盖 team-lead 指定的全部判据：
1. 16 张 → ``wait``（未摸牌/不是出牌态）；
2. 17 张 + 连续两帧一致 → ``discard_ready``；
3. 18 张 / 张数自相矛盾 → ``blocked``；
4. 连续帧手牌变化 → ``wait``；
5. 手牌压暗 → ``wait``；
6. 非 tea_2p → 不放行；
7. 财神未知 / 无建议 → ``wait``；
8. 副露张数守恒（吃碰 3、杠 4、叠杠 3 框 4 张）；
9. 真实采集帧的亮度实测（阈值依据，缺帧则跳过）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from turn_gate import (  # noqa: E402
    MIN_HAND_FACE_MEAN,
    STABLE_FRAMES,
    STATE_BLOCKED,
    STATE_DISCARD_READY,
    STATE_WAIT,
    HandObservation,
    evaluate_turn,
    hand_face_brightness,
    hand_observation,
    known_caishen_id,
    logical_meld_slots,
    meld_total,
)

TEA_ROOT = BOT_DIR.parent
TEMPLATES = TEA_ROOT / "采集" / "templates" / "tea_tiles"
BRIGHT_17 = TEA_ROOT / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"
DIMMED_14 = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
             "live" / "tea_2p" / "20261023_205534_475580_f4a34e.png")
# 实机证据帧（team-lead 2026-10-24 只读截屏）：顶部「牌局结束」+ 中央
# 「多数玩家同意解散比赛！/确定」模态弹窗 + 下方 −20/+20 结算面板 +
# 底部「返回大厅」「分享战绩」。该帧不存在可出牌手牌。
SETTLED_FRAME = (TEA_ROOT / "采集" / "dev_auto_play" / "takeover_live" /
                 "wb_probe_043707.png")
# 同族非对局画面（实测同为「中央大浅色面板」）：
HU_PANEL = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
            "live" / "tea_2p" / "20261023_205820_628578_3698f4.png")
RENEW_MODAL = (TEA_ROOT / "采集" / "assist_sessions" / "20261024_002903" /
               "live" / "tea_2p" / "20261024_003931_190898_b4112e.png")
RECORD_PAGE = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
               "live" / "tea_2p" / "20261023_210406_270753_21df97.png")

BRIGHT_FILL = 235      # 实测正常手牌面 p70(V)=203.6-230.0，属于正常簇
DIMMED_FILL = 100      # 实测「请出牌」整手压暗 p70(V)=65.0-135.6
TABLE_V = 70           # 桌面绿（BGR 40/70/40）：高饱和，不算「亮奶油色」


def frame(fill: int = BRIGHT_FILL) -> np.ndarray:
    """一张像真实对局的 1600x900 假帧。

    上半屏是饱和的绿色桌面，下 160px 是手牌带（V = fill）。**不能**用满屏
    亮色：整块浅色大矩形正是结算/弹窗判据命中的形态，会被（正确地）判成
    非对局画面。
    """
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, TABLE_V, 40)
    image[760:, :] = fill
    return image


def boxes(count: int, *, top: int = 780, left: int = 90,
          pitch: int = 88) -> list[list[int]]:
    """沿手牌区排 count 个互不重叠的框。"""
    return [[left + index * pitch, top, left + index * pitch + 80, top + 108]
            for index in range(count)]


def result_of(hand, *, melds=None, meld_count=0, mode: str = "tea_2p",
              status: str = "advise", caishen: str | None = "w1",
              suggested: bool = True, hand_boxes=None, stacked=None,
              unread: int = 0) -> dict:
    """构造一个 ``analyze_snapshot`` 形状的结果体。

    Args:
        hand: 已识别（accepted）的暗手牌 id。
        unread: 额外有几个**物理牌框没读出牌**（真实链路里是 low_score /
            ambiguous：有框、accepted=False、tile_id=None）。这样 ``hand_ids``
            与 ``vision_diagnostics`` 不会自相矛盾。
    """
    hand = list(hand)
    total = len(hand) + max(0, int(unread))
    hand_boxes = boxes(total) if hand_boxes is None else hand_boxes
    diagnostics = [
        {"tile_id": tile, "box": list(box), "accepted": True,
         "region": "concealed", "label": None, "score": .99, "margin": .2}
        for tile, box in zip(hand, hand_boxes)
    ]
    diagnostics.extend(
        {"tile_id": None, "box": list(box), "accepted": False,
         "region": "concealed", "label": None, "score": .5, "margin": .01,
         "reason": "low_score"}
        for box in hand_boxes[len(hand):total]
    )
    return {
        "status": status,
        "mode": mode,
        "caishen": caishen,
        "hand_ids": hand,
        "melds": [] if melds is None else [list(group) for group in melds],
        "meld_count": meld_count,
        "stacked_kongs": list(stacked or []),
        "suggested_boxes": ([list(hand_boxes[0])] if hand_boxes and suggested else []),
        "suggestion": "4筒" if suggested else None,
        "suggestion_id": 4 if suggested else None,
        "vision_diagnostics": diagnostics,
        "reason": "",
    }


def full_hand() -> list[int]:
    """17 张：暗手满张（2 组 1-9 万 + 3 张筒）。"""
    return [1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8]


def prior_observations(result: dict, *, frames: int = 1, round_seq: int = 1,
                       first_tick: int = 1) -> list[HandObservation]:
    """造出 frames 个紧邻当前帧的历史观测（手牌与本帧一致）。"""
    base = hand_observation(result, round_seq=round_seq, tick=first_tick)
    assert base is not None
    return [HandObservation(tiles=base.tiles, count=base.count,
                            round_seq=round_seq, tick=first_tick + offset)
            for offset in range(frames)]


# ----------------------------------------------------------------------
# 2. 17 张 + 连续两帧一致 → 放行
# ----------------------------------------------------------------------
def test_seventeen_tiles_over_two_stable_frames_is_ready():
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_DISCARD_READY
    assert verdict.ready is True
    assert verdict.evidence["physical_boxes"] == 17      # 守恒按物理牌框数
    assert verdict.evidence["accepted_tiles"] == 17      # 且必须全部读出
    assert verdict.evidence["concealed_count"] == 17     # 兼容别名（= accepted）
    assert verdict.evidence["expected_concealed"] == 17
    assert verdict.evidence["stable_frames"] == STABLE_FRAMES
    assert verdict.evidence["hand_face_mean_p70"] == pytest.approx(BRIGHT_FILL)


def test_first_frame_is_never_released_before_two_stable_frames():
    """单帧证据不足：即使张数与亮度都对，也必须先等一帧。"""
    result = result_of(full_hand())
    verdict = evaluate_turn(result, frame(), [], round_seq=1, tick=1)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["stable_frames"] == 1


# ----------------------------------------------------------------------
# 1. 16 张 → wait
# ----------------------------------------------------------------------
def test_sixteen_tiles_is_wait_not_ready():
    result = result_of(full_hand()[:16])
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert "未摸牌" in verdict.reason


def test_sixteen_minus_melds_is_wait():
    """副露 3 张时，暗手 13 张（=16-3）同样是「不是出牌态」。"""
    result = result_of(full_hand()[:13], melds=[[29, 29, 29]], meld_count=1)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["expected_concealed"] == 14


# ----------------------------------------------------------------------
# 3. 张数自相矛盾 → blocked
# ----------------------------------------------------------------------
def test_eighteen_tiles_is_blocked():
    result = result_of(full_hand() + [9])
    verdict = evaluate_turn(result, frame(), [], round_seq=1, tick=1)

    assert verdict.state == STATE_BLOCKED
    assert "自相矛盾" in verdict.reason


def test_seventeen_tiles_with_melds_is_blocked():
    """已有两副露却还数出 17 张暗手：证据自相矛盾，必须 fail closed。"""
    result = result_of(full_hand(), melds=[[29, 29, 29], [1, 1, 1]], meld_count=2)
    verdict = evaluate_turn(result, frame(), [], round_seq=1, tick=1)

    assert verdict.state == STATE_BLOCKED
    assert verdict.evidence["expected_concealed"] == 11


# ----------------------------------------------------------------------
# 4. 连续帧手牌变化 → wait
# ----------------------------------------------------------------------
def test_changed_hand_between_frames_is_wait():
    current = result_of(full_hand())
    before = result_of([9, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8])
    history = prior_observations(before, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(current, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["stable_frames"] == 1


def test_non_adjacent_frames_do_not_count_as_stable():
    """中间夹了一帧不可读画面：不算「连续」，必须重新攒。"""
    result = result_of(full_hand())
    history = prior_observations(result, frames=1, first_tick=1)

    verdict = evaluate_turn(result, frame(), history, round_seq=1, tick=5)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["stable_frames"] == 1


def test_history_from_previous_round_never_counts():
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES, round_seq=1, first_tick=1)

    verdict = evaluate_turn(result, frame(), history, round_seq=2, tick=3)

    assert verdict.state == STATE_WAIT


# ----------------------------------------------------------------------
# 5. 手牌压暗 → wait
# ----------------------------------------------------------------------
def test_dimmed_hand_is_wait():
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(DIMMED_FILL), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert "压暗" in verdict.reason
    assert verdict.evidence["hand_face_mean_p70"] == pytest.approx(DIMMED_FILL)


def test_brightness_threshold_is_between_measured_clusters():
    """阈值必须落在实测两簇之间：高于最高压暗帧，低于最低正常帧。"""
    assert 135.57 < MIN_HAND_FACE_MEAN < 168.40


def test_missing_boxes_cannot_prove_turn_ownership():
    result = result_of(full_hand(), hand_boxes=[])
    result["vision_diagnostics"] = []
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["hand_face_mean_p70"] is None


# ----------------------------------------------------------------------
# 6. 非 tea_2p / 财神未知 / 无建议 → 一律不放行
# ----------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["tea_4p", "shu_4p", "", None])
def test_non_tea_2p_mode_never_releases(mode):
    result = result_of(full_hand(), mode=mode)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.state != STATE_DISCARD_READY


def test_unknown_caishen_never_releases():
    result = result_of(full_hand(), caishen=None)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.evidence["caishen_id"] is None


@pytest.mark.parametrize("status", ["wait", "abstain", "hu"])
def test_status_without_actionable_advice_is_wait(status):
    result = result_of(full_hand(), status=status)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT


def test_empty_suggested_boxes_is_wait():
    result = result_of(full_hand(), suggested=False)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT


# ----------------------------------------------------------------------
# 8. 副露张数
# ----------------------------------------------------------------------
def test_meld_total_counts_chi_peng_three_and_kong_four():
    result = {"melds": [[1, 1, 1], [29, 29, 29], [21, 22, 23], [5, 5, 5, 5]]}
    assert meld_total(result) == 3 + 3 + 3 + 4


def test_meld_total_counts_stacked_kong_as_four_tiles():
    """可见 3 个框的叠杠实际是 4 张：张数守恒必须按 4 算。"""
    result = {"melds": [[21, 21, 21]], "stacked_kongs": [{"logical_count": 4}]}
    assert meld_total(result) == 4


def test_meld_total_falls_back_to_meld_count():
    assert meld_total({"meld_count": 2}) == 6
    assert meld_total({}) == 0
    assert meld_total({"meld_count": "unknown"}) == 0


def test_eleven_tiles_with_two_melds_is_ready():
    result = result_of(full_hand()[:11], melds=[[29, 29, 29], [1, 1, 1]],
                       meld_count=2)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_DISCARD_READY
    assert verdict.evidence["meld_total"] == 6


def test_known_caishen_accepts_label_or_explicit_id():
    assert known_caishen_id({"caishen": "t2"}) == 22
    assert known_caishen_id({"caishen_id": 29}) == 29
    assert known_caishen_id({"caishen_id": 29, "caishen": "t2"}) == 29
    assert known_caishen_id({"caishen": ""}) is None
    assert known_caishen_id({}) is None


# ----------------------------------------------------------------------
# 9. 真实帧的亮度实测（阈值依据）
# ----------------------------------------------------------------------
def _real_boxes(path: Path) -> tuple[np.ndarray, list[list[int]]]:
    from tile_vision import TileVision

    if not path.is_file():
        pytest.skip(f"实测帧缺失: {path}")
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    vision = TileVision(template_dir=TEMPLATES)
    _, boxes_found = vision.count_tiles(image)
    return image, [list(box) for box in boxes_found]


def test_real_dimmed_frame_measures_below_threshold():
    """「请出牌」整手压暗实战帧：实测均值 116.29，必须低于阈值。"""
    image, hand_boxes = _real_boxes(DIMMED_14)
    measured = hand_face_brightness(image, hand_boxes)

    assert measured is not None
    assert measured == pytest.approx(116.29, abs=0.5)
    assert measured < MIN_HAND_FACE_MEAN


def test_real_bright_frame_measures_above_threshold():
    """正常出牌态实战帧：实测均值 214.00，必须高于阈值。"""
    image, hand_boxes = _real_boxes(BRIGHT_17)
    measured = hand_face_brightness(image, hand_boxes)

    assert measured is not None
    assert measured == pytest.approx(214.0, abs=0.5)
    assert measured > MIN_HAND_FACE_MEAN


# ----------------------------------------------------------------------
# 10. 结算 / 牌局结束 / 模态弹窗 → 永远不放行（实机证据帧）
# ----------------------------------------------------------------------
def _load(path: Path) -> np.ndarray:
    if not path.is_file():
        pytest.skip(f"证据帧缺失: {path}")
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"证据帧无法解码: {path}"
    return image


def test_settled_frame_is_blocked_and_can_never_be_ready():
    """`wb_probe_043707`：牌局结束 + 解散弹窗 + 结算面板 → 必须 blocked。"""
    image = _load(SETTLED_FRAME)
    result = result_of(full_hand())          # 故意给满 17 张 + 完整建议
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, image, history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_BLOCKED
    assert verdict.state != STATE_DISCARD_READY
    assert verdict.code == "gate_settled"
    assert "牌局已结束" in verdict.reason
    assert set(verdict.evidence["settled_signals"]) == {"central_panel",
                                                        "bottom_blackout"}
    assert verdict.evidence["panel_area_pct"] == pytest.approx(14.19, abs=0.5)
    assert verdict.evidence["bottom_mean_v"] == pytest.approx(23.29, abs=0.5)
    assert verdict.evidence["frame_mean_v"] == pytest.approx(67.0, abs=1.0)


def test_settled_frame_already_blocked_on_the_very_first_frame():
    """第一帧就要挡住——不能靠"稳定帧不足"这种副作用碰巧不出手。"""
    image = _load(SETTLED_FRAME)
    result = result_of(full_hand())

    verdict = evaluate_turn(result, image, [], round_seq=1, tick=1)

    assert verdict.state == STATE_BLOCKED
    assert verdict.evidence["stable_frames"] == 1     # 稳定帧确实还不够（wait 才会因它）


def test_settled_frame_blocks_even_without_any_hand_evidence():
    """弹窗压下来时手牌可能仍被数出 16/17 张；没有张数证据也一样要挡。"""
    image = _load(SETTLED_FRAME)
    result = {"status": "advise", "mode": "tea_2p",
              "suggested_boxes": [[90, 780, 170, 888]], "caishen": "w1"}

    verdict = evaluate_turn(result, image, [], round_seq=1, tick=1)

    assert verdict.state == STATE_BLOCKED
    assert verdict.code == "gate_settled"


@pytest.mark.parametrize("path", [SETTLED_FRAME, HU_PANEL, RENEW_MODAL, RECORD_PAGE])
def test_non_playable_screens_never_release(path):
    """同族非对局画面（胡面板 / 申请续桌 / 比赛战绩）同样不得放行。"""
    image = _load(path)
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, image, history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state != STATE_DISCARD_READY


def test_single_signal_frame_is_wait_not_blocked():
    """只命中 1 条特征（胡面板：仅中央面板）→ 记 wait，但仍绝不放行。

    "≥2 条才判 blocked" 是为了不误伤；这类画面由 wait 兜住，永不出手。
    """
    image = _load(HU_PANEL)
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, image, history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.evidence["settled_signals"] == ["central_panel"]
    assert verdict.state == STATE_WAIT
    assert "疑似结算" in verdict.reason


def test_ordinary_game_frames_are_not_mistaken_for_settled():
    """正常出牌态 / 整手压暗 / 合成对局帧都不得触发结算判据。"""
    from turn_gate import detect_settled_screen

    for image in (frame(), frame(DIMMED_FILL), _load(BRIGHT_17), _load(DIMMED_14)):
        hit, evidence = detect_settled_screen(image)
        assert hit is False, evidence
        assert evidence["settled_signals"] == []

    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)
    assert evaluate_turn(result, frame(), history,
                         round_seq=1, tick=STABLE_FRAMES).state == STATE_DISCARD_READY


def test_settled_thresholds_sit_between_the_measured_clusters():
    """阈值必须落在实测两簇之间：正常帧最高 < 阈值 < 弹窗帧最低。"""
    from turn_gate import (REQUIRED_SETTLED_SIGNALS, SETTLED_DARK_BOTTOM_BELOW,
                           SETTLED_PANEL_MIN_PCT)

    assert 2.79 < SETTLED_PANEL_MIN_PCT < 6.28
    assert 36.14 < SETTLED_DARK_BOTTOM_BELOW < 60.00
    assert REQUIRED_SETTLED_SIGNALS == 2


def test_unverified_frame_size_cannot_claim_settled_but_still_waits():
    """尺寸未验证时不硬判结算，但也不会放行（亮度不可测 → wait）。"""
    from turn_gate import detect_settled_screen

    odd = np.full((600, 900, 3), 235, dtype=np.uint8)
    hit, evidence = detect_settled_screen(odd)
    assert hit is False and evidence["settled_unknown"] is True

    result = result_of(full_hand(), hand_boxes=[[10, 10, 60, 90]])
    verdict = evaluate_turn(result, odd, [], round_seq=1, tick=1)
    assert verdict.state != STATE_DISCARD_READY


def test_real_normal_frame_still_reaches_discard_ready():
    """真实正常对局帧：加了结算判据后依然可以放行（证明没误伤）。"""
    image, hand_boxes = _real_boxes(BRIGHT_17)
    assert len(hand_boxes) == 17
    result = result_of(full_hand(), hand_boxes=hand_boxes)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, image, history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.evidence["settled_signals"] == []
    assert verdict.state == STATE_DISCARD_READY
    assert verdict.code == ""
    assert verdict.evidence["hand_face_mean_p70"] == pytest.approx(214.0, abs=0.5)


# ----------------------------------------------------------------------
# 11. 张数守恒改用"物理框数"：没读全必须 wait，绝不许 blocked
# ----------------------------------------------------------------------
def test_advise_with_unread_tiles_is_wait_not_blocked():
    """17 个牌框、只读出 15 张：这是"没读全"→ wait，绝不能硬停机。"""
    result = result_of(full_hand()[:15], unread=2)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.state != STATE_BLOCKED
    assert verdict.state != STATE_DISCARD_READY
    assert "有牌未读出" in verdict.reason
    assert verdict.evidence["physical_boxes"] == 17
    assert verdict.evidence["accepted_tiles"] == 15
    assert verdict.evidence["expected_concealed"] == 17


def test_advise_with_dimmed_hand_is_wait_with_dimming_reason():
    """压暗 + 读不全同时成立：reason 必须是"压暗"，不能被误标成张数问题。"""
    result = result_of(full_hand()[:12], unread=5)      # 17 框 / 12 accepted
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(106), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert "压暗" in verdict.reason
    assert "张数" not in verdict.reason
    assert verdict.evidence["physical_boxes"] == 17
    assert verdict.evidence["accepted_tiles"] == 12


def test_advise_with_full_read_but_dimmed_is_wait_on_brightness():
    """17 框 / 17 accepted 但亮度 100（压暗）→ wait，且 reason 是压暗。"""
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(DIMMED_FILL), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert "压暗" in verdict.reason
    assert verdict.evidence["accepted_tiles"] == 17
    assert verdict.evidence["physical_boxes"] == 17


def test_advise_with_eighteen_boxes_is_blocked():
    """18 个牌框（真矛盾，差 1 张也不该出现）→ blocked。"""
    result = result_of(full_hand(), unread=1)
    verdict = evaluate_turn(result, frame(), [], round_seq=1, tick=1)

    assert verdict.state == STATE_BLOCKED
    assert verdict.evidence["physical_boxes"] == 18
    assert verdict.evidence["accepted_tiles"] == 17


def test_advise_with_one_meld_and_fourteen_boxes_is_ready():
    """1 组吃（3 张）+ 14 张暗手 = 17 → discard_ready。"""
    result = result_of(full_hand()[:14], melds=[[21, 22, 23]], meld_count=1)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_DISCARD_READY
    assert verdict.evidence["physical_boxes"] == 14
    assert verdict.evidence["accepted_tiles"] == 14
    assert verdict.evidence["expected_concealed"] == 14


def test_advise_with_one_meld_and_two_unread_is_wait():
    """1 组吃 + 14 框 / 12 accepted → wait（没读全），绝不是 blocked。"""
    result = result_of(full_hand()[:12], melds=[[21, 22, 23]], meld_count=1,
                       unread=2)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert verdict.state != STATE_BLOCKED
    assert "有牌未读出" in verdict.reason
    assert (verdict.evidence["physical_boxes"],
            verdict.evidence["accepted_tiles"]) == (14, 12)


def test_sixteen_boxes_with_meld_is_wait_on_tile_shortage():
    """1 组吃 + 13 框（=14-1）→ wait「未摸牌」，与"读不全"区分开。"""
    result = result_of(full_hand()[:13], melds=[[21, 22, 23]], meld_count=1)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)

    assert verdict.state == STATE_WAIT
    assert "未摸牌" in verdict.reason


# ----------------------------------------------------------------------
# 12. 真实链路回归：QA 点名的两帧必须是 wait（不许 blocked）
# ----------------------------------------------------------------------
REAL_PARTIAL_FRAMES = (
    TEA_ROOT / "采集" / "2026-10-15" / "room_p2a" / "r01" / "frame_02.png",
    TEA_ROOT / "采集" / "2026-10-15" / "424821" / "r03" / "frame_02.png",
)


@pytest.mark.parametrize("path", REAL_PARTIAL_FRAMES)
def test_real_partial_hand_frames_are_wait_via_real_pipeline(path):
    """真实 analyze_snapshot + 真实帧：识别不全只许 wait，绝不许 blocked。"""
    if not path.is_file():
        pytest.skip(f"回归帧缺失: {path}")
    from assist_tea import analyze_snapshot

    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    result = analyze_snapshot(path, caishen_label="", confirmed_two_player=True)

    verdict = evaluate_turn(result, image, [], round_seq=1, tick=1)

    assert verdict.state == STATE_WAIT, verdict.reason
    assert verdict.state != STATE_BLOCKED
    assert verdict.state != STATE_DISCARD_READY
    assert verdict.evidence["physical_boxes"] is not None
    assert verdict.evidence["accepted_tiles"] is not None
    # 这两帧就是"没读全/不是出牌态"，证据要能自证
    assert (verdict.evidence["accepted_tiles"]
            <= verdict.evidence["physical_boxes"]
            or verdict.evidence["physical_boxes"] == 0)


def test_kong_uses_logical_slots_for_expected_concealed():
    """P0-2: 4-face kong still occupies 3 logical slots => expected 14, not 13."""
    result = result_of(full_hand()[:14], melds=[[29, 29, 29, 29]], meld_count=1)
    history = prior_observations(result, frames=STABLE_FRAMES - 1)
    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)
    assert verdict.evidence["meld_total"] == 4
    assert verdict.evidence["logical_meld_slots"] == 3
    assert verdict.evidence["expected_concealed"] == 14


def test_logical_meld_slots_counts_each_group_as_three():
    assert logical_meld_slots({"melds": [[1, 1, 1], [2, 2, 2, 2]]}) == 6
    assert logical_meld_slots({"meld_count": 2}) == 6
    assert logical_meld_slots({}) == 0


def test_claim_discs_block_discard_ready(monkeypatch):
    """P0-3: visible claim discs => wait, never discard_ready."""
    result = result_of(full_hand())
    history = prior_observations(result, frames=STABLE_FRAMES - 1)

    def fake_ui(image):
        return {
            "action_discs_seen": True,
            "action_disc_count": 2,
            "action_discs": [{"kind": "gold", "box": [1, 2, 3, 4], "center": [2, 3]}],
            "claim_decision": None,
            "claim_reason": "test",
            "countdown_status": "unknown",
            "countdown_seconds": None,
            "mode_source": "unavailable",
            "deck_source": "unavailable",
        }

    monkeypatch.setattr("turn_gate.action_ui_evidence", fake_ui)
    verdict = evaluate_turn(result, frame(), history,
                            round_seq=1, tick=STABLE_FRAMES)
    assert verdict.state == STATE_WAIT
    assert verdict.code == "gate_claim_ui"
    assert "行动圆盘" in verdict.reason
