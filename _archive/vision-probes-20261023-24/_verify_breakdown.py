"""Breakdown of the abstain statistics plus a correctness spot check of the flips."""
import importlib.machinery
import sys
from collections import Counter
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from tile_vision import TileVision, _read

before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before', r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
frames = sorted(S.glob('*.png'))

bv = before_mod.TileVision(template_dir=TPL)
av = TileVision(template_dir=TPL)

counts = Counter()
samples = []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    b = bv.recognize_hand(img)
    b_meld = len(bv.last_meld_boxes)
    b_inc = len(b) < bv.detection_diagnostics.get('concealed_count', 0)
    a = av.recognize_hand(img)
    a_meld = len(av.last_meld_boxes)
    a_inc = len(a) < av.detection_diagnostics.get('concealed_count', 0)
    clean = (a_meld == 0 and b_meld == 0)
    counts['frames'] += 1
    counts['clean_frames' if clean else 'meld_frames'] += 1
    if clean:
        counts['clean_before_incomplete'] += bool(b_inc)
        counts['clean_after_incomplete'] += bool(a_inc)
        counts['clean_before_ok'] += bool(not b_inc)
        counts['clean_after_ok'] += bool(not a_inc)
        counts['clean_before_complete'] += bool(bv.last_complete)
        counts['clean_after_complete'] += bool(av.last_complete)
    if len(a) != len(b) and len(samples) < 8:
        samples.append((f, [x['label'] for x in bv.diagnostics],
                        [x['label'] for x in av.diagnostics],
                        [x.get('caishen_marked') for x in av.diagnostics]))
print(dict(counts))
print()
for f, bl, al, mk in samples:
    print(f'{f.name[:22]}')
    print(f'   before({len(bl)}): {bl}')
    print(f'   after ({len(al)}): {al}')
    print(f'   badge : {[i for i, m in enumerate(mk) if m]}')
