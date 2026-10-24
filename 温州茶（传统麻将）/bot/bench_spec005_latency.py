"""SPEC-005 latency bench: capture / recognize / advise p50 + p95 over 20 runs.

Run from the bot directory:

    python -m bench_spec005_latency

Every segment and capture-to-capture period uses ``time.perf_counter``. The
offline run replays a saved frame and waits for the same adaptive interval as
the window. It does not include Tk rendering or a real hand-change timestamp,
so its numbers cannot substitute for the live acceptance run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from live_capture import (DEFAULT_ADB, FileRunner, SegmentClock, grab_screen,  # noqa: E402
                          read_foreground)
from live_state import LatencyTracker, FrameTiming  # noqa: E402

DEFAULT_FRAME = (BOT_DIR.parent / "采集" / "2026-10-16" / "740195" / "r08" /
                 "frame_02.png")
DEFAULT_SHU_FRAME = (BOT_DIR.parent.parent / "阿法算牌分析" / "熟识牌对照_20261018" /
                     "frame_233s_14.png")


def _frame_for(mode: str) -> Path:
    if mode == "shu_4p" and DEFAULT_SHU_FRAME.is_file():
        return DEFAULT_SHU_FRAME
    return DEFAULT_FRAME


def run(mode: str, runs: int, out: Path, *, live: bool, serial: str) -> dict:
    import os
    from live_capture import analyze_bytes, save_frame, session_folder

    frame = _frame_for(mode)
    if not frame.is_file():
        raise FileNotFoundError(frame)
    runner = None if live else FileRunner(frame)
    session_dir_env = os.environ.get("SPEC005_BENCH_SESSION_DIR")
    if session_dir_env:
        session_dir = Path(session_dir_env) / mode
    else:
        session_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or
                           BOT_DIR.parent / "test_out") / "spec005_bench" / mode
    folder = session_folder(session_dir, mode)
    tracker = LatencyTracker()
    rows = []
    previous_capture_at = None
    for index in range(runs):
        clock = SegmentClock()
        capture_at = clock.started
        if live:
            read_foreground(DEFAULT_ADB, serial)
            raw = grab_screen(DEFAULT_ADB, serial)
        else:
            raw = grab_screen(str(Path(__file__)), serial, runner=runner)
        clock.mark("capture")
        path = save_frame(folder, raw) if live else frame
        result = analyze_bytes(raw, mode=mode, image_path=path,
                               log_dir=folder / "logs", clock=clock)
        timing = clock.timing(folder=str(path))
        period_ms = ((capture_at - previous_capture_at) * 1000
                     if previous_capture_at is not None else None)
        interval = tracker.record(timing, period_ms=period_ms)
        previous_capture_at = capture_at
        rows.append({**timing.as_dict(), "status": result.get("status")})
        print(f"run {index + 1}/{runs}: {timing.as_dict()} status={result.get('status')}")
        if index + 1 < runs:
            time.sleep(interval / 1000)
    summary = tracker.summary()
    summary["interval_ms"] = tracker.interval_ms
    summary["runs"] = runs
    summary["mode"] = mode
    summary["frame"] = str(frame)
    summary["live_capture"] = live
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "frames": rows}, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="tea_2p", choices=("tea_2p", "tea_4p", "shu_4p"))
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--live", action="store_true",
                        help="用真实 adb screencap 抓图（需模拟器已在前台）")
    parser.add_argument("--serial", default="emulator-5556")
    parser.add_argument("--out", type=Path,
                        default=BOT_DIR / "test_out" / "spec005_latency.json")
    parser.add_argument("--session-dir", type=Path, default=None,
                        help="日志/会话目录，默认避开仓库内 .pytest_tmp")
    args = parser.parse_args()
    if args.session_dir is not None:
        import os
        os.environ["SPEC005_BENCH_SESSION_DIR"] = str(args.session_dir)
    run(args.mode, args.runs, args.out, live=args.live, serial=args.serial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
