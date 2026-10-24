"""Sample mean BGR of the top/bottom edge rows of each tile box."""
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

import numpy as np

from tile_vision import _read
from _probe_meld_geom import raw_boxes

FRAMES = [
    ('GOLD  r08/frame_02 (17 闭手)', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
    ('RECON 205345 (6 副露)', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('PONG  205534 (碰 一筒x3)', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205534_475580_f4a34e.png'),
]
for tag, path in FRAMES:
    img = _read(path)
    boxes, _, scale = raw_boxes(img)
    baseline = float(np.median([b[3] for b in boxes]))
    print(f'=== {tag}')
    for i, b in enumerate(boxes[:8]):
        x0, y0, x1, y1 = b
        crop = img[y0:y1, x0 + 6:x1 - 6]
        top = crop[:4].reshape(-1, 3).mean(axis=0)
        bot = crop[-5:].reshape(-1, 3).mean(axis=0)
        raised = 'RAISED' if b[3] < baseline - 8 * scale else '      '
        print(f'  [{i}] bottom={b[3]} h={b[3]-b[1]} w={b[2]-b[0]} {raised} '
              f'topBGR=({top[0]:.0f},{top[1]:.0f},{top[2]:.0f}) '
              f'botBGR=({bot[0]:.0f},{bot[1]:.0f},{bot[2]:.0f})')
    print()
