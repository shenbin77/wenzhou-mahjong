"""Guarded lobby-to-waiting-room seating for the tea two-player table.

Only two clicks exist in this workflow: a visually detected empty-seat ``+``
and the reviewed rule dialog's top-right close button.  It never clicks start,
confirm, recharge, invite, leave, settlement, or a nominal fixed coordinate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Callable

import cv2
import numpy as np

from lobby_autostart import read_lobby_room
from session_watch import WatchState, classify_watch


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "lobby"
RULES_TEMPLATE = TEMPLATE_DIR / "tea2_rules_modal.png"
RULES_ROI = (463, 230, 1139, 665)
RULES_MIN_SCORE = 0.98
TARGET_MAX_DRIFT = 12.0


@dataclass(frozen=True)
class SeatTarget:
    box: tuple[int, int, int, int]
    point: tuple[int, int]


@dataclass(frozen=True)
class SeatPlan:
    status: str
    detail: str
    table_score: float
    candidates: tuple[SeatTarget, ...] = ()
    target: SeatTarget | None = None


@dataclass(frozen=True)
class SeatExecution:
    status: str
    detail: str
    clicked: bool
    final_state: str
    final_image: np.ndarray | None = None


def _load_gray(path: Path) -> np.ndarray | None:
    raw = np.fromfile(path, dtype=np.uint8) if path.is_file() else np.array([])
    return cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE) if raw.size else None


def _roi_score(image: np.ndarray, roi: tuple[int, int, int, int],
               template_path: Path) -> float:
    x0, y0, x1, y1 = roi
    template = _load_gray(template_path)
    if template is None:
        return 0.0
    crop = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if crop.shape != template.shape:
        return 0.0
    return float(cv2.matchTemplate(
        crop, template, cv2.TM_CCOEFF_NORMED)[0, 0])


def _seat_index(serial: str, count: int) -> int:
    suffix = str(serial or "").rsplit("-", 1)[-1]
    if suffix.isdigit():
        return (int(suffix) // 2) % count
    digest = hashlib.sha256(str(serial).encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % count


def plan_auto_seat(image: np.ndarray | None, serial: str,
                   mode: str = "tea_2p") -> SeatPlan:
    if mode != "tea_2p":
        return SeatPlan("blocked", "自动坐下目前只开放温州茶2人", 0.0)
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        return SeatPlan("blocked", "大厅画面不是已验证的1600×900原生帧", 0.0)
    # Keep a single source of truth for the lobby layout.  The old executor had
    # its own stricter table-template gate (0.98) and could therefore reject a
    # live one-seated table that the audited lobby recognizer had already
    # proven safe (measured 0.966).  Execution now consumes the exact same
    # RoomState evidence used by the read-only plan and still performs its own
    # fresh-frame target recheck immediately before tapping.
    room = read_lobby_room(image)
    # ``read_lobby_room`` sorts diamonds by x and only marks a room present
    # when the first diamond is lattice unit 0.  Report that target table's
    # evidence, not a potentially higher score from a later empty table.
    diamonds = room.evidence.get("diamonds", [])
    first_diamond = diamonds[0] if diamonds and isinstance(diamonds[0], dict) else {}
    score = float(first_diamond.get("score", 0.0))
    if not room.present:
        return SeatPlan("blocked", f"目标茶2桌证据不足：{room.reason}", score)
    first_unit = [seat for seat in room.seats if seat.unit == 0]
    targets = tuple(sorted(
        (SeatTarget(seat.box, seat.center) for seat in first_unit),
        key=lambda item: (item.point[1], item.point[0]),
    ))
    if not targets:
        return SeatPlan("blocked", "最左桌没有可复核的空座“+”", score)
    if len(targets) > 2:
        return SeatPlan("blocked", f"最左桌空座候选异常：{len(targets)}个", score, targets)
    target = targets[_seat_index(serial, len(targets))]
    return SeatPlan(
        "ready", f"{room.reason}；最左桌发现{len(targets)}个可复核空座", score,
        targets, target,
    )


def detect_target_rules_modal(image: np.ndarray | None) -> tuple[bool, float, tuple[int, int] | None]:
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        return False, 0.0, None
    score = _roi_score(image, RULES_ROI, RULES_TEMPLATE)
    if score < RULES_MIN_SCORE:
        return False, score, None
    x0, y0, x1, _y1 = RULES_ROI
    # Reviewed X centre relative to the detected, exact target-rules panel.
    return True, score, (x1 - 13, y0 + 24)


def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def execute_auto_seat(
    initial_image: np.ndarray,
    *,
    serial: str,
    capture_image: Callable[[], np.ndarray],
    tap_point: Callable[[int, int], None],
    mode: str = "tea_2p",
    sleep_fn: Callable[[float], None] = time.sleep,
    on_frame: Callable[[str, np.ndarray], None] | None = None,
) -> SeatExecution:
    """Execute one seat attempt with fresh-frame and post-state verification."""
    initial = plan_auto_seat(initial_image, serial, mode)
    if initial.status != "ready" or initial.target is None:
        return SeatExecution("blocked", initial.detail, False, "lobby", initial_image)
    if on_frame:
        on_frame("before", initial_image)

    fresh = capture_image()
    fresh_plan = plan_auto_seat(fresh, serial, mode)
    if fresh_plan.status != "ready" or fresh_plan.target is None:
        return SeatExecution("blocked", f"fresh帧复核失败：{fresh_plan.detail}", False,
                             "lobby", fresh)
    if _distance(initial.target.point, fresh_plan.target.point) > TARGET_MAX_DRIFT:
        return SeatExecution("blocked", "空座目标在fresh帧发生漂移，拒绝点击", False,
                             "lobby", fresh)
    tap_point(*fresh_plan.target.point)
    clicked = True
    # Real devices can spend >0.5s between the seat tap, rules modal and the
    # waiting/in-game scene. Observe several fresh frames before declaring a
    # failed click; never add another seat tap during this grace window.
    after_seat = fresh
    watch = None
    matched = False
    score = 0.0
    close_point = None
    for index in range(8):
        sleep_fn(0.25 if index else 0.45)
        after_seat = capture_image()
        if on_frame:
            on_frame(f"after_seat_{index + 1}", after_seat)
        watch = classify_watch(after_seat)
        if watch.state in (WatchState.WAITING_ROOM, WatchState.IN_GAME):
            return SeatExecution(
                "seated",
                "空座点击后已进入等候室/牌局",
                clicked,
                watch.state.value,
                after_seat,
            )
        matched, score, close_point = detect_target_rules_modal(after_seat)
        if matched and close_point is not None:
            break
        if watch.state not in (WatchState.LOBBY, WatchState.UNKNOWN):
            return SeatExecution(
                "failed",
                f"坐下后进入未授权场景（{watch.state.value}），不再点击",
                clicked,
                watch.state.value,
                after_seat,
            )
    if not matched or close_point is None:
        final_state = watch.state.value if watch is not None else "unknown"
        return SeatExecution(
            "failed",
            f"坐下后多帧仍未确认等候室/牌局/目标规则弹窗（{score:.3f}），不再点击",
            clicked,
            final_state,
            after_seat,
        )

    # Modal close is a second, independently verified action.
    sleep_fn(0.15)
    fresh_modal = capture_image()
    fresh_match, fresh_score, fresh_close = detect_target_rules_modal(fresh_modal)
    if not fresh_match or fresh_close is None \
            or _distance(close_point, fresh_close) > TARGET_MAX_DRIFT:
        return SeatExecution(
            "failed", f"规则弹窗fresh帧复核失败（{fresh_score:.3f}）",
            clicked, "unknown", fresh_modal,
        )
    tap_point(*fresh_close)
    if on_frame:
        on_frame("rules_modal", fresh_modal)

    final = fresh_modal
    final_state = "unknown"
    for _ in range(8):
        sleep_fn(0.4)
        final = capture_image()
        watch = classify_watch(final)
        final_state = watch.state.value
        if watch.state in (WatchState.WAITING_ROOM, WatchState.IN_GAME):
            if on_frame:
                on_frame("seated", final)
            return SeatExecution(
                "seated", "已自动坐下并关闭目标规则说明；等待开始游戏门禁",
                True, final_state, final,
            )
        if watch.state not in (WatchState.LOBBY, WatchState.UNKNOWN):
            break
    return SeatExecution(
        "failed", f"关闭规则说明后未确认进入等候室（{final_state}），不再点击",
        True, final_state, final,
    )
