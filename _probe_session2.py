"""Print raised-group vs concealed composition for every frame of the session.

Melds stay byte-identical across a whole round while hand tiles are replaced.
"""
import sys
from pathlib import Path

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

import numpy as np

from tile_vision import TileVision, _read
from _probe_meld_geom import raw_boxes

SESSION = Path(r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p')
frames = sorted(SESSION.glob('*.png'))
vision = TileVision(template_dir=Path(r'D:\温州麻将\温州茶（传统麻将）\采集\templates\tea_tiles'))
vision.load_templates()

for f in frames:
    img = _read(f)
    boxes, unresolved, scale = raw_boxes(img)
    if len(boxes) < 10:
        print(f'{f.stem[-22:]}  n={len(boxes):2d}  (skip)')
        continue
    baseline = float(np.median([b[3] for b in boxes]))
    labels = []
    for b in boxes:
        d = vision.classify_crop(img[b[1]:b[3], b[0]:b[2]])
        labels.append(d['label'] if d['accepted'] else '?')
    mark = ''.join('R' if b[3] < baseline - 5 else '.' for b in boxes)
    print(f'{f.stem[-22:]} n={len(boxes):2d} {mark}  '
          f'R={[l for l, m in zip(labels, mark) if m == "R"]}  '
          f'C={[l for l, m in zip(labels, mark) if m != "R"]}')
