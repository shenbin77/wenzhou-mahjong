"""Final acceptance run: badge path is opt-in via the round's known caishen label."""
import importlib.machinery
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from tile_vision import TILE_IDS, TileVision, _read
from caishen_vision import detect_caishen

before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before', r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'

print('A. 205345 复现帧（label 已知 s4）')
img = _read(S / '20261023_205345_749900_23750c.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img, caishen_label='s4')
d = tv.detection_diagnostics
print(f'   visible={d["visible_count"]} concealed={d["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} recognized={len(rec)}/{d["concealed_count"]} '
      f'complete={tv.last_complete}')
for x in tv.diagnostics:
    print(f'   {x["label"]:>6s} {x["score"]:.3f} {x["reason"]:<15s} accepted={x["accepted"]}')
print(f'   meld tiles={[x["label"] for x in tv.meld_diagnostics]} （副露，不计入闭手）')

print('\nB. 205534 碰帧（label 已知 t3；t3 不在手，无角标）')
img = _read(S / '20261023_205534_475580_f4a34e.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img, caishen_label='t3')
d = tv.detection_diagnostics
print(f'   concealed={d["concealed_count"]} meld={len(tv.last_meld_boxes)} '
      f'recognized={len(rec)}/{d["concealed_count"]} complete={tv.last_complete}')
print(f'   未识别={[x["label"] for x in tv.diagnostics if not x["accepted"]]}')
print(f'   副露={[x["label"] for x in tv.meld_diagnostics]}')

print('\nC. 旧金标 r08/frame_02（label 已知 s9）')
img = _read(BOT.parent / '采集' / '2026-10-16' / '740195' / 'r08' / 'frame_02.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img, caishen_label='s9')
print(f'   recognized={len(rec)}/{tv.detection_diagnostics["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} complete={tv.last_complete}')

print('\nD. 424821/r08（既有"压暗帧弃权"契约帧，带角标 t5）')
img = _read(BOT.parent / '采集' / '2026-10-15' / '424821' / 'r08' / 'frame_02.png')
for tag, label in (('不给 label', None), ('给 label t5', 't5')):
    tv = TileVision(template_dir=TPL)
    rec = tv.recognize_hand(img, caishen_label=label)
    print(f'   {tag}: recognized={len(rec)}/{tv.detection_diagnostics["concealed_count"]} '
          f'complete={tv.last_complete}')

print('\nE. t2 / 未知 label 健壮性（无 sprite 模板时不得崩）')
img = _read(S / '20261023_205345_749900_23750c.png')
tv = TileVision(template_dir=TPL)
crop = img[768:891, 561:643]
for label in (None, 't2', 'zz', 's4'):
    r = tv.classify_crop(crop, caishen_label=label)
    print(f'   label={str(label):>5s} -> {r["label"]} accepted={r["accepted"]} '
          f'reason={r["reason"]} badge={r["caishen_marked"]}')
