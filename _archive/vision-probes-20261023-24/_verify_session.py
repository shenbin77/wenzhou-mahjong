"""Compare the shipped file against the backup across all 324 live frames."""
import importlib.util
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from tile_vision import TileVision, _read

import importlib.machinery
before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before',
    r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
frames = sorted(S.glob('*.png'))

before_vision = before_mod.TileVision(template_dir=TPL)
after_vision = TileVision(template_dir=TPL)

stats = {'frames': 0, 'before_complete': 0, 'after_complete': 0,
         'both_complete': 0, 'flip_to_complete': 0, 'flip_to_incomplete': 0,
         'meld_frames': 0, 'before_abstain_incomplete': 0, 'after_abstain_incomplete': 0,
         'label_changes': 0, 'tiles_added': 0}
detail = []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    b = before_vision.recognize_hand(img)
    a = after_vision.recognize_hand(img)
    if before_vision.detection_diagnostics.get('visible_count') != \
            after_vision.detection_diagnostics.get('visible_count'):
        print(f'!! geometry changed on {f.name}')
    stats['frames'] += 1
    if before_vision.last_meld_boxes:
        stats['meld_frames'] += 1
    bc, ac = before_vision.last_complete, after_vision.last_complete
    stats['before_complete'] += bool(bc)
    stats['after_complete'] += bool(ac)
    stats['both_complete'] += bool(bc and ac)
    stats['flip_to_complete'] += bool(not bc and ac)
    stats['flip_to_incomplete'] += bool(bc and not ac)
    b_inc = len(b) < before_vision.detection_diagnostics.get('concealed_count', 0)
    a_inc = len(a) < after_vision.detection_diagnostics.get('concealed_count', 0)
    stats['before_abstain_incomplete'] += bool(b_inc)
    stats['after_abstain_incomplete'] += bool(a_inc)
    if len(a) != len(b):
        stats['tiles_added'] += len(a) - len(b)
        detail.append((f.name[:22], len(before_vision.last_meld_boxes), len(b), len(a),
                       after_vision.detection_diagnostics.get('concealed_count'), ac))

print(stats)
print(f'\nframes where more tiles were recognised afterwards ({len(detail)}):')
for row in detail[:40]:
    print(f'  {row[0]} melds={row[1]} before={row[2]} after={row[3]} concealed={row[4]} complete={row[5]}')
