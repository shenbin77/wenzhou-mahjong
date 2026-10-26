"""文字/图元锚点：在给定 ROI 内做多尺度模板匹配，并且要求"次名差"够大。

## 为什么需要"次名差"

``cv2.TM_CCOEFF_NORMED`` 的绝对值不可靠：整片平涂的区域（按钮、面板底色）
会给出 0.95 以上的高分，因为归一化互相关对"几乎没有结构"的图块天然宽松。
只卡一个 ``score >= 0.8`` 的阈值，等于允许模板在任意一块同色平地上"糊"住。

所以这里在 ``score`` 之外再要求 ``margin = 第一名 - 第二名`` 足够大：如果
同一个模板在画面里能糊住好几个地方（典型的"+"、"确定"、相同的图标），
第二名分差就会很小，于是整次匹配被拒。第二名在**抑制掉与第一名重叠的
候选之后**取，避免"同一个目标的邻近像素"被当成竞争者。

## 多尺度

模板来自某一张真帧的裁剪，不同版式/分辨率下目标尺寸会变，因此按
``scales`` 逐档缩放模板（默认 0.85~1.15 步长 0.05，共 7 档），取全局最高分。
缩放后模板超过 ROI 或小于 8px 的档位直接跳过（不抛异常）。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .palette import crop_roi, load_gray

__all__ = [
    "AnchorHit",
    "DEFAULT_SCALES",
    "MIN_TEMPLATE_SIDE",
    "match",
    "iou",
]

Box = tuple[int, int, int, int]
Point = tuple[int, int]

#: 多尺度档位。超过 ±15% 就不该再靠缩放硬扛了——那说明版式变了，
#: 应该重新登记一个画面，而不是把模板拉到 1.5 倍去糊。
DEFAULT_SCALES: tuple[float, ...] = (0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15)

MIN_TEMPLATE_SIDE = 8


@dataclass(frozen=True)
class AnchorHit:
    """一次成功锚定。``margin`` 是第一名与"抑制后的第二名"之差。"""

    score: float
    box: Box
    center: Point
    scale: float
    runner_up: float
    margin: float
    template_size: tuple[int, int]

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "box": list(self.box),
            "center": list(self.center),
            "scale": self.scale,
            "runner_up": round(self.runner_up, 4),
            "margin": round(self.margin, 4),
            "template_size": list(self.template_size),
        }


def iou(a: Box, b: Box) -> float:
    """交并比。用于抑制"同一个目标的邻近命中"。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ValueError(f"无法转灰度：shape={image.shape}")


def match(image: np.ndarray, template: np.ndarray, roi: Box | None = None, *,
          scales: tuple[float, ...] = DEFAULT_SCALES,
          min_score: float = 0.80,
          min_margin: float = 0.03,
          overlap_iou: float = 0.25) -> AnchorHit | None:
    """在 ``roi`` 内锚定 ``template``；不可信就返回 None（默认拒绝）。

    参数
    ----
    image
        整帧 BGR（也接受灰度）。
    template
        灰度模板（``np.ndarray``），必须比 ROI 小。
    roi
        搜索范围 (x0,y0,x1,y1)。None 表示整帧。
    min_score
        第一名最低分。低于它就当"不在场"。
    min_margin
        第一名减第二名的最小差。低于它就当"糊住了、不可信"。
    overlap_iou
        与第一名 IoU 超过这个值的候选视为"同一个目标"，不参与次名计算。
    """
    if template is None or not isinstance(template, np.ndarray) or template.size == 0:
        return None
    if template.ndim != 2:
        template = _to_gray(template)
    if min(template.shape) < MIN_TEMPLATE_SIDE:
        return None

    search = crop_roi(image, roi)
    if search.size == 0:
        return None
    gray = _to_gray(search)
    offset_x = 0 if roi is None else max(0, int(roi[0]))
    offset_y = 0 if roi is None else max(0, int(roi[1]))

    best_score = -1.0
    best_box: Box | None = None
    best_scale = 1.0
    best_size = (0, 0)
    candidates: list[tuple[float, Box]] = []
    best_map: np.ndarray | None = None

    for scale in scales:
        if scale <= 0:
            continue
        width = max(1, int(round(template.shape[1] * scale)))
        height = max(1, int(round(template.shape[0] * scale)))
        if width < MIN_TEMPLATE_SIDE or height < MIN_TEMPLATE_SIDE:
            continue
        if height > gray.shape[0] or width > gray.shape[1]:
            continue
        scaled = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        if scaled.std() < 1e-6:
            # 纯色模板在 TM_CCOEFF_NORMED 下会退化成数值噪声，直接跳过。
            continue
        result = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
        _min_v, max_v, _min_loc, max_loc = cv2.minMaxLoc(result)
        score = float(max_v)
        box = (offset_x + int(max_loc[0]), offset_y + int(max_loc[1]),
               offset_x + int(max_loc[0]) + width, offset_y + int(max_loc[1]) + height)
        candidates.append((score, box))
        if score > best_score:
            best_score, best_box, best_scale, best_size = score, box, float(scale), (width, height)
            best_map = result

    if best_box is None or best_map is None:
        return None

    # 次名：① 同一尺度里抑制掉与第一名重叠的峰；② 其他尺度各自的最高分。
    suppressed = best_map.copy()
    local_x = best_box[0] - offset_x
    local_y = best_box[1] - offset_y
    width, height = best_size
    pad = int(round(max(width, height) * 0.5))
    sx0 = max(0, local_x - pad)
    sy0 = max(0, local_y - pad)
    sx1 = min(suppressed.shape[1], local_x + width + pad)
    sy1 = min(suppressed.shape[0], local_y + height + pad)
    suppressed[sy0:sy1, sx0:sx1] = -1.0
    same_scale_second = float(suppressed.max()) if suppressed.size else -1.0

    other = [score for score, box in candidates if iou(box, best_box) <= overlap_iou]
    runner_up = max([same_scale_second] + other + [0.0])
    margin = best_score - runner_up

    if best_score < min_score or margin < min_margin:
        return None
    return AnchorHit(
        score=best_score,
        box=best_box,
        center=((best_box[0] + best_box[2]) // 2, (best_box[1] + best_box[3]) // 2),
        scale=best_scale,
        runner_up=runner_up,
        margin=margin,
        template_size=best_size,
    )


def match_path(image: np.ndarray, template_path, roi: Box | None = None, **kwargs) -> AnchorHit | None:
    """从磁盘读模板再匹配（中文路径安全，读不到就返回 None）。"""
    template = load_gray(template_path)
    if template is None:
        return None
    return match(image, template, roi, **kwargs)
