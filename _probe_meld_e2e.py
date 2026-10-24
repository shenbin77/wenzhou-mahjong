# -*- coding: utf-8 -*-
"""只读端到端探针：带副露的真实会话帧改造前后可用性对比。

对每个帧：读财神 → TeaBot(默认识牌) see→think。
输出：总帧数、带副露帧数、给出建议帧数、弃权原因分布。
不写任何记录文件、不做任何输入操作。
"""
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent / "温州茶（传统麻将）" / "bot"
sys.path.insert(0, str(BOT_DIR))
from tea_bot import TeaBot, engine_to_vision  # noqa: E402
from tile_vision import TILE_IDS  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

SESSIONS = [
    "温州茶（传统麻将）/采集/assist_sessions/20261023_220643/live/tea_2p",
    "温州茶（传统麻将）/采集/assist_sessions/20261023_201640/live/tea_2p",
]
ROOT = Path(__file__).resolve().parent


def main():
    for session in SESSIONS:
        frames = sorted(Path(ROOT, session).glob("*.png"))
        stats = Counter()
        samples = []
        for frame in frames:
            png = frame.read_bytes()
            img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            caishen_label = detect_caishen(img).label
            caishen_id = None
            if caishen_label and caishen_label in TILE_IDS:
                caishen_id = engine_to_vision(TILE_IDS[caishen_label])
                caishen_id = {66: 72, 67: 73}.get(caishen_id, caishen_id)
            stats["frames"] += 1
            try:
                bot = TeaBot(caishen_id=caishen_id, shot_fn=lambda: png,
                             log_dir=str(ROOT / ".tmp" / "meld_e2e_logs"))
                bot.see(png)
                # 副露信息从 _default_vision 写回实例
                if bot.meld_groups:
                    stats["meld_frames"] += 1
                else:
                    stats["no_meld_frames"] += 1
                result = bot.think(bot.see(png))
                if result.is_hu:
                    stats["hu"] += 1
                elif result.discard is not None:
                    stats["advise"] += 1
                    if len(samples) < 5 and bot.meld_groups:
                        samples.append(f"{frame.name[:23]} 副露{bot.meld_groups} "
                                       f"向听{result.shanten} 打{result.discard}")
                else:
                    stats["wait"] += 1
                    if bot.meld_groups and len(samples) < 8:
                        samples.append(f"{frame.name[:23]} 副露{bot.meld_groups} "
                                       f"向听{result.shanten} wait: {result.reason[:40]}")
            except Exception as exc:  # 弃权路径
                reason = str(exc)
                key = reason.split("，")[0][:24]
                stats[f"abstain:{key}"] += 1
        print(f"== {Path(session).parts[-3]}")
        print(json.dumps(dict(stats), ensure_ascii=False, indent=1))
        for s in samples:
            print("  ·", s)


if __name__ == "__main__":
    main()
