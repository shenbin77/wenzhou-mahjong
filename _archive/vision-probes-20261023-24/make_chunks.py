# -*- coding: utf-8 -*-
"""Shrink live_now.png to a small JPEG and split its base64 into chunk files."""
import base64, io, os
from PIL import Image

src = r"D:\温州麻将\_preflight\live_now.png"
outdir = r"D:\温州麻将\_preflight\chunks"
os.makedirs(outdir, exist_ok=True)
for f in os.listdir(outdir):
    os.remove(os.path.join(outdir, f))

img = Image.open(src).convert("RGB")
w, h = img.size
scale = 960 / w
img = img.resize((960, int(h * scale)))
buf = io.BytesIO()
img.save(buf, "JPEG", quality=52)
data = buf.getvalue()
with open(r"D:\温州麻将\_preflight\live_small.jpg", "wb") as f:
    f.write(data)
b64 = base64.b64encode(data).decode()
CH = 18000
n = (len(b64) + CH - 1) // CH
for i in range(n):
    with open(os.path.join(outdir, f"c{i:02d}.txt"), "w") as f:
        f.write(b64[i * CH:(i + 1) * CH])
print("orig", w, h, "jpgKB", round(len(data) / 1024), "b64chars", len(b64), "chunks", n)
