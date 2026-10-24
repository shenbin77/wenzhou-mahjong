"""Collect face-colour and red-corner statistics for every hand tile in the session.

Goal: find a data-driven, non-guessy discriminator for the 财-marked caishen tile.
"""
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

rows = []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    boxes, _, scale = raw_boxes(img)
    for i, b in enumerate(boxes):
        x0, y0, x1, y1 = b
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        bh, bw = crop.shape[:2]
        # inner face, avoiding the 1-2px gold rim
        face = crop[3:bh - 6, 4:bw - 4]
        hsv = cv2.cvtColor(face, cv2.COLOR_BGR2HSV)
        val = float(np.percentile(hsv[:, :, 2], 70))
        sat = float(np.percentile(hsv[:, :, 1], 30))
        # red corner triangle window: top-left 30% x 30%
        win = crop[:max(8, round(bh * .30)), :max(10, round(bw * .34))]
        blue, green, red = win[:, :, 0].astype(int), win[:, :, 1].astype(int), win[:, :, 2].astype(int)
        red_px = int(((red > 140) & (green < 120) & (blue < 120)).sum())
        # gold 财 stroke: yellow-ish bright pixels in the same window
        gold_px = int(((red > 150) & (green > 130) & (blue < 120)).sum())
        rows.append(dict(frame=f.name[:22], idx=i, x=x0, val=val, sat=sat,
                         red=red_px, gold=gold_px, win=win.shape[0] * win.shape[1]))

arr = rows
print(f'{len(arr)} tile boxes scanned')
val = np.array([r['val'] for r in arr])
print(f'face V70: min={val.min():.0f} p1={np.percentile(val,1):.0f} '
      f'median={np.median(val):.0f} p99={np.percentile(val,99):.0f} max={val.max():.0f}')
dark = [r for r in arr if r['val'] < 190]
print(f'\nboxes with face V70 < 190 (candidate grey/highlight tiles): {len(dark)}')
for r in dark[:40]:
    print(f"  {r['frame']} idx={r['idx']:2d} x={r['x']:4d} V70={r['val']:6.1f} S30={r['sat']:5.1f} "
          f"red={r['red']:4d} gold={r['gold']:4d} win={r['win']}")

print('\nnormal tiles: red distribution')
normal = [r for r in arr if r['val'] >= 190]
reds = np.array([r['red'] for r in normal])
print(f'  red px: median={np.median(reds):.0f} p99={np.percentile(reds,99):.0f} max={reds.max():.0f}')
print(f'  gold px: p99={np.percentile([r["gold"] for r in normal],99):.0f} '
      f'max={max(r["gold"] for r in normal)}')
