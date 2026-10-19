# -*- coding: utf-8 -*-
"""OCR labeling of tile glyph crops with binary normalization + pair trick."""
import cv2, glob, os, numpy as np, json
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

def glyph_boxes(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    ink = cv2.inRange(hsv, np.array([0, 60, 60]), np.array([179, 255, 255]))
    ink |= cv2.inRange(hsv, np.array([0, 0, 0]), np.array([179, 255, 100]))
    h, w = ink.shape
    cv2.rectangle(ink, (0, 0), (w, h), 0, 5)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw * bh >= 350 and bh >= 20 and bw >= 8:
            boxes.append((x, y, bw, bh))
    boxes.sort(key=lambda b: (b[1] > img.shape[0] * 0.52, b[0]))
    return boxes, ink

def to_bw(img, box, ink):
    """binary glyph: black ink on white"""
    x, y, bw, bh = box
    m = ink[y:y+bh, x:x+bw]
    g = np.full((bh, bw), 255, np.uint8)
    g[m > 0] = 0
    return g

def ocr_pair(g1, g2=None, scale=6):
    if g2 is None:
        g2 = g1
    H = max(g1.shape[0], g2.shape[0])
    W = g1.shape[1] + g2.shape[1] + 14
    line = np.full((H, W), 255, np.uint8)
    line[:g1.shape[0], :g1.shape[1]] = g1
    line[:g2.shape[0], g1.shape[1]+14:] = g2
    line = cv2.resize(line, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    h, w = line.shape
    canvas = np.full((max(h, 1600), max(w, 1600)), 255, np.uint8)
    y0 = (canvas.shape[0]-h)//2; x0 = (canvas.shape[1]-w)//2
    canvas[y0:y0+h, x0:x0+w] = line
    res, _ = ocr(canvas)
    if not res:
        return []
    return [(r[1], float(r[2])) for r in res]

if __name__ == "__main__":
    files = sorted(glob.glob("harvest2/clusters/*.png"))
    out = {}
    for fp in files:
        img = cv2.imread(fp)
        boxes, ink = glyph_boxes(img)
        entry = {"n_boxes": len(boxes), "boxes": [list(map(int, b)) for b in boxes], "reads": []}
        for bi, box in enumerate(boxes[:3]):
            g = to_bw(img, box, ink)
            reads = ocr_pair(g)
            entry["reads"].append(reads)
        out[os.path.basename(fp)] = entry
        print(os.path.basename(fp), len(boxes), entry["reads"], flush=True)
    with open("ocr_labels.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
