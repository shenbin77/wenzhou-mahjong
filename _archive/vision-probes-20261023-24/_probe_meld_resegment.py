# -*- coding: utf-8 -*-
"""只读探针：量化「漏检牌位」若被视觉层补回，会话 015005 能救多少帧。

背景（task #13 实测结论）：
  015005 会话 38 帧抛「明牌组数量或牌型未确认」，真实成因不是分区歧义，而是
  `count_tiles` 把一个牌面切成两个 <48px 的 run 后双双丢弃：
      7筒 牌面 x=452..533 被牌面深色竖纹切成 452..493 + 495..533
  于是几何组变成 [3, 2]（西碰 + 5筒6筒，缺 7筒），全局切分候选集为空。

本探针做两件事，全部只读（不写 bot/ 下任何文件）：
  1. 用 patches 版 count_tiles（仅合并「碎片宽度 < 最小牌宽」的相邻 run）重跑
     整会话，统计 see() 状态分布 —— 即视觉补回后的预期转化数。
  2. 打印每帧修复前后的对比（旧状态 / 新状态 / 副露组 / 建议牌）。

输出：采集/workbuddy_review_20261024/meld_resegment_probe.txt
"""
from collections import Counter
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
sys.path.insert(0, str(ROOT / "bot"))

from tea_bot import TeaBot  # noqa: E402
from tile_vision import TileVision, face_region  # noqa: E402

SESSION = ROOT / "采集" / "assist_sessions" / "20261024_015005" / "live" / "tea_2p"
OUT_DIR = ROOT / "采集" / "workbuddy_review_20261024"
TEMPLATES = ROOT / "采集" / "templates" / "tea_tiles"
LOG_DIR = ROOT / ".tmp" / "meld_resegment_logs"


def count_tiles_merged(self, img, roi=None):
    """count_tiles 的探针副本：在丢弃碎片前先合并「纹路造成的同牌分裂」。

    与原实现唯一差异是 runs 收集后插入的合并步骤：
      - 碎片宽度 < 48*scale（低于最小牌宽，原实现必然丢弃）
      - 与相邻 run 的间隙 < 6*scale（远小于组间 12*scale、台面到手中断 21px）
      - 合并后宽度 <= 125*scale（不越过单牌上限）
    三条同时满足才合并，因此不会把两张正常牌并成一张。
    """
    self.last_meld_boxes = []
    h, w = img.shape[:2]
    if roi is None:
        roi = (0, round(h * .8), w, h)
    x0, y0, x1, y1 = map(int, roi)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        self.detection_diagnostics = {'reason': 'empty_roi', 'unresolved_regions': []}
        return 0, []
    band = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 85, 255]))
    scale = w / 1600
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    skin_rows = gray[gray.shape[0] // 3:]
    column_skin = np.percentile(skin_rows, 90, axis=0).astype(np.float32)
    edge_window = max(5, round(17 * scale)) | 1
    local_skin = cv2.dilate(column_skin[None, :], np.ones((1, edge_window), np.uint8))[0]
    low_skin = column_skin < local_skin * .82
    visible_border = np.zeros_like(low_skin)
    ld = np.diff(np.r_[False, low_skin, False].astype(np.int8))
    for a, b in zip(np.flatnonzero(ld == 1), np.flatnonzero(ld == -1)):
        if b - a <= max(5, round(11 * scale)):
            visible_border[a:b] = True
    on = (np.count_nonzero(mask, axis=0) >= max(12 * scale, band.shape[0] * .07))
    on &= ~visible_border
    changes = np.diff(np.r_[False, on, False].astype(np.int8))
    runs = list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))

    # === 探针补丁：合并「比最小牌宽还窄」的碎片 ===
    merged = []
    for a, b in runs:
        if merged:
            pa, pb = merged[-1]
            frag = min(pb - pa, b - a) < 48 * scale
            gap = a - pb
            combined = b - pa
            if frag and gap < 6 * scale and combined <= 125 * scale:
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    runs = merged

    boxes, unresolved = [], []
    for a, b in runs:
        if b - a < 48 * scale:
            continue
        bx, ex = int(a), int(b)
        bw = ex - bx
        rows = np.flatnonzero(np.count_nonzero(mask[:, bx:ex], axis=1) >= bw * .4)
        if not len(rows):
            continue
        by, ey = int(rows[0]), int(rows[-1]) + 1
        candidate = band[by:ey, bx:ex]
        face = face_region(candidate)
        offset = (face.ctypes.data - candidate.ctypes.data) // candidate.strides[0]
        by += int(offset)
        ey = by + face.shape[0]
        bh = ey - by
        if bw > 125 * scale:
            unresolved.append([x0 + bx, y0 + by, x0 + ex, y0 + ey])
            continue
        if not (48 * scale <= bw <= 125 * scale and 80 * scale <= bh <= 180 * scale):
            continue
        if not (.42 <= bw / bh <= 1.05):
            continue
        boxes.append((x0 + bx, y0 + by, x0 + ex, y0 + ey))
    boxes.sort(key=lambda b: b[0])
    visible_count = len(boxes)
    meld_groups = []
    if len(boxes) >= 6:
        melds, boxes, meld_groups = self._split_exposed_prefix(boxes, scale)
        self.last_meld_boxes = melds
    self.detection_diagnostics = {
        'reason': 'visible_borders', 'unresolved_regions': unresolved,
        'roi': [x0, y0, x1, y1], 'visible_count': visible_count,
        'concealed_count': len(boxes),
        'meld_boxes': [list(b) for b in self.last_meld_boxes],
        'meld_groups': meld_groups}
    return len(boxes), boxes


def run_frame(png, patched):
    tv_cls = TileVision
    saved = tv_cls.count_tiles
    if patched:
        tv_cls.count_tiles = count_tiles_merged
    try:
        bot = TeaBot(shot_fn=lambda: png, log_dir=str(LOG_DIR))
        bot.see(png)
        return "ok", None
    except Exception as exc:  # noqa: BLE001 - 弃权即异常
        return "abstain", str(exc)
    finally:
        tv_cls.count_tiles = saved


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frames = sorted(SESSION.glob("*.png"))
    before, after = Counter(), Counter()
    detail = []
    for frame in frames:
        png = frame.read_bytes()
        sb, mb = run_frame(png, patched=False)
        sa, ma = run_frame(png, patched=True)
        before[f"{sb}:{mb}"] += 1
        after[f"{sa}:{ma}"] += 1
        if sb != sa or "明牌组" in (mb or ""):
            detail.append((frame.name, sb, mb, sa, ma))

    lines = [f"会话 {SESSION.name}: {len(frames)} 帧", "", "== 修复前 =="]
    for k, v in sorted(before.items()):
        lines.append(f"  {v:4d}  {k}")
    lines += ["", "== 视觉补回碎片后（探针补丁，只读） =="]
    for k, v in sorted(after.items()):
        lines.append(f"  {v:4d}  {k}")
    converted = sum(v for k, v in before.items() if k.startswith("abstain")) - \
        sum(v for k, v in after.items() if k.startswith("abstain"))
    lines += ["", f"弃权帧减少: {converted}", ""]

    lines.append("== 状态发生变化的帧（before -> after） ==")
    for name, sb, mb, sa, ma in detail:
        lines.append(f"{name}")
        lines.append(f"    before: {sb}  {mb}")
        lines.append(f"    after : {sa}  {ma}")
    text = "\n".join(lines)
    (OUT_DIR / "meld_resegment_probe.txt").write_text(text, encoding="utf-8")
    print(text[:6000])
    print(f"\n... 完整报告 {OUT_DIR / 'meld_resegment_probe.txt'}")


if __name__ == "__main__":
    main()
