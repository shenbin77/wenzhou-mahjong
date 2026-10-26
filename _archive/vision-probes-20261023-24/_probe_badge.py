"""Locate and measure the red 财 badge on hand tiles."""
import os
import sys
from pathlib import Path

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

import cv2
import numpy as np

from tile_vision import _read
from _probe_meld_geom import raw_boxes

OUT = r'D:\温州麻将\.tmp\meld4'
os.makedirs(OUT, exist_ok=True)


def save(name, img, zoom=4):
    big = cv2.resize(img, (img.shape[1] * zoom, img.shape[0] * zoom), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode('.png', big)
    buf.tofile(os.path.join(OUT, name + '.png'))


FRAMES = [
    ('RECON', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('GOLD', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
    ('PONG', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205534_475580_f4a34e.png'),
]

for tag, path in FRAMES:
    img = _read(path)
    boxes, _, scale = raw_boxes(img)
    print(f'=== {tag}  boxes={len(boxes)}')
    for i, b in enumerate(boxes):
        x0, y0, x1, y1 = b
        crop = img[y0:y1, x0:x1]
        bh, bw = crop.shape[:2]
        # candidate badge windows, measured as a fraction of the tile box
        for name, (fy, fx) in (('q25', (.25, .30)), ('q33', (.33, .36))):
            win = crop[:max(8, round(bh * fy)), :max(10, round(bw * fx))]
            blue, green, red = win[:, :, 0].astype(int), win[:, :, 1].astype(int), win[:, :, 2].astype(int)
            hit = (red > 175) & (green < 160) & (blue < 110)
            print(f'  [{i:02d}] x={x0:4d} {name} red={int(hit.sum()):4d} of {hit.size:4d} '
                  f'({hit.mean()*100:5.1f}%)', end='')
        print()

# zoomed view of the marked tile plus a normal neighbour
rec = _read(FRAMES[0][1])
save('recon_box07_marked', rec[768:891, 561:643])
save('recon_box08_normal', rec[768:892, 652:741])
gold = _read(FRAMES[1][1])
save('gold_box00_normal', gold[768:892, 6:95])
pong = _read(FRAMES[2][1])
save('pong_box00_meld', pong[761:875, 6:89])
print('crops written')
