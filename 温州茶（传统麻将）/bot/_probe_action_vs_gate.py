# -*- coding: utf-8 -*-
"""只读探查：动作按钮（吃/碰/杠/胡/过）出现时，玩法门禁是否被误伤。

对照两组帧：abstain「画面未确认茶 2 人局」的帧 vs advise 的帧，
在右侧动作槽带统计高饱和金色/暗红像素占比。
"""
from __future__ import annotations

import json
import io
import sys
from pathlib import Path

import cv2
import numpy as np

SESSION = Path("D:/温州麻将/温州茶（传统麻将）/采集/assist_sessions/20261024_122607")

# 动作槽带（来自 action_inventory.md：槽位 x≈824/1033/1242/1431，y 有浮动）
SLOT = (780, 480, 1580, 700)
# 顶部规则文字带（mode_vision 用 y=140:245）
RULE = (280, 140, 1320, 245)


def imread(path: Path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


def gold_ratio(image) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    x0, y0, x1, y1 = SLOT
    gold = (h >= 12) & (h <= 38) & (s >= 90) & (v >= 150)
    return float(gold[y0:y1, x0:x1].mean())


def red_ratio(image) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    x0, y0, x1, y1 = SLOT
    red = ((h <= 8) | (h >= 172)) & (s >= 110) & (v >= 60)
    return float(red[y0:y1, x0:x1].mean())


def rule_brightness(image) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    x0, y0, x1, y1 = RULE
    return float(gray[y0:y1, x0:x1].mean())


def main() -> int:
    rows = [json.loads(line) for line in io.open(SESSION / "recognition_audit.jsonl",
                                                 encoding="utf-8") if line.strip()]
    groups = {"gate_fail": [], "advise": [], "wait": []}
    for row in rows:
        if row["status"] == "abstain" and "未确认茶" in row["reason"]:
            groups["gate_fail"].append(row)
        elif row["status"] in groups:
            groups[row["status"]].append(row)
    for name, items in groups.items():
        if not items:
            print(name, "无样本")
            continue
        golds, reds, rules = [], [], []
        for row in items:
            path = Path(row["frame"])
            if not path.is_file():
                continue
            img = imread(path)
            if img is None or img.shape[:2] != (900, 1600):
                continue
            golds.append(gold_ratio(img))
            reds.append(red_ratio(img))
            rules.append(rule_brightness(img))
        if not golds:
            print(name, "读不到图")
            continue
        print(f"{name:10s} n={len(golds):3d}  "
              f"金色占比 p50={np.median(golds):.4f} max={max(golds):.4f}  "
              f"红色占比 p50={np.median(reds):.4f} max={max(reds):.4f}  "
              f"顶部亮度 p50={np.median(rules):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
