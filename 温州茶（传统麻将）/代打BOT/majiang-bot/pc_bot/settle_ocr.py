"""Settlement-screen score OCR for 浙江游戏 温州麻将 (independent module).

Reads OUR score (沈瀚辰， bottom player) from a settlement screenshot.

Public API:
    extract_settle_score(img) -> int | None
        img: BGR numpy array (1600x900 screenshot from the emulator).
        Returns our signed score (e.g. -500, +4500), or None if the
        screen is not a settlement screen / recognition fails.

Method (v1):
    1. Crop the score ROI (bottom-center, below our avatar).
    2. Binarize bright text on the dark settlement background.
    3. Contour-segment individual glyphs, left -> right.
    4. Template-match each glyph against 0-9 / '-' / '+' templates
       cropped from real settlement screenshots
       (templates_new/settle_digits/).
    5. Parse the resulting string to int.

Does NOT touch bot.py; import it from wherever needed.
"""

from pathlib import Path

import cv2
import numpy as np

# ----------------------------------------------------------------------------
# Layout constants (1600x900). ROI is scaled if the image size differs.
# Our score sits bottom-center, under the 沈瀚辰 avatar/name on the
# settlement screen, rendered as large bright digits, e.g. "-500".
# ----------------------------------------------------------------------------
_REF_W, _REF_H = 1600, 900
_SCORE_ROI_REF = (800, 600, 1080, 710)  # x1, y1, x2, y2 (2026-10-15 按 f41 +3587 实测修正；旧 640,630,980,790 裁掉末位)

# All four seat score ROIs (1600x900) for template harvesting / multi-seat OCR.
SEAT_SCORE_ROIS = {
    "bottom": _SCORE_ROI_REF,
    "left": (20, 400, 290, 485),
    # right x2 tightened 1600→1545 to exclude 求财运/元宝 UI that polluted the last digit
    "right": (1320, 400, 1545, 485),
    "top": (780, 150, 1080, 230),
}

_DIGIT_DIR = Path(__file__).parent / "templates_new" / "settle_digits"
_GLYPH_NAMES = ["minus", "plus"] + [str(d) for d in range(10)]

_templates_cache = None


def _roi_for(img):
    h, w = img.shape[:2]
    sx, sy = w / _REF_W, h / _REF_H
    x1, y1, x2, y2 = _SCORE_ROI_REF
    return (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))


def _load_templates():
    """Load glyph templates {name: binary image}. Cached."""
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache
    tpl = {}
    if _DIGIT_DIR.is_dir():
        for name in _GLYPH_NAMES:
            p = _DIGIT_DIR / f"{name}.png"
            if p.exists():
                g = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if g is not None:
                    _, g = cv2.threshold(g, 127, 255, cv2.THRESH_BINARY)
                    tpl[name] = g
    _templates_cache = tpl
    return tpl


def _binarize(roi_bgr):
    """Bright text on dark background -> white glyphs on black."""
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    # Text is much brighter than the dark settlement backdrop.
    _, bw = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
    # Clean speckle noise.
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    return bw


def _segment_glyphs(bw):
    """Return glyph bounding boxes sorted left->right."""
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    h, w = bw.shape
    boxes = []
    for c in contours:
        x, y, ww, hh = cv2.boundingRect(c)
        area = ww * hh
        # Glyphs are large (~40-90px tall); drop specks and huge blobs.
        if hh > h * 0.98:
            continue
        # A minus sign is a short wide bar; keep it (was dropped -> "-500" read as 500).
        is_dash = hh >= h * 0.06 and ww >= hh * 2.0 and ww >= 12
        if hh < h * 0.25 and not is_dash:
            continue
        if not is_dash and (ww < 4 or area < 300):
            continue
        boxes.append((x, y, ww, hh))
    boxes.sort(key=lambda b: b[0])
    # Merge boxes that overlap heavily in x (split glyph parts).
    merged = []
    for b in boxes:
        if merged and b[0] < merged[-1][0] + merged[-1][2] * 0.6:
            x0 = min(merged[-1][0], b[0])
            y0 = min(merged[-1][1], b[1])
            x1 = max(merged[-1][0] + merged[-1][2], b[0] + b[2])
            y1 = max(merged[-1][1] + merged[-1][3], b[1] + b[3])
            merged[-1] = (x0, y0, x1 - x0, y1 - y0)
        else:
            merged.append(b)
    return merged


def _classify_glyph(glyph_bw, templates):
    """Template-match one glyph crop; return (name, score)."""
    best, best_s = None, -1.0
    gh, gw = glyph_bw.shape
    for name, t in templates.items():
        th, tw = t.shape
        # Resize template to glyph size for shape comparison.
        tr = cv2.resize(t, (gw, gh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(glyph_bw, tr, cv2.TM_CCOEFF_NORMED)
        s = float(res[0][0])
        if s > best_s:
            best, best_s = name, s
    return best, best_s


_MATCH_THR = 0.55


def _classify_glyph_refined(bw, box, templates, match_thr=_MATCH_THR):
    """Match one segmented box; if weak and tall, try vertical trims.

    Side-seat last digits often pick up 求财运 / shadow above the glyph, making
    the contour taller than a real digit and tanking the template score. Trimming
    back toward a typical digit height recovers 5/6 on f41-right / f69-right.
    """
    x, y, w, h = box
    pad = 3
    crop = bw[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
    name, score = _classify_glyph(crop, templates)
    if score >= match_thr:
        return name, score
    best_n, best_s = name, score
    if h < 50:
        return best_n, best_s
    for th in range(36, min(h, 56), 2):
        for y0 in range(0, max(1, h - th + 1), 2):
            g2 = bw[y + y0:y + y0 + th, max(0, x - 2):min(bw.shape[1], x + w + 2)]
            if g2.size == 0:
                continue
            n2, s2 = _classify_glyph(g2, templates)
            if s2 > best_s:
                best_n, best_s = n2, s2
                if best_s >= match_thr:
                    return best_n, best_s
    return best_n, best_s


def _filter_glyph_boxes(boxes, bw_h):
    """Drop trailing micro-noise blobs that are neither digits nor dashes."""
    if len(boxes) < 2:
        return boxes
    digit_hs = []
    for _x, _y, ww, hh in boxes:
        is_dash = hh >= bw_h * 0.06 and ww >= hh * 2.0 and ww >= 12
        if not is_dash:
            digit_hs.append(hh)
    if not digit_hs:
        return boxes
    med = sorted(digit_hs)[len(digit_hs) // 2]
    kept = []
    for b in boxes:
        x, y, ww, hh = b
        is_dash = hh >= bw_h * 0.06 and ww >= hh * 2.0 and ww >= 12
        if is_dash or hh >= med * 0.55 or hh >= 20:
            kept.append(b)
    return kept or boxes


def extract_score_in_roi(img, roi_ref):
    """OCR one seat's score in a reference-space ROI; int or None."""
    global _SCORE_ROI_REF
    saved = _SCORE_ROI_REF
    try:
        _SCORE_ROI_REF = tuple(roi_ref)
        return extract_settle_score(img)
    finally:
        _SCORE_ROI_REF = saved


def extract_settle_score(img):
    """Return our settlement score as int, or None on failure."""
    if img is None or img.size == 0:
        return None
    templates = _load_templates()
    if len(templates) < 5:  # need a usable glyph set
        return None

    x1, y1, x2, y2 = _roi_for(img)
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    bw = _binarize(roi)
    boxes = _segment_glyphs(bw)
    if not boxes:
        return None

    boxes = _filter_glyph_boxes(boxes, bw.shape[0])
    chars = []
    for box in boxes:
        name, score = _classify_glyph_refined(bw, box, templates, _MATCH_THR)
        if score < _MATCH_THR:  # unreliable glyph -> bail out
            return None
        chars.append("-" if name == "minus" else ("+" if name == "plus" else name))

    text = "".join(chars)
    # Must look like an optional sign followed by digits.
    t = text.lstrip("+")
    if not t or not t.lstrip("-").isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Training helper: crop glyph templates from a settlement screenshot.
# Usage: python settle_ocr.py --train settle_cap_xxx.png
# Saves per-glyph crops to /tmp/glyphs/g{i}.png for manual labeling; then
# rename the labeled ones into templates_new/settle_digits/<name>.png
# (minus.png / plus.png / 0.png ... 9.png).
# ----------------------------------------------------------------------------
def _train(src):
    img = cv2.imread(src)
    if img is None:
        print("cannot read", src)
        return
    x1, y1, x2, y2 = _roi_for(img)
    roi = img[y1:y2, x1:x2]
    bw = _binarize(roi)
    boxes = _segment_glyphs(bw)
    out = Path("/tmp/glyphs")
    out.mkdir(exist_ok=True)
    cv2.imwrite(str(out / "_roi.png"), roi)
    cv2.imwrite(str(out / "_bw.png"), bw)
    for i, (x, y, w, h) in enumerate(boxes):
        pad = 4
        crop = roi[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        cv2.imwrite(str(out / f"g{i}.png"), crop)
    print(f"roi={x1,y1,x2,y2} glyphs={len(boxes)} -> /tmp/glyphs/")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--train":
        _train(sys.argv[2])
    else:
        print("usage: python settle_ocr.py --train <settlement.png>")
