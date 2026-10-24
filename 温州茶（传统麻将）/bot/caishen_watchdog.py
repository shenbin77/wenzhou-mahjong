"""财神自动收集看门狗（只读监控 + 落 pending，不碰辅助器）。

用法（Python 3.11，非沙箱）：
    python caishen_watchdog.py [--once]

轮询最新 assist_session 里的新帧；当财字角标在但模板门禁不过时，
自动把"最像的 label"落成 pending 候选（侧车标注 auto_suggested）。
入库仍需 review_caishen_pending.py --confirm 的人工复核。

状态写 stdout；日志追加到 采集/assist_sessions/diagnostics/caishen_watchdog.log
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from caishen_collector import collect_auto_candidate  # noqa: E402
import caishen_vision  # noqa: E402

SESSIONS = ROOT.parent / "采集" / "assist_sessions"
LOG = SESSIONS / "diagnostics" / "caishen_watchdog.log"
POLL_SECONDS = 2.0


def latest_session() -> Path | None:
    if not SESSIONS.exists():
        return None
    candidates = [p for p in SESSIONS.iterdir()
                  if p.is_dir() and p.name.startswith("2026")]
    return max(candidates, key=lambda p: p.name) if candidates else None


def latest_frame(session: Path) -> Path | None:
    frames = []
    for png in session.rglob("*.png"):
        if png.name.startswith("_"):
            continue
        frames.append(png)
    return max(frames, key=lambda p: p.stat().st_mtime) if frames else None


def read_frame(path: Path) -> np.ndarray | None:
    try:
        return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="只扫当前最新一帧一次就退出")
    args = parser.parse_args()

    seen: Path | None = None
    log("watchdog 启动（盯最新会话的最新帧；识别失败自动落 pending）")
    while True:
        session = latest_session()
        if session is None:
            log("没有会话目录，等待…")
        else:
            frame = latest_frame(session)
            if frame is not None and frame != seen:
                seen = frame
                img = read_frame(frame)
                if img is not None:
                    result = collect_auto_candidate(
                        img, frame_path=frame, session_id=session.name)
                    status = result.get("status")
                    if status == "matched":
                        log(f"{session.name} {frame.name} 财神={result['label']} "
                            f"score={result['score']:.4f}")
                    elif status == "pending":
                        log(f"{session.name} {frame.name} ★自动提名 "
                            f"{result['label']}（score={result.get('score')}）"
                            f" -> pending/{result['file']}，待人工 confirm")
                    elif status == "duplicate":
                        log(f"{session.name} {frame.name} 候选已存在"
                            f"（{result.get('existing')}），跳过")
                    elif status == "too_uncertain":
                        log(f"{session.name} {frame.name} top1="
                            f"{result.get('label')}@{result.get('score'):.4f} "
                            f"太低，不提名")
                    elif status == "no_badge":
                        log(f"{session.name} {frame.name} 无财字角标"
                            f"（{result.get('reason')}），跳过")
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
