"""Quantify the bezel side (gold band top vs bottom) and the pong frame geometry."""
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

import cv2
import numpy as np

from tile_vision import _read
from _probe_meld_geom import raw_boxes

FRAMES = [
    ('GOLD  金标 r08/frame_02', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
    ('RECON 复现 205345', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('MELD  r10/frame_01', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
    ('PONG  205534 碰一筒', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205534_475580_f4a34e.png'),
]


def gold_side(img, box, rows=4):
    """Return (top_gold_ratio, bottom_gold_ratio) for a box.

    Gold bezel = hue 12..32, sat>110, val>110 (the amber band on the tile edge).
    """
    x0, y0, x1, y1 = box
    crop = img[y0:y1, x0 + 6:x1 - 6]
    if crop.size == 0:
        return 0., 0.
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    gold = (hue >= 12) & (hue <= 32) & (sat > 110) & (val > 110)
    top = float(np.count_nonzero(gold[:rows])) / max(1, gold[:rows].size)
    bot = float(np.count_nonzero(gold[-rows:])) / max(1, gold[-rows:].size)
    return top, bot


for tag, path in FRAMES:
    img = _read(path)
    boxes, unresolved, scale = raw_boxes(img)
    baseline = float(np.median([b[3] for b in boxes]))
    th = float(np.median([b[3] - b[1] for b in boxes]))
    print(f'=== {tag}')
    for i, b in enumerate(boxes):
        top, bot = gold_side(img, b)
        raised = 'RAISED' if b[3] < baseline - max(8 * scale, th * .07) else '      '
        print(f'  [{i:02d}] x={b[0]:4d} bottom={b[3]:4d} h={b[3]-b[1]:3d} '
              f'w={b[2]-b[0]:3d} {raised} gold_top={top:.2f} gold_bottom={bot:.2f}')
    print()
