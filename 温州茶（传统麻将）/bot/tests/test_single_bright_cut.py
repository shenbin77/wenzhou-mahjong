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
    SingleBrightCut, SingleBrightCutTracker, box_face_values,
    brightness_pattern, detect_single_bright_cut)
from tile_vision import TileVision  # noqa: E402
from turn_gate import concealed_boxes, hand_face_brightness  # noqa: E402

TEA_ROOT = BOT_DIR.parent
MAJIANG_ROOT = TEA_ROOT.parent
TEMPLATES = TEA_ROOT / "采集" / "templates" / "tea_tiles"
TAKEOVER = MAJIANG_ROOT / "_takeover"

EVIDENCE = MAJIANG_ROOT / "capture_5556.png"
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
