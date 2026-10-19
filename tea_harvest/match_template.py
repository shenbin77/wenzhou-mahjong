#!/usr/bin/env python3
"""Template match helper for tea_harvest. Prints one JSON line to stdout.
Usage: match_template.py <screenshot.png> <template.png> [threshold=0.82]
Exit 0 always; ok=false when no match / bad files.
"""
import json, sys, os
try:
    import cv2
except ImportError:
    print(json.dumps({"ok": False, "error": "no_cv2"}))
    sys.exit(0)

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage"}))
        return
    shot, tmpl = sys.argv[1], sys.argv[2]
    thresh = float(sys.argv[3]) if len(sys.argv) > 3 else 0.82
    if not os.path.isfile(shot) or not os.path.isfile(tmpl):
        print(json.dumps({"ok": False, "error": "missing_file", "shot": shot, "tmpl": tmpl}))
        return
    img = cv2.imread(shot, cv2.IMREAD_COLOR)
    tpl = cv2.imread(tmpl, cv2.IMREAD_COLOR)
    if img is None or tpl is None:
        print(json.dumps({"ok": False, "error": "imread_fail"}))
        return
    th, tw = tpl.shape[:2]
    ih, iw = img.shape[:2]
    if th > ih or tw > iw:
        print(json.dumps({"ok": False, "error": "tmpl_larger", "score": 0.0}))
        return
    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _mn, score, _ml, tl = cv2.minMaxLoc(res)
    cx = int(tl[0] + tw // 2)
    cy = int(tl[1] + th // 2)
    ok = bool(score >= thresh)
    print(json.dumps({
        "ok": ok,
        "score": round(float(score), 4),
        "x": cx,
        "y": cy,
        "tl_x": int(tl[0]),
        "tl_y": int(tl[1]),
        "w": int(tw),
        "h": int(th),
        "thresh": thresh,
        "shot": os.path.basename(shot),
        "tmpl": os.path.basename(tmpl),
    }))

if __name__ == "__main__":
    main()
