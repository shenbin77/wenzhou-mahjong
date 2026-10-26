# -*- coding: utf-8 -*-
"""只读差分探针：碎片合并补丁对「识别结果」的实际影响面。

对每帧跑两次 TileVision.recognize_hand（同一 caishen_label，保证可比）：
  原始 count_tiles vs 合并版 count_tiles
比较：concealed_count / 闭手牌 id 序列 / 台面组张数 / 台面 id 序列 / last_complete。
任何一帧只要这三项有任何一项变化，就算"被补丁影响"，逐帧列出。
目的：证明补丁只影响"碎片被切开"的帧，不动机器人其他帧的识别结果。
"""
from collections import Counter
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
sys.path.insert(0, str(ROOT / "bot"))

from tile_vision import TileVision  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

SESS = ROOT / "采集" / "assist_sessions"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_resegment_diff.txt"
TEMPLATES = ROOT / "采集" / "templates" / "tea_tiles"
SESSIONS = ["20261024_015005", "20261023_220643", "20261023_201640",
            "20261023_223759", "20261024_005400"]


def merged_count_tiles(self, img, roi=None):
    """count_tiles 的探针副本：合并「被牌面竖纹切开的同牌碎片」。

    与原实现唯一差异：runs 收集后先合并相邻碎片。
    三条同时满足才合并 —— 两侧都短于最小牌宽(48*scale)、间隙 < 6*scale、
    合并后不超单牌上限(125*scale)。两张正常牌各自 >= 48*scale，永远不满足第一条。
    """
    self.last_meld_boxes = []
    h, w = img.shape[:2]
    if roi is None:
        roi = (0, round(h * .8), w, h)
    x0, y0, x1, y1 = map(int, roi)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        self.detection_diagnostics = {'reason': 'empty_roi', 'unresolved_regions': []}
        return 0, []
    band = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 85, 255]))
    scale = w / 1600
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    rows = gray[gray.shape[0] // 3:]
    cs = np.percentile(rows, 90, axis=0).astype(np.float32)
    ew = max(5, round(17 * scale)) | 1
    ls = cv2.dilate(cs[None, :], np.ones((1, ew), np.uint8))[0]
    low = cs < ls * .82
    vb = np.zeros_like(low)
    ld = np.diff(np.r_[False, low, False].astype(np.int8))
    for a, b in zip(np.flatnonzero(ld == 1), np.flatnonzero(ld == -1)):
        if b - a <= max(5, round(11 * scale)):
            vb[a:b] = True
    on = (np.count_nonzero(mask, axis=0) >= max(12 * scale, band.shape[0] * .07)) & ~vb
    ch = np.diff(np.r_[False, on, False].astype(np.int8))
    runs = list(zip(np.flatnonzero(ch == 1), np.flatnonzero(ch == -1)))

    merged = []
    for a, b in runs:
        if merged:
            pa, pb = merged[-1]
            if (pb - pa) < 48 * scale and (b - a) < 48 * scale \
                    and (a - pb) < 6 * scale and (b - pa) <= 125 * scale:
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    runs = merged

    boxes, unresolved = [], []
    for a, b in runs:
        if b - a < 48 * scale:
            continue
        bx, ex = int(a), int(b)
        bw = ex - bx
        rr = np.flatnonzero(np.count_nonzero(mask[:, bx:ex], axis=1) >= bw * .4)
        if not len(rr):
            continue
        by, ey = int(rr[0]), int(rr[-1]) + 1
        candidate = band[by:ey, bx:ex]
        from tile_vision import face_region
        face = face_region(candidate)
        offset = (face.ctypes.data - candidate.ctypes.data) // candidate.strides[0]
        by += int(offset)
        ey = by + face.shape[0]
        bh = ey - by
        if bw > 125 * scale:
            unresolved.append([x0 + bx, y0 + by, x0 + ex, y0 + ey])
            continue
        if not (48 * scale <= bw <= 125 * scale and 80 * scale <= bh <= 180 * scale):
            continue
        if not (.42 <= bw / bh <= 1.05):
            continue
        boxes.append((x0 + bx, y0 + by, x0 + ex, y0 + ey))
    boxes.sort(key=lambda b: b[0])
    visible_count = len(boxes)
    meld_groups = []
    if len(boxes) >= 6:
        melds, boxes, meld_groups = self._split_exposed_prefix(boxes, scale)
        self.last_meld_boxes = melds
    self.detection_diagnostics = {
        'reason': 'visible_borders', 'unresolved_regions': unresolved,
        'roi': [x0, y0, x1, y1], 'visible_count': visible_count,
        'concealed_count': len(boxes),
        'meld_boxes': [list(b) for b in self.last_meld_boxes],
        'meld_groups': meld_groups}
    return len(boxes), boxes


def snapshot(img, caishen, patched):
    saved = TileVision.count_tiles
    if patched:
        TileVision.count_tiles = merged_count_tiles
    try:
        tv = TileVision(template_dir=str(TEMPLATES))
        hand = tv.recognize_hand(img, caishen_label=caishen)
        dd = tv.detection_diagnostics
        melds = getattr(tv, "last_melds", [])
        return (int(dd.get("concealed_count", -1)),
                tuple(sorted(int(r[0]) for r in hand if r[0] is not None)),
                tuple(dd.get("meld_groups") or []),
                tuple(int(m[0]) for m in melds),
                bool(tv.last_complete))
    finally:
        TileVision.count_tiles = saved


def main():
    lines = []
    grand_total = 0
    grand_changed = 0
    for name in SESSIONS:
        frames = sorted((SESS / name).glob("live/*/*.png"))
        changed = []
        for p in frames:
            img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            caishen = detect_caishen(img).label
            a = snapshot(img, caishen, False)
            b = snapshot(img, caishen, True)
            grand_total += 1
            if a != b:
                changed.append((p.name, a, b))
        grand_changed += len(changed)
        lines.append(f"{name}: {len(frames)} 帧，识别结果变化 {len(changed)} 帧")
        for fname, a, b in changed:
            lines.append(f"  {fname}")
            lines.append(f"    before: concealed={a[0]} hand={list(a[1])} groups={list(a[2])} "
                         f"melds={list(a[3])} complete={a[4]}")
            lines.append(f"    after : concealed={b[0]} hand={list(b[1])} groups={list(b[2])} "
                         f"melds={list(b[3])} complete={b[4]}")
        lines.append("")
    lines.append(f"合计 {grand_total} 帧，识别结果变化 {grand_changed} 帧")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text[:4000])
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
