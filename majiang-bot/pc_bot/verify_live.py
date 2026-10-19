# -*- coding: utf-8 -*-
"""验证 live 帧与当前模板的匹配度"""
import sys
sys.path.insert(0, r"D:\HanaAgent\majiang-bot\pc_bot")
import cv2
import bot as b
from pathlib import Path

rec = b.TileRecognizer()
rec.load_templates()
frames = sorted(Path(r"D:\HanaAgent\majiang-bot\pc_bot\frames_live").glob("*.png"))
print("live frames:", len(frames), flush=True)
for fp in frames[:3]:
    img = cv2.imread(str(fp))
    c, _ = rec.count_tiles(img)
    r = rec.recognize_hand(img)
    print(f"{fp.name}: count={c} recognized={len(r)}", flush=True)
    if r:
        print("  ->", " ".join(b.tile_name(t) for t, *_ in r[:14]), flush=True)
