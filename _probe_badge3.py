"""Test a brightness-normalised red-corner + gold-glyph badge rule on every hand tile."""
import sys
from pathlib import Path

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

import cv2
import numpy as np

from tile_vision import _read
from _probe_meld_geom import raw_boxes

SESSION = Path(r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p')
frames = sorted(SESSION.glob('*.png'))


def badge_stats(crop):
    """Return (red_frac, gold_frac) inside the top-left badge window.

    Ratios rather than absolute levels, because the game both greys the caishen
    tile and dims the whole hand; only the colour relationship survives.
    """
    bh, bw = crop.shape[:2]
    win = crop[:max(10, round(bh * .32)), :max(12, round(bw * .36))].astype(np.int16)
    blue, green, red = win[:, :, 0], win[:, :, 1], win[:, :, 2]
    red_m = (red - green > 50) & (red - blue > 50) & (red > 70)
    gold_m = ((red > 70) & (green * 100 > red * 45) & (green * 100 < red * 82)
              & (blue * 100 < red * 58) & (green - blue > 18))
    n = float(win.shape[0] * win.shape[1])
    return float(red_m.sum()) / n, float(gold_m.sum()) / n


rows = []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    boxes, _, scale = raw_boxes(img)
    for i, b in enumerate(boxes):
        crop = img[b[1]:b[3], b[0]:b[2]]
        if crop.size == 0:
            continue
        rf, gf = badge_stats(crop)
        rows.append((rf, gf, f.name[:22], i, b[0]))

rows.sort(key=lambda r: -(r[0] * r[1]))
print(f'{len(rows)} boxes; top 25 by red_frac*glod_frac:')
for rf, gf, name, idx, x in rows[:25]:
    print(f'  red={rf:.3f} gold={gf:.3f}  {name} idx={idx:2d} x={x:4d}')

print('\ndistribution of red_frac:')
arr = np.array([r[0] for r in rows])
for q in (50, 90, 99, 99.5, 99.9):
    print(f'  p{q}={np.percentile(arr, q):.3f}')
print(f'  max={arr.max():.3f}')

print('\nboxes with red_frac>=0.12 and gold_frac>=0.04 (candidate badge):')
cand = [r for r in rows if r[0] >= .12 and r[1] >= .04]
print(f'  {len(cand)} boxes')
seen = {}
for rf, gf, name, idx, x in cand:
    seen.setdefault(name, []).append((idx, x, round(rf, 3), round(gf, 3)))
for name in list(seen)[:20]:
    print(f'  {name}: {seen[name]}')
