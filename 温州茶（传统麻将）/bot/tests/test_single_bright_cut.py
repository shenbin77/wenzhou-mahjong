# -*- coding: utf-8 -*-
"""P0 单亮牌摸切检测器回归：证据帧必须命中、负样本必须不命中。

背景：听牌后轮到我方摸切时游戏整手压暗、只亮刚摸的那一张，既有链路
（analyze 读不全 + turn_gate 只看整手平均亮度）必然弃权卡死，951441
第 2 局与 014353 第 2 局两次靠人工点亮牌解开。本文件锁死
single_bright_cut 的五点严规格：

1. 证据帧 capture_5556（17 框、16 暗 100.0 + 亮框南 214.0）必须命中，
   且 tap_point 恒为亮牌框中心；真实连续帧对（play7_5556 020539/020541，
   亮牌白板）经 tracker 必须在第 2 帧确认；
2. 负样本全不许命中：胡提示面板帧（central_panel 单信号）、结算帧
   （双信号）、行动圆盘帧、16 框单亮图案帧（张数不守恒）、2 亮框压暗
   帧、正常手牌的镜像图案（1 暗 + 多亮）；
3. 上下文缺一不可：财神未知 / 非 tea_2p / result 缺失一律不命中；
4. tracker 纪律：不命中清零、亮牌位置或牌面变了重新计数。

第二组锁死「多亮白名单」（亮簇 1..N 张）：实机反馈亮牌可能是 1 张、2 张
甚至更多，放宽后仍必须「物理框守恒 + 暗簇内聚 + 亮暗双峰」，且必须先把
财神位从亮簇剔除；1 亮 / 2 亮 / 3 亮 / 全暗 / 全亮 / 张数不符 / 财神位
在亮簇里 各有明确断言，真实多亮帧（wb_live/5558_t4，南+北）必须命中。
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from single_bright_cut import (  # noqa: E402
    BRIGHT_SUBSET_GATE, BrightSubsetCut, BrightSubsetCutTracker,
    SingleBrightCut, SingleBrightCutTracker, box_face_values,
    bright_subset_pattern, brightness_pattern, detect_bright_subset_cut,
    detect_single_bright_cut)
from tile_vision import TileVision  # noqa: E402
from turn_gate import concealed_boxes, hand_face_brightness  # noqa: E402

TEA_ROOT = BOT_DIR.parent
MAJIANG_ROOT = TEA_ROOT.parent
TEMPLATES = TEA_ROOT / "采集" / "templates" / "tea_tiles"
TAKEOVER = MAJIANG_ROOT / "_takeover"
WB_LIVE = TAKEOVER / "wb_live"
MULTI_BRIGHT_FRAME = WB_LIVE / "5558_t4.png"

EVIDENCE = (MAJIANG_ROOT / "_archive" / "vision-probes-20261023-24"
            / "capture_5556.png")
PAIR_A = (TAKEOVER / "play7_5556" / "frames"
          / "20261025_020539_153114_before_8df0c1.png")
PAIR_B = (TAKEOVER / "play7_5556" / "frames"
          / "20261025_020541_438790_before_e327f7.png")
PANEL_PLAY6 = (TAKEOVER / "play6_5556" / "frames"
               / "20261025_015859_367619_before_f948b6.png")
PANEL_PLAY3 = (TAKEOVER / "play3_5556" / "frames"
               / "20261025_014201_407831_before_6c1e83.png")
CLAIM_FRAME = TAKEOVER / "claim_emulator-5556.png"
MIRROR_FRAME = (TAKEOVER / "play7_5556" / "frames"
                / "20261025_020629_595177_before_069f11.png")
DIMMED_14 = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640"
             / "live" / "tea_2p" / "20261023_205534_475580_f4a34e.png")
DIMMED_16 = TEA_ROOT / "采集" / "2026-10-15" / "424821" / "r08" / "frame_01.png"
SETTLED_FRAME = (TEA_ROOT / "采集" / "dev_auto_play" / "takeover_live"
                 / "wb_probe_043707.png")


def _image(path):
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, "帧缺失: %s" % path
    return image


def _result(image, caishen_label, melds=None):
    """按 assist_tea._recording_vision 的同一口径构造 analyze 返回体。"""
    vision = TileVision(template_dir=TEMPLATES)
    vision.recognize_hand(image, caishen_label=caishen_label)
    diagnostics = [dict(d) for d in vision.diagnostics]
    hand_ids = [int(d["tile_id"]) for d in diagnostics
                if d.get("accepted") and d.get("tile_id") is not None]
    groups = melds or []
    return {"mode": "tea_2p", "status": "abstain", "caishen": caishen_label,
            "vision_diagnostics": diagnostics, "hand_ids": hand_ids,
            "melds": groups, "meld_count": len(groups)}


@pytest.fixture(scope="module")
def evidence():
    image = _image(EVIDENCE)
    return image, _result(image, "t5")


@pytest.fixture(scope="module")
def pair():
    image_a, image_b = _image(PAIR_A), _image(PAIR_B)
    return (image_a, _result(image_a, "dong")), (image_b, _result(image_b, "dong"))


@pytest.fixture(scope="module")
def panel_frames():
    image6, image3 = _image(PANEL_PLAY6), _image(PANEL_PLAY3)
    return [(image6, _result(image6, "bai")), (image3, _result(image3, "t5"))]


@pytest.fixture(scope="module")
def claim_frame():
    image = _image(CLAIM_FRAME)
    return image, _result(image, "bai")


@pytest.fixture(scope="module")
def mirror_frame():
    # 该帧逐框亮度为 [100, 214×13]：1 个暗框（财神角标展示位）+ 13 个亮框。
    # 补一组副露让应有张数 = 14，好让判定走到亮度图案这一关被拒，
    # 而不是提前死在张数守恒上。
    image = _image(MIRROR_FRAME)
    return image, _result(image, "dong", melds=[[61, 61, 61]])


@pytest.fixture(scope="module")
def dimmed_frames():
    image14, image16 = _image(DIMMED_14), _image(DIMMED_16)
    return (image14, _result(image14, "t3")), (image16, _result(image16, None))


@pytest.fixture(scope="module")
def settled_frame():
    image = _image(SETTLED_FRAME)
    return image, _result(image, None)


# ---------------------------------------------------------------- 命中侧

def test_evidence_frame_hits_the_bright_nan(evidence):
    """951441 证据帧：17 框 16 暗 + 亮框南，必须命中且只给亮牌坐标。"""
    image, result = evidence
    verdict = detect_single_bright_cut(image, result)
    assert verdict.hit, verdict.reason
    assert verdict.bright_index == 15
    assert verdict.bright_box == (1390, 768, 1480, 892)
    assert verdict.tap_point == (1435, 830)
    assert verdict.tile_label == "nan"
    assert verdict.tile_id == 62
    assert verdict.confirmed is False  # 单帧直出不许执行（规格第 3 条）
    assert verdict.evidence["bright_count"] == 1
    assert verdict.evidence["dim_mean"] == 100.0
    assert verdict.evidence["gap"] == 114.0


def test_box_values_share_turn_gate_scale(evidence):
    """逐框亮度与 turn_gate.hand_face_brightness 必须是同一把尺。"""
    image, result = evidence
    boxes = concealed_boxes(result)
    values = box_face_values(image, boxes)
    assert None not in values
    assert float(np.mean(values)) == pytest.approx(
        hand_face_brightness(image, boxes))


def test_verdict_ledger_is_json_serialisable(evidence):
    image, result = evidence
    verdict = detect_single_bright_cut(image, result)
    json.dumps(verdict.ledger(), ensure_ascii=False)


def test_tracker_confirms_evidence_on_second_frame(evidence):
    image, result = evidence
    tracker = SingleBrightCutTracker()
    first = tracker.feed(image, result)
    assert first.hit and not first.confirmed and first.streak == 1
    second = tracker.feed(image, result)
    assert second.confirmed and second.streak == 2
    assert second.tap_point == first.tap_point


def test_real_consecutive_pair_confirms_bai(pair):
    """014353 第 2 局真实连续帧对：亮牌白板，第 2 帧确认。"""
    (image_a, result_a), (image_b, result_b) = pair
    tracker = SingleBrightCutTracker()
    first = tracker.feed(image_a, result_a)
    assert first.hit, first.reason
    assert first.tile_label == "bai" and not first.confirmed
    second = tracker.feed(image_b, result_b)
    assert second.confirmed and second.tile_label == "bai"
    assert second.bright_index == first.bright_index == 14


# ---------------------------------------------------------------- 负样本

def test_hint_panel_frames_never_hit(panel_frames):
    """胡提示面板帧带 central_panel 单信号，必须在前置排除出局。"""
    for image, result in panel_frames:
        verdict = detect_single_bright_cut(image, result)
        assert not verdict.hit, verdict.reason
        assert "信号命中" in verdict.reason


def test_settlement_frame_never_hits(settled_frame):
    image, result = settled_frame
    verdict = detect_single_bright_cut(image, result, caishen_label="t5")
    assert not verdict.hit
    assert "信号命中" in verdict.reason


def test_claim_discs_visible_never_hit(claim_frame):
    """吃/过圆盘在场时归行动路径，单亮牌检测不许抢。"""
    image, result = claim_frame
    verdict = detect_single_bright_cut(image, result)
    assert not verdict.hit
    assert "圆盘" in verdict.reason


def test_dimmed_16_frame_rejected_by_box_conservation(dimmed_frames):
    """16 框 + 单亮图案与证据帧一模一样，靠物理框守恒排除（未摸牌）。"""
    _, (image, result) = dimmed_frames
    verdict = detect_single_bright_cut(image, result, caishen_label="t5")
    assert not verdict.hit
    assert "物理框 16" in verdict.reason


def test_two_bright_dimmed_frame_never_hits(dimmed_frames):
    """14 框含 2 个亮面：恰-1 亮不成立（且张数也不守恒）。"""
    (image, result), _ = dimmed_frames
    verdict = detect_single_bright_cut(image, result)
    assert not verdict.hit


def test_normal_mirror_pattern_never_hits(mirror_frame):
    """正常手牌的镜像图案（1 暗 + 13 亮）必须死在亮度图案这一关。"""
    image, result = mirror_frame
    verdict = detect_single_bright_cut(image, result)
    assert not verdict.hit
    assert verdict.evidence.get("bright_count") == 13


# ---------------------------------------------------------------- 上下文门

def test_unknown_caishen_never_hits(evidence):
    image, result = evidence
    verdict = detect_single_bright_cut(image, dict(result, caishen=None))
    assert not verdict.hit
    assert "财神未知" in verdict.reason


def test_wrong_mode_never_hits(evidence):
    image, result = evidence
    verdict = detect_single_bright_cut(image, dict(result, mode="tea_4p"))
    assert not verdict.hit


def test_missing_inputs_never_hit(evidence):
    image, _ = evidence
    assert not detect_single_bright_cut(image, None).hit
    assert not detect_single_bright_cut(None, {"mode": "tea_2p"}).hit
    assert not detect_single_bright_cut(image, {}).hit


# ---------------------------------------------------------------- 图案单元

@pytest.mark.parametrize("values,expected", [
    ([100.0] * 16 + [214.0], 16),
    ([214.0] + [100.0] * 13, 0),
])
def test_pattern_hits_single_bright(values, expected):
    index, stats = brightness_pattern(values)
    assert index == expected
    assert stats["bright_count"] == 1


@pytest.mark.parametrize("values", [
    [214.0] * 17,                          # 全亮：没有暗簇
    [100.0] * 17,                          # 全暗：没有亮框
    [100.0] * 15 + [214.0] * 2,            # 两个亮框
    [60.0, 140.0] + [100.0] * 14 + [214.0],  # 暗簇极差 80 > 40，不内聚
    [100.0] * 16 + [165.0],                # 亮框不够亮（< 170）
    [148.0] * 16 + [185.0],                # 亮暗差 37 < 50，双峰不分明
    [100.0] * 16 + [None],                 # 有框不可测
    [214.0],                               # 框数不足
])
def test_pattern_rejects(values):
    index, _stats = brightness_pattern(values)
    assert index is None


# ---------------------------------------------------------------- tracker 单元

def _hit(center_x, label="nan"):
    box = (center_x - 45, 768, center_x + 45, 892)
    return SingleBrightCut(hit=True, reason="合成命中", bright_index=3,
                           bright_box=box, tap_point=(center_x, 830),
                           tile_id=62, tile_label=label, streak=1)


def test_tracker_two_consistent_frames_confirm():
    tracker = SingleBrightCutTracker()
    assert not tracker.observe(_hit(1000)).confirmed
    assert tracker.observe(_hit(1000)).confirmed


def test_tracker_miss_resets_streak():
    tracker = SingleBrightCutTracker()
    tracker.observe(_hit(1000))
    tracker.observe(SingleBrightCut(hit=False, reason="不命中"))
    verdict = tracker.observe(_hit(1000))
    assert not verdict.confirmed and verdict.streak == 1


def test_tracker_changed_tile_restarts_streak():
    tracker = SingleBrightCutTracker()
    tracker.observe(_hit(1000, "nan"))
    verdict = tracker.observe(_hit(1000, "dong"))  # 同位置、不同牌面
    assert not verdict.confirmed and verdict.streak == 1
    verdict = tracker.observe(_hit(1300, "dong"))  # 牌面同、位置变了
    assert not verdict.confirmed and verdict.streak == 1


def test_tracker_signature_fallback_without_labels():
    signature = np.zeros((88, 64, 3), dtype=np.float32)
    signature[10, 10, 0] = 1.0
    tracker = SingleBrightCutTracker()
    first = replace(_hit(1000), tile_label=None, tile_id=None,
                    face_signature=signature)
    assert not tracker.observe(first).confirmed
    assert tracker.observe(first).confirmed


# ==================================================================
# 多亮白名单（亮簇 1..N 张）
# ==================================================================
# 合成帧：手牌带底色 100（实测压暗值），逐框另涂；face_background 是
# p70(V)，纯色填充框因此可精确指定面亮度。
SYNTH_BOXES = [[90 + index * 88, 780, 170 + index * 88, 888]
               for index in range(17)]

# 引擎/视觉牌 id：1=w1（本组测试的财神）、61=东、62=南、64=北、63=西。
ID_W1, ID_DONG, ID_NAN, ID_BEI, ID_XI = 1, 61, 62, 64, 63
LABELS = {ID_W1: "w1", ID_DONG: "dong", ID_NAN: "nan", ID_BEI: "bei",
          ID_XI: "xi"}


def synth_frame(levels, band=100):
    """按 ``SYNTH_BOXES`` 的框序给每个框涂指定亮度，返回 BGR 合成帧。"""
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, 70, 40)                 # 桌面绿
    image[760:, :] = band                      # 手牌带底色（压暗值）
    for box, level in zip(SYNTH_BOXES, levels):
        x0, y0, x1, y1 = box
        image[y0:y1, x0:x1] = int(level)
    return image


def synth_result(ids, *, caishen="w1", melds=None, marked=None):
    """按 assist_tea 的同一口径构造 analyze 返回体（逐框给 id）。"""
    boxes = SYNTH_BOXES[:len(ids)]
    diagnostics = []
    for index, (tile_id, box) in enumerate(zip(ids, boxes)):
        diagnostics.append({
            "tile_id": tile_id,
            "label": LABELS.get(tile_id) if tile_id is not None else None,
            "box": list(box),
            "accepted": tile_id is not None,
            "region": "concealed",
            "score": .99 if tile_id is not None else .1,
            "margin": .2,
            "caishen_marked": bool(marked[index]) if marked else False,
        })
    groups = melds or []
    return {"mode": "tea_2p", "status": "abstain", "caishen": caishen,
            "vision_diagnostics": diagnostics,
            "hand_ids": [int(t) for t in ids if t is not None],
            "melds": groups, "meld_count": len(groups)}


def two_bright_case(dim_ids=None):
    """15 暗 + 2 亮（南/北）：就是实机 5558_t4 的图案。"""
    ids = list(dim_ids or [None] * 15) + [ID_NAN, ID_BEI]
    levels = [100.0] * 15 + [214.0] * 2
    return levels, ids


def test_subset_two_bright_hits_and_lists_whitelist():
    """15 暗 + 南/北 两亮：命中，白名单恰两张，坐标=框中心。"""
    levels, ids = two_bright_case([ID_W1] + [None] * 14)
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert verdict.hit, verdict.reason
    assert verdict.bright_count == 2 and verdict.multi_candidate
    assert verdict.unresolved is False
    assert [c.tile_id for c in verdict.candidates] == [ID_NAN, ID_BEI]
    assert [c.tap_point for c in verdict.candidates] == [(1450, 834),
                                                         (1538, 834)]
    assert verdict.evidence["dim_mean"] == 100.0
    assert verdict.evidence["dim_spread"] == 0.0
    assert verdict.evidence["gap"] == 114.0
    assert verdict.evidence["max_bright_candidates"] == 8
    json.dumps(verdict.ledger(), ensure_ascii=False)


def test_subset_single_bright_hits_but_is_not_multi_candidate():
    """1 亮也命中（同一套图案），但 multi_candidate=False → 单亮路径仍归它。"""
    levels = [100.0] * 16 + [214.0]
    ids = [None] * 16 + [ID_NAN]
    image, result = synth_frame(levels), synth_result(ids)
    verdict = detect_bright_subset_cut(image, result)
    assert verdict.hit and verdict.bright_count == 1
    assert verdict.multi_candidate is False
    assert verdict.whitelist_ids() == (ID_NAN,)
    single = detect_single_bright_cut(image, result)
    assert single.hit and single.bright_index == 16
    assert single.tap_point == verdict.candidates[0].tap_point


def test_subset_three_bright_hits():
    """用户截图「1条×2 + 南」：3 张亮牌同样命中，白名单三张。"""
    levels = [100.0] * 7 + [214.0, 214.0] + [100.0] * 7 + [214.0]
    ids = ([None] * 7 + [ID_NAN, ID_NAN] + [None] * 7 + [ID_BEI])
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert verdict.hit, verdict.reason
    assert verdict.bright_count == 3 and verdict.multi_candidate
    assert verdict.whitelist_ids() == (ID_NAN, ID_NAN, ID_BEI)


def test_subset_all_dim_misses():
    """全暗：没有亮牌可打，不命中。"""
    verdict = detect_bright_subset_cut(synth_frame([100.0] * 17),
                                       synth_result([None] * 17))
    assert not verdict.hit
    assert "没有亮框" in verdict.reason


def test_subset_all_bright_misses_normal_play_state():
    """全亮 = 正常出牌态：亮框超过上限，绝不当作压暗态处理。"""
    ids = [ID_W1, ID_DONG, ID_NAN, ID_BEI, ID_XI] * 3 + [ID_NAN, ID_BEI]
    verdict = detect_bright_subset_cut(synth_frame([214.0] * 17),
                                       synth_result(ids))
    assert not verdict.hit
    assert verdict.evidence["bright_count"] == 17
    assert "上限" in verdict.reason


def test_subset_mirror_pattern_misses():
    """正常手牌的镜像图案（1 暗 + 16 亮）同样不命中。"""
    levels = [100.0] + [214.0] * 16
    ids = [ID_W1] + [ID_NAN] * 16
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert not verdict.hit
    assert verdict.evidence["bright_count"] == 16


def test_subset_box_count_mismatch_misses():
    """16 框（未摸牌）在图案之前就被物理框守恒挡掉。"""
    levels = [100.0] * 14 + [214.0] * 2
    ids = [None] * 14 + [ID_NAN, ID_BEI]
    result = synth_result(ids)
    result["vision_diagnostics"] = result["vision_diagnostics"][:16]
    verdict = detect_bright_subset_cut(synth_frame(levels), result)
    assert not verdict.hit
    assert "物理框 16" in verdict.reason


def test_subset_meld_slots_shift_expected_count():
    """副露 1 组（3 槽）→ 应有 14 框；给 14 框才算守恒。"""
    levels = [100.0] * 12 + [214.0] * 2
    ids = [None] * 12 + [ID_NAN, ID_BEI]
    result = synth_result(ids, melds=[[ID_DONG] * 3])
    boxes = SYNTH_BOXES[:14]
    result["vision_diagnostics"] = [
        dict(item, box=list(box))
        for item, box in zip(result["vision_diagnostics"], boxes)]
    image = synth_frame(levels)
    verdict = detect_bright_subset_cut(image, result)
    assert verdict.hit, verdict.reason
    assert verdict.evidence["expected_concealed"] == 14


def test_subset_excludes_caishen_badge_from_whitelist():
    """财神位（「财」角标）落在亮簇里必须先剔除，不许当可打牌。"""
    levels = [100.0] * 15 + [214.0, 214.0]
    ids = [None] * 15 + [ID_W1, ID_NAN]
    marked = [False] * 15 + [True, False]
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids, marked=marked))
    assert verdict.hit, verdict.reason
    assert verdict.bright_count == 2
    assert [c.tile_id for c in verdict.candidates] == [ID_NAN]
    assert verdict.excluded[0]["index"] == 15
    assert verdict.excluded[0]["caishen_marked"] is True
    assert verdict.evidence["excluded_caishen_count"] == 1


def test_subset_excludes_caishen_by_tile_id():
    """牌面 id == 本局财神也算财神位（角标没被识别时的兜底）。"""
    levels = [100.0] * 15 + [214.0, 214.0]
    ids = [None] * 15 + [ID_W1, ID_NAN]
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert verdict.hit
    assert [c.tile_id for c in verdict.candidates] == [ID_NAN]
    assert verdict.excluded[0]["tile_id"] == ID_W1


def test_subset_all_bright_are_caishen_misses():
    """亮簇全是财神位 → 没有可打白名单，不命中。"""
    levels = [100.0] * 15 + [214.0, 214.0]
    ids = [None] * 15 + [ID_W1, ID_W1]
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert not verdict.hit
    assert "全部是财神位" in verdict.reason


def test_subset_marks_unresolved_when_label_missing():
    """任一候选牌面读不出 → hit 但 unresolved=True，由调用方弃权。"""
    levels, _ = two_bright_case()
    ids = [None] * 15 + [ID_NAN, None]
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert verdict.hit and verdict.unresolved is True
    assert verdict.whitelist_ids() == (ID_NAN,)
    assert "弃权" in verdict.reason


def test_subset_mid_brightness_rejected():
    """亮暗之间有中间亮度框（150-170）：分簇不干净 → 不命中。"""
    levels = [100.0] * 14 + [160.0, 214.0, 214.0]
    ids = [None] * 15 + [ID_NAN, ID_BEI]
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert not verdict.hit
    assert verdict.evidence["mid_count"] == 1


def test_subset_requires_known_caishen_and_tea_mode():
    """上下文缺一不可：财神未知 / 非 tea_2p 一律不命中。"""
    levels, ids = two_bright_case()
    image = synth_frame(levels)
    assert "财神未知" in detect_bright_subset_cut(
        image, synth_result(ids, caishen=None)).reason
    assert not detect_bright_subset_cut(
        image, dict(synth_result(ids), mode="tea_4p")).hit
    assert not detect_bright_subset_cut(image, None).hit


def test_subset_rejects_hint_panel_and_settlement(panel_frames, settled_frame):
    """胡提示面板帧 / 结算帧必须在前置排除出局（与单亮同口径）。"""
    for image, result in panel_frames:
        verdict = detect_bright_subset_cut(image, result)
        assert not verdict.hit, verdict.reason
    image, result = settled_frame
    verdict = detect_bright_subset_cut(image, result, caishen_label="t5")
    assert not verdict.hit


def test_subset_ledger_records_gate_name():
    levels, ids = two_bright_case()
    verdict = detect_bright_subset_cut(synth_frame(levels),
                                       synth_result(ids))
    assert verdict.ledger()["gate"] == BRIGHT_SUBSET_GATE
    assert verdict.ledger()["bright_count"] == 2
    json.dumps(verdict.ledger(), ensure_ascii=False)


@pytest.mark.parametrize("values,expected", [
    ([100.0] * 16 + [214.0], [16]),                       # 1 亮
    ([100.0] * 15 + [214.0] * 2, [15, 16]),               # 2 亮
    ([100.0] * 14 + [214.0] * 3, [14, 15, 16]),           # 3 亮
    ([100.0] * 9 + [214.0] * 8, list(range(9, 17))),      # 上限 8 张
])
def test_subset_pattern_hits(values, expected):
    indices, stats = bright_subset_pattern(values)
    assert indices == expected
    assert stats["bright_count"] == len(expected)
    assert stats["mid_count"] == 0


@pytest.mark.parametrize("values", [
    [214.0] * 17,                                  # 全亮（正常出牌态）
    [100.0] * 17,                                  # 全暗
    [100.0] * 8 + [214.0] * 9,                     # 9 亮 > 上限 8
    [100.0] * 15 + [214.0] * 2 + [None],           # 有框不可测
    [100.0] * 14 + [160.0, 214.0, 214.0],          # 中间亮度框
    [100.0] * 14 + [214.0] * 3 + [None],          # 框数/可测性兜底
    [60.0, 140.0] + [100.0] * 13 + [214.0] * 2,    # 暗簇极差 80 > 40
    [100.0] * 15 + [165.0, 214.0],                 # 亮框不够亮
    [148.0] * 15 + [185.0, 190.0],                 # 亮暗差 < 50
    [214.0],                                       # 框数不足
])
def test_subset_pattern_rejects(values):
    indices, _stats = bright_subset_pattern(values)
    assert indices is None


# ---------------------------------------------------------------- tracker
def _subset(hits, *, ids=(ID_NAN, ID_BEI), start_x=1342, labels=None,
            unresolved=False):
    """合成一份多亮结论（hits = 每个候选的 (dx, label, tile_id)）。"""
    candidates = []
    for index, (dx, label) in enumerate(hits):
        tile_id = ids[index] if ids[index] is not None else None
        box = (start_x + dx - 44, 780, start_x + dx + 44, 888)
        candidates.append({
            "index": index, "box": box,
            "tap_point": (start_x + dx, 834),
            "tile_id": tile_id, "tile_label": label,
            "face_value": 214.0})
    from single_bright_cut import BrightCandidate
    return BrightSubsetCut(
        hit=True, reason="合成多亮", bright_count=len(hits),
        candidates=tuple(BrightCandidate(**item) for item in candidates),
        unresolved=unresolved, streak=1)


def _subset_verdict(*, x=1342, ids=(ID_NAN, ID_BEI)):
    hits = [(0, LABELS.get(ids[0])), (92, LABELS.get(ids[1]))]
    return _subset(hits, ids=ids, start_x=x)


def test_subset_tracker_confirms_two_consistent_frames():
    tracker = BrightSubsetCutTracker()
    assert not tracker.observe(_subset_verdict()).confirmed
    verdict = tracker.observe(_subset_verdict())
    assert verdict.confirmed and verdict.streak == 2


def test_subset_tracker_miss_resets_streak():
    tracker = BrightSubsetCutTracker()
    tracker.observe(_subset_verdict())
    tracker.observe(BrightSubsetCut(hit=False, reason="不命中"))
    verdict = tracker.observe(_subset_verdict())
    assert not verdict.confirmed and verdict.streak == 1


def test_subset_tracker_changed_whitelist_restarts_streak():
    tracker = BrightSubsetCutTracker()
    tracker.observe(_subset_verdict())
    # 换了一张牌（南→东）
    verdict = tracker.observe(_subset_verdict(ids=(ID_DONG, ID_BEI)))
    assert not verdict.confirmed and verdict.streak == 1
    # 位置漂移 30px > 20px 容差
    verdict = tracker.observe(_subset_verdict(x=1372))
    assert not verdict.confirmed and verdict.streak == 1
    # 张数变了（少一张亮牌）
    short = _subset([(0, "nan")], ids=(ID_NAN,))
    assert not tracker.observe(short).confirmed


def test_subset_tracker_signature_fallback_without_ids():
    signature = np.zeros((88, 64, 3), dtype=np.float32)
    signature[10, 10, 0] = 1.0
    first = replace(_subset_verdict(ids=(None, None)),
                    candidates=tuple(
                        replace(candidate, face_signature=signature)
                        for candidate in _subset_verdict(
                            ids=(None, None)).candidates))
    tracker = BrightSubsetCutTracker()
    assert not tracker.observe(first).confirmed
    assert tracker.observe(first).confirmed


# ---------------------------------------------------------------- 真实帧
@pytest.fixture(scope="module")
def multi_bright_frame():
    """实机多亮帧（2026-10-25，房间 604624：南 + 北 两亮、其余 15 张压暗）。"""
    if not MULTI_BRIGHT_FRAME.is_file():
        pytest.skip(f"证据帧缺失（外部采集产物）：{MULTI_BRIGHT_FRAME}")
    image = _image(MULTI_BRIGHT_FRAME)
    return image, _result(image, "w5")


def test_real_multi_bright_frame_hits_whitelist(multi_bright_frame):
    """真实多亮帧：单亮检测不命中（卡死根因），多亮白名单必须命中两张。"""
    image, result = multi_bright_frame
    single = detect_single_bright_cut(image, result)
    assert not single.hit
    assert single.evidence["bright_count"] == 2
    verdict = detect_bright_subset_cut(image, result)
    assert verdict.hit, verdict.reason
    assert verdict.evidence["physical_boxes"] == 17
    assert verdict.evidence["expected_concealed"] == 17
    assert verdict.evidence["bright_count"] == 2
    assert verdict.evidence["dim_count"] == 15
    assert verdict.evidence["dim_mean"] == 100.0
    assert verdict.evidence["gap"] == 114.0
    assert verdict.multi_candidate is True
    assert verdict.unresolved is False
    assert [c.tile_label for c in verdict.candidates] == ["nan", "bei"]
    assert [c.tile_id for c in verdict.candidates] == [ID_NAN, ID_BEI]
    assert [c.tap_point for c in verdict.candidates] == [(1342, 830),
                                                         (1435, 830)]


def test_real_multi_bright_frame_confirms_on_second_frame(multi_bright_frame):
    image, result = multi_bright_frame
    tracker = BrightSubsetCutTracker()
    assert not tracker.feed(image, result).confirmed
    second = tracker.feed(image, result)
    assert second.confirmed
    assert second.whitelist_ids() == (ID_NAN, ID_BEI)
