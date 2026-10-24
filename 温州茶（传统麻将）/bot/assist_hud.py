# -*- coding: utf-8 -*-
"""Read-only HUD formatters for the live follow window.

Surfaces existing backends (table_state / countdown_vision / analysis status)
as short Chinese labels. Never invents values: unknown → 「未识别」.
Does not enable CLAIM taps and does not call ADB.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "UNKNOWN",
    "FollowHud",
    "format_wall_cards",
    "format_countdown",
    "format_turn_action",
    "format_river_summary",
    "build_follow_hud",
    "load_result_bgr",
]

UNKNOWN = "未识别"


@dataclass(frozen=True)
class FollowHud:
    wall: str
    countdown: str
    turn: str
    river: str

    def as_dict(self) -> dict[str, str]:
        return {
            "wall": self.wall,
            "countdown": self.countdown,
            "turn": self.turn,
            "river": self.river,
        }


def format_wall_cards(wall_cards: Any) -> str:
    if wall_cards is None:
        return f"余牌：{UNKNOWN}"
    try:
        value = int(wall_cards)
    except (TypeError, ValueError):
        return f"余牌：{UNKNOWN}"
    if value < 0:
        return f"余牌：{UNKNOWN}"
    return f"余牌：{value}"


def format_countdown(status: Any, seconds: Any = None) -> str:
    status_s = str(status or "").strip().lower()
    if status_s in {"", "absent", "unreadable", "unknown", "none"}:
        return f"倒计时：{UNKNOWN}"
    sec: int | None
    try:
        sec = None if seconds is None else int(seconds)
    except (TypeError, ValueError):
        sec = None
    if status_s == "expired":
        return "倒计时：0秒"
    if status_s in {"present", "low"} and sec is not None and sec >= 0:
        return f"倒计时：{sec}秒"
    if status_s in {"present", "low"}:
        return f"倒计时：{UNKNOWN}"
    return f"倒计时：{UNKNOWN}"


def format_turn_action(*, status: Any, actionable: bool) -> str:
    """Map analysis status to HUD turn text without inventing seat ownership.

    - play + actionable → 我方可出
    - wait → 等待 (algorithm wait; never 「对方出牌」)
    - anything else / weak → 未识别
    """
    status_s = str(status or "").strip().lower()
    if status_s == "play" and actionable:
        return "当前行动：我方可出"
    if status_s == "wait":
        return "当前行动：等待"
    if status_s == "hu" and actionable:
        return "当前行动：我方可出"
    return f"当前行动：{UNKNOWN}"


def format_river_summary(result: Mapping[str, Any] | None) -> str:
    """Compact 牌河 line from fields already on the analysis result only.

    Does not run discard_vision (avoids extra cost / risk on every UI tick).
    """
    result = dict(result or {})
    incoming = result.get("incoming_tile")
    source = result.get("incoming_source")
    opp = result.get("opponent_river")
    own = result.get("own_river")
    # Also accept nested discard block
    block = result.get("discard") or result.get("discards") or {}
    if isinstance(block, Mapping):
        incoming = incoming if incoming is not None else block.get("incoming_tile")
        source = source or block.get("incoming_source")
        opp = opp if opp is not None else block.get("opponent_river")
        own = own if own is not None else block.get("own_river")

    parts: list[str] = []
    src_map = {"opponent": "对家刚出", "self": "我方刚出", "own": "我方刚出"}
    if source in src_map:
        if incoming is not None:
            parts.append(f"{src_map[source]}:{incoming}")
        else:
            parts.append(f"{src_map[source]}:{UNKNOWN}")

    def _count(value: Any) -> int | None:
        # Empty []/() is not trusted evidence — show 未识别, never 河0/0.
        if isinstance(value, (list, tuple)):
            return len(value) if len(value) > 0 else None
        return None

    opp_n = _count(opp)
    own_n = _count(own)
    if opp_n is not None or own_n is not None:
        parts.append(
            f"河{UNKNOWN if opp_n is None else opp_n}/"
            f"{UNKNOWN if own_n is None else own_n}"
        )

    if not parts:
        return f"牌河：{UNKNOWN}"
    return "牌河：" + " · ".join(parts)


def load_result_bgr(result: Mapping[str, Any] | None):
    """Best-effort BGR frame from analysis result paths (no ADB)."""
    import cv2
    import numpy as np

    result = dict(result or {})
    candidates: list[Any] = []
    for key in ("image_bgr", "frame_bgr", "bgr"):
        value = result.get(key)
        if value is not None:
            candidates.append(value)
    event = result.get("audit_event") or {}
    if isinstance(event, Mapping):
        for key in ("frame", "image"):
            if event.get(key):
                candidates.append(event.get(key))
    for key in ("image", "frame", "frame_path"):
        if result.get(key):
            candidates.append(result.get(key))

    for item in candidates:
        if isinstance(item, np.ndarray) and item.ndim == 3:
            return item
        path = Path(str(item)) if item is not None else None
        if path is None:
            continue
        try:
            if not path.is_file():
                continue
            data = np.fromfile(str(path), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is not None:
                return image
        except OSError:
            continue
    return None


def build_follow_hud(
    result: Mapping[str, Any] | None,
    *,
    image: Any = None,
    status: Any = None,
    actionable: bool = False,
    wall_cards: Any = None,
    run_countdown: bool = True,
) -> FollowHud:
    """Compose HUD labels from analysis + optional live image."""
    result = dict(result or {})
    wall = wall_cards if wall_cards is not None else result.get("wall_cards")

    cd_status = result.get("countdown_status")
    cd_seconds = result.get("countdown_seconds")
    if run_countdown:
        try:
            from table_state import build_table_state

            frame = image
            if frame is None:
                frame = load_result_bgr(result)
            table = build_table_state(result, frame, wall_cards=wall)
            wall = table.wall_cards if wall is None else wall
            cd_status = table.countdown_status
            cd_seconds = table.countdown_seconds
        except Exception:
            # Fail closed to 未识别 labels; never raise into the UI loop.
            if cd_status is None:
                cd_status = "unreadable"

    turn_status = status if status is not None else result.get("status")
    return FollowHud(
        wall=format_wall_cards(wall),
        countdown=format_countdown(cd_status, cd_seconds),
        turn=format_turn_action(status=turn_status, actionable=bool(actionable)),
        river=format_river_summary(result),
    )
