"""Locate the yellow triangle markers above the bottom hand row."""
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
import cv2
import numpy as np
from tile_vision import _read

FRAMES = [
    ('RECON', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('R10  ', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
    ('GOLD ', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
]

for tag, path in FRAMES:
    img = _read(path)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, w = img.shape[:2]
    band = hsv[round(h * .72):round(h * .87)]
    hue, sat, val = band[:, :, 0], band[:, :, 1], band[:, :, 2]
    yellow = ((hue >= 18) & (hue <= 35) & (sat > 150) & (val > 190)).astype(np.uint8)
    num, labels, stats, cents = cv2.connectedComponentsWithStats(yellow, 8)
    hits = []
    for i in range(1, num):
        x, y, bw, bh, area = stats[i]
        if area < 120 or bw < 10 or bh < 8 or bw > 60 or bh > 45:
            continue
        if bw / max(1, bh) < .9:
            continue
        hits.append((int(cents[i][0]), int(y + round(h * .72)), int(bw), int(bh), int(area)))
    hits.sort()
    print(f'=== {tag} triangles(count={len(hits)})')
    for cx, y, bw, bh, area in hits:
        print(f'    cx={cx:4d} y={y:4d} w={bw:3d} h={bh:3d} area={area}')
    print()
