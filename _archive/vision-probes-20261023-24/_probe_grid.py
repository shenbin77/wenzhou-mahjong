"""Overlay a coordinate grid on the full frame to locate UI regions reliably."""
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


def grid(img, step=100):
    out = img.copy()
    for x in range(0, out.shape[1], step):
        cv2.line(out, (x, 0), (x, out.shape[0]), (0, 0, 255), 1)
        cv2.putText(out, str(x), (x + 2, 14), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 255), 1)
    for y in range(0, out.shape[0], step):
        cv2.line(out, (0, y), (out.shape[1], y), (255, 0, 255), 1)
        cv2.putText(out, str(y), (2, max(12, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 0, 255), 1)
    return out


RECON = r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'
R10 = r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'
rec = _read(RECON)
save(os.path.join(OUT, 'grid_recon.png'), grid(rec))
r10 = _read(R10)
save(os.path.join(OUT, 'grid_r10.png'), grid(r10))
print('ok')
