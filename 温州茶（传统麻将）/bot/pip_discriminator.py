"""独立的筒牌（饼子）点数判据：不依赖模板相似度，直接数圆点。

背景：实机帧里 四筒/五筒 的字形模板相似度只差 0.0123（score 0.8921 vs
候选 t5），低于全局 margin 阈值而整手弃权。字形的灰度/色块签名对"少一个
中心圆点"这种局部差异不敏感，继续调模板权重或阈值只会把五筒放成四筒。

本模块给出一个**物理量**：牌面 ROI 上到底有几个筒子圆点，以及这些圆点的
排布是否符合该点数的标准形状。它是独立证据，不是"在两个候选里挑一个"：
  * 数得出、排布对、而且和候选之一吻合 → 可以定牌；
  * 数不出、排布不标准、或和两个候选都不吻合 → 调用方必须继续弃权。

实现要点（都是可复算的确定性步骤，无模型、无随机性）：
1. 牌面 ROI：把牌框往里收 5% 高 / 7% 宽（圆点永远不会落在边框上），得到
   参与统计的内区。本模块刻意不 import tile_vision：模板签名与点数判据是
   两条互相独立的证据链，共用预处理会让"同一次误判"同时骗过两个判据。
2. 墨迹掩膜：红、绿、深墨三色分开取并集。同一套牌里 4/5 筒是绿环+红心，
   8 筒是深墨环，所以三色都必须收。
3. 圆形定位：把每个外部轮廓填充成实心圆盘（环形→圆盘），再做距离变换，
   距离变换的局部极大值正好落在每个圆点的圆心，峰值≈圆点半径。两个圆点
   粘连时轮廓会合并成一个连通域，但距离变换仍会在各自圆心出峰。
4. 粘连接缝伪峰：多个圆点相切处会出现一个**半径更大**的峰（实测 8 筒的
   2×4 排布接缝峰 19.0 px vs 真实圆点 13.8 px；7 筒接缝 16.2 vs 12.0）。
   因此只保留半径落在中位数 [0.70, 1.25] 倍区间内的峰。
5. 排布校验：把圆心坐标归一化到牌面，按标准筒子排布逐点数校验（见
   `_layout_ok`）。排布不符就当作"读不出来"，不做降级猜测。
6. 宁可少认，不可多认：内区被裁到边的连通域（上方悬垂的弃牌角标、或半颗
   被裁掉的圆点）直接判"点数不可信"；单峰连通域不是圆的（牌背/整块色块）
   同样判不可信。这两条都只会让调用方继续弃权，不会让它猜。

边界：本模块只认筒牌 1-9 点。条/万/字牌没有圆点结构，调用方不得用它去
裁决非筒牌候选（`expected_pip_count` 对非 t* 标签返回 None）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

# 牌面内裁剪边距：圆点不会贴边，去掉 5% 高 / 7% 宽可避开边框抗锯齿与方向标记。
_INNER_Y = .05
_INNER_X = .07

# 轮廓填充门槛（占内区面积）：低于此值的碎点是噪声，不是筒子圆点。
_MIN_BLOB_FRACTION = .003

# 距离变换峰值门槛（占内区高度）：圆点半径约为牌高的 11%-13%。
_MIN_PEAK_FRACTION = .045

# 峰值半径一致性：真实圆点半径彼此相同，粘连接缝峰明显更大。
_PEAK_SHRINK = .70
_PEAK_GROW = 1.25

# 同一圆点可能产生相邻的多个极大值（平台），按归一化距离合并。
_MERGE_DISTANCE = .10

# 横/纵坐标聚类容差与最小簇间距（归一化到牌面）。
_CLUSTER_TOL = .14
_MIN_GRID_GAP = .17

# 斜排（3 点、7 点的上半段）：三颗圆点沿对角线，横向间隔必然大，纵向间隔
# 会被压缩（7 筒实测 dy 只有 0.108-0.116），所以纵/横用不同下限，并要求
# 两段的斜率彼此接近（真是同一条斜线，而不是散点）。
_DIAGONAL_MIN_DX = .10
_DIAGONAL_MIN_DY = .07
_DIAGONAL_SLOPE_RATIO = 3.0

# 连通域面积自检：每个圆点分摊到的"离心率²面积"应约为 πr²。实测所有正常
# 筒牌模板/实机帧的比值是 1.02-1.15（含 7 筒、9 筒这类圆点相切成整块的排布）；
# 圆点被融合吞掉时比值 2.38（某皮肤的 8 筒）。取 1.35 留出余量，方向永远是
# "宁可弃权"。
_BLOB_AREA_RATIO = 1.35

# 红心判定：以圆心为心、0.85 倍半径为半径的圆盘内，红色像素占比。
_RED_DISC_SCALE = .85
_RED_FRACTION = .20

# 标准排布里"红心"的硬约束。只对 4/5 点设硬门槛：这两档正是本判据要裁决
# 的歧义对，且实测（两套模板 + 两张实机帧）四筒 0 个红点、五筒恰 1 个红点。
# 其他点数不设门槛，避免因皮肤差异反而丢掉可读的牌。
_RED_RULE = {4: 0, 5: 1}

DOT_LABELS = tuple(f't{i}' for i in range(1, 10))


@dataclass(frozen=True)
class PipReading:
    """One dot-tile pip reading; every field is directly checkable evidence.

    Attributes:
        count: 读出的圆点个数；不可读时为 None。
        layout_ok: 圆点排布是否符合 count 点数的标准形状。
        confidence: 0-1 的置信度，只用于排序/审计，调用方不得用它放宽门槛。
        centers: 归一化圆心坐标 (cx, cy)，按 y 再按 x 排序。
        radii: 每个圆心的距离变换峰值（像素），与 centers 一一对应。
        red_flags: 每个圆心是否判定为红心。
        reason: 结论来源。'ok' 表示证据完整；其余都是弃权原因：
            'empty'（无牌面 ROI）、'no_ink'（几乎没有墨迹）、'no_blob'
            （找不到圆斑）、'radius_spread'（圆点大小不一致）、'border_touch'
            （连通域被内区裁边）、'blob_shape'（单峰连通域不是圆的）、
            'fused_blob'（圆点糊成一块、点数会偏小）、'layout'（排布不符标准）、
            'red_rule'（红心数量与该点数矛盾）。
    """

    count: Optional[int]
    layout_ok: bool
    confidence: float
    centers: Tuple[Tuple[float, float], ...]
    radii: Tuple[float, ...]
    red_flags: Tuple[bool, ...]
    reason: str

    @property
    def red_count(self) -> int:
        """Number of pips judged red-centred."""
        return int(sum(1 for flag in self.red_flags if flag))

    def as_dict(self) -> dict:
        """JSON-friendly evidence for vision diagnostics / audit logs."""
        return {
            'count': self.count,
            'layout_ok': self.layout_ok,
            'confidence': round(float(self.confidence), 4),
            'reason': self.reason,
            'red_count': self.red_count,
            'centers': [[round(float(x), 4), round(float(y), 4)]
                        for x, y in self.centers],
            'radii': [round(float(r), 2) for r in self.radii],
        }


def expected_pip_count(label) -> Optional[int]:
    """Return the pip count a vision label denotes, or None for non-dot labels."""
    if not isinstance(label, str) or len(label) < 2 or label[0] != 't':
        return None
    if not label[1:].isdigit():
        return None
    value = int(label[1:])
    return value if 1 <= value <= 9 else None


def is_dot_label(label) -> bool:
    """True for the nine dot (筒) labels t1..t9."""
    return expected_pip_count(label) is not None


def _face_pixels(crop) -> Optional[np.ndarray]:
    """Return the inner face pixels (border trimmed), or None when unusable."""
    if crop is None or getattr(crop, 'size', 0) == 0:
        return None
    if crop.ndim != 3 or crop.shape[2] != 3:
        return None
    height, width = crop.shape[:2]
    if height < 24 or width < 24:
        return None
    dy, dx = max(2, int(height * _INNER_Y)), max(2, int(width * _INNER_X))
    inner = crop[dy:height - dy, dx:width - dx]
    if inner.size == 0 or inner.shape[0] < 12 or inner.shape[1] < 12:
        return None
    return inner


def _ink_channels(inner):
    """Split the face into red / green / dark ink masks.

    The three colours are kept apart on purpose: red carries the 5-dot centre
    and the 3/9-dot middle row, green carries most rings, and the 8-dot skin
    is drawn in dark ink instead of green.
    """
    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    gray = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY)
    background = float(np.percentile(gray, 75))
    red = ((hue < 19) | (hue > 165)) & (sat > 100) & (val > 35)
    green = (hue >= 32) & (hue <= 95) & (sat > 90) & (val > 35)
    dark = (gray < max(35., background * .62)) & ~red & ~green
    return red, green, dark


def _fill_blobs(mask):
    """Fill every external contour into a solid disc.

    A pip is drawn as a ring (ink annulus + cream hole + centre dot). Filling
    the external contour turns it into a disc so the distance transform has a
    single peak at the pip centre instead of a ridge along the ring.

    Returns:
        (filled, labels, border_labels). `labels` 保存每个圆盘的连通域编号；
        `border_labels` 是"被内区裁到边"的连通域集合 —— 它们既可能是画面上
        方悬垂的弃牌角标，也可能是 ROI 裁掉了半颗圆点。两种情况下点数都不可
        信，调用方必须弃权（宁可少认，不可把 6 筒读成 4 筒）。
    """
    height, width = mask.shape[:2]
    area = float(height * width)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros((height, width), np.uint8)
    border_labels = set()
    next_label = 0
    for contour in contours:
        if cv2.contourArea(contour) < area * _MIN_BLOB_FRACTION:
            continue
        next_label += 1
        cv2.drawContours(filled, [contour], -1, next_label, -1)
        x, y, w, h = cv2.boundingRect(contour)
        if x <= 0 or y <= 0 or x + w >= width or y + h >= height:
            border_labels.add(next_label)
    mask = (filled > 0).astype(np.uint8)
    return mask, filled, border_labels


def _peaks(filled, labels=None):
    """Local maxima of the distance transform, sorted by (y, x).

    Returns a list of (normalised_x, normalised_y, radius, component_label).
    """
    height, width = filled.shape[:2]
    if np.count_nonzero(filled) == 0:
        return []
    distance = cv2.distanceTransform(filled, cv2.DIST_L2, 5)
    # A 9x9 dilation window (±4 px) is far smaller than the pip pitch (~27 px)
    # but larger than the distance-transform ripple inside one pip, so two
    # touching pips keep their own peaks while one pip yields exactly one.
    dilated = cv2.dilate(distance, np.ones((9, 9), np.uint8))
    local_max = (distance >= dilated - 1e-4) & (distance > height * _MIN_PEAK_FRACTION)
    ys, xs = np.where(local_max)
    raw = [(int(x), int(y), float(distance[y, x])) for y, x in zip(ys, xs)]
    raw.sort(key=lambda item: (-item[2], item[1], item[0]))
    merged = []
    for x, y, radius in raw:
        nx, ny = x / width, y / height
        if all(abs(nx - mx) > _MERGE_DISTANCE or abs(ny - my) > _MERGE_DISTANCE
               for mx, my, _, _ in merged):
            label = int(labels[y, x]) if labels is not None else 0
            merged.append((nx, ny, radius, label))
    merged.sort(key=lambda item: (item[1], item[0]))
    return merged


def _clusters(values: Sequence[float], tol: float = _CLUSTER_TOL):
    """Group near-equal coordinates; returns a list of value lists."""
    groups = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tol:
            groups[-1].append(value)
        else:
            groups.append([value])
    return groups


def _grid_ok(points, columns: int, rows: int) -> bool:
    """True when points form a regular `columns` x `rows` grid."""
    if len(points) != columns * rows or columns < 1 or rows < 1:
        return False
    xs = _clusters([p[0] for p in points])
    ys = _clusters([p[1] for p in points])
    if len(xs) != columns or len(ys) != rows:
        return False
    if any(len(group) != rows for group in xs):
        return False
    if any(len(group) != columns for group in ys):
        return False
    if columns > 1:
        centers = [float(np.mean(g)) for g in xs]
        if min(b - a for a, b in zip(centers, centers[1:])) < _MIN_GRID_GAP:
            return False
    if rows > 1:
        centers = [float(np.mean(g)) for g in ys]
        if min(b - a for a, b in zip(centers, centers[1:])) < _MIN_GRID_GAP:
            return False
    return True


def _diagonal_indices(points) -> Optional[Tuple[int, ...]]:
    """Indices of the three points forming a monotone diagonal, if any.

    The 3-dot tile runs corner to corner; the 7-dot tile puts the same three
    pips on a compressed diagonal above its 2x2 block. Both are accepted here,
    so the test is structural: distinct, increasing x steps, monotone y steps
    in the same direction, and a consistent slope.
    """
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            for k in range(j + 1, len(points)):
                triple = sorted((points[i], points[j], points[k]))
                xs = [p[0] for p in triple]
                ys = [p[1] for p in triple]
                dxs = [b - a for a, b in zip(xs, xs[1:])]
                dys = [b - a for a, b in zip(ys, ys[1:])]
                if min(dxs) < _DIAGONAL_MIN_DX:
                    continue
                if min(abs(dy) for dy in dys) < _DIAGONAL_MIN_DY:
                    continue
                if dys[0] * dys[1] <= 0:  # must keep turning the same way
                    continue
                slopes = [dy / dx for dx, dy in zip(dxs, dys)]
                if max(abs(s) for s in slopes) > _DIAGONAL_SLOPE_RATIO * min(
                        abs(s) for s in slopes):
                    continue
                return (i, j, k)
    return None



def _is_diagonal(points) -> bool:
    """True when the three points run corner to corner."""
    return len(points) == 3 and _diagonal_indices(points) is not None


def _layout_ok(points, count: int) -> bool:
    """Validate the standard mahjong dot layout for `count` pips.

    Reference positions were measured on the game's own renders (both the
    4-player and 2-player skins); every accepted layout below was observed on
    a frame whose template top-1 label matched the count.
    """
    if count == 1:
        return (len(points) == 1
                and abs(points[0][0] - .50) <= .22
                and abs(points[0][1] - .54) <= .22)
    if count == 2:
        return (_grid_ok(points, 1, 2)
                and abs(float(np.mean([p[0] for p in points])) - .50) <= .22)
    if count == 3:
        return _is_diagonal(points)
    if count == 4:
        return _grid_ok(points, 2, 2)
    if count == 5:
        # Quincunx: four corner pips plus one centre pip (red on every skin).
        for index, point in enumerate(points):
            if abs(point[0] - .50) > .17 or abs(point[1] - .54) > .17:
                continue
            rest = [p for i, p in enumerate(points) if i != index]
            if all(abs(p[0] - .50) > .20 or abs(p[1] - .54) > .20 for p in rest) \
                    and _grid_ok(rest, 2, 2):
                return True
        return False
    if count == 6:
        return _grid_ok(points, 2, 3)
    if count == 7:
        # Three pips on the diagonal above a 2x2 block.
        diagonal = _diagonal_indices(points)
        if diagonal is None:
            return False
        low = max(points[i][1] for i in diagonal)
        rest = [p for i, p in enumerate(points) if i not in diagonal]
        if any(p[1] <= low + .05 for p in rest):
            return False
        return _grid_ok(rest, 2, 2)
    if count == 8:
        # Two columns of four (both observed skins); a rotated 4x2 is equally
        # standard, so either orientation is accepted.
        return _grid_ok(points, 2, 4) or _grid_ok(points, 4, 2)
    if count == 9:
        return _grid_ok(points, 3, 3)
    return False


def _red_flags(inner, red_mask, centers, radii):
    """Mark pips whose disc is dominated by red pixels (5/3/9-dot centres)."""
    height, width = inner.shape[:2]
    flags = []
    for (cx, cy), radius in zip(centers, radii):
        px, py = int(round(cx * width)), int(round(cy * height))
        reach = max(2, int(round(radius * _RED_DISC_SCALE)))
        x0, x1 = max(0, px - reach), min(width, px + reach + 1)
        y0, y1 = max(0, py - reach), min(height, py + reach + 1)
        disc = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.circle(disc, (px - x0, py - y0), reach, 1, -1)
        area = int(np.count_nonzero(disc))
        if area == 0:
            flags.append(False)
            continue
        hit = int(np.count_nonzero(red_mask[y0:y1, x0:x1] & (disc > 0)))
        flags.append(hit / area >= _RED_FRACTION)
    return tuple(flags)


def _empty(reason: str) -> PipReading:
    """A reading that carries no usable evidence."""
    return PipReading(count=None, layout_ok=False, confidence=0.,
                      centers=(), radii=(), red_flags=(), reason=reason)


def read_dot_pips(crop) -> PipReading:
    """Count and validate the pips of one dot-tile face crop.

    Args:
        crop: BGR tile crop (the whole tile, including its base) or None.

    Returns:
        A PipReading. `count` is None whenever the evidence is not conclusive;
        callers must then keep their existing abstention behaviour.
    """
    inner = _face_pixels(crop)
    if inner is None:
        return _empty('empty')
    red_mask, green_mask, dark_mask = _ink_channels(inner)
    ink = (red_mask | green_mask | dark_mask).astype(np.uint8)
    if np.count_nonzero(ink) < inner.shape[0] * inner.shape[1] * .01:
        return _empty('no_ink')
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    filled, labels, border_labels = _fill_blobs(ink)
    peaks = _peaks(filled, labels)
    if not peaks:
        return _empty('no_blob')

    radii = np.array([p[2] for p in peaks], dtype=np.float32)
    median = float(np.median(radii))
    if median <= 0:
        return _empty('no_blob')
    keep = [p for p in peaks
            if _PEAK_SHRINK * median <= p[2] <= _PEAK_GROW * median]
    if not keep:
        return _empty('radius_spread')
    kept_radii = np.array([p[2] for p in keep], dtype=np.float32)
    # A single pip (1-dot, and any tile whose pips merged into one blob) has no
    # second radius to compare against; that is reported, not trusted blindly.
    if len(keep) > 1:
        spread = float(kept_radii.max() - kept_radii.min()) / float(kept_radii.mean())
        if spread > .45:
            return _empty('radius_spread')
    else:
        spread = 0.
    centers = tuple((float(p[0]), float(p[1])) for p in keep)
    flags = _red_flags(inner, red_mask, centers, kept_radii)
    count = len(centers)
    # A peak inside a blob that the ROI cut at the border may be a hanging
    # discard marker, or half a pip with the other half outside the box. The
    # count is then not evidence of anything, so it is reported only for audit.
    if any(p[3] in border_labels for p in keep):
        return PipReading(count=None, layout_ok=False, confidence=0.,
                          centers=centers, radii=tuple(float(r) for r in kept_radii),
                          red_flags=flags, reason='border_touch')
    # 每个连通域"面积 / (峰数 × πr²)"必须在 1 附近：圆点是等大的实心圆盘，被
    # 数到的每一颗都该分摊到自己那份面积。比值超标说明这个连通域里有圆点被
    # 融合吞掉了（实测：某皮肤 8 筒 8 颗点全糊成一块，距离变换只在四角出峰、
    # 中间两颗没有峰，比值 2.38）——此时点数会**偏小且看着很合理**（4 点还
    # 正好是标准 2×2），必须判为不可读，绝不能拿它去裁决四筒。
    per_blob = {}
    for peak in keep:
        per_blob.setdefault(peak[3], []).append(peak)
    for blob, group in per_blob.items():
        pixels = int(np.count_nonzero(labels == blob))
        radius = float(np.median([p[2] for p in group]))
        expected = len(group) * np.pi * radius ** 2
        if pixels > _BLOB_AREA_RATIO * expected:
            return PipReading(count=None, layout_ok=False, confidence=0.,
                              centers=centers,
                              radii=tuple(float(r) for r in kept_radii),
                              red_flags=flags, reason='fused_blob')
    layout_ok = _layout_ok(centers, count)

    if not layout_ok:
        return PipReading(count=None, layout_ok=False, confidence=0.,
                          centers=centers, radii=tuple(float(r) for r in kept_radii),
                          red_flags=flags, reason='layout')
    required_red = _RED_RULE.get(count)
    if required_red is not None and int(sum(flags)) != required_red:
        return PipReading(count=None, layout_ok=True, confidence=0.,
                          centers=centers, radii=tuple(float(r) for r in kept_radii),
                          red_flags=flags, reason='red_rule')
    # Confidence rewards pip-radius agreement and penalises a loose grid; it is
    # audit metadata only, never a threshold relaxation for the caller.
    confidence = max(0., min(1., 1. - spread))
    return PipReading(count=count, layout_ok=True, confidence=float(confidence),
                      centers=centers, radii=tuple(float(r) for r in kept_radii),
                      red_flags=flags, reason='ok')
