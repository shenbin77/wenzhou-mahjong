# -*- coding: utf-8 -*-
"""Timing probe: measure recognize_hand() cost on a real screenshot."""
import sys, time
sys.path.insert(0, "D:/HanaAgent/majiang-bot/pc_bot")
import os
os.chdir("D:/HanaAgent/majiang-bot/pc_bot")
from bot import TileRecognizer, decode_png, ADB

adb = ADB(serial="127.0.0.1:5557")
t0 = time.time()
png = adb.screencap()
t1 = time.time()
img = decode_png(png)
t2 = time.time()
print(f"screencap: {t1-t0:.2f}s, decode: {t2-t1:.2f}s, shape: {img.shape}", flush=True)

r = TileRecognizer()
t3 = time.time()
r.load_templates()
t4 = time.time()
print(f"load_templates: {t4-t3:.2f}s, n_templates: {len(r.templates)}", flush=True)

t5 = time.time()
res = r.recognize_hand(img)
t6 = time.time()
print(f"recognize_hand: {t6-t5:.2f}s, count: {len(res)}", flush=True)
print("TOTAL tick estimate: %.2fs" % (t6 - t0), flush=True)
