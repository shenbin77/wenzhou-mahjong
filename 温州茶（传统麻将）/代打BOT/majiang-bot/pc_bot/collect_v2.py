# -*- coding: utf-8 -*-
"""
CNN 数据采集 v2：
1. 用 btn_xiayiju.png 模板排除结算画面帧
2. 只从实战帧（count 13/14）切牌
3. 每张牌 per-crop 模板匹配预标注
4. score>=0.80 进 cnn_data/<folder>/，否则进 _uncertain/
"""
import sys, os, csv, time, shutil
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
THRESH = 0.80
CROP_SIZE = (64, 96)
PAD = 2
XIANGJU_THRESH = 0.85  # 下一局按钮判定阈值


def is_settlement(img_gray, tmpl):
    """结算画面？（有下一局按钮）"""
    res = cv2.matchTemplate(img_gray, tmpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max()) >= XIANGJU_THRESH


def classify_crop(crop_gray, templates):
    ch, cw = crop_gray.shape
    best_tid, best_score, best_name = None, -2.0, None
    for name, tid, tmpl in templates:
        rs = cv2.resize(tmpl, (cw, ch), interpolation=cv2.INTER_AREA)
        s = float(cv2.matchTemplate(crop_gray, rs,
                                    cv2.TM_CCOEFF_NORMED)[0, 0])
        if s > best_score:
            best_tid, best_score, best_name = tid, s, name
    return best_tid, best_score, best_name


def main():
    # 清掉 v1 的垃圾数据
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    UNCERTAIN.mkdir()

    rec = botmod.TileRecognizer()
    rec.load_templates()
    templates = rec.templates
    print(f"[v2] 模板数: {len(templates)}", flush=True)

    xj = cv2.imread(str(PC_BOT / "templates_new" / "btn_xiayiju.png"),
                    cv2.IMREAD_GRAYSCALE)
    if xj is None:
        print("[v2] 警告：btn_xiayiju.png 不存在，不做结算过滤", flush=True)

    frames = sorted((PC_BOT / "frames_raw").glob("*.png"))
    print(f"[v2] 帧数: {len(frames)}", flush=True)

    mf = open(OUT / "manifest.csv", "w", newline="", encoding="utf-8")
    wr = csv.writer(mf)
    wr.writerow(["frame", "idx", "x1", "y1", "x2", "y2",
                 "pred_tid", "folder", "score", "kept"])

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
        if xj is not None and is_settlement(gray, xj):
            n_settlement += 1
            continue
        try:
            count, boxes = rec.count_tiles(img)
        except Exception:
            continue
        if count not in (13, 14):
            n_badcount += 1
            continue
        n_frames_used += 1
        for bi, (x1, y1, x2, y2) in enumerate(boxes):
            if (x2 - x1) < 40 or (y2 - y1) < 60:
                continue
            crop = img[max(0, y1 - PAD):min(h, y2 + PAD),
                       max(0, x1 - PAD):min(w, x2 + PAD)]
            if crop.size == 0:
                continue
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            tid, score, name = classify_crop(g, templates)
            folder = TID_TO_FOLDER.get(tid, f"unk{tid}")
            if score >= THRESH:
                d = OUT / folder
                d.mkdir(exist_ok=True)
                stats[folder] += 1
                kept = 1
            else:
                d = UNCERTAIN
                n_uncertain += 1
                kept = 0
            std = cv2.resize(g, CROP_SIZE, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(d / f"{fp.stem}_{bi:02d}_{score:.2f}.png"), std)
            wr.writerow([fp.name, bi, x1, y1, x2, y2, tid, folder,
                         f"{score:.4f}", kept])
        if (fi + 1) % 40 == 0:
            print(f"[v2] {fi+1}/{len(frames)} 帧，用时 {time.time()-t0:.0f}s",
                  flush=True)
    mf.close()

    print("=" * 50)
    print(f"[v2] 结算帧跳过: {n_settlement}, 张数不对跳过: {n_badcount}, "
          f"使用帧: {n_frames_used}")
    print(f"[v2] 有效 {sum(stats.values())} 张，_uncertain {n_uncertain} 张")
    for folder in sorted(stats):
        print(f"  {folder}: {stats[folder]}")
    all_f = ([f"w{n}" for n in range(1, 10)] + [f"t{n}" for n in range(1, 10)] +
             [f"s{n}" for n in range(1, 10)] +
             ["hongzhong", "dong", "nan", "xi", "bei", "zhong", "fa", "bai"])
    missing = [f for f in all_f if f not in stats]
    print(f"[v2] 缺类 ({len(missing)}): {missing}")


if __name__ == "__main__":
    main()
