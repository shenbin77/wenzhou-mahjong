# -*- coding: utf-8 -*-
"""43 帧逐帧前后状态表（交付物）。

"修复前"直接取会话审计原文 `recognition_audit.jsonl`（真机跑出来的，不是我复算的）；
"修复后"用当前代码真实跑 TeaBot(see + think)，caishen_id 取该帧审计里的值
（38 帧 image=61 东，5 帧 manual=48），保证与线上同一条件。
输出：采集/workbuddy_review_20261024/meld_43frames_before_after.txt
"""
import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path("D:/温州麻将/温州茶（传统麻将）")
sys.path.insert(0, str(ROOT / "bot"))

from tea_bot import TeaBot  # noqa: E402

SESS = ROOT / "采集" / "assist_sessions" / "20261024_015005"
OUT = ROOT / "采集" / "workbuddy_review_20261024" / "meld_43frames_before_after.txt"
LOG_DIR = ROOT / ".tmp" / "meld_43_logs"


def main():
    rows = [json.loads(line) for line in
            (SESS / "recognition_audit.jsonl").open(encoding="utf-8")
            if line.strip()]
    targets = [r for r in rows if "明牌组" in (r.get("reason") or "")]
    lines = [f"会话 {SESS.name}：审计里「明牌组数量或牌型未确认」共 {len(targets)} 帧",
             "修复前 = 审计原文；修复后 = 当前代码真实重跑（caishen_id 取审计值）", ""]
    after_status = Counter()
    detail = []
    for row in targets:
        frame = Path(row["frame"])
        png = frame.read_bytes()
        caishen_id = row.get("caishen_id")
        bot = TeaBot(shot_fn=lambda: png, caishen_id=caishen_id,
                     log_dir=str(LOG_DIR))
        try:
            hand = bot.see(png)
            thought = bot.think(hand)
            after = (f"{thought.shanten} 向听"
                     + (f" 打{thought.discard}" if thought.discard else " 等待摸牌"))
            if thought.is_hu:
                after = "胡（异常：不应在副露未验证局面报胡）"
            status = "decided"
        except Exception as exc:  # noqa: BLE001 - 弃权即异常
            after = f"弃权：{exc}"
            status = "abstain"
        after_status[status] += 1
        detail.append((frame.name, caishen_id, row.get("caishen_source"),
                       row.get("status"), row.get("reason"), after,
                       bot.meld_groups))

    lines.append(f"修复后：可决策 {after_status.get('decided', 0)} 帧，"
                 f"仍弃权 {after_status.get('abstain', 0)} 帧")
    lines.append("")
    lines.append("帧名\t财神id\t来源\t修复前状态\t修复后\t副露组")
    for name, cid, src, st, reason, after, groups in detail:
        lines.append(f"{name}\t{cid}\t{src}\t{st}/{reason}\t{after}\t{groups}")
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print("\n".join(lines[:6]))
    print("...")
    for line in lines[7:12]:
        print(line)
    print("... 写入", OUT)


if __name__ == "__main__":
    main()
