# -*- coding: utf-8 -*-
"""Diagnose count_tiles missing left tiles: dump per-column intermediates."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot.tile_vision import TileVision  # noqa: E402

IMG = Path(r"D:/温州麻将/温州茶（传统麻将）/采集/dev_auto_play/tap_diag_0814/40_shot.png")
img = cv2.imdecode(np.fromfile(str(IMG), np.uint8), cv2.IMREAD_COLOR)
h, w = img.shape[:2]
print(f"image {w}x{h}")

roi = (0, round(h*.8), w, h)
x0, y0, x1, y1 = roi
band = img[y0:y1, x0:x1]
hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
mask = cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 85, 255]))
scale = w/1600
gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
skin_rows = gray[gray.shape[0]//3:]
column_skin = np.percentile(skin_rows, 90, axis=0).astype(np.float32)
edge_window = max(5, round(17*scale)) | 1
local_skin = cv2.dilate(column_skin[None, :], np.ones((1, edge_window), np.uint8))[0]
low_skin = column_skin < local_skin*.82
visible_border = np.zeros_like(low_skin)
ld = np.diff(np.r_[False, low_skin, False].astype(np.int8))
for a, b in zip(np.flatnonzero(ld == 1), np.flatnonzero(ld == -1)):
    if b-a <= max(5, round(11*scale)):
        visible_border[a:b] = True
mask_count = np.count_nonzero(mask, axis=0)
on_thresh = max(12*scale, band.shape[0]*.07)
on = (mask_count >= on_thresh)
on &= ~visible_border

print(f"scale={scale:.3f} edge_window={edge_window} on_thresh={on_thresh:.1f} band_shape={band.shape}")

def dump(label, a, b, step=6):
    print(f"\n== {label} x[{a}:{b}] ==")
    print(f"{'x':>5} {'maskc':>5} {'skin':>6} {'localsk':>7} {'lowsk':>5} {'visb':>4} {'on':>3}")
    for x in range(a, b, step):
        print(f"{x:>5} {mask_count[x]:>5} {column_skin[x]:>6.0f} {local_skin[x]:>7.0f} "
              f"{int(low_skin[x]):>5} {int(visible_border[x]):>4} {int(on[x]):>3}")

dump("MISSING LEFT (0-560)", 0, 560)
dump("DETECTED (600-900)", 600, 900)

# Summarize contiguous runs of on
changes = np.diff(np.r_[False, on, False].astype(np.int8))
runs = list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))
print("\non runs:", [(int(a), int(b), int(b-a)) for a, b in runs])

# Where is visible_border True (contiguous runs)?
vb_changes = np.diff(np.r_[False, visible_border, False].astype(np.int8))
vb_runs = list(zip(np.flatnonzero(vb_changes == 1), np.flatnonzero(vb_changes == -1)))
wide = [(int(a), int(b), int(b-a)) for a, b in vb_runs if b-a > max(5, round(11*scale))]
narrow = [(int(a), int(b), int(b-a)) for a, b in vb_runs if b-a <= max(5, round(11*scale))]
print(f"low_skin runs kept as border ({len(narrow)}):", narrow[:20], "..." if len(narrow) > 20 else "")
print(f"low_skin runs REJECTED as too wide ({len(wide)}):", wide[:20])

# What does the current count_tiles actually return?
import bot.tile_vision as tv
v = TileVision(Path(__file__).resolve().parent / "bot" / "templates")
try:
    n, boxes = v.count_tiles(img)
    print(f"\ncount_tiles -> {n} boxes")
    for b in boxes:
        print("  ", b)
    print("diag:", v.detection_diagnostics.get('visible_count'), v.detection_diagnostics.get('unresolved_regions'))
except Exception as e:
    print("count_tiles failed:", e)
