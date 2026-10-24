# -*- coding: utf-8 -*-
"""Detect the right-side action row before any automatic discard.

The tea client always showed a blue ``过``/``取消`` disc in the verified
action frames.  This module deliberately answers only the safety question
"may a discard be attempted now?"; it does not guess which action to choose.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


FRAME_SIZE = (900, 1600)
ACTION_BAND_Y = (480, 745)
RIGHT_ROI_X = (1280, 1600)


@dataclass(frozen=True)
class ActionGuardResult:
    state: str
    boxes: tuple[tuple[int, int, int, int], ...] = ()
    reason: str = ""

    @property
    def blocks_discard(self) -> bool:
        return self.state != "clear"


def detect_action_ui(image: np.ndarray | None) -> ActionGuardResult:
    """Return ``clear``, ``action_visible`` or ``uncertain``.

    The strict geometry was replayed over 3,184 native 1600x900 frames: all
    215 frames containing the blue action disc were found and no clear frame
    was selected.  A looser near-match is treated as uncertain and also blocks
    discarding.
    """
    if image is None or not isinstance(image, np.ndarray):
        return ActionGuardResult("uncertain", reason="行动界面检测没有有效画面")
    if image.shape[:2] != FRAME_SIZE:
        return ActionGuardResult(
            "uncertain",
            reason=f"行动界面检测尺寸 {image.shape[1]}x{image.shape[0]} 未验证",
        )

    y0, y1 = ACTION_BAND_Y
    x0, x1 = RIGHT_ROI_X
    crop = image[y0:y1, x0:x1]
    blue = crop[:, :, 0].astype(np.int16)
    red = crop[:, :, 2].astype(np.int16)
    mask = (((blue - red) > 40) & (blue > 90)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((15, 15), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            np.ones((7, 7), np.uint8))
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)

    strict: list[tuple[int, int, int, int]] = []
    near: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if width <= 0 or height <= 0:
            continue
        fill = area / float(width * height)
        gx, gy = x + x0, y + y0
        cx, cy = gx + width / 2.0, gy + height / 2.0
        box = (gx, gy, gx + width, gy + height)
        if (1280 <= cx <= 1580 and 500 <= cy <= 710
                and 70 <= width <= 155 and 70 <= height <= 155
                and area >= 3000 and fill >= 0.30):
            near.append(box)
        if (1320 <= cx <= 1540 and 520 <= cy <= 690
                and 88 <= width <= 135 and 88 <= height <= 135
                and area >= 5200 and fill >= 0.45):
            strict.append(box)

    if strict:
        return ActionGuardResult(
            "action_visible", tuple(strict),
            "检测到吃碰杠胡/过的行动界面，暂停自动出牌",
        )
    if near:
        return ActionGuardResult(
            "uncertain", tuple(near),
            "右侧疑似行动按钮但几何不完整，暂停自动出牌",
        )
    return ActionGuardResult("clear", reason="未检测到行动按钮")

