"""Guarded lobby bridge from device selection to the existing follow engine.

Only the reviewed tea-2p seat workflow may tap here. It never clicks start,
confirm/recharge, invite, leave, settlement, or game-center controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

import cv2
import numpy as np

from lobby_preflight import LobbyAssessment, assess_frame
from lobby_seat import SeatExecution, execute_auto_seat
from live_capture import capture_frame, read_foreground


@dataclass(frozen=True)
class LobbyProgress:
    state: str
    message: str
    room_cards: int | None
    room_card_status: str
    clicked: bool = False
    seated: bool = False
    terminal: bool = False


def _capture_image(serial: str, adb_path: str, session_dir: Path) -> np.ndarray:
    raw, _ = capture_frame(
        mode="tea_2p", adb_path=adb_path, serial=serial,
        session_dir=Path(session_dir))
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("ADB截图无法解码，不能进入自动场次")
    return image


def probe(serial: str, adb_path: str, session_dir: Path) -> LobbyAssessment:
    """Read-only scene/room-card observation."""
    return assess_frame(_capture_image(serial, adb_path, session_dir))


def _tap_lobby(serial: str, adb_path: str, x: int, y: int, *, runner=None) -> None:
    """One whitelisted lobby tap after execute_auto_seat fresh-frame proof."""
    if not (0 <= int(x) < 1600 and 0 <= int(y) < 900):
        raise ValueError("大厅点击坐标超出1600×900原生画面")
    if not serial or any(ch.isspace() for ch in serial):
        raise ValueError("模拟器序列号无效")
    runner = runner or subprocess.run
    # Re-check foreground immediately before every seat/modal-close click.
    read_foreground(adb_path, serial, runner=runner)
    runner(
        [str(adb_path), "-s", serial, "shell", "input", "tap",
         str(int(x)), str(int(y))],
        capture_output=True, timeout=12, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def advance(
    serial: str,
    adb_path: str,
    session_dir: Path,
    *,
    allow_seat: bool = True,
    runner=None,
    sleep_fn=time.sleep,
) -> LobbyProgress:
    """Observe one scene and, only in a proven lobby, make one guarded seat attempt.

    Room-card OCR is reported here but is intentionally not a seat gate:
    sitting down does not start a paid round. The card gate belongs to the
    future "start game" action, which this module never clicks.
    """
    image = _capture_image(serial, adb_path, session_dir)
    assessment = assess_frame(image)
    state = assessment.state

    if state == "in_game":
        return LobbyProgress(
            state, "已进入牌局，转交现有跟随/代打引擎。",
            assessment.room_cards, assessment.card_status,
            seated=True,
        )
    if state == "waiting_room":
        return LobbyProgress(
            state, "已在两人等候室；自动开始游戏尚未启用。",
            assessment.room_cards, assessment.card_status,
            seated=True,
        )
    if state != "lobby":
        return LobbyProgress(
            state, assessment.warning,
            assessment.room_cards, assessment.card_status,
            terminal=True,
        )
    if not allow_seat:
        return LobbyProgress(
            state, assessment.warning,
            assessment.room_cards, assessment.card_status,
        )

    def capture_again() -> np.ndarray:
        return _capture_image(serial, adb_path, session_dir)

    def tap_point(x: int, y: int) -> None:
        _tap_lobby(serial, adb_path, x, y, runner=runner)

    execution: SeatExecution = execute_auto_seat(
        image,
        serial=serial,
        capture_image=capture_again,
        tap_point=tap_point,
        mode="tea_2p",
        sleep_fn=sleep_fn,
    )
    if execution.status == "seated":
        return LobbyProgress(
            execution.final_state,
            execution.detail,
            assessment.room_cards,
            assessment.card_status,
            clicked=execution.clicked,
            seated=True,
        )
    if execution.clicked:
        # Once a click happened but post-state is uncertain, never retry.
        return LobbyProgress(
            execution.final_state,
            execution.detail,
            assessment.room_cards,
            assessment.card_status,
            clicked=True,
            terminal=True,
        )
    # No click occurred: table full/no reviewed target can be observed again.
    return LobbyProgress(
        "lobby",
        execution.detail,
        assessment.room_cards,
        assessment.card_status,
        clicked=False,
        terminal=False,
    )
