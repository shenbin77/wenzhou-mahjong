"""Runtime evidence for the report."""
import sys
from pathlib import Path

BOT = r'D:\温州麻将\温州茶（传统麻将）\bot'
sys.path.insert(0, BOT)

from tile_vision import TileVision, _read

TPL = Path(BOT).parent / '采集' / 'templates' / 'tea_tiles'
FRAMES = [
    ('GOLD  旧金标 r08/frame_02', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
    ('RECON 复现帧 live/205345', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('MELD  r10/frame_01', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
    ('PONG  205534 碰一筒', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205534_475580_f4a34e.png'),
]
for tag, path in FRAMES:
    img = _read(path)
    tv = TileVision(template_dir=TPL)
    rec = tv.recognize_hand(img)
    d = tv.detection_diagnostics
    print(f'{tag}')
    print(f'  visible={d["visible_count"]} concealed={d["concealed_count"]} '
          f'meld_boxes={len(tv.last_meld_boxes)} complete={tv.last_complete}')
    print(f'  meld_x={[b[0] for b in tv.last_meld_boxes]}')
    print(f'  recognized={len(rec)} hand={[t for t, *_ in rec]}')
    print()

# Shanten-meld capability check for the report.
from tea_bot import calc_shanten
print('calc_shanten signature has no meld parameter; engine works on 16/17 closed tiles only.')
