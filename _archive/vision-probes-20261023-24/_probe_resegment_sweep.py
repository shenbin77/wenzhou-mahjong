# -*- coding: utf-8 -*-
"""只读扫描：碎片合并补丁在全库帧上的触发面（纯分割，不做模板识别，快）。

判定：若某帧存在「碎片宽度 < 最小牌宽 48*scale」且「与相邻 run 间隙 < 6*scale」
且「合并后 <= 125*scale」的 run 对，补丁会合并它们。
结论要求：触发只应发生在同一张牌被纹路切开的场合（合并后宽度落在正常牌宽区间），
不得把两张正常牌并成一张（合并后宽度会 > 130*scale 或碎片本身 >= 48*scale）。
"""
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
SESS = ROOT / "采集" / "assist_sessions"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_resegment_sweep.txt"

SESSIONS = [
    "20261024_015005",  # 本次缺陷会话
    "20261023_220643",  # 带吃（验收会话）
    "20261023_201640",  # 带副露（验收会话）
    "20261023_223759",  # 财神八萬会话
    "20261024_005400",  # 西杠会话
]


def scan(path):
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    scale = w / 1600
    x0, y0, x1, y1 = 0, round(h * .8), w, h
    band = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 85, 255]))
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
    hits = []
    for i in range(1, len(runs)):
        pa, pb = runs[i - 1]
        a, b = runs[i]
        gap = a - pb
        frag = min(pb - pa, b - a)
        comb = b - pa
        if frag < 48 * scale and gap < 6 * scale and comb <= 125 * scale:
            hits.append((frag, gap, comb, int(pa), int(b)))
    return hits


def main():
    lines = []
    total_frames = 0
    all_hits = []
    per_session = Counter()
    for name in SESSIONS:
        frames = sorted((SESS / name).glob("live/*/*.png"))
        n_hit = 0
        for p in frames:
            total_frames += 1
            hits = scan(p)
            if hits:
                n_hit += 1
                per_session[name] += 1
                for frag, gap, comb, pa, b in hits:
                    all_hits.append((name, p.name, frag, gap, comb, pa, b))
        lines.append(f"{name}: {len(frames)} 帧，触发合并 {n_hit} 帧")
    lines.append("")
    lines.append(f"合计扫描 {total_frames} 帧，触发 {len(all_hits)} 处")
    lines.append(f"触发帧数（按会话）: {dict(per_session)}")
    lines.append("")
    lines.append("碎片宽度(<48*scale)分布: " + str(sorted(Counter(h[2] for h in all_hits).items())))
    lines.append("间隙分布: " + str(sorted(Counter(h[3] for h in all_hits).items())))
    lines.append("合并后宽度分布: " + str(sorted(Counter(h[4] for h in all_hits).items())))
    lines.append("")
    lines.append("明细（会话, 帧, 碎片宽, 间隙, 合并后宽, x起, x止）:")
    for row in all_hits:
        lines.append("  " + ", ".join(str(x) for x in row))
    text = "\n".join(lines)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text[:3000])
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
