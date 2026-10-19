# -*- coding: utf-8 -*-
"""Harvest hand-tile crops from all frames, cluster by perceptual hash."""
import os, glob
import cv2
import numpy as np
from detect_tiles import detect_hand_tiles

FRAMES = "frames_raw"
OUT = "harvest"
os.makedirs(OUT, exist_ok=True)

def ahash(img, size=16):
    g = cv2.resize(img, (size, size))
    g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY)
    return (g > g.mean()).flatten()

def hdist(a, b):
    return np.count_nonzero(a != b)

clusters = []  # list of dict(hash, crops=[(frame_idx, tile_idx, path, box)])
total = 0
for fp in sorted(glob.glob(os.path.join(FRAMES, "f*.png"))):
    name = os.path.splitext(os.path.basename(fp))[0]
    img = cv2.imread(fp)
    if img is None or img.shape[1] != 1600:
        continue
    boxes = detect_hand_tiles(img)
    for ti, (x, y, w, h) in enumerate(boxes):
        crop = img[y:y+h, x:x+w]
        # inner region only (avoid edge shadow variance)
        hh, ww = crop.shape[:2]
        inner = crop[int(hh*0.06):int(hh*0.94), int(ww*0.06):int(ww*0.94)]
        hv = ahash(inner)
        placed = False
        for cl in clusters:
            if hdist(hv, cl["hash"]) <= 26:
                cl["crops"].append((name, ti))
                cl["imgs"].append(crop)
                placed = True
                break
        if not placed:
            clusters.append({"hash": hv, "crops": [(name, ti)], "imgs": [crop]})
        total += 1

print(f"total crops {total}, clusters {len(clusters)}")
# save best (median-ish: middle crop) of each cluster, sorted by count desc
clusters.sort(key=lambda c: -len(c["crops"]))
tiles_dir = os.path.join(OUT, "clusters")
os.makedirs(tiles_dir, exist_ok=True)
for ci, cl in enumerate(clusters):
    mid = cl["imgs"][len(cl["imgs"]) // 2]
    cv2.imwrite(os.path.join(tiles_dir, f"c{ci:02d}_n{len(cl['crops']):03d}_{cl['crops'][0][0]}_t{cl['crops'][0][1]}.png"), mid)
    print(ci, len(cl["crops"]), cl["crops"][0])
