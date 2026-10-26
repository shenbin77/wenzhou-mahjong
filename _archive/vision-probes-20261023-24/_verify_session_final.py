"""324-frame comparison along the product path (caishen label known, as TeaBot does)."""
import importlib.machinery
import sys
from collections import Counter
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from caishen_vision import detect_caishen
from tile_vision import TileVision, _read

before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before', r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
frames = sorted(S.glob('*.png'))

bv = before_mod.TileVision(template_dir=TPL)
av = TileVision(template_dir=TPL)
c = Counter()
flips = []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    label = detect_caishen(img).label
    b = bv.recognize_hand(img)                       # 旧实现（无 label 概念）
    b_inc = len(b) < bv.detection_diagnostics.get('concealed_count', 0)
    a = av.recognize_hand(img, caishen_label=label)  # 新实现（产品路径）
    a_inc = len(a) < av.detection_diagnostics.get('concealed_count', 0)
    melds = len(av.last_meld_boxes)
    c['frames'] += 1
    c['meld_frames' if melds else 'clean_frames'] += 1
    key = 'meld_' if melds else 'clean_'
    c[key + 'before_incomplete'] += bool(b_inc)
    c[key + 'after_incomplete'] += bool(a_inc)
    c[key + 'before_complete'] += bool(bv.last_complete)
    c[key + 'after_complete'] += bool(av.last_complete)
    c['badge_recovered'] += bool(len(a) > len(b))
    if len(a) > len(b):
        flips.append((f.name[:22], melds, len(b), len(a),
                      av.detection_diagnostics.get('concealed_count'),
                      [x['label'] for x in av.diagnostics if x.get('caishen_marked')]))
print(dict(c))
print(f'\nframes where the caishen tile added to the closed hand ({len(flips)}):')
for row in flips:
    print(f'  {row[0]} melds={row[1]} before={row[2]} after={row[3]}/{row[4]} marked={row[5]}')
