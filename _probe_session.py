"""Track the bottom-row geometry across every frame of the assist session.

If the left group is a meld set it must stay fixed while the right part changes.
"""
import sys
from pathlib import Path

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')

import numpy as np

from tile_vision import TileVision, _read

sys.path.insert(0, r'D:\温州麻将')
from _probe_meld_geom import raw_boxes  # noqa: E402  (reuses the identical geometry copy)

SESSION = Path(r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p')
frames = sorted(SESSION.glob('*.png'))
vision = TileVision(template_dir=Path(r'D:\温州麻将\温州茶（传统麻将）\采集\templates\tea_tiles'))
vision.load_templates()

print(f'{len(frames)} frames')
for f in frames:
    img = _read(f)
    boxes, unresolved, scale = raw_boxes(img)
    if not boxes:
        print(f'{f.stem}: no boxes')
        continue
    baseline = float(np.median([b[3] for b in boxes]))
    raised = [b for b in boxes if b[3] < baseline - 5]
    labels = []
    for b in boxes:
        d = vision.classify_crop(img[b[1]:b[3], b[0]:b[2]])
        labels.append(d['label'] if d['accepted'] else '?' + str(d['label']))
    mark = ''.join('R' if b[3] < baseline - 5 else '.' for b in boxes)
    print(f'{f.stem[-22:]}  n={len(boxes):2d} raised={len(raised)}  pattern={mark}  '
          f'raised_x={[b[0] for b in raised]}')
    print(f'    labels={labels}')
