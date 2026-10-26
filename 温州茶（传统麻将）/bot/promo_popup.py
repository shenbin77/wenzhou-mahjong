# -*- coding: utf-8 -*-
"""促销弹窗「限时优惠」关闭按钮（菱形 ❌）的只读识别。

这条链路与出牌、吃碰杠胡、返回大厅完全分离。只有同一帧同时满足
「多尺度模板命中 ≥ :data:`SCORE_MIN`」和「命中处的结构特征与模板一致」，
才发布可执行的关闭落点；证据不全时只报告 ``present=True``，不提供落点。

关闭按钮的实测样貌（BOSS 于 2026-10-26 指认并提供原图）：
奶油色菱形（旋转 45° 的圆角方块）中间一个深橙 ❌，压在金色「+5」上。
模板裁自该原图（``templates/promo/popup_close_x.png``，39x42）。

实机验证（1600x900）：
* emulator-5556 弹窗帧：最佳 0.987 @ (1438,161)，scale=1.45 → 中心 (1466,191)；
* emulator-5558 无弹窗帧：最佳 0.616 —— 与 0.90 阈值之间余量充足。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BOT_DIR / "templates" / "promo" / "popup_close_x.png"

#: 模板匹配接受下限（实测弹窗帧 0.987 / 无弹窗帧 0.616，取 0.90 居中）。
SCORE_MIN = 0.90
#: 多尺度扫描范围（实机最佳 1.45，两侧各留余量）。
SCALE_LO, SCALE_HI, SCALE_STEP = 1.10, 1.60, 0.05
#: 次名差要求。多尺度扫描下相邻尺度的峰值天然接近，跨尺度比次名差没有判别力，
#: 因此默认关闭（0.0），真正的判别交给 :data:`STRUCT_TOLERANCE` 的结构校验。
SCORE_MARGIN = 0.0
#: 结构校验：命中窗口内「奶油菱形」「深橙 ❌」占比与模板自身占比的允许偏差。
STRUCT_TOLERANCE = 0.14


@dataclass(frozen=True)
class PromoPopupState:
    present: bool
    close_center: tuple[int, int] | None = None
    close_box: tuple[int, int, int, int] | None = None
    score: float = 0.0
    scale: float = 0.0
    margin: float = 0.0
    struct_ok: bool = False
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict:
        return {
            "present": self.present,
            "close_center": list(self.close_center) if self.close_center else None,
            "close_box": list(self.close_box) if self.close_box else None,
            "score": round(self.score, 4),
            "scale": round(self.scale, 3),
            "margin": round(self.margin, 4),
            "struct_ok": self.struct_ok,
            "reason": self.reason,
        }


_TEMPLATE_CACHE: dict[str, np.ndarray] = {}


def _template_gray() -> np.ndarray | None:
    """Load (and cache) the close-button template in grayscale."""
    cached = _TEMPLATE_CACHE.get("gray")
    if cached is not None:
        return cached
    if not TEMPLATE_PATH.is_file():
        return None
    raw = np.fromfile(str(TEMPLATE_PATH), dtype=np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _TEMPLATE_CACHE["gray"] = gray
    _TEMPLATE_CACHE["color"] = img
    return gray


def _structure_fractions(image: np.ndarray, box: tuple[int, int, int, int]) -> tuple[float, float]:
    """Return ``(cream_fraction, dark_x_fraction)`` inside ``box``."""
    x0, y0, x1, y1 = box
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    cream = ((val >= 140) & (sat >= 90) & (hue >= 10) & (hue <= 45)).mean()
    dark_x = ((val <= 110) & (hue >= 5) & (hue <= 30)).mean()
    return float(cream), float(dark_x)


def _template_fractions() -> tuple[float, float]:
    color = _TEMPLATE_CACHE.get("color")
    if color is None:
        return 0.0, 0.0
    return _structure_fractions(color, (0, 0, color.shape[1], color.shape[0]))


def read_promo_popup(image: np.ndarray) -> PromoPopupState:
    """识别促销弹窗关闭按钮；证据不足一律不给落点。"""
    if image is None or image.ndim != 3 or image.dtype != np.uint8:
        return PromoPopupState(present=False, reason="帧像素类型未验证")
    tmpl = _template_gray()
    if tmpl is None:
        return PromoPopupState(present=False, reason="关闭按钮模板缺失")
    if image.shape[0] < 200 or image.shape[1] < 200:
        return PromoPopupState(present=False, reason="帧尺寸不在已知范围")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    th, tw = tmpl.shape[:2]
    best_score, best_loc, best_scale, runner = 0.0, None, 0.0, 0.0
    scale = SCALE_LO
    while scale <= SCALE_HI + 1e-9:
        size = (max(2, round(tw * scale)), max(2, round(th * scale)))
        if size[0] >= gray.shape[1] or size[1] >= gray.shape[0]:
            scale += SCALE_STEP
            continue
        resized = cv2.resize(tmpl, size, interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        _, peak, _, loc = cv2.minMaxLoc(result)
        if peak > best_score:
            if best_loc is not None:
                runner = max(runner, best_score)
            best_score, best_loc, best_scale = float(peak), loc, scale
        elif peak > runner and best_loc is not None:
            if abs(loc[0] - best_loc[0]) > tw * scale or abs(loc[1] - best_loc[1]) > th * scale:
                runner = float(peak)
        scale += SCALE_STEP
    if best_loc is None or best_score < SCORE_MIN:
        return PromoPopupState(
            present=False, score=best_score, scale=best_scale,
            reason=f"模板分不足：{best_score:.3f} < {SCORE_MIN}")
    bw, bh = round(tw * best_scale), round(th * best_scale)
    box = (best_loc[0], best_loc[1], best_loc[0] + bw, best_loc[1] + bh)
    margin = best_score - runner
    cream, dark_x = _structure_fractions(image, box)
    t_cream, t_dark = _template_fractions()
    struct_ok = (
        abs(cream - t_cream) <= STRUCT_TOLERANCE
        and abs(dark_x - t_dark) <= STRUCT_TOLERANCE)
    evidence = {
        "template": str(TEMPLATE_PATH),
        "box": list(box),
        "cream_fraction": round(cream, 4),
        "dark_x_fraction": round(dark_x, 4),
        "template_cream": round(t_cream, 4),
        "template_dark_x": round(t_dark, 4),
        "runner_up": round(runner, 4),
    }
    if not struct_ok:
        return PromoPopupState(
            present=True, score=best_score, scale=best_scale, margin=margin,
            struct_ok=False, reason="结构特征与模板不符，拒绝给出落点",
            evidence=evidence)
    if margin < SCORE_MARGIN and runner > 0.0:
        return PromoPopupState(
            present=True, score=best_score, scale=best_scale, margin=margin,
            struct_ok=True, reason=f"次名差不足 {margin:.3f}，拒绝给出落点",
            evidence=evidence)
    center = (best_loc[0] + bw // 2, best_loc[1] + bh // 2)
    return PromoPopupState(
        present=True, close_center=center, close_box=box, score=best_score,
        scale=best_scale, margin=margin, struct_ok=True,
        reason="促销弹窗关闭按钮命中", evidence=evidence)
