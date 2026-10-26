"""How many frames have a 财-marked tile but no readable table caishen sprite?"""
import sys
from collections import Counter
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))
sys.path.insert(0, r'D:\温州麻将')

from caishen_vision import detect_caishen
from tile_vision import caishen_badge, _read
from _probe_meld_geom import raw_boxes

S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
counts = Counter()
examples = []
for f in sorted(S.glob('*.png')):
    img = _read(f)
    if img is None:
        continue
    boxes, _, _ = raw_boxes(img)
    marked = []
    for i, b in enumerate(boxes):
        crop = img[b[1]:b[3], b[0]:b[2]]
        if crop.size and caishen_badge(crop):
            marked.append(i)
    label = detect_caishen(img).label
    key = ('badge' if marked else 'no_badge') + '_' + ('label' if label else 'no_label')
    counts[key] += 1
    if marked and not label:
        examples.append((f.name[:22], marked))
print(dict(counts))
print(f'\n有角标牌但读不到桌面财神的帧：{len(examples)}')
for e in examples[:10]:
    print('  ', e)
