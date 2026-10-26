"""Acceptance checks for the caishen-marked tile fix."""
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from tile_vision import TileVision, _read

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
CASES = [
    ('1 RECON 205345', S / '20261023_205345_749900_23750c.png'),
    ('3 PONG  205534', S / '20261023_205534_475580_f4a34e.png'),
    ('2 GOLD  r08/frame_02', BOT.parent / '采集' / '2026-10-16' / '740195' / 'r08' / 'frame_02.png'),
]
for tag, path in CASES:
    img = _read(path)
    tv = TileVision(template_dir=TPL)
    rec = tv.recognize_hand(img)
    d = tv.detection_diagnostics
    print(f'{tag}')
    print(f'   visible={d["visible_count"]} concealed={d["concealed_count"]} '
          f'meld={len(tv.last_meld_boxes)} complete={tv.last_complete} '
          f'recognized={len(rec)} caishen={tv.caishen_label}/auto={tv._read_table_caishen(img)}')
    for x in tv.diagnostics:
        mark = ' <== CAISHEN' if x.get('caishen_marked') else ''
        gf = f' glyph={x.get("glyph_label")}@{x.get("glyph_score"):.3f}' if 'glyph_label' in x else ''
        print(f'     concealed {x["label"]:>6s} {x["score"]:.3f}/{x["margin"]:.3f} '
              f'{x["reason"]:<15s} accepted={x["accepted"]}{gf}{mark}')
    for x in tv.meld_diagnostics:
        print(f'     exposed   {x["label"]:>6s} {x["score"]:.3f}/{x["margin"]:.3f} '
              f'{x["reason"]:<15s} accepted={x["accepted"]}')
    print()
