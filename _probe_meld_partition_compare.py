# -*- coding: utf-8 -*-
"""只读对比：副露分组"旧自由切分"与"新几何硬约束"的判定差异。

Part A（回归面）：对 5 个实战会话逐帧取视觉层的 (ids, meld_groups)，分别用
   old = 旧的全局宽度 3/4 搜索（候选集必须恰好 1 个）
   new = 新的按组边界逐组判定（每组 3/4 张且牌型合法）
算出两种判定，统计迁移方向：
   old ok -> new ok    不变（正常帧）
   old 弃 -> new ok    收益（旧实现说不清、新实现有据可判）
   old ok -> new 弃    回归（必须为 0，否则新实现更保守了，要复核）
Part B（交卷表）：会话 20261024_015005 的 38 帧逐帧状态表（旧/新/若视觉补回碎片）。
输出：采集/workbuddy_review_20261024/meld_partition_compare.txt
"""
from collections import Counter
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
sys.path.insert(0, str(ROOT / "bot"))

from tile_vision import TileVision  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

SESS = ROOT / "采集" / "assist_sessions"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_partition_compare.txt"
TEMPLATES = ROOT / "采集" / "templates" / "tea_tiles"
SESSIONS = ["20261024_015005", "20261023_220643", "20261023_201640",
            "20261023_223759", "20261024_005400"]
TARGET = "20261024_015005"


def shape_ok(group):
    if len(set(group)) == 1:
        return len(group) in (3, 4)
    if len(group) != 3 or len(set(group)) != 3:
        return False
    suits = (range(1, 10), range(21, 30), range(41, 50))
    return any(all(t in suit for t in group) for suit in suits) \
        and max(group) - min(group) == 2


def old_verdict(ids):
    """旧实现：全组自由搜索，候选恰好 1 个才放行。"""
    def partitions(offset):
        if offset == len(ids):
            return {()}
        found = set()
        for width in (3, 4):
            group = ids[offset:offset + width]
            if len(group) == width and shape_ok(group):
                found.update((tuple(group),) + rest
                             for rest in partitions(offset + width))
        return found

    possible = partitions(0)
    if len(possible) != 1:
        return "弃权", "明牌组数量或牌型未确认"
    return "通过", ""


def new_verdict(ids, sizes):
    """新实现：组边界由视觉层给定，组内只判 3/4 张 + 牌型。"""
    if not sizes:
        return old_verdict(ids)
    if sum(sizes) != len(ids):
        return "弃权", "明牌组边界与识别张数不符"
    cursor = 0
    for index, size in enumerate(sizes, start=1):
        chunk = ids[cursor:cursor + size]
        cursor += size
        if size not in (3, 4):
            return "弃权", f"明牌第{index}组仅{size}张（疑似漏检）"
        if not shape_ok(chunk):
            return "弃权", f"明牌第{index}组牌型非法"
    return "通过", ""


def snapshot(path):
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    caishen = detect_caishen(img).label
    tv = TileVision(template_dir=str(TEMPLATES))
    tv.recognize_hand(img, caishen_label=caishen)
    dd = tv.detection_diagnostics
    melds = getattr(tv, "last_melds", [])
    ids = [int(row[0]) for row in melds if row[0] is not None]
    sizes = list(dd.get("meld_groups") or [])
    return ids, sizes, bool(tv.last_complete), int(dd.get("concealed_count", -1))


def main():
    lines = ["== Part A：旧自由切分 vs 新几何硬约束（5 个实战会话） =="]
    trans = Counter()
    regressions = []
    for name in SESSIONS:
        frames = sorted((SESS / name).glob("live/*/*.png"))
        per = Counter()
        for p in frames:
            snap = snapshot(p)
            if snap is None:
                continue
            ids, sizes, complete, concealed = snap
            if not ids:
                per["无副露"] += 1
                trans[("无副露", "", "")] += 1
                continue
            ov, orr = old_verdict(ids)
            nv, nrr = new_verdict(ids, sizes)
            per[f"{ov}->{nv}"] += 1
            trans[(ov, nv, f"{list(sizes)}")] += 1
            if ov == "通过" and nv == "弃权":
                regressions.append((name, p.name, ids, sizes, nrr))
        lines.append(f"  {name} ({len(frames)} 帧): {dict(per)}")

    lines += ["", "迁移汇总（旧 -> 新，按组边界形态）:"]
    agg = Counter()
    for (ov, nv, sz), n in trans.items():
        agg[(ov, nv)] += n
    for k, v in sorted(agg.items(), key=lambda x: -x[1]):
        lines.append(f"  {k[0]} -> {k[1]}: {v}")
    lines.append("")
    lines.append(f"回归帧（旧通过、新弃权）: {len(regressions)}")
    for name, fname, ids, sizes, reason in regressions[:20]:
        lines.append(f"  {name}/{fname} ids={ids} groups={sizes} 新理由={reason}")

    lines += ["", "", f"== Part B：{TARGET} 逐帧状态表 ==",
              "帧名\t台面 id\t组边界\t闭手\t旧判定\t新判定"]
    for p in sorted((SESS / TARGET).glob("live/*/*.png")):
        snap = snapshot(p)
        if snap is None:
            continue
        ids, sizes, complete, concealed = snap
        if not ids:
            continue
        ov, orr = old_verdict(ids)
        nv, nrr = new_verdict(ids, sizes)
        lines.append(f"{p.name}\t{ids}\t{sizes}\t{concealed}\t{ov}\t{nv}"
                     f"{('/' + nrr) if nrr else ''}")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text[:4500])
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
