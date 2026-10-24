# -*- coding: utf-8 -*-
"""Analyze EchoBird sidebar pixel rows in the native grab to find row bands.
Prints y-ranges of distinct horizontal bands in the sidebar strip."""
from PIL import Image
import sys

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\温州麻将\_eb_win3.png"
img = Image.open(path).convert("RGB")
W, H = img.size
print("size", W, H)
# sidebar strip: x 20..230 (window coords), compute per-row mean brightness
strip = img.crop((20, 60, 230, H - 40))
sw, sh = strip.size
rows = []
for y in range(sh):
    px = [strip.getpixel((x, y)) for x in range(0, sw, 7)]
    mean = sum(sum(p) / 3 for p in px) / len(px)
    rows.append(mean)
# background baseline ~ median of rows
srt = sorted(rows)
base = srt[len(srt) // 2]
print("baseline brightness", round(base, 1))
# find contiguous bands notably brighter than baseline (highlight / hover)
bands = []
in_band = False
start = 0
for y, v in enumerate(rows):
    if v > base + 6 and not in_band:
        in_band, start = True, y
    elif v <= base + 6 and in_band:
        in_band = False
        bands.append((start + 60, y + 60))  # back to window coords (+60 offset)
if in_band:
    bands.append((start + 60, sh + 60))
for b in bands:
    if b[1] - b[0] >= 8:
        print("band y", b[0], "-", b[1], "center", (b[0] + b[1]) // 2)
