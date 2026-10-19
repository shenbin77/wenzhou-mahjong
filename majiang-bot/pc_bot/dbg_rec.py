# -*- coding: utf-8 -*-
"""调试：为什么 recognize_hand 在 frames_raw 上认不出牌"""
import sys
sys.path.insert(0, r"D:\HanaAgent\majiang-bot\pc_bot")
import cv2
import bot as b

rec = b.TileRecognizer()
rec.load_templates()
print("templates:", len(rec.templates), flush=True)

for fn in ["f29.png", "f80.png", "f31.png"]:
    img = cv2.imread(r"D:\HanaAgent\majiang-bot\pc_bot\frames_raw\\" + fn)
    if img is None:
        print(fn, "读不到", flush=True)
        continue
    c, boxes = rec.count_tiles(img)
    r = rec.recognize_hand(img)
    print(f"{fn}: count={c} recognized={len(r)}", flush=True)
    # 看模板匹配的原始分数分布
    gray = cv2.cvtColor(img[740:900, 0:1600], cv2.COLOR_BGR2GRAY)
    name, tid, tmpl = rec.templates[0]
    import numpy as np
    res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    print(f"  模板{name}: max={res.max():.3f} mean={res.mean():.3f}", flush=True)
