# -*- coding: utf-8 -*-
"""
CNN 数据采集 v3（detect-then-classify）：
- count_tiles() 定位置（bbox 准）
- recognize_hand() 定类别（tile_id 准）
- 按 bbox 中心最近邻匹配 label
- 存 cnn_data/<folder>/64x96.png
"""
import sys, os, csv, time, shutil, math
from pathlib import Path
from collections import defaultdict

PC_BOT = Path(r"D:\HanaAgent\majiang-bot\pc_bot")
sys.path.insert(0, str(PC_BOT))
os.chdir(str(PC_BOT))

import cv2
import numpy as np
import bot as botmod

TID_TO_FOLDER = {}
for n in range(1, 10):
    TID_TO_FOLDER[n] = f"w{n}"
    TID_TO_FOLDER[20 + n] = f"t{n}"
    TID_TO_FOLDER[40 + n] = f"s{n}"
TID_TO_FOLDER.update({55: "hongzhong", 61: "dong", 62: "nan",
                      63: "xi", 64: "bei", 65: "zhong", 66: "fa", 67: "bai"})

OUT = PC_BOT / "cnn_data"
UNCERTAIN = OUT / "_uncertain"
CROP_SIZE = (64, 96)
TILE_W, TILE_H = 75, 110  # 标准牌面尺寸（实测）
MATCH_DIST = 60           # bbox 中心与识别结果中心的最大距离


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    UNCERTAIN.mkdir()

    rec = botmod.TileRecognizer()
    rec.load_templates()
    print(f"[v3] 模板数: {len(rec.templates)}", flush=True)

    xj = cv2.imread(str(PC_BOT / "templates_new" / "btn_xiayiju.png"),
                    cv2.IMREAD_GRAYSCALE)

    frames = sorted((PC_BOT / "frames_raw").glob("*.png"))
    mf = open(OUT / "manifest.csv", "w", newline="", encoding="utf-8")
    wr = csv.writer(mf)
    wr.writerow(["frame", "idx", "x1", "y1", "x2", "y2",
                 "pred_tid", "folder", "kept"])

    stats = defaultdict(int)
    n_settlement = n_badcount = n_uncertain = n_frames_used = 0
    t0 = time.time()

    for fi, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        h, w = img.shape[:2]
        if (w, h) != (1600, 900):
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if xj is not None:
            if float(cv2.matchTemplate(
                    gray, xj, cv2.TM_CCOEFF_NORMED).max()) >= 0.85:
                n_settlement += 1
                continue
        try:
            count, boxes = rec.count_tiles(img)
        except Exception:
            continue
        if count not in (13, 14):
            n_badcount += 1
            continue
        try:
            recs = rec.recognize_hand(img)  # [(tid,x1,y1,x2,y2)]
        except Exception as e:
            print(f"[v3] {fp.name} recognize 异常: {e}", flush=True)
            continue
        n_frames_used += 1
        # 建中心点索引
        rcenters = [((r[1] + r[3]) / 2, (r[2] + r[4]) / 2, r[0])
                    for r in recs]
        for bi, (x1, y1, x2, y2) in enumerate(boxes):
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            best, bestd = None, 1e9
            for rx, ry, tid in rcenters:
                d = math.hypot(cx - rx, cy - ry)
                if d < bestd:
                    bestd, best = d, tid
            if best is None or bestd > MATCH_DIST:
                # 无匹配 -> uncertain（用 bbox 中心切固定尺寸）
                tid, folder, kept = None, "_uncertain", 0
                n_uncertain += 1
            else:
                tid = best
                folder = TID_TO_FOLDER.get(tid, f"unk{tid}")
                kept = 1
                stats[folder] += 1
            # 以 bbox 中心切 TILE_W x TILE_H（固定尺寸，不过度依赖 bbox 大小）
            ix1 = int(max(0, cx - TILE_W / 2))
            iy1 = int(max(0, cy - TILE_H / 2))
            ix2 = int(min(w, cx + TILE_W / 2))
            iy2 = int(min(h, cy + TILE_H / 2))
            crop = img[iy1:iy2, ix1:ix2]
            if crop.size == 0:
                continue
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            std = cv2.resize(g, CROP_SIZE, interpolation=cv2.INTER_AREA)
            d = OUT / folder if kept else UNCERTAIN
            d.mkdir(exist_ok=True)
            cv2.imwrite(str(d / f"{fp.stem}_{bi:02d}.png"), std)
            wr.writerow([fp.name, bi, x1, y1, x2, y2, tid or "", folder, kept])
        if (fi + 1) % 40 == 0:
            print(f"[v3] {fi+1}/{len(frames)} 帧，用时 {time.time()-t0:.0f}s",
                  flush=True)
    mf.close()
    print("=" * 50)
    print(f"[v3] 结算跳过:{n_settlement} 张数跳过:{n_badcount} "
          f"使用帧:{n_frames_used}")
    print(f"[v3] 有效 {sum(stats.values())} 张，_uncertain {n_uncertain} 张")
    for folder in sorted(stats):
        print(f"  {folder}: {stats[folder]}")
    all_f = ([f"w{n}" for n in range(1, 10)] + [f"t{n}" for n in range(1, 10)] +
             [f"s{n}" for n in range(1, 10)] +
             ["hongzhong", "dong", "nan", "xi", "bei", "zhong", "fa", "bai"])
    print(f"[v3] 缺类 ({len([f for f in all_f if f not in stats])}): "
          f"{[f for f in all_f if f not in stats]}")


if __name__ == "__main__":
    main()
