# -*- coding: utf-8 -*-
"""取证：行动圆盘帧在玩法门禁上的"修改前 / 修改后"对照。

只读：不写图、不点、不碰生产语料。逐会话按文件名顺序播放，每个会话用**独立的**
``TeaModeLatch``（等于实战里"一间房一个进程"的行为），因此：

* ''before'' = 修改前的逐帧口径 ``detect_tea_player_count``；
* ''after''  = 走粘滞后的同帧结论（锁存形成前的帧仍会弃权，这是 fail-closed 本意）。

用法（项目根目录）：
    PY=C:/Users/Administrator/AppData/Local/Programs/Python/Python311/python.exe
    $PY bot/_probe_mode_latch.py            # 每会话最多 400 帧
    $PY bot/_probe_mode_latch.py 0          # 不限帧数（内存吃紧时不建议）
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
    TeaModeLatch, detect_tea_no_zhong_fa, detect_tea_player_count,
    frame_round_identity,
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


def frame_paths(session: Path, limit: int) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(session.rglob("*.png")):
        parts = {p.lower() for p in path.parts}
        if "tap_probe" in parts:      # 测试泄漏的假目录，跳过
            continue
        paths.append(path)
        if limit and len(paths) >= limit:
            break
    return paths


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    sessions = session_dirs()
    print(f"真局会话目录 {len(sessions)} 个，每会话上限 {limit or '不限'} 帧",
          flush=True)

    scanned = 0
    disc_frames = 0
    before_fail = 0          # 修改前：按钮帧被判成"画面不认识"
    before_fail_zhong = 0    # 其中还同时挂在无中发规则门禁上的
    after_fail = 0           # 修改后：仍然拿不到玩法结论
    after_latched = 0        # 修改后：这一帧是吃粘滞过关的
    hud_known = 0
    hud_known_on_disc = 0
    caishen_known = 0
    caishen_known_on_disc = 0
    recovered: list[str] = []
    residual: list[str] = []
    for session in sessions:
        latch = TeaModeLatch()          # 一间房一把锁：新会话必须重新确认
        for index, path in enumerate(frame_paths(session, limit)):
            image = load(path)
            if image is None:
                continue
            scanned += 1
            try:
                hud, caishen = frame_round_identity(image)
                before = detect_tea_player_count(image)
                reading = latch.observe(image)
                after = reading.players
                if hud is not None:
                    hud_known += 1
                if caishen is not None:
                    caishen_known += 1
                if gold_ratio_in_action_row(image) < GOLD_CANDIDATE_RATIO:
                    continue
                state = read_claims(image)
                if not state.known:
                    continue
                disc_frames += 1
                if hud is not None:
                    hud_known_on_disc += 1
                if caishen is not None:
                    caishen_known_on_disc += 1
                failed_before = before != 2
                if failed_before:
                    before_fail += 1
                    if not detect_tea_no_zhong_fa(image):
                        before_fail_zhong += 1
                failed_after = after != 2
                if failed_after:
                    after_fail += 1
                    if len(residual) < 30:
                        residual.append(
                            f"{session.name}/{path.name}: reason={reading.reason} "
                            f"resets={latch.resets} age={reading.age_frames}f "
                            f"hud={'Y' if hud is not None else 'N'} "
                            f"caishen={caishen} raw={before}")
                elif failed_before and reading.source == "latched":
                    after_latched += 1
                    if len(recovered) < 6:
                        recovered.append(
                            f"{path.name}  (score={before}, raw=None → "
                            f"latched {after}, age={reading.age_frames}f)")
            finally:
                del image
                if index % 25 == 24:
                    gc.collect()
        gc.collect()
        print(f"  ... {session.name}：累计 {scanned} 帧 / 圆盘帧 {disc_frames} "
              f"/ 修改前弃权 {before_fail} / 修改后弃权 {after_fail}", flush=True)

    print("=== 汇总 ===")
    print(f"扫描帧            {scanned}")
    print(f"检出行动圆盘的帧  {disc_frames}")
    print(f"修改前 弃权       {before_fail}  ({before_fail / max(1, disc_frames):.1%})")
    print(f"  其中同时卡无中发 {before_fail_zhong}")
    print(f"修改后 弃权       {after_fail}  ({after_fail / max(1, disc_frames):.1%})")
    print(f"修改后 吃粘滞救回 {after_latched}")
    print(f"HUD 可读率        全帧 {hud_known}/{scanned}，圆盘帧 "
          f"{hud_known_on_disc}/{disc_frames}")
    print(f"财神 可读率       全帧 {caishen_known}/{scanned}，圆盘帧 "
          f"{caishen_known_on_disc}/{disc_frames}")
    print("--- 救回样本（前 6）---")
    for line in recovered:
        print(f"  {line}")
    print(f"--- 修改后仍弃权的帧（前 30 / 共 {after_fail}）---")
    for line in residual:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
