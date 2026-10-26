# -*- coding: utf-8 -*-
"""只读：若视觉层补回被切碎的碎片，会话 015005 那 38 帧端到端给出什么决策。

对每帧跑两次 TeaBot(see + think)：
  before = 现有 tile_vision
  after  = 只读探针补丁（合并两侧都窄于一张牌的相邻 run）
输出：采集/workbuddy_review_20261024/meld_38frames_decisions.txt
"""
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tea_bot import TeaBot  # noqa: E402
from tile_vision import TileVision  # noqa: E402
from _probe_resegment_diff import merged_count_tiles  # noqa: E402

SESSION = ROOT / "采集" / "assist_sessions" / "20261024_015005" / "live" / "tea_2p"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_38frames_decisions.txt"
LOG_DIR = ROOT / ".tmp" / "meld_38_logs"


def run(png, patched):
    saved = TileVision.count_tiles
    if patched:
        TileVision.count_tiles = merged_count_tiles
    try:
        bot = TeaBot(shot_fn=lambda: png, log_dir=str(LOG_DIR))
        hand = bot.see(png)
        thought = bot.think(hand)
        return (f"向听{thought.shanten} 打{thought.discard} "
                f"{'胡' if thought.is_hu else ''}{thought.reason[:40]}"
                f" | 闭手{len(hand)} 副露{bot.meld_groups}")
    except Exception as exc:  # noqa: BLE001 - 弃权即异常
        return f"弃权: {exc}"
    finally:
        TileVision.count_tiles = saved


def main():
    lines = ["会话 20261024_015005：38 帧弃权帧的端到端决策对比", ""]
    rows = []
    for frame in sorted(SESSION.glob("*.png")):
        png = frame.read_bytes()
        before = run(png, False)
        if "明牌" not in before:
            continue
        after = run(png, True)
        rows.append((frame.name, before, after))
    lines.append(f"命中 38 帧中的 {len(rows)} 帧（筛选条件：弃权理由含「明牌」）")
    lines.append("")
    for name, before, after in rows:
        lines.append(f"{name}")
        lines.append(f"    before: {before}")
        lines.append(f"    after : {after}")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text[:3000])
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
