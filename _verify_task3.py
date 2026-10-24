"""Task #3 final checks on the merged state: gold, acceptance frames, caishen coverage."""
import sys
from collections import Counter
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

from assist_tea import analyze_snapshot
from caishen_vision import detect_caishen
from tile_vision import TILE_IDS, TileVision, _read

LOG = r'D:\温州麻将\.tmp\eng_logs'
TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
S = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'

print('1) 旧金标端到端')
r = analyze_snapshot(BOT.parent / '采集' / '2026-10-16' / '740195' / 'r08' / 'frame_02.png',
                     caishen_label='s9', confirmed_two_player=True, log_dir=LOG)
print(f'   status={r["status"]} hand={len(r.get("hand") or [])} suggest={r.get("suggestion")}')

print('2) 复现帧 vision 层（label=s4）')
img = _read(S / '20261023_205345_749900_23750c.png')
tv = TileVision(template_dir=TPL)
rec = tv.recognize_hand(img, caishen_label='s4')
print(f'   concealed={len(rec)}/{tv.detection_diagnostics["concealed_count"]} '
      f'meld={len(tv.last_meld_boxes)} complete={tv.last_complete} '
      f'marked={[x["label"] for x in tv.diagnostics if x.get("caishen_marked")]}')

print('3) 财神 sprite 读取覆盖率（对方 22:08 改了 templates/caishen，交叉验证）')
labels = Counter()
missing = 0
for f in sorted(S.glob('*.png')):
    img = _read(f)
    if img is None:
        continue
    read = detect_caishen(img)
    if read.label is None:
        missing += 1
    else:
        labels[read.label] += 1
print(f'   324 帧：读到 {sum(labels.values())} 帧，读不到 {missing} 帧，分布={dict(labels)}')

print('4) 阈值不变性')
import re
src = (BOT / 'tile_vision.py').read_text(encoding='utf-8')
m = re.search(r'def __init__\(self, template_dir=None, min_score=([\d.]+), min_margin=([\d.]+)', src)
print(f'   tile_vision min_score={m.group(1)} min_margin={m.group(2)}')
cs = (BOT / 'caishen_vision.py').read_text(encoding='utf-8')
print('   caishen_vision:', re.search(r'_MIN_SCORE = ([\d.]+)', cs).group(0),
      re.search(r'_MIN_MARGIN = ([\d.]+)', cs).group(0))
