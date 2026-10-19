# -*- coding: utf-8 -*-
"""
分析 frames_raw 每一帧的质量：
- count_tiles 张数
- 是否有广告条（左下 x<420 区域彩色像素占比）
输出 frame_stats.csv: frame, w, h, count, ad_ratio, usable
usable = (w,h)==(1600,900) and count in [13,14] and ad_ratio < 0.15
"""
import sys, os, csv
from pathlib import Path

PC_BOT = Path(r"D:\HanaAgent\majiang-bot\pc_bot")
sys.path.insert(0, str(PC_BOT))
os.chdir(str(PC_BOT))

import cv2
import numpy as np
import bot as botmod

AD_X1 = 420  # 广告条大致覆盖左下 x<420


def ad_ratio(img):
    """左下广告区彩色像素占比。手牌区正常是白牌+绿底；广告是彩色图片。"""
    x0, y0, x1, y1 = 0, 740, AD_X1, 900
    crop = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # 高饱和+中等亮度 = 彩色广告内容（排除白牌低饱和、绿底特定色相）
    s = hsv[:, :, 1].astype(np.float32)
    v = hsv[:, :, 2].astype(np.float32)
    colorful = ((s > 80) & (v > 50)).mean()
    return float(colorful)


def main():
    rec = botmod.TileRecognizer()
    frames = sorted((PC_BOT / "frames_raw").glob("*.png"))
    out = PC_BOT / "cnn_data" / "frame_stats.csv"
    out.parent.mkdir(exist_ok=True)
    n_usable = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["frame", "w", "h", "count", "ad_ratio", "usable"])
        for fp in frames:
            img = cv2.imread(str(fp))
            if img is None:
                wr.writerow([fp.name, 0, 0, -1, -1, 0])
                continue
            h, w = img.shape[:2]
            try:
                count, _ = rec.count_tiles(img)
            except Exception:
                count = -1
            ad = ad_ratio(img) if (w, h) == (1600, 900) else -1
            usable = int((w, h) == (1600, 900) and count in (13, 14)
                         and ad >= 0 and ad < 0.15)
            n_usable += usable
            wr.writerow([fp.name, w, h, count, f"{ad:.3f}", usable])
    print(f"[analyze] {len(frames)} 帧，可用 {n_usable} 帧 -> {out}", flush=True)


if __name__ == "__main__":
    main()
