"""Dump full frames at half scale for table-context inspection."""
import os
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
import cv2
from tile_vision import _read

OUT = r'D:\温州麻将\.tmp\meldfix'
os.makedirs(OUT, exist_ok=True)


def save(path, img):
    ok, buf = cv2.imencode('.png', img)
    buf.tofile(path)


JOBS = [
    ('recon_full', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('r10_full', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
]
for name, path in JOBS:
    img = _read(path)
    small = cv2.resize(img, (1000, int(img.shape[0] * 1000 / img.shape[1])))
    save(os.path.join(OUT, name + '.png'), small)
print('ok')
