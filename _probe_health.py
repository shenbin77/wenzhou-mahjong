"""Scan every live session frame: is a meld ever reported for an illegal hand size?

A legal tea hand is 16/17 slots. With n melds the closed part must be 16-3n or 17-3n.
Any meld call whose closed count breaks that arithmetic is a false positive.
"""
import sys
from collections import Counter
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))
sys.path.insert(0, r'D:\温州麻将')

import numpy as np

from tile_vision import TileVision, _read
from _probe_meld_geom import raw_boxes

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
vision = TileVision(template_dir=TPL)

frames = sorted(Path(r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions').glob('*/live/*/*.png'))
print(f'scanning {len(frames)} live frames')

meld_frames, suspicious = [], []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    boxes, unresolved, scale = raw_boxes(img)
    if len(boxes) < 6:
        continue
    baseline = float(np.median([b[3] for b in boxes]))
    typical_h = float(np.median([b[3] - b[1] for b in boxes]))
    typical_w = float(np.median([b[2] - b[0] for b in boxes]))
    cut = 0
    for b in boxes:
        if b[3] < baseline - max(8 * scale, typical_h * .07) and b[2] - b[0] < typical_w * .98:
            cut += 1
        else:
            break
    if cut < 3 or cut >= len(boxes):
        continue
    group = boxes[:cut]
    gap = boxes[cut][0] - group[-1][2]
    tight = max(b[3] for b in group) - min(b[3] for b in group) <= 4 * scale
    if not (gap >= 8 * scale and tight):
        continue
    closed = len(boxes) - cut
    meld_frames.append((f, cut, closed))
    legal = any(abs(closed - base) <= 1 for base in (16 - 3 * (cut // 3), 17 - 3 * (cut // 3)))
    if not legal:
        suspicious.append((f, cut, closed))

print(f'meld detected on {len(meld_frames)} frames')
print('cut/closed distribution:', Counter((c, cl) for _, c, cl in meld_frames))
print(f'suspicious (closed count breaks 16/17 - 3*melds arithmetic): {len(suspicious)}')
for f, cut, closed in suspicious[:15]:
    print(f'  {f.parent.parent.parent.name}/{f.parent.name}/{f.name[:24]} cut={cut} closed={closed}')
