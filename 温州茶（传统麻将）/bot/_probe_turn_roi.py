# -*- coding: utf-8 -*-
"""只读探查：用连续帧的时域变化定位「倒计时/当前行动玩家」候选区域。

不写任何文件到采集目录，stdout 输出文本热力图。用于给 P0「当前行动玩家 +
倒计时读数」模块定 ROI 提供量化依据，替代肉眼看图。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

LIST_LIMIT = 60


def imread(path: Path):
    data = np.fromfile(str(path), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if root is None or not root.is_dir():
        print("用法: _probe_turn_roi.py <帧目录>")
        return 2
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    files = sorted(list(root.glob("*.png")))[start:start + LIST_LIMIT]
    if len(files) < 8:
        print(f"帧数不足: {len(files)}")
        return 2
    frames = []
    for path in files:
        img = imread(path)
        if img is not None and img.shape[:2] == (900, 1600):
            frames.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.int16))
    if len(frames) < 8:
        print("可用帧不足")
        return 2
    stack = np.stack(frames)
    print(f"帧数={len(stack)}  取用文件 {files[0].name} .. {files[-1].name}")

    # 相邻帧差分的最大值（抵抗偶发噪声，突出"持续在动"的区域）
    diffs = np.abs(np.diff(stack, axis=0))
    activity = diffs.mean(axis=0)
    # 再看「每帧都在动」的比例：倒计时每秒一跳，稳定区域不会
    always = (diffs > 8).mean(axis=0)

    height, width = activity.shape
    block_h, block_w = 50, 50
    rows, cols = height // block_h, width // block_w
    print(f"\n== 时域活跃度热力图（每格 {block_h}x{block_w}px，值=相邻帧平均绝对差）==")
    header = "     " + "".join(f"{c % 10}" for c in range(cols))
    print(header)
    grid = np.zeros((rows, cols))
    for r in range(rows):
        for c in range(cols):
            patch = activity[r * block_h:(r + 1) * block_h, c * block_w:(c + 1) * block_w]
            grid[r, c] = float(patch.mean())
        line = "".join(" .:-=+*#@"[min(8, int(grid[r, c] / 1.2))] for c in range(cols))
        print(f"y{r:<3} {line}")

    print("\n== 活跃度最高的非手牌区（y < 0.72H）格子 ==")
    limit_row = int(rows * 0.72)
    flat = [(float(grid[r, c]), r, c) for r in range(limit_row) for c in range(cols)]
    flat.sort(reverse=True)
    for value, r, c in flat[:15]:
        always_val = float(always[r * block_h:(r + 1) * block_h,
                                 c * block_w:(c + 1) * block_w].mean())
        print(f"  y={r * block_h:>3}-{(r + 1) * block_h:<3} x={c * block_w:>4}-{(c + 1) * block_w:<4}"
              f"  活动={value:6.2f}  持续变化率={always_val:.2f}")

    # 连通域：把「几乎每帧都在变」的像素聚成块，数字类控件是小方块
    print("\n== 持续变化像素的连通域（候选：倒计时数字 / 行动指示）==")
    ticking = ((diffs > 10).mean(axis=0) >= 0.35).astype(np.uint8)
    ticking[:int(height * 0.72), :] = ticking[:int(height * 0.72), :]
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(ticking, 8)
    boxes = []
    for index in range(1, count):
        x, y = int(stats[index][cv2.CC_STAT_LEFT]), int(stats[index][cv2.CC_STAT_TOP])
        w, h = int(stats[index][cv2.CC_STAT_WIDTH]), int(stats[index][cv2.CC_STAT_HEIGHT])
        area = int(stats[index][cv2.CC_STAT_AREA])
        if area < 12:
            continue
        boxes.append((area, x, y, w, h))
    boxes.sort(reverse=True)
    for area, x, y, w, h in boxes[:20]:
        print(f"  box x={x:>4} y={y:>3} w={w:>3} h={h:>3}  面积={area:>5}")
    print(f"\n  持续变化像素总数 = {int(ticking.sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
