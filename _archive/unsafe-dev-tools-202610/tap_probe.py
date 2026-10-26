# -*- coding: utf-8 -*-
"""tap_probe.py — 单次点击探针：找出螺牌真实命中区，不做第二次确认点击。

用法：
    python tap_probe.py [偏移...]       如 0 -20 20 -40

每轮：抓帧 → analyze → 取建议牌盒 → 单个偏移做单次点击 → 再抓帧 →
计算「目标列」与「整手牌带」两个尺度的变化指标，并把三张帧全部落盘。
只发单次点击（相当于选中），不会出牌。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import analyze_snapshot  # noqa: E402

ADB = r"D:/leidian/LDPlayer14/adb.exe"
SERIAL = "emulator-5556"
CAISHEN = __import__("os").environ.get("PROBE_CAISHEN", "w2")
WAIT = int(__import__("os").environ.get("PROBE_WAIT", "90"))
OUT = BOT_DIR.parent / "采集" / "dev_auto_play" / ("tap_probe2_" + time.strftime("%H%M%S"))
OUT.mkdir(parents=True, exist_ok=True)


def shot(tag: str) -> tuple[np.ndarray, Path]:
    path = OUT / f"{tag}.png"
    data = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=False).stdout
    path.write_bytes(data)
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR), path


def tap(x: int, y: int) -> None:
    subprocess.run([ADB, "-s", SERIAL, "shell", "input", "tap", str(int(x)), str(int(y))],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def metrics(before: np.ndarray, after: np.ndarray, box) -> tuple[float, float]:
    x0, y0, x1, y1 = (int(v) for v in box[:4])
    height, width = before.shape[:2]
    x0, x1 = max(0, x0 - 12), min(width, x1 + 12)
    y0, y1 = max(0, y0 - 90), min(height, y1 + 8)
    diff = cv2.absdiff(before[y0:y1, x0:x1], after[y0:y1, x0:x1])
    return float(diff.mean()), float((diff > 15).mean())


def band_metrics(before: np.ndarray, after: np.ndarray) -> tuple[float, float]:
    """整条手牌带的变化：用于判断点击是否真的送到了游戏画面。"""
    h = before.shape[0]
    b0, b1 = int(h * 0.75), h
    diff = cv2.absdiff(before[b0:b1, :], after[b0:b1, :])
    return float(diff.mean()), float((diff > 15).mean())


def main() -> int:
    offsets = [int(a) for a in sys.argv[1:]] or [0]
    waited = 0
    res: dict = {}
    while waited < WAIT:
        img, path = shot(f"wait_{waited:02d}")
        if img is not None:
            res = analyze_snapshot(path, caishen_label=CAISHEN,
                                   confirmed_two_player=True)
            if res.get("status") == "advise":
                break
        time.sleep(1.0)
        waited += 1
    print("轮询", waited, "次 -> status =", res.get("status"),
          "| suggestion =", res.get("suggestion"), flush=True)
    if res.get("status") != "advise":
        print("[SKIP] 未进入我方出牌态，未点击。reason =", res.get("reason"), flush=True)
        return 0

    for off in offsets:
        before, before_path = shot(f"off{off:+d}_before")
        r = analyze_snapshot(before_path, caishen_label=CAISHEN,
                             confirmed_two_player=True)
        if r.get("status") != "advise" or not r.get("suggested_boxes"):
            print(f"[off {off:+d}] 点击前已无可建议状态，停止。", flush=True)
            return 0
        box = r["suggested_boxes"][0]
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2 + off
        tap(cx, cy)
        time.sleep(0.7)
        after, _ = shot(f"off{off:+d}_after")
        col_mean, col_sig = metrics(before, after, box)
        band_mean, band_sig = band_metrics(before, after)
        ok = col_mean >= 2.5 and col_sig >= 0.015
        print(f"[off {off:+d}] tap=({cx},{cy}) 目标列 mean={col_mean:.2f} sig={col_sig:.4f} "
              f"| 手牌带 mean={band_mean:.2f} sig={band_sig:.4f} -> "
              f"{'抬起✓' if ok else '未抬起✗'}", flush=True)
        if not ok:
            tap(int(x0 + 4), int(y0 - 60))  # 点牌列上方空桌面撤销选中（不会出牌）
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
