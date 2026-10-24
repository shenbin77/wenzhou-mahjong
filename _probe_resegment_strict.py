# -*- coding: utf-8 -*-
"""只读扫描（严格版补丁）：只合并「两侧都是碎片」的相邻 run。

规则：两侧宽度都 < 48*scale（各自都低于最小牌宽，原实现必丢）
      + 间隙 < 6*scale + 合并后 <= 125*scale
比宽松版少了「碎片 + 整牌」的合并，避免把邻牌的边缘碎片并进真牌。
"""
from collections import Counter
from pathlib import Path
import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
SESS = ROOT / "采集" / "assist_sessions"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_resegment_strict.txt"
SESSIONS = ["20261024_015005", "20261023_220643", "20261023_201640",
            "20261023_223759", "20261024_005400"]


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
        wa, wb = pb - pa, b - a
        comb = b - pa
        if wa < 48 * scale and wb < 48 * scale and gap < 6 * scale and comb <= 125 * scale:
            hits.append((int(wa), int(wb), int(gap), int(comb), int(pa), int(b)))
    return hits


def main():
    lines = []
    total = 0
    hits_all = []
    per = Counter()
    for name in SESSIONS:
        frames = sorted((SESS / name).glob("live/*/*.png"))
        n = 0
        for p in frames:
            total += 1
            hs = scan(p)
            if hs:
                n += 1
                per[name] += 1
                for h in hs:
                    hits_all.append((name, p.name) + h)
        lines.append(f"{name}: {len(frames)} 帧，触发 {n} 帧")
    lines += ["", f"合计 {total} 帧，触发 {len(hits_all)} 处，触发帧 {sum(per.values())}",
              f"按会话 {dict(per)}", "",
              "合并后宽度分布: " + str(sorted(Counter(h[5] for h in hits_all).items())),
              "双侧碎片宽度分布: " + str(sorted(Counter((h[3], h[4]) for h in hits_all).items())),
              "", "明细:"]
    for row in hits_all:
        lines.append("  " + ", ".join(str(x) for x in row))
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text[:2500])
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
