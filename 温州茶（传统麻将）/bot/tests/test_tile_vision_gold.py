"""整张纯金"财神牌"的手牌框（20261025 房间 915995 整帧弃权的根因）。

实测那一帧（1600x900）：三张六条明牌组 + 14 张暗手，其中两张财神牌被渲染成
整块金黄、牌面完全空白——没有奶油底色、也没有任何牌面墨迹。既有"奶油低饱和
连通列"通道对它们零命中，暗手只数到 12 框，tea_bot 因此抛
"暗手应为13或14张，识别到12张"，一招都打不出去。

这里锁住四件事：

* 金色牌必须计入暗手框（真帧 14 框，加明牌组 3 框 = 17 张）；
* 财神未知时金色牌严格 fail-closed，且弃权原因写明 gold_tile_caishen_unknown；
* 普通牌面（奶油底 + 墨迹）不得被误判成金色牌；
* 三张六条的明牌组判定不受影响（仍是那 3 个框）。
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tea_bot import _TEMPLATE_DIR  # noqa: E402
from tile_vision import (  # noqa: E402
    TILE_IDS,
    TileVision,
    _GOLD_MAX_INK_STD,
    _GOLD_MIN_FRACTION,
    gold_tile_evidence,
    is_gold_tile,
)

# 根因帧：房间 915995 第 2/16 局，emulator-5556。
_ROOT = BOT_DIR.parents[1]
_ROOT_FRAME = (_ROOT / "_takeover" / "wb_live" / "w5"
               / "emulator-5556_20261025_233059_g1" / "frames"
               / "20261025_233119_686643_before_684d78.png")

# 改动前在真帧上量到的几何（见 tile_vision 顶部金色取样注释）。
MELD_BOXES = [(6, 761, 89, 875), (93, 761, 175, 875), (179, 761, 263, 875)]
GOLD_BOXES = [(278, 768, 363, 884), (451, 768, 536, 884)]

# 合成图的取色：同一帧实测中位数（BGR）。
_TABLE = (42, 76, 22)      # 手牌带上方的桌面绒布
_CREAM = (214, 209, 193)   # 八萬 牌面奶油底
_GOLD = (22, 162, 233)     # 纯金牌面

_PITCH, _WIDTH, _TOP, _BOTTOM = 92, 85, 768, 892


def _real_frame():
    if not _ROOT_FRAME.exists():
        pytest.skip("根因实机帧已不在此机器上")
    image = cv2.imdecode(np.fromfile(str(_ROOT_FRAME), dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2] == (900, 1600)
    return image


def _synthetic_hand(gold_positions=(), tiles=14):
    """Lay out one hand on the felt: cream printed faces plus flat gold tiles."""
    image = np.zeros((900, 1600, 3), np.uint8)
    image[:] = _TABLE
    for index in range(tiles):
        left = 20 + index * _PITCH
        if index in gold_positions:
            image[_TOP:_BOTTOM, left:left + _WIDTH] = _GOLD
            continue
        image[_TOP:_BOTTOM, left:left + _WIDTH] = _CREAM
        # 一大块深色墨迹：普通牌面既有墨，灰度方差也远高于金面。
        image[_TOP + 30:_TOP + 95, left + 20:left + 65] = (40, 40, 45)
    return image


# --- 真帧 -------------------------------------------------------------------

def test_real_frame_counts_both_gold_tiles_into_the_hand():
    image = _real_frame()
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="t7")
    recognised = vision.recognize_hand(image)
    diagnostics = vision.detection_diagnostics
    assert diagnostics["concealed_count"] == 14
    assert len(vision.last_gold_boxes) == 2
    assert vision.last_gold_boxes == GOLD_BOXES
    assert len(recognised) == 14
    assert len(recognised) + len(vision.last_meld_boxes) == 17
    assert vision.last_complete is True


def test_real_frame_keeps_the_three_tile_meld_group():
    image = _real_frame()
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="t7")
    vision.recognize_hand(image)
    assert vision.last_meld_boxes == MELD_BOXES
    assert vision.detection_diagnostics["meld_groups"] == [3]
    assert len(vision.meld_diagnostics) == 3
    assert all(d["accepted"] for d in vision.meld_diagnostics)


def test_real_frame_gold_tiles_read_as_this_round_caishen():
    image = _real_frame()
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="t7")
    vision.recognize_hand(image)
    gold = [d for d in vision.diagnostics if d.get("gold_tile")]
    assert len(gold) == 2
    assert {d["tile_id"] for d in gold} == {TILE_IDS["t7"]}
    assert all(d["accepted"] and d["reason"] == "gold_tile_caishen" for d in gold)


def test_real_frame_without_caishen_fails_closed():
    image = _real_frame()
    vision = TileVision(template_dir=_TEMPLATE_DIR)  # 本局财神未知
    recognised = vision.recognize_hand(image)
    diagnostics = vision.detection_diagnostics
    assert vision.last_complete is False
    assert diagnostics["reason"] == "gold_tile_caishen_unknown"
    assert diagnostics["gold_tiles_without_caishen"] == [list(b) for b in GOLD_BOXES]
    assert diagnostics["concealed_count"] == 14
    assert len(recognised) == 12  # 两张金的绝不瞎猜
    assert all(d["tile_id"] is None and not d["accepted"]
               for d in vision.diagnostics if d.get("gold_tile"))


def test_real_frame_ordinary_faces_are_not_gold():
    image = _real_frame()
    vision = TileVision(template_dir=_TEMPLATE_DIR)
    vision.count_tiles(image)
    assert vision.last_gold_boxes == GOLD_BOXES  # 只有那两张，且位置精确
    # 同帧其它牌：八萬 / 六筒 / 北（暗手基线）+ 六条（明牌组）
    for left, top, right, bottom in ((560, 768, 648, 892), (652, 768, 741, 892),
                                     (1491, 768, 1581, 892), (6, 761, 89, 875)):
        crop = image[top:bottom, left:right]
        fraction, ink_std = gold_tile_evidence(crop)
        assert fraction < _GOLD_MIN_FRACTION, (left, fraction)
        assert ink_std > _GOLD_MAX_INK_STD, (left, ink_std)
        assert is_gold_tile(crop) is False


# --- 合成图 -----------------------------------------------------------------

def test_synthetic_gold_tiles_are_counted_as_their_own_boxes():
    image = _synthetic_hand(gold_positions=(3, 9))
    vision = TileVision(template_dir=_TEMPLATE_DIR)
    count, boxes = vision.count_tiles(image)
    assert count == 14
    expected = [(20 + 3 * _PITCH, _TOP, 20 + 3 * _PITCH + _WIDTH, _BOTTOM),
                (20 + 9 * _PITCH, _TOP, 20 + 9 * _PITCH + _WIDTH, _BOTTOM)]
    assert vision.last_gold_boxes == expected
    assert len(boxes) == 14
    assert set(expected).issubset(set(boxes))
    assert vision.last_meld_boxes == []


def test_synthetic_cream_only_hand_has_no_gold_boxes():
    image = _synthetic_hand()
    vision = TileVision(template_dir=_TEMPLATE_DIR)
    count, _ = vision.count_tiles(image)
    assert count == 14
    assert vision.last_gold_boxes == []
    assert vision.detection_diagnostics["visible_count"] == 14


def test_synthetic_gold_evidence_separates_gold_from_cream():
    gold = _synthetic_hand(gold_positions=(0,))[768:892, 20:105]
    cream = _synthetic_hand()[768:892, 112:197]
    assert is_gold_tile(gold) is True
    assert is_gold_tile(cream) is False
    # 判据留了明显余量，不是贴边阈值。
    fraction, ink_std = gold_tile_evidence(gold)
    assert fraction >= 0.95 and ink_std <= 5
    fraction, ink_std = gold_tile_evidence(cream)
    assert fraction <= 0.05 and ink_std >= 60


def test_empty_crop_is_never_a_gold_tile():
    assert is_gold_tile(np.zeros((0, 0, 3), np.uint8)) is False
    assert is_gold_tile(None) is False
