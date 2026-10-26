"""Validate grey-face normalisation across every hand tile of every session frame.

Checks, for all 3684 boxes:
  * does normalisation ever flip an accepted tile to rejected, or change its label?
  * for badge (caishen) tiles: is the recovered label the caishen label read from the table?
  * for the dimmed-hand frames: how many tiles are recovered?
"""
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))
sys.path.insert(0, r'D:\温州麻将')

import cv2
import numpy as np

from tile_vision import TileVision, _read, face_region
from caishen_vision import detect_caishen
from _probe_meld_geom import raw_boxes

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
vision = TileVision(template_dir=TPL)
vision.load_templates()

SESSION = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'
frames = sorted(SESSION.glob('*.png'))


def face_bg(crop):
    face = face_region(crop)
    hsv = cv2.cvtColor(face, cv2.COLOR_BGR2HSV)
    return float(np.percentile(hsv[:, :, 2], 70))


def normalise(crop, target=238.0):
    bg = face_bg(crop)
    if bg <= 1.0 or bg >= 200.0:
        return crop
    gain = target / bg
    return np.clip(crop.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def badge_red(crop):
    bh, bw = crop.shape[:2]
    win = crop[:max(10, round(bh * .32)), :max(12, round(bw * .36))].astype(np.int16)
    blue, green, red = win[:, :, 0], win[:, :, 1], win[:, :, 2]
    red_m = (red - green > 50) & (red - blue > 50) & (red > 70)
    return float(red_m.sum()) / float(win.shape[0] * win.shape[1])


total = flipped = relabelled = 0
badge_rows, dark_rows = [], []
for f in frames:
    img = _read(f)
    if img is None:
        continue
    caishen = detect_caishen(img).label
    boxes, _, scale = raw_boxes(img)
    for i, b in enumerate(boxes):
        crop = img[b[1]:b[3], b[0]:b[2]]
        if crop.size == 0:
            continue
        total += 1
        raw = vision.classify_crop(crop)
        norm = vision.classify_crop(normalise(crop))
        bg = face_bg(crop)
        if raw['accepted'] and not norm['accepted']:
            flipped += 1
            print(f'  !! normalisation broke an accepted tile: {f.name[:22]} idx={i} '
                  f'{raw["label"]}->{norm["label"]} bg={bg:.0f}')
        if raw['accepted'] and norm['accepted'] and raw['label'] != norm['label']:
            relabelled += 1
            print(f'  ?? label changed: {f.name[:22]} idx={i} {raw["label"]}->{norm["label"]} bg={bg:.0f}')
        if badge_red(crop) >= .30:
            badge_rows.append((f.name[:22], i, caishen, raw['label'], raw['accepted'],
                               norm['label'], norm['accepted'], round(norm['score'], 3), round(bg, 1)))
        if bg < 200:
            dark_rows.append((f.name[:22], i, raw['label'], raw['accepted'],
                              norm['label'], norm['accepted'], round(bg, 1)))

print(f'\nscanned {total} tiles: flipped-accepted={flipped} label_changes={relabelled}')
print(f'\nbadge tiles (red_frac>=0.30): {len(badge_rows)}')
ok = 0
for name, i, caishen, rl, ra, nl, na, score, bg in badge_rows:
    good = na and nl == caishen
    ok += good
    print(f'  {name} idx={i:2d} caishen={str(caishen):>4s} bg={bg:6.1f} raw={rl}({ra}) '
          f'norm={nl}({na},{score}) {"OK" if good else "<<< MISMATCH"}')
print(f'badge tiles recovered with the correct caishen label: {ok}/{len(badge_rows)}')

print(f'\ndark tiles (face bg<200): {len(dark_rows)}')
rec_raw = sum(1 for r in dark_rows if r[3])
rec_norm = sum(1 for r in dark_rows if r[5])
print(f'  accepted before={rec_raw} after={rec_norm}')
for r in dark_rows[:15]:
    print(f'  {r[0]} idx={r[1]:2d} raw={r[2]}({r[3]}) norm={r[4]}({r[5]}) bg={r[6]}')
