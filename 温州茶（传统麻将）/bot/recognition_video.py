"""Review an existing MP4 through the same read-only recognition pipeline.

The assistant recognizes one frame per second, matching its live capture scale.
Every sampled frame is retained even when recognition abstains or raises.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from threading import Event
from typing import Callable

import cv2
import numpy as np

from live_capture import analyze_bytes, save_frame
from recognition_audit import append_frame_event, build_frame_event


VALID_SIZES = {(1600, 900), (1600, 948)}
VALID_MODES = {"tea_2p", "tea_4p", "shu_4p"}


def audit_video(video: str | Path, *, mode: str, session_dir: str | Path,
                stop: Event | None = None,
                progress: Callable[[int], None] | None = None) -> dict:
    source = Path(video).resolve(strict=True)
    if source.suffix.lower() != ".mp4" or not source.is_file():
        raise ValueError("只支持现有 MP4 录像")
    if mode not in VALID_MODES:
        raise ValueError("玩法无效")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise FileNotFoundError("录像复核需要本机 ffmpeg 和 ffprobe")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(source)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams:
        raise ValueError("录像没有可读取的视频轨道")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    if (width, height) not in VALID_SIZES:
        raise ValueError(f"录像尺寸 {width}×{height} 未验证")
    source_before = (source.stat().st_size, source.stat().st_mtime_ns)
    folder = Path(session_dir) / "video_review" / mode
    folder.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-vf", "fps=1", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    frame_size = width * height * 3
    count = 0
    errors = 0
    stopped = False
    try:
        while True:
            if stop is not None and stop.is_set():
                stopped = True
                break
            raw = process.stdout.read(frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError("录像解码输出不完整")
            image = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
            encoded_ok, encoded = cv2.imencode(".png", image)
            if not encoded_ok:
                raise RuntimeError("录像抽帧无法编码 PNG")
            png = encoded.tobytes()
            frame = save_frame(folder, png)
            try:
                result = analyze_bytes(
                    png, mode=mode, image_path=frame,
                    log_dir=folder / "logs")
            except Exception as exc:
                result = {"status": "error", "reason": str(exc)}
                errors += 1
            event = build_frame_event(png, frame, result, mode=mode, source="mp4_1fps")
            event["video_source"] = str(source)
            event["sample_index"] = count
            event["approx_second"] = count
            append_frame_event(session_dir, event)
            count += 1
            if progress is not None:
                progress(count)
        if stopped:
            process.terminate()
        error = process.stderr.read().decode(errors="replace")
        code = process.wait(timeout=10)
        if not stopped and code != 0:
            raise RuntimeError("录像解码失败：" + error[:300])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if (source.stat().st_size, source.stat().st_mtime_ns) != source_before:
        raise RuntimeError("源录像在复核期间发生变化")
    return {"sampled_frames": count, "analysis_errors": errors, "stopped": stopped,
            "audit": str(Path(session_dir) / "recognition_audit.jsonl")}
