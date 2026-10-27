"""Guarded waiting-room start for an explicitly authorised tea-2p session.

This is intentionally separate from the in-game :mod:`tap_executor`.  The
only coordinate it can emit is the centre of the reviewed orange
``开始游戏`` button, after mode, local identity, opponent occupancy and a
fresh-frame visual proof all agree.  It never clicks recharge or any dialog.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile
import time
from typing import Callable, Iterator

import cv2
import numpy as np

from player_identity import PlayerIdentity, detect_player_identity
from session_watch import WatchState, classify_watch, detect_waiting_room
from tile_vision import TileVision


TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "lobby" /
    "tea2_start_button.png"
)
MEMBER_OVERLAY_TEMPLATE = (
    Path(__file__).resolve().parent / "templates" / "lobby" /
    "tea2_member_overlay.png"
)
START_MIN_SCORE = 0.98
OVERLAY_MIN_SCORE = 0.98
START_MAX_DRIFT = 10.0
START_BUTTON_BOX = (663, 525, 937, 625)
MEMBER_OVERLAY_ROI = (0, 0, 485, 120)
MEMBER_OVERLAY_CLOSE_POINT = (520, 480)
OPPONENT_ROI = (1160, 0, 1380, 140)
OPPONENT_AVATAR_ROI = (1165, 18, 1265, 115)
LOCK_PATH = Path(tempfile.gettempdir()) / "wenzhou_tea2_start.lock"


@dataclass(frozen=True)
class StartPlan:
    status: str
    detail: str
    point: tuple[int, int] | None = None
    box: tuple[int, int, int, int] | None = None
    button_score: float = 0.0
    identity: PlayerIdentity | None = None


@dataclass(frozen=True)
class StartExecution:
    status: str
    detail: str
    clicked: bool
    final_state: str
    final_image: np.ndarray | None = None


@dataclass(frozen=True)
class OverlayExecution:
    status: str
    detail: str
    clicked: bool
    final_image: np.ndarray


def _load_gray(path: Path) -> np.ndarray | None:
    raw = np.fromfile(path, dtype=np.uint8) if path.is_file() else np.array([])
    return cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE) if raw.size else None


def _button_score(image: np.ndarray, box: tuple[int, int, int, int]) -> float:
    template = _load_gray(TEMPLATE_PATH)
    if template is None:
        return 0.0
    x0, y0, x1, y1 = box
    crop = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if crop.shape != template.shape:
        return 0.0
    return float(cv2.matchTemplate(
        crop, template, cv2.TM_CCOEFF_NORMED)[0, 0])


def _fixed_roi_score(
    image: np.ndarray | None,
    roi: tuple[int, int, int, int],
    template_path: Path,
) -> float:
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        return 0.0
    template = _load_gray(template_path)
    if template is None:
        return 0.0
    x0, y0, x1, y1 = roi
    crop = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if crop.shape != template.shape:
        return 0.0
    return float(cv2.matchTemplate(
        crop, template, cv2.TM_CCOEFF_NORMED)[0, 0])


def waiting_room_visual_evidence(image: np.ndarray | None) -> dict:
    """Recognise the reviewed waiting room even if Member List hides rules."""
    button_score = _fixed_roi_score(image, START_BUTTON_BOX, TEMPLATE_PATH)
    overlay_score = _fixed_roi_score(
        image, MEMBER_OVERLAY_ROI, MEMBER_OVERLAY_TEMPLATE)
    return {
        "matched": button_score >= START_MIN_SCORE,
        "button_score": round(button_score, 4),
        "member_overlay": overlay_score >= OVERLAY_MIN_SCORE,
        "overlay_score": round(overlay_score, 4),
    }


def close_member_overlay(
    image: np.ndarray,
    *,
    capture_image: Callable[[], np.ndarray],
    tap_point: Callable[[int, int], None],
    sleep_fn: Callable[[float], None] = time.sleep,
    on_frame: Callable[[str, np.ndarray], None] | None = None,
) -> OverlayExecution:
    """Toggle the exact reviewed Member List closed; never touch invite rows."""
    evidence = waiting_room_visual_evidence(image)
    if not evidence["member_overlay"]:
        return OverlayExecution("clear", "成员列表未展开", False, image)
    fresh = capture_image()
    fresh_evidence = waiting_room_visual_evidence(fresh)
    if not (fresh_evidence["member_overlay"] and fresh_evidence["matched"]):
        return OverlayExecution(
            "blocked", "成员列表fresh帧或等待室按钮复核失败", False, fresh)
    if on_frame:
        on_frame("member_overlay_before", fresh)
    tap_point(*MEMBER_OVERLAY_CLOSE_POINT)
    last = fresh
    for _ in range(6):
        sleep_fn(0.3)
        last = capture_image()
        after = waiting_room_visual_evidence(last)
        if not after["member_overlay"] and after["matched"]:
            if on_frame:
                on_frame("member_overlay_closed", last)
            return OverlayExecution(
                "closed", "已收起成员列表；未点击任何邀请按钮", True, last)
    if on_frame:
        on_frame("member_overlay_unverified", last)
    return OverlayExecution(
        "failed", "点击成员页签后未确认面板收起，不再点击", True, last)


def opponent_seat_present(image: np.ndarray) -> tuple[bool, dict]:
    """Require both a nickname-like gold mask and non-background avatar."""
    x0, y0, x1, y1 = OPPONENT_ROI
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 44)
        & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 115)
    ).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    text_pixels = 0
    text_components = 0
    for index in range(1, count):
        _x, _y, _w, height, area = (int(v) for v in stats[index])
        if area >= 5 and height >= 3:
            text_pixels += int((labels == index).sum())
            text_components += 1

    ax0, ay0, ax1, ay1 = OPPONENT_AVATAR_ROI
    avatar = image[ay0:ay1, ax0:ax1]
    avatar_std = float(cv2.cvtColor(avatar, cv2.COLOR_BGR2GRAY).std())
    evidence = {
        "gold_text_pixels": text_pixels,
        "gold_components": text_components,
        "avatar_std": round(avatar_std, 2),
    }
    matched = text_pixels >= 80 and text_components >= 2 and avatar_std >= 22.0
    return matched, evidence


def game_started_evidence(image: np.ndarray | None) -> tuple[bool, dict]:
    """Require an actual dealt tea-2p hand, not the waiting-room ready animation.

    The generic green-table classifier also labels the non-host ready animation
    as ``in_game``.  A real start additionally has the reviewed Start button
    gone and a complete 16/17-tile concealed hand on the native frame.
    """
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        return False, {"reason": "not_native_frame"}
    watch = classify_watch(image)
    waiting = waiting_room_visual_evidence(image)
    try:
        vision = TileVision()
        concealed_count, _concealed_boxes = vision.count_tiles(image)
        exposed_count = len(vision.last_meld_boxes or ())
        physical_count = int(concealed_count) + int(exposed_count)
    except Exception as exc:  # configuration/vision faults remain visible
        return False, {
            "watch": watch.state.value,
            "start_button": waiting["matched"],
            "hand_count": 0,
            "reason": f"hand_probe_failed:{exc}",
        }
    evidence = {
        "watch": watch.state.value,
        "start_button": waiting["matched"],
        # Keep ``hand_count`` for existing ledgers while making its meaning
        # explicit.  A mid-hand restart may already have melds, and a dimmed
        # local hand may have valid physical boxes even when strict tile-label
        # classification deliberately abstains.  The lobby bridge only needs
        # proof that dealing happened; the normal follow/auto gates still
        # re-read every tile before making a decision.
        "hand_count": physical_count,
        "concealed_count": int(concealed_count),
        "exposed_box_count": int(exposed_count),
    }
    matched = (
        watch.state == WatchState.IN_GAME
        and not waiting["matched"]
        and physical_count in (16, 17)
        and exposed_count % 3 == 0
    )
    evidence["reason"] = "dealt_hand" if matched else "not_dealt"
    return matched, evidence


def plan_auto_start(
    image: np.ndarray | None,
    *,
    expected_account: str,
    room_cards: int | None,
) -> StartPlan:
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        return StartPlan("blocked", "等候室画面不是已验证的1600×900原生帧")
    waiting = detect_waiting_room(image)
    if not waiting.get("matched"):
        return StartPlan(
            "wait", f"尚未确认温州茶2人开始按钮（{waiting.get('status')}）")

    identity = detect_player_identity(image, expected_account)
    if identity.status == "mismatch":
        return StartPlan("blocked", identity.detail, identity=identity)
    if not identity.matched:
        return StartPlan("wait", identity.detail, identity=identity)

    occupied, occupant_evidence = opponent_seat_present(image)
    if not occupied:
        return StartPlan(
            "wait", "已坐下，等待另一名玩家入座"
            f"（对手位证据：{occupant_evidence}）", identity=identity)
    if room_cards == 0:
        return StartPlan(
            "blocked", "房卡已明确为0，禁止开始游戏且不会自动充值",
            identity=identity)
    if isinstance(room_cards, bool) or (
            room_cards is not None and room_cards < 0):
        return StartPlan("blocked", "房卡证据无效，拒绝开始游戏", identity=identity)

    raw_box = waiting.get("start_button_box")
    if not (isinstance(raw_box, list) and len(raw_box) == 4):
        return StartPlan("wait", "开始游戏按钮几何证据缺失", identity=identity)
    box = tuple(int(v) for v in raw_box)
    score = _button_score(image, box)
    if score < START_MIN_SCORE:
        return StartPlan(
            "wait", f"开始游戏按钮模板证据不足（{score:.3f}）",
            box=box, button_score=score, identity=identity)
    x0, y0, x1, y1 = box
    point = ((x0 + x1) // 2, (y0 + y1) // 2)
    card_note = (
        f"房卡余额{room_cards}张" if room_cards is not None
        else "房卡读数未知，按本次“开始跟随”授权正常开局；绝不充值"
    )
    return StartPlan(
        "ready", f"本人/玩法/对手/开始按钮均已核验；{card_note}",
        point=point, box=box, button_score=score, identity=identity)


def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


@contextmanager
def _exclusive_start_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
    """Short cross-process lock so two local emulators do not click together."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except (ImportError, OSError):
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
        handle.close()


def execute_auto_start(
    initial_image: np.ndarray,
    *,
    expected_account: str,
    room_cards: int | None,
    capture_image: Callable[[], np.ndarray],
    tap_point: Callable[[int, int], None],
    sleep_fn: Callable[[float], None] = time.sleep,
    on_frame: Callable[[str, np.ndarray], None] | None = None,
    lock_path: Path = LOCK_PATH,
) -> StartExecution:
    """Click Start Game once, with fresh proof and in-game post-verification."""
    overlay = close_member_overlay(
        initial_image,
        capture_image=capture_image,
        tap_point=tap_point,
        sleep_fn=sleep_fn,
        on_frame=on_frame,
    )
    if overlay.status in ("blocked", "failed"):
        return StartExecution(
            overlay.status, overlay.detail, overlay.clicked,
            "waiting_room", overlay.final_image)
    maintenance_clicked = overlay.clicked
    initial_image = overlay.final_image
    initial = plan_auto_start(
        initial_image, expected_account=expected_account,
        room_cards=room_cards)
    if initial.status != "ready" or initial.point is None:
        return StartExecution(
            initial.status, initial.detail, maintenance_clicked,
            "waiting_room", initial_image)
    if on_frame:
        on_frame("before_start", initial_image)

    with _exclusive_start_lock(lock_path) as acquired:
        if not acquired:
            return StartExecution(
                "busy", "另一实例正在复核开始游戏；本实例等待下一帧",
                maintenance_clicked, "waiting_room", initial_image)
        fresh = capture_image()
        fresh_plan = plan_auto_start(
            fresh, expected_account=expected_account, room_cards=room_cards)
        if fresh_plan.status != "ready" or fresh_plan.point is None:
            return StartExecution(
                fresh_plan.status, f"开始前fresh帧复核失败：{fresh_plan.detail}",
                False, "waiting_room", fresh)
        if _distance(initial.point, fresh_plan.point) > START_MAX_DRIFT:
            return StartExecution(
                "blocked", "开始按钮在fresh帧发生漂移，拒绝点击",
                False, "waiting_room", fresh)
        tap_point(*fresh_plan.point)
        if on_frame:
            on_frame("start_tap", fresh)

        last = fresh
        last_state = "waiting_room"
        last_start_evidence: dict = {}
        for _ in range(10):
            sleep_fn(0.4)
            last = capture_image()
            watch = classify_watch(last)
            last_state = watch.state.value
            started, last_start_evidence = game_started_evidence(last)
            if started:
                if on_frame:
                    on_frame("game_started", last)
                return StartExecution(
                    "started", "已自动开始游戏并确认真实发牌",
                    True, last_state, last)
            if watch.state in (
                    WatchState.LOGIN_SCREEN, WatchState.LOBBY,
                    WatchState.SETTLEMENT):
                break
        if on_frame:
            on_frame("start_unverified", last)
        if (last_state == WatchState.IN_GAME.value
                and not last_start_evidence.get("start_button")
                and not last_start_evidence.get("hand_count")):
            return StartExecution(
                "ready_wait", "已完成入座准备，等待房主开始并发牌",
                True, "waiting_room", last)
        return StartExecution(
            "failed", f"点击开始游戏后未确认进入牌局（{last_state}），不再点击",
            True, last_state, last)
