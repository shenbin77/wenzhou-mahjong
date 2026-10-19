# -*- coding: utf-8 -*-
"""Build a horizontal strip of detected hand tiles for visual reading."""
import sys, os
import cv2
import numpy as np
from detect_tiles import detect_hand_tiles

def strip(frame, out):
    img = cv2.imread(frame)
    boxes = detect_hand_tiles(img)
    if not boxes:
        print(frame, "no tiles")
        return
    mh = max(b[3] for b in boxes)
    W = sum(b[2] + 6 for b in boxes) + 6
    canvas = np.full((mh + 34, W, 3), 30, np.uint8)
    x = 6
    for i, (bx, by, bw, bh) in enumerate(boxes):
        crop = img[by:by+bh, bx:bx+bw]
        canvas[28:28+bh, x:x+bw] = crop
        cv2.putText(canvas, str(i), (x + bw//2 - 8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        x += bw + 6
    canvas = cv2.resize(canvas, (W*2, (mh+34)*2), interpolation=cv2.INTER_CUBIC)
    cv2.imwrite(out, canvas)
    print(out, len(boxes), "tiles")

if __name__ == "__main__":
    os.makedirs("strips", exist_ok=True)
    for f in sys.argv[1:]:
        name = os.path.splitext(os.path.basename(f))[0]
        strip(f, os.path.join("strips", name + "_strip.png"))
