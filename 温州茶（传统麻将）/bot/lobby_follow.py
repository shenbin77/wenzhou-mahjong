"""Guarded lobby bridge from device selection to the existing follow engine.

Only the reviewed tea-2p seat and explicitly authorised start workflow may tap
here. It never clicks confirm/recharge, invite, leave, settlement, wash/cut,
or game-center controls.
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
from lobby_start import (
    StartExecution,
    execute_auto_start,
    waiting_room_visual_evidence,
)
from live_capture import capture_frame, read_foreground


@dataclass(frozen=True)
class LobbyProgress:
    state: str
    message: str
    room_cards: int | None
    room_card_status: str
    clicked: bool = False
    seated: bool = False
    started: bool = False
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
    """One whitelisted session-entry tap after a dedicated fresh-frame proof."""
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


def _evidence_writer(session_dir: Path):
    evidence_dir = Path(session_dir) / "lobby_actions"

    def save(stage: str, image: np.ndarray) -> None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        safe_stage = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in stage)
        path = evidence_dir / f"{time.time_ns()}_{safe_stage}.png"
        ok, encoded = cv2.imencode(".png", image)
        if ok:
            encoded.tofile(path)
    return save


def advance(
    serial: str,
    adb_path: str,
    session_dir: Path,
    *,
    allow_seat: bool = True,
    allow_start: bool = False,
    expected_account: str = "",
    room_cards_snapshot: int | None = None,
    runner=None,
    sleep_fn=time.sleep,
) -> LobbyProgress:
    """Observe one scene and, only in a proven lobby, make one guarded seat attempt.

    Sitting down is free and does not use the room-card gate.  Start Game is a
    separate, explicit action: it requires the caller's ``allow_start`` plus
    visual identity/mode/opponent/button proof.  A known zero balance blocks;
    unknown balance is logged but may proceed because the user explicitly
    authorised normal game-start consumption by pressing Start Follow.
    """
    image = _capture_image(serial, adb_path, session_dir)
    assessment = assess_frame(image)
    waiting_visual = waiting_room_visual_evidence(image)
    # The game may auto-open Member List after seating.  It obscures the rule
    # text and makes the generic green-table classifier say in_game, while the
    # exact Start Game button proves this is still the waiting room.
    state = "waiting_room" if waiting_visual["matched"] else assessment.state

    known_cards = (
        assessment.room_cards
        if assessment.room_cards is not None else room_cards_snapshot)
    card_status = (
        assessment.card_status
        if assessment.room_cards is not None else
        ("cached" if room_cards_snapshot is not None else assessment.card_status))
    save_evidence = _evidence_writer(Path(session_dir))

    if state == "in_game":
        return LobbyProgress(
            state, "已进入牌局，转交现有跟随/代打引擎。",
            known_cards, card_status,
            seated=True, started=True,
        )
    if state == "waiting_room":
        if not allow_start:
            return LobbyProgress(
                state, "已在两人等候室；等待开始游戏授权。",
                known_cards, card_status, seated=True)
        if not str(expected_account or "").strip():
            return LobbyProgress(
                state, "已坐下，但玩家昵称为空，不能自动开始游戏。",
                known_cards, card_status, seated=True, terminal=True)

        def capture_again() -> np.ndarray:
            return _capture_image(serial, adb_path, session_dir)

        def tap_point(x: int, y: int) -> None:
            _tap_lobby(serial, adb_path, x, y, runner=runner)

        started: StartExecution = execute_auto_start(
            image,
            expected_account=expected_account,
            room_cards=known_cards,
            capture_image=capture_again,
            tap_point=tap_point,
            sleep_fn=sleep_fn,
            on_frame=save_evidence,
        )
        if started.status == "started":
            return LobbyProgress(
                started.final_state, started.detail, known_cards, card_status,
                clicked=True, seated=True, started=True)
        terminal = started.status in ("blocked", "failed")
        return LobbyProgress(
            started.final_state, started.detail, known_cards, card_status,
            clicked=started.clicked, seated=True, terminal=terminal)
    if state != "lobby":
        return LobbyProgress(
            state, assessment.warning,
            known_cards, card_status,
            terminal=True,
        )
    if not allow_seat:
        return LobbyProgress(
            state, assessment.warning,
            known_cards, card_status,
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
        on_frame=save_evidence,
    )
    if execution.status == "seated":
        final_waiting = (
            execution.final_image is not None
            and waiting_room_visual_evidence(execution.final_image)["matched"])
        if (allow_start and execution.final_image is not None
                and (execution.final_state == "waiting_room" or final_waiting)):
            return advance(
                serial, adb_path, session_dir,
                allow_seat=False, allow_start=True,
                expected_account=expected_account,
                room_cards_snapshot=known_cards,
                runner=runner, sleep_fn=sleep_fn)
        return LobbyProgress(
            execution.final_state,
            execution.detail,
            known_cards,
            card_status,
            clicked=execution.clicked,
            seated=True,
        )
    if execution.clicked:
        # Once a click happened but post-state is uncertain, never retry.
        return LobbyProgress(
            execution.final_state,
            execution.detail,
            known_cards,
            card_status,
            clicked=True,
            terminal=True,
        )
    # No click occurred: table full/no reviewed target can be observed again.
    return LobbyProgress(
        "lobby",
        execution.detail,
        known_cards,
        card_status,
        clicked=False,
        terminal=False,
    )
