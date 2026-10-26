"""Full acceptance run for task #4 (gated brightness compensation)."""
import importlib.machinery
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from tile_vision import TileVision, _read, caishen_badge

before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before', r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'

print('=' * 78)
print('A. 205345 复现帧')
img = _read(S / '20261023_205345_749900_23750c.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img)
d = tv.detection_diagnostics
print(f'   visible={d["visible_count"]} concealed={d["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} recognized={len(rec)} complete={tv.last_complete}')
print(f'   auto caishen={tv._read_table_caishen(img)}')
for x in tv.diagnostics:
    print(f'   {x["label"]:>6s} {x["score"]:.3f} {x["reason"]:<15s} accepted={x["accepted"]} '
          f'badge={x.get("caishen_marked")}')

print('=' * 78)
print('B. 205534 碰帧')
img = _read(S / '20261023_205534_475580_f4a34e.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img)
d = tv.detection_diagnostics
print(f'   visible={d["visible_count"]} concealed={d["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} recognizable={len(rec)}/{d["concealed_count"]} '
      f'complete={tv.last_complete} auto caishen={tv._read_table_caishen(img)}')
for i, x in enumerate(tv.diagnostics):
    print(f'   [{i:02d}] {x["label"]:>6s} {x["score"]:.3f} {x["reason"]:<15s} accepted={x["accepted"]}')
print(f'   meld tiles -> {[x["label"] for x in tv.meld_diagnostics]} (all exposed, not concealed)')

print('=' * 78)
print('C. 旧金标 r08/frame_02')
img = _read(BOT.parent / '采集' / '2026-10-16' / '740195' / 'r08' / 'frame_02.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img)
print(f'   recognized={len(rec)}/{tv.detection_diagnostics["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} complete={tv.last_complete}')

print('=' * 78)
print('D. 整手压暗（对手回合）帧是否仍弃权 —— 424821/r08 + live all-dim frames')
for tag, p in [('424821/r08', BOT.parent / '采集' / '2026-10-15' / '424821' / 'r08' / 'frame_02.png'),
               ('live 205404', S / '20261023_205404_403250_abb9d7.png'),
               ('live 205454', S / '20261023_205454_276989_52eb4a.png')]:
    img = _read(p)
    tv = TileVision(template_dir=TPL)
    rec = tv.recognize_hand(img)
    d = tv.detection_diagnostics
    badges = [round(1) for x in tv.diagnostics if x.get('caishen_marked')]
    print(f'   {tag}: recognized={len(rec)}/{d["concealed_count"]} complete={tv.last_complete} '
          f'badge_tiles={len(badges)}')

print('=' * 78)
print('E. t2 当财神 / label 未知 的健壮性')
img = _read(S / '20261023_205345_749900_23750c.png')
tv = TileVision(template_dir=TPL)
crop = img[768:891, 561:643]
for label in (None, 't2', 'zz', 's4'):
    r = tv.classify_crop(crop, caishen_label=label)
    print(f'   caishen_label={str(label):>5s} -> label={r["label"]} accepted={r["accepted"]} '
          f'reason={r["reason"]} badge={r["caishen_marked"]}')
tv.caishen_label = None
orig = tv._read_table_caishen
tv._read_table_caishen = lambda _img: None        # simulate missing sprite template
rec = tv.recognize_hand(img)
print(f'   sprite unreadable -> recognized={len(rec)} '
      f'complete={tv.last_complete} (no crash)')
tv._read_table_caishen = orig
