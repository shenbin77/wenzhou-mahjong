# -*- coding: utf-8 -*-
"""入桌邀请弹窗的只读识别。

这条链路与行动行、吃牌候选面板的蓝色圆形“取消”完全分离。只有同一帧
同时命中“邀请您进入”文案和矩形“取消”按钮，且两者几何关系符合实测弹窗，
才发布可执行的取消目标；证据不全时只报告 ``present=True``，不提供落点。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


FRAME_SIZE = (900, 1600)
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
PHRASE_TEMPLATE = TEMPLATE_DIR / "invite_phrase.png"
CANCEL_TEMPLATE = TEMPLATE_DIR / "invite_cancel.png"
PHRASE_THRESHOLD = 0.92
CANCEL_THRESHOLD = 0.94


@dataclass(frozen=True)
class InvitationState:
    present: bool
    complete: bool
    phrase_score: float = 0.0
    cancel_score: float = 0.0
    phrase_box: tuple[int, int, int, int] | None = None
    cancel_box: tuple[int, int, int, int] | None = None
    cancel_center: tuple[int, int] | None = None
    reason: str = ""

    def ledger(self) -> dict:
        return {
            "present": self.present,
            "complete": self.complete,
            "phrase_score": round(self.phrase_score, 4),
            "cancel_score": round(self.cancel_score, 4),
            "phrase_box": list(self.phrase_box) if self.phrase_box else None,
            "cancel_box": list(self.cancel_box) if self.cancel_box else None,
            "cancel_center": list(self.cancel_center) if self.cancel_center else None,
            "reason": self.reason,
        }


@lru_cache(maxsize=2)
def _load_gray(path: str) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"邀请弹窗模板不可读: {path}")
    return image


def _match(gray: np.ndarray, path: Path) -> tuple[float, tuple[int, int, int, int]]:
    template = _load_gray(str(path))
    height, width = template.shape[:2]
    if gray.shape[0] < height or gray.shape[1] < width:
        return 0.0, (0, 0, 0, 0)
    result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    _minimum, score, _min_at, at = cv2.minMaxLoc(result)
    x0, y0 = int(at[0]), int(at[1])
    return float(score), (x0, y0, x0 + width, y0 + height)


def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    x0, y0, x1, y1 = box
    return (x0 + x1) // 2, (y0 + y1) // 2


def read_invitation(image: np.ndarray | None) -> InvitationState:
    """读取 1600x900 原生帧里的入桌邀请；不做任何点击。"""
    if image is None:
        return InvitationState(False, False, reason="截图为空")
    if image.shape[:2] != FRAME_SIZE:
        return InvitationState(
            False, False,
            reason=(f"画面尺寸 {image.shape[1]}x{image.shape[0]} 不是已标定的 "
                    f"{FRAME_SIZE[1]}x{FRAME_SIZE[0]}"),
        )
    if image.ndim == 2:
        gray = image
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    phrase_score, phrase_box = _match(gray, PHRASE_TEMPLATE)
    if phrase_score < PHRASE_THRESHOLD:
        return InvitationState(
            False, False, phrase_score=phrase_score,
            reason="未命中邀请您进入文案",
        )

    cancel_score, cancel_box = _match(gray, CANCEL_TEMPLATE)
    if cancel_score < CANCEL_THRESHOLD:
        return InvitationState(
            True, False, phrase_score=phrase_score,
            cancel_score=cancel_score, phrase_box=phrase_box,
            reason="命中邀请文案，但矩形取消按钮证据不足",
        )

    phrase_x, phrase_y = _center(phrase_box)
    cancel_x, cancel_y = _center(cancel_box)
    height, width = gray.shape[:2]
    geometry_ok = (
        0.30 * width <= phrase_x <= 0.72 * width
        and 0.22 * height <= phrase_y <= 0.62 * height
        and 0.15 * width <= cancel_x <= 0.58 * width
        and 0.42 * height <= cancel_y <= 0.84 * height
        and 0.05 * width <= phrase_x - cancel_x <= 0.42 * width
        and 0.08 * height <= cancel_y - phrase_y <= 0.48 * height
    )
    if not geometry_ok:
        return InvitationState(
            True, False, phrase_score=phrase_score,
            cancel_score=cancel_score, phrase_box=phrase_box,
            cancel_box=cancel_box,
            reason="邀请文案与矩形取消按钮的相对几何不符合已标定弹窗",
        )

    return InvitationState(
        True, True, phrase_score=phrase_score, cancel_score=cancel_score,
        phrase_box=phrase_box, cancel_box=cancel_box,
        cancel_center=(cancel_x, cancel_y),
        reason="邀请文案与矩形取消按钮同帧命中，几何关系通过",
    )
