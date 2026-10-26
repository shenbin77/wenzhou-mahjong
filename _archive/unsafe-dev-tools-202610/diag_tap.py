# -*- coding: utf-8 -*-
"""diag_tap.py — 带存证的单击诊断：完整走 TapExecutor 四道闸，落盘每阶段图像。

与生产行为的唯一差别：把点击前/点击后的目标列裁剪图存到 --out 目录，
用于人工核对「点的是哪张牌、有没有抬起」。不绕过任何门禁。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import analyze_snapshot  # noqa: E402
from tap_executor import TapAborted, TapExecutor  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else BOT_DIR / ".pytest_tmp" / "tap_diag"
OUT.mkdir(parents=True, exist_ok=True)
MANUAL_CAISHEN = (sys.argv[2].strip() if len(sys.argv) > 2 else "")
STAGE = {"n": 0}


class DiagExecutor(TapExecutor):
    """只在 _screenshot 上加存证，其余全部继承生产实现。"""

    def _screenshot(self) -> np.ndarray:
        image = super()._screenshot()
        STAGE["n"] += 1
        path = OUT / f"{STAGE['n']:02d}_shot.png"
        cv2.imencode(".png", image)[1].tofile(str(path))
        self._last = image
        return image


def main() -> int:
    loop_no = time.strftime("%H%M%S")
    executor = DiagExecutor(enabled=True)
    analysis = None
    analysis_path = None
    for attempt in range(40):
        raw = executor._screenshot()
        analysis_path = OUT / f"{loop_no}_frame.png"
        cv2.imencode(".png", raw)[1].tofile(str(analysis_path))
        analysis = analyze_snapshot(
            analysis_path, caishen_label=MANUAL_CAISHEN,
            confirmed_two_player=True,
            require_visible_caishen=not MANUAL_CAISHEN,
            log_dir=OUT / "logs")
        if analysis.get("status") == "advise":
            break
        time.sleep(1.5)
    else:
        print("[ABORT] 40 帧内没有出现可执行建议，未发任何点击。")
        return 0
    STAGE["n"] = 0
    print("status=%s suggestion=%s boxes=%s" % (
        analysis.get("status"), analysis.get("suggestion"),
        analysis.get("suggested_boxes")))
    plan = executor.plan(analysis)
    print("plan: tile=%s tap=(%d,%d) box=%s" % (plan.label, plan.x, plan.y, plan.box))
    image = executor._last
    crop = image[plan.box[1]:plan.box[3], plan.box[0]:plan.box[2]]
    cv2.imencode(".png", crop)[1].tofile(str(OUT / f"{loop_no}_target.png"))
    try:
        outcome = executor.execute(analysis)
        print("[VERIFIED]", outcome)
        return 0
    except TapAborted as exc:
        print("[ABORTED]", exc)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
