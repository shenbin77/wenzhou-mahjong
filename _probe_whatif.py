"""Show what the recon frame would yield if meld detection were ignored entirely."""
import sys
from pathlib import Path

BOT = r'D:\温州麻将\温州茶（传统麻将）\bot'
sys.path.insert(0, BOT)

import cv2
import numpy as np

import tile_vision
from tile_vision import TileVision, _read

TPL = Path(BOT).parent / '采集' / 'templates' / 'tea_tiles'
RECON = r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'

img = _read(RECON)
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img)
print('meld diagnostics (the 6 boxes the detector calls exposed):')
for d in tv.meld_diagnostics:
    print(f"  box={d['box']} label={d['label']} score={d['score']:.3f} "
          f"margin={d['margin']:.3f} accepted={d['accepted']} reason={d['reason']}")
print('concealed diagnostics:')
for d in tv.diagnostics:
    print(f"  box={d['box']} label={d['label']} score={d['score']:.3f} "
          f"margin={d['margin']:.3f} accepted={d['accepted']} reason={d['reason']}")

acc = [d for d in tv.diagnostics + tv.meld_diagnostics if d['accepted']]
print(f'\nIf meld detection were switched off: {len(acc)} of 17 slots classified, '
      f'hand size would be {len(acc)} -> last_complete would still be False '
      f'(16 != 17), so detect_hand() still returns [] and see() still raises 识牌不完整.')
