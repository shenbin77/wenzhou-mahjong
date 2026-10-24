"""Visual proof: the raised group appearing when the player pongs."""
import os
import sys
sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
import cv2
from tile_vision import _read

OUT = r'D:\温州麻将\.tmp\meldfix'
os.makedirs(OUT, exist_ok=True)
SESSION = r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p'


def save(name, img, zoom=1):
    if zoom != 1:
        img = cv2.resize(img, (img.shape[1] * zoom, img.shape[0] * zoom), interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode('.png', img)
    buf.tofile(os.path.join(OUT, name + '.png'))


for stem in ('20261023_205532_504131_8ffe2e', '20261023_205534_475580_f4a34e',
             '20261023_205536_561799_31e9e4'):
    img = _read(os.path.join(SESSION, stem + '.png'))
    save('proof_' + stem[-6:], img[730:900, 0:700])
print('ok')
