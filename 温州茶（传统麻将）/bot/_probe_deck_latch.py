# -*- coding: utf-8 -*-
"""取证：第二道门禁「无中发规则」在圆盘帧上的修改前 / 修改后对照。

用法（项目根目录）：
    PY=C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe
    $PY bot/_probe_deck_latch.py            # 每会话扫 200 帧，凑够 20 个样本即停
    $PY bot/_probe_deck_latch.py 200 25     # (每会话上限, 目标样本数)

只读：不写图、不点、不动生产语料。逐会话独立 ``TeaRuleGates``（= 一间房一套锁），
按钮帧只有在本会话此前**已经确认过**规则时才被放行 —— 没确认过的仍然弃权，
这是 fail-closed 的本分，会被如实计入"修改后仍弃权"。
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

import cv2
import numpy as np

BOT = Path(__file__).resolve().parent
ROOT = BOT.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from claim_vision import (  # noqa: E402
    ACTION_ROI_X0, ACTION_ROI_X1, ACTION_ROI_Y0, ACTION_ROI_Y1,
    GOLD_HUE_MAX, GOLD_HUE_MIN, GOLD_SAT_MIN, GOLD_VAL_MIN, read_claims,
)
from mode_vision import (  # noqa: E402
    TeaRuleGates, detect_tea_no_zhong_fa, detect_tea_player_count,
)

SESSIONS = ROOT / "采集" / "assist_sessions"
GOLD_CANDIDATE_RATIO = 0.01


def load(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    del data
    if image is None or image.shape != (900, 1600, 3):
        return None
    return image


def gold_ratio_in_action_row(image: np.ndarray) -> float:
    roi = image[ACTION_ROI_Y0:ACTION_ROI_Y1, ACTION_ROI_X0:ACTION_ROI_X1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    gold = ((hsv[:, :, 0] >= GOLD_HUE_MIN) & (hsv[:, :, 0] <= GOLD_HUE_MAX)
            & (hsv[:, :, 1] >= GOLD_SAT_MIN) & (hsv[:, :, 2] >= GOLD_VAL_MIN))
    ratio = float(gold.sum()) / float(gold.size)
    del roi, hsv, gold
    return ratio


def session_dirs() -> list[Path]:
    found: list[Path] = []
    if not SESSIONS.is_dir():
        return found
    for child in sorted(SESSIONS.iterdir()):
        if not child.is_dir():
            continue
        if (child / "recognition_audit.jsonl").is_file() or (child / "live").is_dir():
            found.append(child)
    return found


def iter_frames(session: Path, limit: int):
    """流式产生帧路径：不先 rglob 成列表，读一张忘一张。"""
    count = 0
    for path in session.rglob("*.png"):
        parts = {p.lower() for p in path.parts}
        if "tap_probe" in parts:      # 测试泄漏的假目录
            continue
        yield path
        count += 1
        if limit and count >= limit:
            return


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    scanned = 0
    disc_frames = 0
    deck_blocked_before = 0
    deck_ok_after = 0
    mode_ok_after = 0
    samples: list[str] = []
    residual: list[str] = []

    for session in session_dirs():
        gates = TeaRuleGates()
        for index, path in enumerate(iter_frames(session, limit)):
            image = load(path)
            if image is None:
                continue
            scanned += 1
            try:
                raw_mode = detect_tea_player_count(image)
                raw_deck = detect_tea_no_zhong_fa(image)
                mode, deck = gates.observe(image)
                if gold_ratio_in_action_row(image) < GOLD_CANDIDATE_RATIO:
                    continue
                if not read_claims(image).known:
                    continue
                disc_frames += 1
                if raw_deck:
                    continue                      # 这一帧第二道门禁本来就没卡住
                deck_blocked_before += 1
                if deck.value is True:
                    deck_ok_after += 1
                    if mode.players is not None:
                        mode_ok_after += 1
                    if len(samples) < 8:
                        samples.append(
                            f"{session.name}/{path.name}: raw_mode={raw_mode} "
                            f"after_mode={mode.players}({mode.source}) "
                            f"deck={deck.source}")
                elif len(residual) < 8:
                    residual.append(
                        f"{session.name}/{path.name}: reason={deck.reason} "
                        f"resets={deck.resets} raw_mode={raw_mode} "
                        f"mode={mode.players}/{mode.source}")
                if deck_blocked_before >= target:
                    break
            finally:
                del image
                if index % 20 == 19:
                    gc.collect()
        gc.collect()
        print(f"  ... {session.name} 扫完：累计样本 {deck_blocked_before}",
              flush=True)
        if deck_blocked_before >= target:
            break

    print("=== 汇总（第二道门禁：无中发规则）===")
    print(f"扫描帧                {scanned}")
    print(f"检出行动圆盘的帧      {disc_frames}")
    print(f"第二道门禁被卡住的帧  {deck_blocked_before}")
    print(f"修改后 第二道放行     {deck_ok_after}"
          f"  ({deck_ok_after / max(1, deck_blocked_before):.1%})")
    print(f"  其中第一道也已放行  {mode_ok_after}（= 真正能进分析层）")
    print(f"修改后 仍弃权         {deck_blocked_before - deck_ok_after}")
    print("--- 救回样本（前 8）---")
    for line in samples:
        print(f"  {line}")
    print("--- 仍弃权样本（前 8）---")
    for line in residual:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
