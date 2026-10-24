"""Zoom precisely on an opponent's exposed meld row to learn the game's meld rendering."""
import os
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
import cv2
from tile_vision import _read

OUT = r'D:\温州麻将\.tmp\meldfix'
os.makedirs(OUT, exist_ok=True)


def save(path, img, zoom=3):
    big = cv2.resize(img, (img.shape[1] * zoom, img.shape[0] * zoom), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode('.png', big)
    buf.tofile(path)


RECON = r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'
rec = _read(RECON)
save(os.path.join(OUT, 'z_meld_opp_left.png'), rec[450:570, 320:680], 3)
save(os.path.join(OUT, 'z_meld_opp_top.png'), rec[0:90, 900:1230], 3)
print('ok')
