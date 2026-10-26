"""Crop the left tile group of the three frames for visual comparison."""
import os
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
import cv2
import numpy as np
from tile_vision import _read

OUT = r'D:\温州麻将\.tmp\meldfix'
os.makedirs(OUT, exist_ok=True)


def save(path, img):
    ok, buf = cv2.imencode('.png', img)
    buf.tofile(path)


JOBS = [
    ('recon_left', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('r10_left', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
    ('gold_left', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
]
for name, path in JOBS:
    img = _read(path)
    crop = img[730:900, 0:760]
    crop = cv2.resize(crop, (crop.shape[1] * 2, crop.shape[0] * 2), interpolation=cv2.INTER_NEAREST)
    save(os.path.join(OUT, name + '.png'), crop)

for name, path in JOBS:
    img = _read(path)
    save(os.path.join(OUT, name + '_full.png'), img[700:900, 0:1600])
print('done')
