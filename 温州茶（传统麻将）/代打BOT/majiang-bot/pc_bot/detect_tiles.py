# -*- coding: utf-8 -*-
"""Detect upright hand tiles in the bottom band of a 1600x900 ZJ game frame.
Cream mask + column projection; wide runs are split by known tile pitch."""
import sys, os
import math
import cv2
import numpy as np

BAND_TOP_RATIO = 0.80
TILE_PITCH = 92.7   # horizontal pitch of hand tiles
TILE_W = 90

def detect_hand_tiles(img):
    h, w = img.shape[:2]
    y0 = int(h * BAND_TOP_RATIO)
    band = img[y0:, :]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([179, 80, 255]))
    colsum = (mask > 0).sum(axis=0).astype(np.int32)
    th = int(mask.shape[0] * 0.20)
    cols = (colsum >= th).astype(np.uint8)
    runs = []
    i = 0
    n = len(cols)
    while i < n:
        if cols[i]:
            j = i
            while j < n and cols[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    boxes = []
    for (a, b) in runs:
        bw = b - a + 1
        if bw < 50:
            continue
        cnt = max(1, int(round(bw / TILE_PITCH)))
        step = (b - a + 1) / cnt
        for k in range(cnt):
            xa = int(round(a + k * step))
            xb = int(round(a + (k + 1) * step)) - 1
            sub = mask[:, xa:xb + 1]
            width = xb - xa + 1
            rows = np.where((sub > 0).sum(axis=1) >= width * 0.4)[0]
            if len(rows) == 0:
                continue
            rt, rb = rows[0], rows[-1]
            if rb - rt + 1 < 80:
                continue
            boxes.append((xa, y0 + rt, width, rb - rt + 1))
    return boxes

if __name__ == "__main__":
    src = sys.argv[1]
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    img = cv2.imread(src)
    boxes = detect_hand_tiles(img)
    vis = img.copy()
    for i, (x, y, w, h) in enumerate(boxes):
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(vis, str(i), (x + 4, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        crop = img[y:y + h, x:x + w]
        cv2.imwrite(os.path.join(outdir, f"tile_{i:02d}.png"), crop)
    cv2.imwrite(os.path.join(outdir, "_vis.png"), vis)
    print(f"{len(boxes)} tiles:", [b[0] for b in boxes])
