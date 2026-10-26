"""从颜色掩膜里找「圆角矩形按钮 / 色块」——与具体画面无关的通用几何件。

## 从哪儿来的

经验来自两处已经在真机上跑通的实现，但这里**不复制它们的 ROI**，只吸收
判定口径：

* ``match_end._button_fill_mask``：按钮填充是「亮 + 有饱和度」
  （V>=150 & S>=40），并且用 ``ROI_MAX_FILL=0.85`` 挡整片同色背景、
  用 ``ROI_MARGIN=6`` 挡"贴边" —— 贴边意味着命中的是整片地板而不是按钮。
* ``lobby_seat``：模板匹配 + 面积/几何门槛（36x36 加号、6px 粗笔画）。

本模块把这些收敂成一个和画面无关的扫描器：**连通域 → 填充率上下限 →
长宽比 → 四边留白 → 面积下限**，全部可配。任何一步不过就丢，并且记下
丢弃原因，便于事后复盘"为什么没认出来"。

## 为什么填充率上下限能挡住"整片地板"

* 纯色地板铺满 ROI：单个连通域几乎等于整个 ROI ⇒ ``fill`` 接近 1.0
  超过 ``max_fill``，同时四边留白 ≈ 0，两道都能拦。
* 但有纹理的地板（本游戏的地毯有明暗块）会碎成很多块，每块面积小、
  填充率高 —— 这时靠 ``min_area`` 和"面积占 ROI 比例上限"拦，以及更根本的：
  地板颜色根本不进具名色区间（见 palette.beige_panel 的注释）。

所以调用方必须同时给出**具名颜色**和**ROI**，本模块从不做"整帧找亮点"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import cv2
import numpy as np

from .palette import ColorSpec, crop_roi, get_spec, mask

__all__ = [
    "ShapeLimits",
    "ButtonShape",
    "ColorRegion",
    "Component",
    "BUTTON_LIMITS",
    "BADGE_LIMITS",
    "REGION_LIMITS",
    "CIRCLE_LIMITS",
    "mask_components",
    "scan_shapes",
    "find_buttons",
    "find_regions",
    "center_of",
    "pick",
    "union_region",
]

Box = tuple[int, int, int, int]
Point = tuple[int, int]


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ShapeLimits:
    """几何过滤条件。所有阈值都作用在**ROI 内**的连通域上。"""

    #: 连通域面积 / 外接矩形面积。太低=细碎图形（文字、描边、图标笔画），
    #: 太高=整个画面同色（地板、纯色背景）。
    min_fill: float = 0.55
    max_fill: float = 0.95
    #: 外接矩形宽高比（w/h）。按钮一律宽>高；圆形件用 CIRCLE_LIMITS。
    min_aspect: float = 0.75
    max_aspect: float = 5.5
    #: 面积绝对下限（像素）。
    min_area: int = 2500
    #: 面积占 ROI 的比例上限。整片同色背景会在这里被挡掉。
    max_area_frac: float = 0.30
    #: 外接矩形到 ROI 四边的最小留白（像素）。贴边 = 命中的是背景。
    min_margin: int = 6
    #: 形态学开运算核（去 1~2px 噪点）。0 表示不做。
    open_ksize: int = 3
    #: 形态学闭运算核（补内部小洞，例如按钮上的文字镂空）。0 表示不做。
    close_ksize: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_fill <= self.max_fill <= 1.0:
            raise ValueError("要求 0 <= min_fill <= max_fill <= 1")
        if not 0.0 < self.min_aspect <= self.max_aspect:
            raise ValueError("要求 0 < min_aspect <= max_aspect")
        if self.min_area < 0:
            raise ValueError("min_area 不能为负")
        if not 0.0 < self.max_area_frac <= 1.0:
            raise ValueError("max_area_frac 必须在 (0,1]")
        if self.min_margin < 0:
            raise ValueError("min_margin 不能为负")

    def as_dict(self) -> dict:
        return {
            "min_fill": self.min_fill,
            "max_fill": self.max_fill,
            "min_aspect": self.min_aspect,
            "max_aspect": self.max_aspect,
            "min_area": self.min_area,
            "max_area_frac": self.max_area_frac,
            "min_margin": self.min_margin,
        }


#: 圆角矩形按钮：宽>高、填充率高、必须离 ROI 四边留白。
BUTTON_LIMITS = ShapeLimits(
    min_fill=0.55, max_fill=0.95,
    min_aspect=0.75, max_aspect=5.5,
    min_area=2500, max_area_frac=0.30, min_margin=6,
)

#: 近方形小件（加号卡片、图标底、角标）。
BADGE_LIMITS = ShapeLimits(
    min_fill=0.45, max_fill=0.96,
    min_aspect=0.70, max_aspect=1.60,
    min_area=1800, max_area_frac=0.25, min_margin=4,
)

#: 大色块（牌桌本体、桌面、抽屉面板）。填充率可以很低（菱形桌布只占外接
#: 矩形的 39%），所以下界放到 0.15，且不要求四边留白。
REGION_LIMITS = ShapeLimits(
    min_fill=0.15, max_fill=1.00,
    min_aspect=0.30, max_aspect=4.00,
    min_area=4000, max_area_frac=0.90, min_margin=0,
)

#: 圆环件（claim 面板的「胡」「过」）。掩膜只抓到外环，填充率天然低。
CIRCLE_LIMITS = ShapeLimits(
    min_fill=0.08, max_fill=0.60,
    min_aspect=0.80, max_aspect=1.25,
    min_area=1200, max_area_frac=0.20, min_margin=2,
)


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ButtonShape:
    """一个通过全部几何门槛的按钮候选。字段与验收口径一一对应。"""

    box: Box
    center: Point
    fill: float
    aspect: float
    margin: int
    color_name: str
    area: int = 0
    roi: Box | None = None

    def as_dict(self) -> dict:
        return {
            "box": list(self.box),
            "center": list(self.center),
            "fill": round(self.fill, 4),
            "aspect": round(self.aspect, 3),
            "margin": self.margin,
            "color": self.color_name,
            "area": self.area,
        }


@dataclass(frozen=True)
class ColorRegion:
    """一块同色区域（不要求是按钮形状）。"""

    box: Box
    center: Point
    fill: float
    aspect: float
    area: int
    color_name: str
    margin: int = 0

    def as_dict(self) -> dict:
        return {
            "box": list(self.box),
            "center": list(self.center),
            "fill": round(self.fill, 4),
            "aspect": round(self.aspect, 3),
            "area": self.area,
            "margin": self.margin,
            "color": self.color_name,
        }


@dataclass(frozen=True)
class Component:
    """一个原始连通域 + 它有没有过几何门槛、为什么没过。"""

    box: Box
    area: int
    fill: float
    aspect: float
    margin: int
    roi_area: int
    color_name: str
    ok: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "box": list(self.box),
            "area": self.area,
            "fill": round(self.fill, 4),
            "aspect": round(self.aspect, 3),
            "margin": self.margin,
            "color": self.color_name,
            "ok": self.ok,
            "reason": self.reason,
        }


def center_of(box: Box) -> Point:
    x0, y0, x1, y1 = (int(v) for v in box[:4])
    return ((x0 + x1) // 2, (y0 + y1) // 2)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def mask_components(mask_image: np.ndarray, *,
                    open_ksize: int = 0, close_ksize: int = 0) -> list[tuple[Box, int]]:
    """掩膜 → [(box, area)]，box 是帧坐标的 (x0,y0,x1,y1)。"""
    if mask_image.ndim != 2:
        raise ValueError("mask_components 需要二维掩膜")
    work = (mask_image > 0).astype(np.uint8) * 255
    if close_ksize and close_ksize > 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_ksize, close_ksize))
        work = cv2.morphologyEx(work, cv2.MORPH_CLOSE, kernel)
    if open_ksize and open_ksize > 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_ksize, open_ksize))
        work = cv2.morphologyEx(work, cv2.MORPH_OPEN, kernel)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(work, 8)
    out: list[tuple[Box, int]] = []
    for index in range(1, count):
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        w = int(stats[index, cv2.CC_STAT_WIDTH])
        h = int(stats[index, cv2.CC_STAT_HEIGHT])
        area = int(stats[index, cv2.CC_STAT_AREA])
        out.append(((x, y, x + w, y + h), area))
    return out


def scan_shapes(image: np.ndarray, spec: ColorSpec | str, roi: Box | None = None,
                limits: ShapeLimits = BUTTON_LIMITS, space: str = "hsv") -> list[Component]:
    """扫描 ROI 内该颜色的所有连通域，逐个标注是否通过几何门槛及原因。

    这是 :func:`find_buttons` / :func:`find_regions` 的公共内核；把"被拒绝
    的原因"也一并返回，是为了让自检脚本能统计拒绝原因分布，而不是只看到
    "没找到"。
    """
    resolved = get_spec(spec) if isinstance(spec, str) else spec
    crop = crop_roi(image, roi)
    if crop.size == 0:
        return []
    offset_x = 0 if roi is None else max(0, int(roi[0]))
    offset_y = 0 if roi is None else max(0, int(roi[1]))
    roi_h, roi_w = crop.shape[:2]
    roi_area = max(1, roi_w * roi_h)

    mask_image = mask(crop, resolved, space=space)
    records: list[Component] = []
    for (bx0, by0, bx1, by1), area in mask_components(
            mask_image, open_ksize=limits.open_ksize, close_ksize=limits.close_ksize):
        width = max(1, bx1 - bx0)
        height = max(1, by1 - by0)
        fill = area / float(width * height)
        aspect = width / float(height)
        margin = min(bx0, by0, roi_w - bx1, roi_h - by1)
        box = (offset_x + bx0, offset_y + by0, offset_x + bx1, offset_y + by1)
        component = Component(
            box=box, area=area, fill=fill, aspect=aspect, margin=margin,
            roi_area=roi_area, color_name=resolved.name,
        )
        reason = ""
        if area < limits.min_area:
            reason = "area_below_min"
        elif area > limits.max_area_frac * roi_area:
            reason = "area_covers_roi"
        elif not (limits.min_fill <= fill <= limits.max_fill):
            reason = "fill_out_of_range"
        elif not (limits.min_aspect <= aspect <= limits.max_aspect):
            reason = "aspect_out_of_range"
        elif margin < limits.min_margin:
            reason = "touches_roi_edge"
        records.append(replace(component, ok=(reason == ""), reason=reason))
    return records


def find_buttons(image: np.ndarray, spec: ColorSpec | str, roi: Box | None = None,
                 limits: ShapeLimits = BUTTON_LIMITS, space: str = "hsv") -> list[ButtonShape]:
    """找圆角矩形按钮，按面积从大到小返回。"""
    found = [
        ButtonShape(
            box=rec.box, center=center_of(rec.box), fill=rec.fill, aspect=rec.aspect,
            margin=rec.margin, color_name=rec.color_name, area=rec.area,
            roi=roi,
        )
        for rec in scan_shapes(image, spec, roi=roi, limits=limits, space=space)
        if rec.ok
    ]
    found.sort(key=lambda item: item.area, reverse=True)
    return found


def find_regions(image: np.ndarray, spec: ColorSpec | str, roi: Box | None = None,
                 limits: ShapeLimits = REGION_LIMITS, space: str = "hsv") -> list[ColorRegion]:
    """找同色大区域（牌桌本体、桌面、面板底色），按面积从大到小返回。"""
    found = [
        ColorRegion(
            box=rec.box, center=center_of(rec.box), fill=rec.fill, aspect=rec.aspect,
            area=rec.area, color_name=rec.color_name, margin=rec.margin,
        )
        for rec in scan_shapes(image, spec, roi=roi, limits=limits, space=space)
        if rec.ok
    ]
    found.sort(key=lambda item: item.area, reverse=True)
    return found


def union_region(candidates: list) -> ColorRegion | None:
    """把多个候选并成一个外接色块（例如"手牌带"="所有牌面外接框"）。"""
    if not candidates:
        return None
    x0 = min(item.box[0] for item in candidates)
    y0 = min(item.box[1] for item in candidates)
    x1 = max(item.box[2] for item in candidates)
    y1 = max(item.box[3] for item in candidates)
    area = sum(item.area for item in candidates)
    width, height = max(1, x1 - x0), max(1, y1 - y0)
    return ColorRegion(
        box=(x0, y0, x1, y1),
        center=center_of((x0, y0, x1, y1)),
        fill=min(1.0, area / float(width * height)),
        aspect=width / float(height),
        area=area,
        color_name=candidates[0].color_name,
        margin=min(item.margin for item in candidates),
    )


def pick(candidates: list, mode: str, roi: Box | None = None):
    """从候选里按策略挑一个；挑不到返回 None。

    支持 ``largest`` / ``smallest`` / ``leftmost`` / ``rightmost`` /
    ``topmost`` / ``bottommost`` / ``nearest_roi_center`` / ``union``。
    策略名写错直接报错，不允许静默取第一个。
    """
    if not candidates:
        return None
    if mode == "largest":
        return max(candidates, key=lambda c: c.area)
    if mode == "smallest":
        return min(candidates, key=lambda c: c.area)
    if mode == "leftmost":
        return min(candidates, key=lambda c: c.center[0])
    if mode == "rightmost":
        return max(candidates, key=lambda c: c.center[0])
    if mode == "topmost":
        return min(candidates, key=lambda c: c.center[1])
    if mode == "bottommost":
        return max(candidates, key=lambda c: c.center[1])
    if mode == "union":
        return union_region(candidates)
    if mode == "nearest_roi_center":
        if roi is None:
            raise ValueError("nearest_roi_center 需要同时给出 roi")
        anchor = center_of(roi)
        return min(candidates, key=lambda c: (c.center[0] - anchor[0]) ** 2
                   + (c.center[1] - anchor[1]) ** 2)
    raise ValueError(f"未知选取策略 {mode!r}")
