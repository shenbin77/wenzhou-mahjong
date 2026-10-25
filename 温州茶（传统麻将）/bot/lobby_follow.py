"""Read-only bridge from the game lobby to the existing follow engine.

A lobby state is NOT sufficient to press seat or start. That is a separate
positive-evidence executor, intentionally not bypassed by this observer.
"""
from __future__ import annotations

from pathlib import Path
import cv2
import numpy as np

from lobby_preflight import LobbyAssessment, assess_frame
from live_capture import capture_frame


def probe(serial: str, adb_path: str, session_dir: Path) -> LobbyAssessment:
    raw, _ = capture_frame(
        mode="tea_2p", adb_path=adb_path, serial=serial,
        session_dir=Path(session_dir))
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("ADB截图无法解码，不能进入自动场次")
    return assess_frame(img)
