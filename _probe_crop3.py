"""Zoom on the meld rows shown elsewhere on the table, to learn the meld rendering style."""
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
R10 = r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'
GOLD = r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'

rec = _read(RECON)
# other players' exposed meld rows (table centre / opponents)
save(os.path.join(OUT, 'z_center_melds.png'), rec[330:460, 380:720], 3)
save(os.path.join(OUT, 'z_right_melds.png'), rec[30:140, 740:960], 3)
save(os.path.join(OUT, 'z_mid_melds.png'), rec[100:200, 420:640], 3)

# same-region compare: my left group vs my right group, tight on one tile each
save(os.path.join(OUT, 'z_recon_tile1.png'), rec[750:900, 0:100], 4)
save(os.path.join(OUT, 'z_recon_tile8.png'), rec[750:900, 648:748], 4)
g = _read(GOLD)
save(os.path.join(OUT, 'z_gold_tile1.png'), g[750:900, 0:100], 4)
r = _read(R10)
save(os.path.join(OUT, 'z_r10_tile1.png'), r[750:900, 0:100], 4)
save(os.path.join(OUT, 'z_r10_tile8.png'), r[750:900, 556:656], 4)
print('ok')
