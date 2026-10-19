"""Select distinct, complete 2-player tea frames from an existing replay video.

This is a read-only source scan. Saved frames are classifier predictions, never
ground truth or training labels. The original video is not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from mode_vision import detect_tea_player_count
from tile_vision import TileVision


FRAME_SIZE = (1600, 900)
MAX_CANDIDATES = 8
TEMPLATES = Path(__file__).resolve().parent.parent / "采集" / "templates" / "tea_tiles"


def sample_video(video: Path, output_dir: Path, *, fps: int = 1) -> dict:
    if not isinstance(fps, int) or not 1 <= fps <= 5:
        raise ValueError("采样帧率须为 1–5 帧/秒")
    video = video.resolve(strict=True)
    output_dir = output_dir.resolve()
    if not video.is_file() or video.suffix.lower() != ".mp4":
        raise ValueError("需要现有 MP4 回放文件")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FileNotFoundError("ffmpeg/ffprobe 未安装")
    index = output_dir / "candidate_index.json"
    if index.exists():
        raise FileExistsError(index)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams or (streams[0].get("width"), streams[0].get("height")) != FRAME_SIZE:
        raise ValueError("回放视频不是已验证的 1600×900 原生画面")

    source_before = (video.stat().st_size, video.stat().st_mtime_ns)
    source_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
               "-vf", f"fps={fps}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    vision = TileVision(template_dir=TEMPLATES)
    candidates: list[tuple[int, np.ndarray, list[int]]] = []
    seen_hands: set[tuple[int, ...]] = set()
    counts = {"frames": 0, "visible_2p": 0, "complete_2p": 0}
    frame_bytes = FRAME_SIZE[0] * FRAME_SIZE[1] * 3
    try:
        while True:
            raw = process.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError("ffmpeg 输出了不完整帧")
            image = np.frombuffer(raw, np.uint8).reshape(FRAME_SIZE[1], FRAME_SIZE[0], 3)
            sample_index = counts["frames"]
            counts["frames"] += 1
            if detect_tea_player_count(image) != 2:
                continue
            counts["visible_2p"] += 1
            hand = vision.recognize_hand(image)
            if not vision.last_complete or vision.last_meld_boxes or len(hand) not in (16, 17):
                continue
            counts["complete_2p"] += 1
            tile_ids = [tile_id for tile_id, *_ in hand]
            signature = tuple(tile_ids)
            if signature not in seen_hands and len(candidates) < MAX_CANDIDATES:
                seen_hands.add(signature)
                candidates.append((sample_index, image.copy(), tile_ids))
        process.stdout.close()
        error = process.stderr.read().decode(errors="replace")
        if process.wait(timeout=10) != 0:
            raise RuntimeError("ffmpeg 解码失败: " + error[:300])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    if (video.stat().st_size, video.stat().st_mtime_ns) != source_before:
        raise RuntimeError("源视频在扫描期间发生变化")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / f"candidate_{sample_index:03d}.png"
             for sample_index, _, _ in candidates]
    if any(path.exists() for path in paths):
        raise FileExistsError("已有同名候选帧，拒绝覆盖")
    records = []
    for sample_index, image, tile_ids in candidates:
        path = output_dir / f"candidate_{sample_index:03d}.png"
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise RuntimeError("候选帧 PNG 编码失败")
        with path.open("xb") as stream:
            stream.write(encoded.tobytes())
        record = {"file": path.name,
                  "sha256": hashlib.sha256(encoded.tobytes()).hexdigest(),
                  "tile_ids": tile_ids,
                  "status": "vision_prediction_not_human_ground_truth"}
        record["sample_index_at_1fps" if fps == 1 else "sample_index_at_fps"] = sample_index
        records.append(record)
    report = {"schema": "tea_replay_candidates_v1" if fps == 1 else "tea_replay_candidates_v2",
              "source_video": str(video), "source_sha256": source_sha,
              "sampling": f"ffmpeg fps={fps}; ordinal index is not exact PTS",
              "counts": counts, "max_saved": MAX_CANDIDATES,
              "candidates": records,
              "status": "visible_2p_complete_predictions_only_not_training_gold"}
    if fps != 1:
        report["sample_fps"] = fps
    with index.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fps", type=int, default=1, help="每秒抽帧数，1–5；默认 1")
    args = parser.parse_args()
    destination = args.output_dir or args.video.parent
    result = sample_video(args.video, destination, fps=args.fps)
    print(json.dumps({"counts": result["counts"], "saved": len(result["candidates"]),
                      "output": str(destination / "candidate_index.json")}, ensure_ascii=False))
