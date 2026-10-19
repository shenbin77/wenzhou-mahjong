# -*- coding: utf-8 -*-
"""Extract number glyphs from man-tile clusters, re-cluster, ASCII + OCR each."""
import cv2, glob, os, json
import numpy as np
from ascii_tiles import ink_mask
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

# man clusters identified so far (structure: top number + bottom 萬)
MAN_CLUSTERS = ["c04", "c07", "c09", "c10", "c19", "c22", "c23", "c24",
                "c31", "c32", "c33", "c34", "c36", "c51", "c54"]

def number_glyph(img):
    ink = ink_mask(img)
    H = ink.shape[0]
    top = ink[:int(H * 0.50), :]
    ys, xs = np.where(top > 0)
    if len(ys) == 0:
        return None
    y1, y2, x1, x2 = ys.min(), ys.max(), xs.min(), xs.max()
    sub = top[y1:y2 + 1, x1:x2 + 1]
    if sub.shape[0] < 15 or sub.shape[1] < 15:
        return None
    return sub

def sig(m):
    g = cv2.resize(m, (32, 32)).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6)
    return g

def ocr_row(images, scale=4):
    H = max(g.shape[0] for g in images)
    W = sum(g.shape[1] for g in images) + 8 * (len(images) - 1)
    row = np.full((H, W), 255, np.uint8)
    x = 0
    for g in images:
        row[:g.shape[0], x:x + g.shape[1]] = 255 - g
        x += g.shape[1] + 8
    row = cv2.resize(row, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    h, w = row.shape
    canvas = np.full((max(h, 1600), max(w, 1600)), 255, np.uint8)
    y0 = (canvas.shape[0] - h) // 2; x0 = (canvas.shape[1] - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = row
    res, _ = ocr(canvas)
    return [r[1] for r in res] if res else []

clusters = []
for name in MAN_CLUSTERS:
    for fp in sorted(glob.glob(f"harvest2/clusters/{name}_*.png")):
        img = cv2.imread(fp)
        m = number_glyph(img)
        if m is None:
            continue
        s = sig(m)
        best, bi = -1, -1
        for ci, cl in enumerate(clusters):
            r = float(np.mean(s * cl["sig"]))
            if r > best:
                best, bi = r, ci
        if best > 0.86:
            clusters[bi]["members"].append((name, os.path.basename(fp)))
            clusters[bi]["masks"].append(m)
        else:
            clusters.append({"sig": s, "members": [(name, os.path.basename(fp))], "masks": [m]})

os.makedirs("numbers", exist_ok=True)
for ci, cl in enumerate(clusters):
    mids = [n for n, _ in cl["members"]]
    # majority render: use middle mask
    m = cl["masks"][len(cl["masks"]) // 2]
    # ASCII
    cols = 40
    rows = int(m.shape[0] / m.shape[1] * cols * 0.55)
    small = cv2.resize(m, (cols, rows), interpolation=cv2.INTER_AREA)
    art = '\n'.join(''.join('#' if small[r, c] > 40 else '.' for c in range(cols)) for r in range(rows))
    reads = ocr_row([m, m, m])
    print("=" * 40, f"numcluster {ci} count={len(cl['members'])} from={mids}")
    print(art)
    print("OCR:", reads, flush=True)
    cv2.imwrite(f"numbers/n{ci:02d}.png", 255 - m)
with open("numbers/mapping.json", "w", encoding="utf-8") as f:
    json.dump([{"idx": ci, "members": cl["members"]} for ci, cl in enumerate(clusters)],
              f, ensure_ascii=False, indent=1)
