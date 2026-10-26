"""牌局结束 → 返回大厅 的规格测试。

实机版式（1600x900，20261025 22:58 真帧 1105x622 等比换算）：
  返回大厅 box=(246,777,554,873) 中心=(399,825)
  分享战绩 中心≈(800,812)
  继续游戏 box=(1045,764,1413,890) 中心=(1229,826)

规格：三个固定 ROI 各找到一个"离四边都留余量、填充率 0.30~0.85"的按钮，
且左右按钮中心与实测版式偏差 ≤80px，才判「牌局结束」。任一不成立就
present=False，调用方保持原有停机语义。
"""

from __future__ import annotations

import cv2
import numpy as np

from match_end import (
    BUTTON_ROIS,
    EXPECTED_LEAVE_CENTER,
    read_match_end,
)

FRAME_W, FRAME_H = 1600, 900
BUTTON_BGR = (95, 205, 170)          # V≈205、S≈137：亮且有饱和度
FELT_BGR = (48, 96, 60)              # 桌面绿：V 低
LEAVE_BOX = (246, 777, 554, 872)
SHARE_BOX = (646, 777, 954, 872)
CONTINUE_BOX = (1046, 764, 1413, 873)


def frame_with_buttons(boxes=(), *, background=FELT_BGR) -> np.ndarray:
    image = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    image[:, :] = background
    for box in boxes:
        x0, y0, x1, y1 = box
        cv2.rectangle(image, (x0, y0), (x1, y1), BUTTON_BGR, -1)
    return image


def test_match_end_layout_is_recognised():
    state = read_match_end(frame_with_buttons(
        [LEAVE_BOX, SHARE_BOX, CONTINUE_BOX]))

    assert state.present is True
    assert state.leave_box is not None
    # cv2 画框是闭区间，检出框允许 ±1 像素
    for got, want in zip(state.leave_box, LEAVE_BOX):
        assert abs(got - want) <= 1
    centre = ((LEAVE_BOX[0] + LEAVE_BOX[2]) // 2,
              (LEAVE_BOX[1] + LEAVE_BOX[3]) // 2)
    assert abs(centre[0] - EXPECTED_LEAVE_CENTER[0]) <= 2


def test_plain_table_frame_is_not_match_end():
    """正常牌桌：底部没有这种按钮排，必须不命中（否则会乱点返回大厅）。"""
    assert read_match_end(frame_with_buttons()).present is False


def test_missing_one_button_is_rejected():
    """只剩两个按钮（例如被弹窗遮住一个）→ 不判牌局结束。"""
    state = read_match_end(frame_with_buttons([LEAVE_BOX, SHARE_BOX]))

    assert state.present is False
    assert "命中 2/3" in state.reason


def test_shifted_layout_is_rejected():
    """按钮整体左移 200px：布局与实测不符 → 不猜、不点。"""
    shifted = [(x0 - 200, y0, x1 - 200, y1)
               for x0, y0, x1, y1 in (LEAVE_BOX, SHARE_BOX, CONTINUE_BOX)]

    state = read_match_end(frame_with_buttons(shifted))

    assert state.present is False


def test_full_bleed_background_is_rejected():
    """整片亮色背景（大厅地板/纯色）会把 ROI 填满 → 必须不命中。"""
    bright_floor = (150, 200, 190)
    state = read_match_end(frame_with_buttons((), background=bright_floor))

    assert state.present is False


def test_wrong_frame_size_is_rejected():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)

    state = read_match_end(image)

    assert state.present is False
    assert "帧尺寸未验证" in state.reason


def test_roi_table_matches_measured_geometry():
    """固定 ROI 必须真的把实测按钮包在中间（留出余量）。"""
    for (kind, (x0, y0, x1, y1)), box in zip(
            BUTTON_ROIS, (LEAVE_BOX, SHARE_BOX, CONTINUE_BOX)):
        assert x0 < box[0] and y0 < box[1], kind
        assert box[2] < x1 and box[3] < y1, kind
