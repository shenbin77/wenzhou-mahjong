# -*- coding: utf-8 -*-
"""Re-cluster harvested crops with strict correlation-based signatures."""
import os, glob
import cv2
import numpy as np
from detect_tiles import detect_hand_tiles

FRAMES = "frames_raw"
OUT = "harvest2"
os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(OUT, "clusters"), exist_ok=True)

def sig(img):
    inner = img[8:-8, 5:-5]
    g = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (32, 44)).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6)
    return g

clusters = []
total = 0
for fp in sorted(glob.glob(os.path.join(FRAMES, "f*.png"))):
    name = os.path.splitext(os.path.basename(fp))[0]
    img = cv2.imread(fp)
    if img is None or img.shape[1] != 1600:
        continue
    for ti, (x, y, w, h) in enumerate(detect_hand_tiles(img)):
        crop = img[y:y+h, x:x+w]
        if crop.shape[0] < 100 or crop.shape[1] < 70:
            continue
        s = sig(crop)
        best, bi = -1, -1
        for ci, cl in enumerate(clusters):
            r = float(np.mean(s * cl["sig"]))
            if r > best:
                best, bi = r, ci
        if best > 0.90:
            clusters[bi]["crops"].append((name, ti))
            clusters[bi]["imgs"].append(crop)
        else:
            clusters.append({"sig": s, "crops": [(name, ti)], "imgs": [crop]})
        total += 1

print(f"total {total} crops -> {len(clusters)} clusters")
# drop junk clusters (1-2 members with extreme brightness)
clusters.sort(key=lambda c: -len(c["crops"]))
for ci, cl in enumerate(clusters):
    mid = len(cl["imgs"]) // 2
    cv2.imwrite(os.path.join(OUT, "clusters", f"c{ci:02d}_n{len(cl['crops']):04d}.png"), cl["imgs"][mid])
    print(ci, len(cl["crops"]), cl["crops"][0])
# also dump 2 extra members for verification of top clusters
os.makedirs(os.path.join(OUT, "verify"), exist_ok=True)
for ci, cl in enumerate(clusters[:60]):
    for k in (0, len(cl["imgs"]) - 1):
        cv2.imwrite(os.path.join(OUT, "verify", f"c{ci:02d}_v{k}.png"), cl["imgs"][k])
