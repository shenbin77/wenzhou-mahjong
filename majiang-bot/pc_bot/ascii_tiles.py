# -*- coding: utf-8 -*-
"""Render cluster ink masks as ASCII art for structural reading."""
import cv2, glob, os, sys
import numpy as np

def ink_mask(tile):
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    ink = cv2.inRange(hsv, np.array([0, 60, 60]), np.array([179, 255, 255]))
    ink |= cv2.inRange(hsv, np.array([0, 0, 0]), np.array([179, 255, 100]))
    h, w = ink.shape
    cv2.rectangle(ink, (0, 0), (w, h), 0, 4)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return ink

def ascii_art(ink, cols=24):
    h, w = ink.shape
    rows = int(h / w * cols * 0.55)
    small = cv2.resize(ink, (cols, rows), interpolation=cv2.INTER_AREA)
    lines = []
    for r in range(rows):
        line = ''.join('#' if small[r, c] > 40 else '.' for c in range(cols))
        lines.append(line)
    return '\n'.join(lines)

if __name__ == "__main__":
    pat = sys.argv[1] if len(sys.argv) > 1 else "harvest2/clusters/*.png"
    for fp in sorted(glob.glob(pat)):
        img = cv2.imread(fp)
        if img is None:
            continue
        art = ascii_art(ink_mask(img))
        print("=" * 30, os.path.basename(fp))
        print(art)
