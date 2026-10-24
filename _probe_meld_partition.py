# -*- coding: utf-8 -*-
"""只读探针：复现会话 20261024_015005 的副露分区弃权，导出歧义真实形态。

对每一帧跑 tea_bot._default_vision 链路（只读），筛出
「明牌组数量或牌型未确认」的帧，再打印：
- 视觉层几何组边界 meld_groups（每组张数）与各框坐标
- 明牌组识别出的 label 序列
- 现行全局自由切分 partitions() 的全部候选
- 逐组独立判定的结果（拟采用的修法）
输出：采集/workbuddy_review_20261024/meld_partition_probe.txt
"""
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
BOT = ROOT / "bot"
sys.path.insert(0, str(BOT))

from tea_bot import TeaBot, engine_to_vision  # noqa: E402
from tile_vision import TILE_IDS, TileVision  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

SESSION = ROOT / "采集" / "assist_sessions" / "20261024_015005" / "live" / "tea_2p"
OUT_DIR = ROOT / "采集" / "workbuddy_review_20261024"
TEMPLATES = ROOT / "采集" / "templates" / "tea_tiles"
LOG_DIR = ROOT / ".tmp" / "meld_partition_logs"


def valid_group(group):
    """与 tea_bot._default_vision 内同一判定（逐字复制）。"""
    if len(set(group)) == 1:
        return len(group) in (3, 4)
    if len(group) != 3 or len(set(group)) != 3:
        return False
    suits = (range(1, 10), range(21, 30), range(41, 50))
    return any(all(tile in suit for tile in group) for suit in suits) \
        and max(group) - min(group) == 2


def all_partitions(ids):
    """现行全局自由切分的全部候选（逐字复制 tea_bot 内 partitions）。"""
    def partitions(offset):
        if offset == len(ids):
            return {()}
        groups = set()
        for width in (3, 4):
            group = ids[offset:offset + width]
            if len(group) == width and valid_group(group):
                groups.update((tuple(group),) + rest
                              for rest in partitions(offset + width))
        return groups
    return partitions(0)


def per_group_verdict(ids, sizes):
    """拟采用修法：按几何组边界逐组独立判定，不做跨组自由切分。"""
    if not sizes or sum(sizes) != len(ids):
        return None, f"组张数{sizes}与识别张数{len(ids)}不符"
    out, cursor = [], 0
    for size in sizes:
        group = ids[cursor:cursor + size]
        cursor += size
        if not valid_group(group):
            return None, f"组{group}牌型非法"
        out.append(list(group))
    return out, "ok"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = []
    frames = sorted(SESSION.glob("*.png"))
    status = Counter()
    detail_frames = []
    for frame in frames:
        png = frame.read_bytes()
        bot = TeaBot(shot_fn=lambda: png, log_dir=str(LOG_DIR))
        try:
            bot.see(png)
            status["ok"] += 1
        except Exception as exc:  # 弃权
            msg = str(exc)
            if "明牌组" in msg:
                status[f"meld_abstain:{msg}"] += 1
                detail_frames.append(frame)
            else:
                status[f"other:{msg.split('，')[0][:20]}"] += 1

    report.append(f"会话 {SESSION.name}: {len(frames)} 帧")
    for key, cnt in sorted(status.items()):
        report.append(f"  {key}: {cnt}")
    report.append("")

    shape = Counter()
    for frame in detail_frames:
        png = frame.read_bytes()
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        caishen = detect_caishen(img).label
        tv = TileVision(template_dir=TEMPLATES)
        tv.recognize_hand(img, caishen_label=caishen)
        dd = tv.detection_diagnostics
        sizes = dd.get("meld_groups")
        boxes = dd.get("meld_boxes") or []
        rows = getattr(tv, "last_melds", [])
        ids = [row[0] for row in rows]
        diags = getattr(tv, "meld_diagnostics", [])
        cands = all_partitions(ids)
        per, note = per_group_verdict(ids, sizes)
        report.append(f"--- {frame.name}  caishen={caishen}")
        report.append(f"    geometry groups={sizes} meld_boxes={len(boxes)} "
                      f"recognized={len(ids)} complete={tv.last_complete}")
        report.append(f"    boxes={[list(b) for b in boxes]}")
        report.append(f"    labels={[d['label'] for d in diags]}")
        report.append(f"    accepted={[d['accepted'] for d in diags]} "
                      f"reasons={[d['reason'] for d in diags]}")
        report.append(f"    ids={ids}")
        report.append(f"    global_partitions candidates={len(cands)}: "
                      f"{[list(c) for c in cands]}")
        report.append(f"    per_group_verdict={per} note={note}")
        report.append("")
        shape[(len(cands) == 1, bool(per))] += 1

    report.append("== 形态统计（global候选==1?, 逐组判定可解?）: "
                  f"{dict(shape)}")
    text = "\n".join(report)
    (OUT_DIR / "meld_partition_probe.txt").write_text(text, encoding="utf-8")
    print(text[:4000])
    print(f"\n... 完整报告写入 {OUT_DIR / 'meld_partition_probe.txt'}")


if __name__ == "__main__":
    main()
