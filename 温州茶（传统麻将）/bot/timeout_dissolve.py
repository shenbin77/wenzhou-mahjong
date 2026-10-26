# -*- coding: utf-8 -*-
"""“等待时间过长解散比赛！”弹窗的只读、专用视觉识别。

这个模块刻意不包含 ADB 或点击代码。只有 1600x900 原生帧同时满足：

* 已标定的单一奶油色面板；
* 面板外的结算遮罩；
* 精确消息区域的橙色像素指纹；
* 全帧唯一、位于已标定位置的青色矩形按钮；
* 按钮内恰好两个大号白色字形；

才返回 ``confirm_box`` / ``confirm_center``。这条白名单不能被其它“确定”
弹窗复用；证据不完整时 fail closed，只返回原因与量化证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

import cv2
import numpy as np


FRAME_SIZE = (900, 1600)

# 固定原生 1600x900 标定区域。消息指纹来自两台模拟器的独立原始帧，
# 两帧阈值化结果逐像素一致；它不是 OCR，也不依赖外部图片模板。
MESSAGE_ROI = (620, 380, 980, 440)
MESSAGE_SIZE = (48, 12)
MESSAGE_THRESHOLD = 64
MESSAGE_MAX_HAMMING = 4
MESSAGE_INK_RANGE = (3150, 3350)
_MESSAGE_BITS = bytes.fromhex(
    "000000000000000000000000000000000000"
    "3ab5f2ab557852be2ecf7dba3bf5f2ef773a7ffdf2cff53a"
    "7b35f6aff5282a843efcb7ba"
    "000000000000000000000000000000000000"
)
assert len(_MESSAGE_BITS) * 8 == MESSAGE_SIZE[0] * MESSAGE_SIZE[1]


@dataclass(frozen=True)
class TimeoutDissolveState:
    """一次只读识别的完整证据。

    ``present`` 只表示精确消息指纹命中；``complete`` 才表示所有点击门禁
    同时通过。落点仅在 ``complete=True`` 时发布。
    """

    present: bool
    complete: bool
    confirm_box: tuple[int, int, int, int] | None = None
    confirm_center: tuple[int, int] | None = None
    panel_box: tuple[int, int, int, int] | None = None
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict:
        return {
            "present": self.present,
            "complete": self.complete,
            "confirm_box": list(self.confirm_box) if self.confirm_box else None,
            "confirm_center": (
                list(self.confirm_center) if self.confirm_center else None
            ),
            "panel_box": list(self.panel_box) if self.panel_box else None,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def _components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask)
    return [tuple(map(int, stats[index])) for index in range(1, count)]


def _message_evidence(hsv: np.ndarray) -> tuple[bool, dict]:
    x0, y0, x1, y1 = MESSAGE_ROI
    crop = hsv[y0:y1, x0:x1]
    orange = cv2.inRange(
        crop,
        np.array([3, 100, 80], dtype=np.uint8),
        np.array([28, 255, 255], dtype=np.uint8),
    )
    ink = int(np.count_nonzero(orange))
    reduced = cv2.resize(orange, MESSAGE_SIZE, interpolation=cv2.INTER_AREA)
    actual = np.packbits(reduced >= MESSAGE_THRESHOLD).tobytes()
    xor = bytes(a ^ b for a, b in zip(actual, _MESSAGE_BITS))
    hamming = sum(value.bit_count() for value in xor)
    digest = hashlib.sha256(actual).hexdigest()[:16]
    ink_ok = MESSAGE_INK_RANGE[0] <= ink <= MESSAGE_INK_RANGE[1]
    return ink_ok and hamming <= MESSAGE_MAX_HAMMING, {
        "roi": list(MESSAGE_ROI),
        "orange_ink_pixels": ink,
        "ink_ok": ink_ok,
        "fingerprint_hamming": hamming,
        "fingerprint_limit": MESSAGE_MAX_HAMMING,
        "fingerprint_sha256_16": digest,
    }


def _panel_evidence(hsv: np.ndarray) -> tuple[bool, tuple[int, int, int, int] | None, dict]:
    cream = cv2.inRange(
        hsv,
        np.array([10, 15, 180], dtype=np.uint8),
        np.array([40, 180, 255], dtype=np.uint8),
    )
    candidates = []
    for x, y, width, height, area in _components(cream):
        if (
            460 <= x <= 490
            and 210 <= y <= 235
            and 660 <= width <= 690
            and 420 <= height <= 445
            and area >= 235_000
        ):
            candidates.append((x, y, x + width, y + height, area))
    box = tuple(candidates[0][:4]) if len(candidates) == 1 else None
    return len(candidates) == 1, box, {
        "candidate_count": len(candidates),
        "candidates": [list(candidate) for candidate in candidates],
    }


def _overlay_evidence(hsv: np.ndarray) -> tuple[bool, dict]:
    outside = np.ones(FRAME_SIZE, dtype=bool)
    outside[200:680, 440:1160] = False
    dark_fraction = float(np.mean(hsv[:, :, 2][outside] < 110))
    return dark_fraction >= 0.985, {
        "outside_dark_fraction": round(dark_fraction, 6),
        "minimum": 0.985,
    }


def _button_label_ok(
    hsv: np.ndarray,
    box: tuple[int, int, int, int],
) -> tuple[bool, dict]:
    x0, y0, x1, y1 = box
    crop = hsv[y0:y1, x0:x1]
    white = cv2.inRange(
        crop,
        np.array([0, 0, 180], dtype=np.uint8),
        np.array([179, 80, 255], dtype=np.uint8),
    )
    glyphs = []
    for x, y, width, height, area in _components(white):
        if 35 <= width <= 45 and 38 <= height <= 48 and 800 <= area <= 1_300:
            glyphs.append((x, y, width, height, area))
    glyphs.sort()
    geometry_ok = (
        len(glyphs) == 2
        and 52 <= glyphs[0][0] <= 65
        and 110 <= glyphs[1][0] <= 122
        and abs(glyphs[0][1] - glyphs[1][1]) <= 3
    )
    return geometry_ok, {
        "large_white_glyph_count": len(glyphs),
        "large_white_glyphs": [list(glyph) for glyph in glyphs],
    }


def _button_evidence(
    hsv: np.ndarray,
) -> tuple[bool, tuple[int, int, int, int] | None, dict]:
    teal = cv2.inRange(
        hsv,
        np.array([75, 80, 100], dtype=np.uint8),
        np.array([105, 255, 255], dtype=np.uint8),
    )
    button_like = []
    for x, y, width, height, area in _components(teal):
        fill = area / float(width * height)
        if 180 <= width <= 330 and 70 <= height <= 115 and fill >= 0.68:
            button_like.append((x, y, x + width, y + height, area, fill))

    box = None
    geometry_ok = False
    if len(button_like) == 1:
        candidate = button_like[0]
        x0, y0, x1, y1 = candidate[:4]
        geometry_ok = (
            680 <= x0 <= 705
            and 530 <= y0 <= 555
            and 205 <= x1 - x0 <= 225
            and 80 <= y1 - y0 <= 100
        )
        if geometry_ok:
            box = (x0, y0, x1, y1)

    label_ok, label_evidence = (
        _button_label_ok(hsv, box) if box is not None else (False, {
            "large_white_glyph_count": 0,
            "large_white_glyphs": [],
        })
    )
    evidence = {
        "candidate_count": len(button_like),
        "candidates": [
            [*candidate[:5], round(candidate[5], 6)]
            for candidate in button_like
        ],
        "geometry_ok": geometry_ok,
        "label_ok": label_ok,
        **label_evidence,
    }
    return box is not None and label_ok, box, evidence


def read_timeout_dissolve(image: np.ndarray | None) -> TimeoutDissolveState:
    """读取精确超时解散弹窗；不做任何点击。"""
    if image is None or not isinstance(image, np.ndarray):
        return TimeoutDissolveState(False, False, reason="截图为空")
    if image.shape[:2] != FRAME_SIZE or image.ndim != 3 or image.dtype != np.uint8:
        height = image.shape[0] if image.ndim >= 1 else 0
        width = image.shape[1] if image.ndim >= 2 else 0
        return TimeoutDissolveState(
            False,
            False,
            reason=(
                f"画面 {width}x{height} 或像素格式不是已标定的 "
                f"{FRAME_SIZE[1]}x{FRAME_SIZE[0]} BGR uint8"
            ),
        )

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    message_ok, message = _message_evidence(hsv)
    panel_ok, panel_box, panel = _panel_evidence(hsv)
    overlay_ok, overlay = _overlay_evidence(hsv)
    button_ok, button_box, button = _button_evidence(hsv)
    evidence = {
        "frame_size": [image.shape[1], image.shape[0]],
        "message": message,
        "panel": panel,
        "overlay": overlay,
        "button": button,
        "checks": {
            "message": message_ok,
            "panel": panel_ok,
            "overlay": overlay_ok,
            "button": button_ok,
        },
    }

    present = message_ok
    complete = message_ok and panel_ok and overlay_ok and button_ok
    if complete and button_box is not None:
        x0, y0, x1, y1 = button_box
        return TimeoutDissolveState(
            True,
            True,
            confirm_box=button_box,
            confirm_center=((x0 + x1) // 2, (y0 + y1) // 2),
            panel_box=panel_box,
            reason="精确解散文案、结算遮罩与唯一青色确定按钮同帧通过",
            evidence=evidence,
        )

    failed = [name for name, ok in evidence["checks"].items() if not ok]
    return TimeoutDissolveState(
        present,
        False,
        panel_box=panel_box,
        reason="证据不完整，拒绝给出落点：" + ", ".join(failed),
        evidence=evidence,
    )
