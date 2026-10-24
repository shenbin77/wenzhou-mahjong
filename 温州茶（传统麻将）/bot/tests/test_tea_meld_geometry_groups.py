# -*- coding: utf-8 -*-
"""副露分组必须由视觉层几何边界决定，不允许全组自由组合。

背景（2026-10-24 会话 20261024_015005 的 38 帧弃权）：
  台面真值是「碰西西西 + 吃 5筒6筒7筒」，但视觉层把 7筒 那张牌切成两段
  （x=452..493 + 495..533，各自短于最小牌宽）后双双丢弃，发布的组边界变成
  `[3, 2]`。旧实现做的是"全局宽度 3/4 自由切分"，对这种形态只能报
  「明牌组数量或牌型未确认」，既说不清是哪一组坏了，也掩盖了"少一张牌"这个
  真实缺陷。新实现只认视觉层给的组边界：
    - 组内张数是 3（吃/碰）还是 4（杠）由几何定，组之间不许重排；
    - 任何一组张数不是 3/4、或组内牌型非法，一律弃权并点名第几组、x 区间。
本文件锁定这条契约，防止回退到自由组合。
"""
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tea_bot import TeaBot  # noqa: E402
from tile_vision import TILE_IDS, TileVision  # noqa: E402


def _png():
    ok, encoded = cv2.imencode(".png", np.zeros((32, 32, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _install_fake_vision(monkeypatch, meld_ids, meld_boxes, meld_groups,
                         hand_ids, complete=True):
    """装一个只回答「识别结果」的视觉替身，走真实 _default_vision 接线。"""

    class FakeVision:
        def __init__(self, template_dir):
            self.last_complete = complete
            self.last_meld_boxes = list(meld_boxes)
            self.last_melds = [(tile, *box)
                               for tile, box in zip(meld_ids, meld_boxes)]
            self.detection_diagnostics = {"meld_groups": list(meld_groups)}
            self.caishen_label = None

        def recognize_hand(self, image):
            return [(tile, 0, 0, 1, 1) for tile in hand_ids]

    monkeypatch.setitem(sys.modules, "tile_vision",
                        types.SimpleNamespace(TileVision=FakeVision,
                                              TILE_IDS=TILE_IDS))
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _: 2,
        detect_tea_no_zhong_fa=lambda _: True))


# 闭手 10 张：与两组副露（3+3）相加 = 16，属于"等待摸牌"态。
HAND_10 = [1, 2, 3, 4, 5, 6, 21, 22, 23, 24]
# 闭手 11 张：与 5 张台面牌相加 = 16。
HAND_11 = [42, 44, 44, 45, 48, 23, 24, 26, 27, 62, 62]


def test_geometry_groups_split_two_exposed_sets(tmp_path, monkeypatch):
    # 碰西西西 + 吃 5筒6筒7筒：视觉层按组间间隙切成 [3, 3]。
    meld_ids = [63, 63, 63, 45, 46, 47]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875),
             (278, 761, 362, 875), (366, 761, 448, 875),
             (452, 761, 534, 875)]
    _install_fake_vision(monkeypatch, meld_ids, boxes, [3, 3], HAND_10)
    bot = TeaBot(log_dir=str(tmp_path))
    assert bot.see(_png()) == HAND_10
    assert bot.meld_count == 2
    assert bot.meld_groups == [[63, 63, 63], [45, 46, 47]]


def test_native_west_peng_and_stacked_kong_have_distinct_logical_counts():
    """005400 has 83 flat 西碰 frames and 101 raised 西杠 frames.

    Both show three readable 西 faces. Only the middle tile's full 26px
    translation justifies counting the hidden fourth tile for the engine.
    """
    folder = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_005400"
              / "live" / "tea_2p")
    samples = [
        ("20261024_005413_959184_662f3d.png", 3),
        ("20261024_005700_684588_0ae39a.png", 4),
    ]
    for name, expected in samples:
        frame = folder / name
        if not frame.is_file():
            pytest.skip(f"实战帧缺失: {frame}")
        image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
        vision = TileVision(template_dir=BOT_DIR.parent / "采集" / "templates" / "tea_tiles")
        vision.recognize_hand(image)
        faces = vision.meld_diagnostics[:3]
        assert len(faces) == 3
        assert all(face["accepted"] and face["label"] == "xi" for face in faces)
        boxes = vision.last_meld_boxes[:3]
        groups = TeaBot._meld_groups_by_geometry([63, 63, 63], boxes, [3])
        assert groups == [[63] * expected]
        assert len(boxes) == 3  # The stacked fourth tile has no separate box.


def test_raised_middle_without_matching_faces_is_not_a_kong():
    boxes = [(6, 761, 89, 875), (93, 735, 175, 849),
             (179, 761, 263, 875)]
    assert TeaBot._meld_groups_by_geometry([45, 46, 47], boxes, [3]) == [[45, 46, 47]]


def test_two_tile_group_abstains_and_names_the_group_span(tmp_path, monkeypatch):
    # 会话 20261024_015005 的真实形态：7筒 被切成碎片后丢弃，组边界成了 [3, 2]。
    # 真值闭手 11 张 + 台面 6 张 = 17，检测成 11 + 5 = 16 只是"看起来合规"，
    # 少一张牌就绝不允许进入决策。
    meld_ids = [63, 63, 63, 45, 46]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875),
             (278, 761, 362, 875), (366, 761, 448, 875)]
    _install_fake_vision(monkeypatch, meld_ids, boxes, [3, 2], HAND_11)
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError) as excinfo:
        bot.see(_png())
    message = str(excinfo.value)
    assert "明牌第2组仅2张" in message
    assert "疑似漏检" in message
    assert "x≈278..448" in message
    # 旧的模糊理由不能再用它顶替：它不指出是哪一组坏了。
    assert "明牌组数量或牌型未确认" not in message


def test_single_tile_group_abstains_like_the_xigang_geometry(tmp_path, monkeypatch):
    # 暗杠的 4 张叠成 3 个可见位时，几何可能给出 [3, 1, 1]（会话 005400 实测）。
    # 这种形态张数对不上，必须弃权，不能把叠起来的那张"推算"出来。
    meld_ids = [63, 63, 63, 45, 46]
    boxes = [(6, 761, 89, 875), (93, 735, 175, 849), (93, 761, 175, 875),
             (278, 761, 362, 875), (366, 761, 448, 875)]
    _install_fake_vision(monkeypatch, meld_ids, boxes, [3, 1, 1], HAND_11)
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="明牌第2组仅1张"):
        bot.see(_png())


def test_group_with_illegal_shape_abstains(tmp_path, monkeypatch):
    # 1万 2万 4万 同花色但跨 3，不是顺子；同牌又不同，也不是碰。
    meld_ids = [1, 2, 4]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875)]
    _install_fake_vision(monkeypatch, meld_ids, boxes, [3],
                         [5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 62, 62])
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="明牌第1组牌型非法"):
        bot.see(_png())


def test_geometry_that_does_not_add_up_abstains(tmp_path, monkeypatch):
    # 组边界之和与识别张数不符：说明视觉层发布的两份数据互相矛盾，直接停。
    meld_ids = [63, 63, 63, 45, 46]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875),
             (278, 761, 362, 875), (366, 761, 448, 875)]
    _install_fake_vision(monkeypatch, meld_ids, boxes, [3, 3], HAND_11)
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="明牌组边界.*识别张数"):
        bot.see(_png())


def test_four_tile_kong_group_counts_as_one_meld(tmp_path, monkeypatch):
    # 杠 4 张仍是一组（实物 4）；逻辑位只占 3，等待态闭手必须 13 张（13+3=16）。
    # 旧断言「闭手 12 + 杠 4 = 16」已随 P0-1 作废：第 4 张不回灌、不多扣暗手。
    hand = [1, 2, 3, 4, 5, 6, 21, 22, 23, 41, 41, 41, 42]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875),
             (278, 735, 362, 849)]
    _install_fake_vision(monkeypatch, [63, 63, 63, 63], boxes, [4], hand)
    bot = TeaBot(log_dir=str(tmp_path))
    assert bot.see(_png()) == hand
    assert bot.meld_count == 1
    assert bot.meld_groups == [[63, 63, 63, 63]]
    assert sum(len(group) for group in bot.meld_groups) == 4


def test_twelve_plus_one_kong_is_rejected_as_illegal_spare(tmp_path, monkeypatch):
    # 回归：WorkBuddy 曾把「12 闭手 + 1 杠」当合法；正确茶规下必须拒。
    hand = [1, 2, 3, 4, 5, 6, 21, 22, 23, 41, 41, 41]
    boxes = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875),
             (278, 735, 362, 849)]
    _install_fake_vision(monkeypatch, [63, 63, 63, 63], boxes, [4], hand)
    bot = TeaBot(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="暗手应为13或14张|杠后补牌不多扣暗手"):
        bot.see(_png())


def test_vision_without_geometry_falls_back_to_full_search(tmp_path, monkeypatch):
    # 旧适配器/测试替身不发布 meld_groups：退回旧的全组搜索，行为保持不变。
    class LegacyVision:
        def __init__(self, template_dir):
            self.last_complete = True
            self.last_meld_boxes = [(0, 0, 1, 1)] * 6
            self.last_melds = [(tile, 0, 0, 1, 1)
                               for tile in [29, 29, 29, 1, 1, 1]]

        def recognize_hand(self, image):
            return [(tile, 0, 0, 1, 1) for tile in HAND_10]

    monkeypatch.setitem(sys.modules, "tile_vision",
                        types.SimpleNamespace(TileVision=LegacyVision,
                                              TILE_IDS=TILE_IDS))
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _: 2,
        detect_tea_no_zhong_fa=lambda _: True))
    bot = TeaBot(log_dir=str(tmp_path))
    assert bot.see(_png()) == HAND_10
    assert bot.meld_groups == [[29, 29, 29], [1, 1, 1]]
