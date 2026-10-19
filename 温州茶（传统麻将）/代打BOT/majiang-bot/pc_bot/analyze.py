# -*- coding: utf-8 -*-
"""Comprehensive cluster analysis: color, structure, circles, bars, OCR."""
import cv2, glob, os, json
import numpy as np
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()

def ink_mask(tile):
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    ink = cv2.inRange(hsv, np.array([0, 60, 60]), np.array([179, 255, 255]))
    ink |= cv2.inRange(hsv, np.array([0, 0, 0]), np.array([179, 255, 100]))
    h, w = ink.shape
    cv2.rectangle(ink, (0, 0), (w, h), 0, 4)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return ink

def color_stats(tile, ink):
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    m = ink > 0
    if m.sum() == 0:
        return {"red": 0, "green": 0, "blue": 0, "dark": 0, "other": 0}
    h = hsv[:, :, 0][m]; s = hsv[:, :, 1][m]; v = hsv[:, :, 2][m]
    red = (((h <= 10) | (h >= 170)) & (s > 60)).mean()
    green = ((h >= 35) & (h <= 85) & (s > 60)).mean()
    blue = ((h >= 86) & (h <= 130) & (s > 60)).mean()
    dark = (v < 100).mean()
    other = 1 - red - green - blue - dark
    return {"red": round(float(red), 3), "green": round(float(green), 3),
            "blue": round(float(blue), 3), "dark": round(float(dark), 3),
            "other": round(float(other), 3)}

def components(ink, min_area=60):
    cnts, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    comps = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        a = bw * bh
        if a < min_area:
            continue
        comps.append({"x": int(x), "y": int(y), "w": int(bw), "h": int(bh),
                      "ar": round(bw / bh, 2)})
    return comps

def ocr_row(images, scale=3):
    """paste list of gray images (ink black on white) in a row on big canvas"""
    hs = [g.shape[0] for g in images]
    H = max(hs)
    W = sum(g.shape[1] for g in images) + 8 * (len(images) - 1)
    row = np.full((H, W), 255, np.uint8)
    x = 0
    for g in images:
        row[:g.shape[0], x:x + g.shape[1]] = g
        x += g.shape[1] + 8
    row = cv2.resize(row, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    h, w = row.shape
    canvas = np.full((max(h, 1600), max(w, 1600)), 255, np.uint8)
    y0 = (canvas.shape[0] - h) // 2; x0 = (canvas.shape[1] - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = row
    res, _ = ocr(canvas)
    return [r[1] for r in res] if res else []

def mask_to_gray(ink):
    g = np.full(ink.shape, 255, np.uint8)
    g[ink > 0] = 0
    return g

if __name__ == "__main__":
    out = {}
    for fp in sorted(glob.glob("harvest2/clusters/*.png")):
        img = cv2.imread(fp)
        name = os.path.basename(fp)
        ink = ink_mask(img)
        density = float((ink > 0).mean())
        entry = {"density": round(density, 3), "colors": color_stats(img, ink)}
        comps = components(ink)
        entry["n_comp"] = len(comps)
        entry["comps"] = comps[:16]
        # ink extent: top-half vs bottom-half
        hh = ink.shape[0]
        top = (ink[:hh // 2] > 0).sum()
        bot = (ink[hh // 2:] > 0).sum()
        entry["ink_top_bot"] = [int(top), int(bot)]
        # OCR full tile x3
        g = mask_to_gray(ink)
        entry["ocr_full"] = ocr_row([g, g, g])
        out[name] = entry
        print(name, json.dumps(entry, ensure_ascii=False), flush=True)
    with open("analysis.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
