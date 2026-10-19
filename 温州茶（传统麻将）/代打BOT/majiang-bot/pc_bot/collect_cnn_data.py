# -*- coding: utf-8 -*-
"""
CNN 识牌数据采集（并行任务 C）
- 对 frames_raw/ 每一帧：count_tiles() 切 bbox（位置准）
- 每个 bbox：模板逐个匹配做预标注（detect-then-classify）
- 按牌种存入 cnn_data/<folder>/，低置信度进 _uncertain/
- 输出 manifest.csv + 统计
只读 bot.py，不改它；不碰线上 bot 进程。
"""
import sys, os, csv, time
from pathlib import Path
from collections import defaultdict

PC_BOT = Path(r"D:\HanaAgent\majiang-bot\pc_bot")
sys.path.insert(0, str(PC_BOT))
os.chdir(str(PC_BOT))

import cv2
import numpy as np

import bot as botmod  # import-safe：只建日志，不启动 bot

TID_TO_FOLDER = {}
for n in range(1, 10):
    TID_TO_FOLDER[n] = f"w{n}"
    TID_TO_FOLDER[20 + n] = f"t{n}"
    TID_TO_FOLDER[40 + n] = f"s{n}"
TID_TO_FOLDER.update({55: "hongzhong", 61: "dong", 62: "nan",
                      63: "xi", 64: "bei", 65: "zhong", 66: "fa", 67: "bai"})

OUT = PC_BOT / "cnn_data"
UNCERTAIN = OUT / "_uncertain"
THRESH = 0.80          # 预标注置信度阈值
CROP_SIZE = (64, 96)   # 统一输出尺寸（w,h）
PAD = 2                # bbox 外扩像素


def classify_crop(crop_gray, templates):
    """对单个牌面 crop，逐模板匹配，返回 (best_tid, best_score, best_name)。"""
    ch, cw = crop_gray.shape
    best = (None, -2.0, None)
    for name, tid, tmpl in templates:
        th, tw = tmpl.shape
        # 把模板缩放到 crop 尺寸再匹配（小图，极快）
        rs = cv2.resize(tmpl, (cw, ch), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(crop_gray, rs, cv2.TM_CCOEFF_NORMED)
        score = float(res[0, 0])
        if score > best[1]:
            best = (tid, score, name)
    return best


def main():
    rec = botmod.TileRecognizer()
    rec.load_templates()
    print(f"[collect] 模板数: {len(rec.templates)}", flush=True)
    templates = rec.templates

    frames = sorted((PC_BOT / "frames_raw").glob("*.png"))
    print(f"[collect] 帧数: {len(frames)}", flush=True)

    OUT.mkdir(exist_ok=True)
    UNCERTAIN.mkdir(exist_ok=True)

    manifest_path = OUT / "manifest.csv"
    mf = open(manifest_path, "w", newline="", encoding="utf-8")
    wr = csv.writer(mf)
    wr.writerow(["frame", "idx", "x1", "y1", "x2", "y2",
                 "pred_tid", "folder", "score", "kept"])

    stats = defaultdict(int)
    uncertain_n = 0
    skipped_frames = 0
    t0 = time.time()

    for fi, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        if img is None:
            skipped_frames += 1
            continue
        h, w = img.shape[:2]
        if (w, h) != (1600, 900):
            # 非 1600x900 的帧跳过（hand_roi 是按 1600x900 定的）
            skipped_frames += 1
            continue
        try:
            count, boxes = rec.count_tiles(img)
        except Exception as e:
            print(f"[collect] {fp.name} count_tiles 异常: {e}", flush=True)
            skipped_frames += 1
            continue
        for bi, (x1, y1, x2, y2) in enumerate(boxes):
            bw, bh = x2 - x1, y2 - y1
            if bw < 40 or bh < 60:
                continue  # 碎片框
            px1 = max(0, x1 - PAD); py1 = max(0, y1 - PAD)
            px2 = min(w, x2 + PAD); py2 = min(h, y2 + PAD)
            crop = img[py1:py2, px1:px2]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            tid, score, name = classify_crop(gray, templates)
            folder = TID_TO_FOLDER.get(tid, f"unk{tid}")
            kept = score >= THRESH
            if kept:
                d = OUT / folder
                d.mkdir(exist_ok=True)
                stats[folder] += 1
            else:
                d = UNCERTAIN
                uncertain_n += 1
            # 统一尺寸存盘（CNN 输入一致）
            std = cv2.resize(gray, CROP_SIZE, interpolation=cv2.INTER_AREA)
            fn = f"{fp.stem}_{bi:02d}_{score:.2f}.png"
            cv2.imwrite(str(d / fn), std)
            wr.writerow([fp.name, bi, x1, y1, x2, y2, tid, folder,
                         f"{score:.4f}", int(kept)])
        if (fi + 1) % 20 == 0:
            print(f"[collect] {fi+1}/{len(frames)} 帧，用时 {time.time()-t0:.0f}s",
                  flush=True)

    mf.close()
    print("=" * 50)
    print(f"[collect] 完成：{len(frames)} 帧，跳过 {skipped_frames} 帧，"
          f"用时 {time.time()-t0:.0f}s")
    print(f"[collect] 有效 {sum(stats.values())} 张，_uncertain {uncertain_n} 张")
    print("[collect] 各类统计:")
    for folder in sorted(stats):
        print(f"  {folder}: {stats[folder]}")
    # 缺的类
    all_folders = [f"w{n}" for n in range(1, 10)] + \
                  [f"t{n}" for n in range(1, 10)] + \
                  [f"s{n}" for n in range(1, 10)] + \
                  ["hongzhong", "dong", "nan", "xi", "bei",
                   "zhong", "fa", "bai"]
    missing = [f for f in all_folders if f not in stats]
    print(f"[collect] 缺类 ({len(missing)}): {missing}")


if __name__ == "__main__":
    main()
