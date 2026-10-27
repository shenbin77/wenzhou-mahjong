"""Segmented read-only frame pipeline for the v2 assist window.

Three explicit stages with independent timings:

``capture``  ADB foreground check + ``exec-out screencap -p`` into memory
``recognize`` tile recognition + rule gate (pure image work)
``advise``   existing tea / Shu rule engines (same code as recorder uses)

The module never writes JSONL, never clicks and never types.  Recording stays
in ``recorder.AssistRecorder``; this pipeline only hands it the analysis.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from assist_shu import analyze_shu_snapshot
from assist_tea import analyze_snapshot
from frame_input import DecodedFrame
from harvest_tea import DEFAULT_ADB, DEFAULT_SERIAL, EXPECTED_PACKAGE
from live_state import FrameTiming
from recorder import normalize_candidates
from tile_vision import TILE_IDS


@dataclass
class FrameAnalysis:
    result: dict
    frame_path: Path | None
    timing: FrameTiming
    raw_sha256: str


@dataclass
class SegmentClock:
    """Manual stage marks so capture/recognize/advise can be timed separately."""

    started: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, stage: str) -> float:
        now = time.perf_counter()
        self.marks[stage] = now
        return now

    def timing(self, *, folder: str = "") -> FrameTiming:
        now = time.perf_counter()
        capture_end = self.marks.get("capture")
        recognize_end = self.marks.get("recognize", capture_end)
        advise_end = self.marks.get("advise", now)

        def span(start: float | None, end: float | None) -> float:
            if start is None or end is None:
                return 0.0
            return max(0.0, (end - start) * 1000)

        return FrameTiming(
            capture_ms=span(self.started, capture_end),
            recognize_ms=span(capture_end, recognize_end),
            advise_ms=span(recognize_end, advise_end),
            cycle_ms=span(self.started, now),
            folder=folder,
        )


def player_prefix(mode: str) -> tuple[bool, bool]:
    """Mode string -> (confirmed_two_player, confirmed_four_player)."""
    if mode == "tea_2p":
        return True, False
    if mode == "tea_4p":
        return False, True
    return False, False


def session_folder(session_dir: Path, mode: str) -> Path:
    """Live frames stay under the session folder, split by mode."""
    folder = Path(session_dir) / "live" / mode
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_frame(folder: Path, raw: bytes) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    path = Path(folder) / f"{stamp}.png"
    with path.open("xb") as handle:
        handle.write(raw)
    return path


def _adb_prefix(adb_path: str, serial: str) -> list[str]:
    adb = Path(adb_path)
    if not adb.is_file():
        raise FileNotFoundError(f"ADB 不存在: {adb}")
    if not serial or any(char.isspace() for char in serial):
        raise ValueError("模拟器序列号无效")
    return [str(adb), "-s", serial]


def read_foreground(adb_path: str, serial: str, *, runner=None) -> str:
    """Return the resumed activity line; raises when the game is not in front."""
    runner = runner or subprocess.run
    output = runner(_adb_prefix(adb_path, serial) + [
        "shell", "dumpsys", "activity", "activities",
    ], capture_output=True, timeout=15, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode(errors="replace")
    resumed = [line for line in output.splitlines() if "topResumedActivity=" in line]
    if not any(f"{EXPECTED_PACKAGE}/" in line for line in resumed):
        raise RuntimeError("游戏不在模拟器前台，停止采集")
    return resumed[0] if resumed else ""


def grab_screen(adb_path: str, serial: str, *, runner=None) -> bytes:
    """Second half of the capture stage: one PNG frame, read-only."""
    runner = runner or subprocess.run
    raw = runner(_adb_prefix(adb_path, serial) + ["exec-out", "screencap", "-p"],
                 capture_output=True, timeout=20, check=True,
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    if not raw or cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) is None:
        raise RuntimeError("模拟器截图不是有效 PNG")
    return raw


def observed_caishen(image: np.ndarray, mode: str) -> dict:
    """Only real recognisers: tea reads the 财 corner badge, Shu fixes red Zhong."""
    if mode.startswith("shu_"):
        return {"caishen": "hongzhong", "caishen_id": TILE_IDS["hongzhong"],
                "caishen_source": "rule"}
    from caishen_vision import detect_caishen

    observed = detect_caishen(image)
    if observed.label:
        return {"caishen": observed.label,
                "caishen_id": TILE_IDS.get(observed.label),
                "caishen_source": "image"}
    return {"caishen": None, "caishen_id": None, "caishen_source": None}


def display_candidates(result: dict, mode: str) -> list[dict]:
    """Top-5 candidate rows, exactly the shape the recorder writes."""
    raw = result.get("candidates")
    if raw is None:
        raw = result.get("options")
    if not raw:
        return []
    caishen_id = result.get("caishen_id")
    hand_ids = [int(value) for value in (result.get("hand_ids") or [])
                if value is not None]
    return normalize_candidates(
        mode, raw, hand_ids=hand_ids, caishen_id=caishen_id,
        meld_count=int(result.get("meld_count", 0) or 0),
    )


def analyze_bytes(
    raw: bytes, *, mode: str, image_path: Path, log_dir: Path,
    clock: SegmentClock | None = None, caishen_label: str = "",
    manual_caishen: bool = False, frame: DecodedFrame | None = None,
) -> dict:
    """Recognition + advice for one PNG frame (no sidecar, no annotation).

    ``clock`` (optional) is marked after recognition and after advice so the
    window can report the two segments separately.  ``caishen_label`` is the
    user's manual pick; it only reaches the tea engine when ``manual_caishen``
    is set AND the frame itself did not reveal a caishen (see below).
    An optional decoded frame must belong to these exact raw bytes.  It is
    local to this observation; new captures/verification never reuse old pixels.
    """
    base = {"mode": mode, "image": str(image_path), "annotation": None}
    if frame is not None:
        if not isinstance(frame, DecodedFrame):
            raise TypeError("frame must be a DecodedFrame")
        if frame.raw != raw:
            raise ValueError("decoded frame does not match raw PNG bytes")
    else:
        try:
            frame = DecodedFrame.from_png(raw)
        except ValueError:
            return {**base, "status": "abstain", "reason": "截图不是有效 PNG"}
    image = frame.image
    two, four = player_prefix(mode)
    mode = mode if mode in {"tea_2p", "tea_4p", "shu_4p"} else "tea_2p"
    manual = bool(manual_caishen and caishen_label and mode.startswith("tea_"))
    observed = observed_caishen(image, mode)
    recognized = clock.mark if clock is not None else None
    if mode.startswith("tea_"):
        result = analyze_snapshot(
            image_path, caishen_label=caishen_label if manual else "",
            confirmed_two_player=two, confirmed_four_player=four,
            log_dir=log_dir, output_path=None, frame=frame,
            on_recognized=(lambda: recognized("recognize")) if recognized else None,
        )
    else:
        result = analyze_shu_snapshot(
            image_path, confirmed_four_player=True,
            on_recognized=(lambda: recognized("recognize")) if recognized else None,
        )
    if not result.get("mode"):
        result["mode"] = mode
    result["mode"] = result["mode"] if result["mode"] in {
        "tea_2p", "tea_4p", "shu_4p"} else mode
    if clock is not None and "recognize" not in clock.marks:
        clock.mark("recognize")
    auto = observed.get("caishen")
    if auto and manual and auto != caishen_label:
        result.update(observed)
        result["status"] = "abstain"
        result["reason"] = "手选财神与画面财神不一致，弃权"
    elif manual and not auto:
        result["caishen"] = caishen_label
        result["caishen_id"] = TILE_IDS.get(caishen_label)
        result["caishen_source"] = "manual"
    else:
        result.update(observed)
        if result.get("caishen_source") == "manual" and not result.get("caishen"):
            result["caishen_source"] = None
    if result.get("status") == "abstain" and not result.get("vision_diagnostics"):
        result.setdefault("hand", [])
    if result.get("suggestion") and result.get("suggestion_id") is None:
        result["suggestion_id"] = TILE_IDS.get(result["suggestion"])
    if result.get("status") == "advise" or result.get("candidates") is not None:
        try:
            result["candidates"] = display_candidates(result, result["mode"])
        except (ValueError, KeyError):
            result["candidates"] = []
    if clock is not None:
        clock.mark("advise")
    return result


def capture_result(
    raw: bytes, *, mode: str, image_path: Path, log_dir: Path,
    folder: str = "", caishen_label: str = "", manual_caishen: bool = False,
) -> tuple[dict, FrameTiming]:
    """Run recognize + advise with their own timings, no capture step."""
    clock = SegmentClock(time.perf_counter())
    clock.mark("capture")
    result = analyze_bytes(raw, mode=mode, image_path=image_path,
                           log_dir=log_dir, clock=clock,
                           caishen_label=caishen_label,
                           manual_caishen=manual_caishen)
    return result, clock.timing(folder=folder)


def capture_frame(
    *, mode: str, adb_path: str = DEFAULT_ADB, serial: str = DEFAULT_SERIAL,
    session_dir: Path, runner=None,
) -> tuple[bytes, FrameTiming]:
    """Capture stage only; returns the PNG bytes and its timing."""
    clock = SegmentClock(time.perf_counter())
    read_foreground(adb_path, serial, runner=runner)
    raw = grab_screen(adb_path, serial, runner=runner)
    return raw, clock.timing()


def analyze_once(
    *, mode: str, adb_path: str = DEFAULT_ADB, serial: str = DEFAULT_SERIAL,
    session_dir: Path, runner=None, frame_path: Path | None = None,
    raw: bytes | None = None, save: bool = True, clock: SegmentClock | None = None,
    check_foreground: bool = False,
) -> FrameAnalysis:
    """One full frame through all three stages; never sends game input."""
    clock = clock or SegmentClock()
    if raw is None:
        if check_foreground:
            read_foreground(adb_path, serial, runner=runner)
        raw = grab_screen(adb_path, serial, runner=runner)
    clock.mark("capture")
    folder = session_folder(session_dir, mode)
    path = Path(frame_path) if frame_path is not None else save_frame(folder, raw)
    result = analyze_bytes(raw, mode=mode, image_path=path,
                           log_dir=folder / "logs", clock=clock)
    timing = clock.timing(folder=str(path))
    return FrameAnalysis(result=result, frame_path=path, timing=timing,
                         raw_sha256=hashlib.sha256(raw).hexdigest())


class FileRunner:
    """Test/benchmark stand-in for ``subprocess.run`` returning a saved PNG."""

    def __init__(self, image: Path) -> None:
        self.image = Path(image)
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "dumpsys" in command:
            return _Completed(b"topResumedActivity=" + EXPECTED_PACKAGE.encode()
                              + b"/org.cocos2dx.lua.AppActivity")
        if "screencap" in command:
            return _Completed(self.image.read_bytes())
        raise AssertionError(command)


class _Completed:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.returncode = 0


class LiveCapture:
    """Live capture stage for the window: foreground check, then one frame.

    The foreground ``dumpsys`` result is cached for a few seconds so a tight
    polling loop does not pay for it on every frame; a background game still
    stops the loop immediately.
    """

    def __init__(self, adb_path: str = DEFAULT_ADB, serial: str = DEFAULT_SERIAL,
                 *, runner=None, check_every: float = 3.0) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.runner = runner
        self.check_every = float(check_every)
        self.checked_at: float | None = None

    def grab(self, *, now: float | None = None) -> bytes:
        now = time.perf_counter() if now is None else now
        if (self.checked_at is None or now - self.checked_at >= self.check_every
                or self.runner is not None):
            read_foreground(self.adb_path, self.serial, runner=self.runner)
            self.checked_at = now
        return grab_screen(self.adb_path, self.serial, runner=self.runner)

    def reset(self) -> None:
        self.checked_at = None
