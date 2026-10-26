"""Single front end for recommendation follow and explicitly armed autoplay.

``开始跟随`` seats and starts a game (it does tap, and it spends 房卡), then
falls back to read-only observation/recommendation.  ``开启全自动代打`` is
the separate hand-off to the guarded ``AutoPlayLoop`` and is currently limited
to the development target (tea 2-player; full-match acceptance is pending).
Both paths keep immutable
evidence.  A dedicated lobby executor may click the reviewed tea-2p seat and
Start Game controls after the user presses Start Follow/Full Auto; the generic
in-game executor still rejects payment, leave-table and settlement UI.
"""

from __future__ import annotations

import json
import hashlib
import argparse
import os
import re
import time
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from tkinter import filedialog, simpledialog, ttk

from PIL import Image, ImageTk

import cv2
import numpy as np

from assist_shu import analyze_shu_manual_hand
from assist_tea import analyze_manual_hand
from caishen_vision import detect_caishen
from harvest_tea import DEFAULT_SERIAL
from live_capture import (LiveCapture, capture_result,
                          session_folder, save_frame)
from live_state import (BASE_INTERVAL_MS, FrameTiming, LatencyTracker,
                        LiveRefreshState, describe_age)
from recognition_audit import (append_frame_event, build_frame_event,
                               read_appended_events)
from recognition_review import FLAG_NAMES, draw_recognition_boxes
from recorder import AssistRecorder
from tea_bot import tile_name, vision_to_engine
from mode_vision import detect_shu_rule_text, score_tea_player_count
from tile_vision import TARGET_IDS, TILE_IDS
from tile_render import render_advice, render_candidates, render_hand
from instance_profile import profile_paths, read_binding_for_serial, safe_serial
from device_connection import DeviceConnection, verify_device
from player_identity import PlayerIdentity, detect_player_identity


TEA_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = Path(__file__).resolve().parent / "assets" / "温州麻将辅助器.ico"
LAST_ACCOUNT_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".last_account"
WINDOW_STATE_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".window_state.json"
BINDING_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".binding.json"
DEFAULT_SIZE = (1180, 860)
SIZE_RANGE = {"w": (900, 1600), "h": (760, 1200)}
FLOATING_BAR_SIZE = (132, 36)
FLOATING_BAR_MARGIN = 8
#: Size of the floating bar's explicit quit control (a small "×" top-right).
#: It intentionally stays smaller than :data:`FLOATING_BAR_SIZE` so it never
#: covers the status text or the "click anywhere to restore" area.
FLOATING_BAR_CLOSE_SIZE = (12, 12)
#: Outcome identifiers of :meth:`TeaAssistWindow.window_close_action`.
#: The main window's close box (X) is bound to the *collapse* action only;
#: quitting is an explicit, separately reachable action.
CLOSE_ACTION_COLLAPSE = "collapse"
CLOSE_ACTION_QUIT = "quit"
#: Action ids of the floating bar's right-click menu.
FLOATING_BAR_MENU_RESTORE = "restore"
FLOATING_BAR_MENU_QUIT = "quit"
#: Floating-bar context menu entries as ``(label, action id)``, in display
#: order.  Data-driven so the menu can be verified without a Tk instance.
FLOATING_BAR_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("恢复窗口", FLOATING_BAR_MENU_RESTORE),
    ("退出程序", FLOATING_BAR_MENU_QUIT),
)
#: Label of the explicit quit entry appended to the main window's 更多 menu.
QUIT_MENU_LABEL = "退出程序"
_WINDOW_GEOMETRY_PATTERN = re.compile(
    r"^\s*(?P<width>\d+)x(?P<height>\d+)"
    r"(?P<x>[+-]\d+)(?P<y>[+-]\d+)\s*$")
STALE_SECONDS = 3.0
OFFLINE_SOURCES = {"offline_preflight", "imported_image", "mp4_1fps", "manual_correction"}
OFFLINE_NOTICES = {
    "offline_preflight": "旧截图结果：不能用于当前对局，仅供检查识牌链路。",
    "imported_image": "导入截图结果：不能用于当前对局。",
    "mp4_1fps": "录像抽取帧：不能用于当前对局。",
    "manual_correction": "修正重算基于旧画面：不能用于当前对局。",
}
OFFLINE_FRESH = {
    "offline_preflight": "离线预检 · 旧截图（非实时）",
    "imported_image": "导入截图（非实时）",
    "mp4_1fps": "录像抽取帧（非实时）",
    "manual_correction": "修正重算（非实时）",
}
TILE_CHOICES = {
    f"{tile_name(vision_to_engine(tid))} ({label})": label
    for label, tid in TILE_IDS.items() if tid in TARGET_IDS
}


def stacked_kong_summary(result: dict) -> str:
    """Describe proven visible stacked kongs without treating them as advice."""
    names = []
    for item in result.get("stacked_kongs") or []:
        tile_id = item.get("tile_id")
        if item.get("logical_count") != 4 or tile_id not in TARGET_IDS:
            continue
        names.append(f"{tile_name(vision_to_engine(tile_id))}杠（4张）")
    return "、".join(names)
MODES = {"温州茶 · 2人": "tea_2p", "温州茶 · 4人": "tea_4p", "温州熟 · 4人": "shu_4p"}
PROJECT_ROOT = TEA_ROOT.parent
PREFLIGHT_FRAMES = {
    "tea_2p": ("温州茶（传统麻将）/采集/2026-10-16/740195/r08/frame_02.png",
               "83feb4992e6f7eae6b8d6ed7c7d35d1c724c38857ebd58810e826b004d1bd37e"),
    "tea_4p": ("温州茶（传统麻将）/采集/2026-10-13/323028/r01/frame_02.png",
               "f2d834952bdf4609fe96d97834337c2858d29cfb420988510e6d908377902f2f"),
    "shu_4p": ("阿法算牌分析/熟识牌对照_20261018/frame_233s_14.png",
               "48b41ee12e954600bb578ac6b69e7c7cf3913898e5cc2b07e8961ac32e72d3ab"),
}


def frame_result_source(result: dict) -> str:
    event = result.get("audit_event") or {}
    return str(event.get("source") or result.get("source") or "")


def append_user_flag(session_dir: str | Path, event: dict) -> Path:
    """Append one user round flag to ``<session_dir>/user_flags.jsonl``.

    The flag is the user's only in-session correction signal: a single
    click meaning "this round looked wrong".  Finding the actual problem
    is the auto review's job (offline, after the session), never a
    per-hand form the user has to fill in while playing.
    """
    target = Path(session_dir) / "user_flags.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


def format_review_summary(result: dict) -> tuple[str, str]:
    """Render an auto-review outcome for the problem strip (text, level)."""
    count = int(result.get("issue_count") or 0)
    report = str(result.get("report_md") or "")
    suffix = f" · 报告：{report}" if report else ""
    if count <= 0:
        return f"自动复盘：未发现疑似问题{suffix}", "ok"
    by_category = result.get("by_category") or {}
    parts = [
        f"{name}×{int(n)}"
        for name, n in sorted(
            by_category.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:3]
    ]
    summary = "、".join(parts) if parts else "未分类"
    return f"自动复盘：疑似问题 {count} 个（{summary}）{suffix}", "wait"


def format_match_summary(round_no=None, round_total=None, score=None) -> str:
    """Compact, honest match progress; never invent an unread score."""
    current = str(round_no) if isinstance(round_no, int) and round_no > 0 else "—"
    total = str(round_total) if isinstance(round_total, int) and round_total > 0 else "—"
    score_text = f"{score:+d}" if isinstance(score, int) else "待结算识别"
    return f"本场进度：{current}/{total} · 累计积分：{score_text}"


def tea_round_fingerprint(image_path: str | Path | None) -> bytes | None:
    """Identify the visible room/round HUD; unknown screens cannot lock a session."""
    if not image_path or not Path(image_path).is_file():
        return None
    with Image.open(image_path) as image:
        if image.size != (1600, 900):
            return None
        # The live "买底请神顶" avatar can cover the room number and round.
        # Its red crown moves between frames, so a pixel fingerprint would
        # falsely report a new round and discard the user's caishen choice.
        # Treat an occluded HUD as unknown until its text is visible again.
        avatar = image.convert("RGB").crop((35, 95, 115, 140)).tobytes()
        red_pixels = sum(
            avatar[i] > 90
            and avatar[i] > avatar[i + 1] * 1.4
            and avatar[i] > avatar[i + 2] * 1.4
            for i in range(0, len(avatar), 3)
        )
        if red_pixels > 100:
            return None
        hud = image.convert("L").crop((15, 55, 225, 140))
        compact = bytes(pixel // 8 for pixel in hud.tobytes())
    return compact


def same_tea_round(first: bytes, second: bytes) -> bool:
    if len(first) != len(second):
        return False
    return sum(a != b for a, b in zip(first, second)) <= 8


def tea_round_caishen_evidence(image_path: str | Path | None) -> tuple[str | None, float]:
    """读取桌面财神小牌，作为独立的"局身份"证据。

    房号/局数 HUD 会被"买底请神顶"的红色视频头像长期遮挡，像素指纹可能
    整局不可用。桌面财神小牌 (622,348,677,423) 不受头像影响，且是游戏对
    "本局财神"的一等声明：它的识别分数沿用 caishen_vision 的 0.98/0.02
    阈值，label 非 None 即代表该帧通过了完整阈值检验。

    返回 (label, score)；画面不是已验证的 1600×900 牌桌截图或牌面不可读
    时 label 为 None。本函数只读，不产生任何建议。
    """
    if not image_path or not Path(image_path).is_file():
        return None, 0.0
    data = Path(image_path).read_bytes()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape != (900, 1600, 3):
        return None, 0.0
    read = detect_caishen(image)
    return read.label, float(read.score)


def load_window_state(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else WINDOW_STATE_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_window_state(state: dict, path: Path | None = None) -> None:
    target = Path(path) if path is not None else WINDOW_STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass



def load_binding(path: Path | None = None) -> dict | None:
    """Read the persisted local binding (account/device/mode), if any.

    The binding is the single persisted identity snapshot for the main
    window: account tag, emulator serial and selected mode, written only
    by :func:`save_binding` from the top "绑定" button.
    """
    target = Path(path) if path is not None else BINDING_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not str(data.get("account") or "").strip():
        return None
    return data


def save_binding(binding: dict, path: Path | None = None) -> bool:
    """Persist the binding snapshot; returns False when the write fails."""
    target = Path(path) if path is not None else BINDING_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


def clear_binding(path: Path | None = None) -> None:
    target = Path(path) if path is not None else BINDING_PATH
    try:
        target.unlink()
    except OSError:
        pass


def detect_table_mode(image) -> tuple[str | None, str]:
    """Read the current table's mode off one frame (existing recognizers).

    Returns ``(mode_key, detail)``; ``mode_key`` is None when the rule text
    cannot be established.  This is one fresh read for the pre-start gate;
    in-game continuity keeps using the sticky rule latches as before.
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3 \
            or image.shape[:2] != (900, 1600):
        return None, "画面不是已验证的 1600×900 牌桌截图"
    try:
        if detect_shu_rule_text(image):
            return "shu_4p", "识别到温州熟 · 4人规则文字"
    except Exception:
        pass
    players, score, margin = score_tea_player_count(image)
    if players == 2:
        return "tea_2p", f"识别到温州茶 · 2人（分数 {score:.2f}／差 {margin:.2f}）"
    if players == 4:
        return "tea_4p", f"识别到温州茶 · 4人（分数 {score:.2f}／差 {margin:.2f}）"
    return None, f"规则文字未确认（最佳分数 {score:.2f}／差 {margin:.2f}）"


def read_table_mode(serial: str) -> tuple[str | None, str]:
    """Grab one fresh frame from ``serial`` and read its table mode."""
    try:
        from live_capture import capture_frame

        raw, _timing = capture_frame(
            mode="tea_2p", adb_path=get_adb_path(), serial=serial,
            session_dir=TEA_ROOT / "采集" / "assist_sessions")
    except Exception as exc:  # noqa: BLE001 - 抓不到图就是读不出，按未识别处理
        return None, f"抓图失败：{exc}"
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None, "抓图无法解码"
    return detect_table_mode(image)


def read_player_identity(serial: str, expected_account: str) -> PlayerIdentity:
    """Grab one fresh native frame and verify the bottom-left local player."""
    try:
        from live_capture import capture_frame

        raw, _timing = capture_frame(
            mode="tea_2p", adb_path=get_adb_path(), serial=serial,
            session_dir=TEA_ROOT / "采集" / "assist_sessions")
    except Exception as exc:  # noqa: BLE001 - identity failure must fail closed
        return PlayerIdentity(
            "unknown", expected_account, None, 0.0, 0.0,
            f"身份核验抓图失败：{exc}",
        )
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    return detect_player_identity(image, expected_account)


def parse_window_geometry(geometry: str) -> tuple[int, int, int, int] | None:
    """Parse a Tk geometry string as ``(width, height, x, y)``.

    Tk permits signed coordinates (for example ``1180x860-8+24``).  Invalid
    or incomplete strings are rejected instead of being partially restored.
    """
    match = _WINDOW_GEOMETRY_PATTERN.fullmatch(str(geometry or ""))
    if match is None:
        return None
    return (
        int(match.group("width")),
        int(match.group("height")),
        int(match.group("x")),
        int(match.group("y")),
    )


def format_window_geometry(width: int, height: int, x: int, y: int) -> str:
    """Return a complete Tk geometry string from integer dimensions."""
    return f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}"


def calculate_bottom_right_position(
        screen_width: int, screen_height: int,
        bar_width: int = FLOATING_BAR_SIZE[0],
        bar_height: int = FLOATING_BAR_SIZE[1],
        margin: int = FLOATING_BAR_MARGIN) -> tuple[int, int]:
    """Calculate a non-negative bottom-right position for a floating bar."""
    safe_margin = max(0, int(margin))
    x = max(0, int(screen_width) - max(1, int(bar_width)) - safe_margin)
    y = max(0, int(screen_height) - max(1, int(bar_height)) - safe_margin)
    return x, y


def clamp_geometry(x: int, y: int, width: int, height: int,
                   screen: tuple[int, int]) -> tuple[int, int, int, int]:
    """Keep a remembered window at least partly on the current screen."""
    screen_w, screen_h = screen
    width = max(SIZE_RANGE["w"][0], min(SIZE_RANGE["w"][1], int(width)))
    height = max(SIZE_RANGE["h"][0], min(SIZE_RANGE["h"][1], int(height)))
    x = max(0, min(int(x), max(0, screen_w - width)))
    y = max(0, min(int(y), max(0, screen_h - height)))
    return x, y, width, height


def restore_window_geometry(
        geometry: str, screen: tuple[int, int]) -> str | None:
    """Return a parsed, on-screen main-window geometry, or ``None``.

    This is deliberately Tk-independent so persistence and restore behavior
    can be verified without opening a desktop window.
    """
    parsed = parse_window_geometry(geometry)
    if parsed is None:
        return None
    width, height, x, y = parsed
    x, y, width, height = clamp_geometry(x, y, width, height, screen)
    return format_window_geometry(width, height, x, y)


class TeaAssistWindow:
    def __init__(self, root: tk.Tk, *, serial: str | None = None):
        self.root = root
        self._profile_paths = profile_paths(
            TEA_ROOT / "采集" / "assist_sessions", serial or DEFAULT_SERIAL)
        root.title(f"温州麻将辅助器 · {self._profile_paths.serial}")
        if ICON_PATH.is_file():
            root.iconbitmap(str(ICON_PATH))
        state = load_window_state(self._profile_paths.geometry)
        screen = (root.winfo_screenwidth(), root.winfo_screenheight())
        saved_width = int(state.get("w", DEFAULT_SIZE[0]))
        saved_height = int(state.get("h", DEFAULT_SIZE[1]))
        # Upgrade the previous 440px compact window to the unified live view.
        migrate_compact = saved_width < SIZE_RANGE["w"][0]
        if migrate_compact:
            saved_width = DEFAULT_SIZE[0]
        if saved_height < SIZE_RANGE["h"][0]:
            saved_height = DEFAULT_SIZE[1]
        x, y, width, height = clamp_geometry(
            screen[0] - DEFAULT_SIZE[0] - 40 if migrate_compact else
            state.get("x", screen[0] - DEFAULT_SIZE[0] - 40),
            40 if migrate_compact else state.get("y", 40),
            saved_width, saved_height,
            screen,
        )
        root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        root.attributes("-topmost", bool(state.get("topmost", True)))
        self.session_dir = (
            TEA_ROOT / "采集" / "assist_sessions" /
            (datetime.now().strftime("%Y%m%d_%H%M%S_%f")
             + "_" + self._profile_paths.serial)
        )
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.auto_executor = ThreadPoolExecutor(max_workers=1)
        self.future: Future | None = None
        self.video_future: Future | None = None
        self.video_stop = Event()
        self.video_count = 0
        self.video_audit_offset = 0
        self.future_context: tuple | None = None
        self.future_started: float | None = None
        self.continuous = False
        self.timer: str | None = None
        self.ticker: str | None = None
        self.next_run = 0.0
        self.current: dict | None = None
        self.recorder: AssistRecorder | None = None
        self.round_no = 1
        self.recorder_timer: str | None = None
        self.show_hand = False
        self.hold_hand = None
        self.hold_until = 0.0
        self.last_render_key = None
        self.tracker = LatencyTracker()
        self.refresh = LiveRefreshState()
        self.latency_log: list[dict] = []
        self.images: dict[str, ImageTk.PhotoImage] = {}
        # Kept as a capture-loop guard; the obsolete one-shot semi-auto path
        # was removed in favor of recommendation mode + explicit full auto.
        self._tap_pending = False
        self.full_auto_running = False
        self._auto_loop = None
        self._lobby_bootstrap_enabled = True
        self._lobby_active = False
        self._lobby_in_game_verified = False
        self._lobby_room_cards: int | None = None
        self._lobby_card_status = "unknown"
        self._lobby_future: Future | None = None
        self._lobby_timer: str | None = None
        self._auto_future: Future | None = None
        self._auto_poll_timer: str | None = None
        self._auto_events: Queue = Queue()
        self._suppress_review = False
        self._review_thread: Thread | None = None
        self._review_target: Path | None = None
        self._review_events: Queue = Queue()
        self._review_poll_timer: str | None = None
        self._binding: dict | None = None
        self._identity_state = "unchecked"
        self._identity_generation = 0
        self._identity_key: tuple[str, str] | None = None
        self._identity_detail = ""
        self._identity_detected: str | None = None
        self._identity_verified_at: float | None = None
        self._identity_pending: str | None = None
        self._identity_events: Queue = Queue()
        self._identity_poll_timer: str | None = None
        self._check_state = "unchecked"
        self._check_mode_key: str | None = None
        self._check_detail = ""
        self._check_detected: str | None = None
        self._check_pending: str | None = None
        self._check_events: Queue = Queue()
        self._check_poll_timer: str | None = None
        self._check_executor = ThreadPoolExecutor(max_workers=1)
        self._binding_future: Future | None = None
        self._binding_fields_pending: dict | None = None
        self._adb_diagnostic_events: Queue | None = None
        self._adb_diagnostic_timer: str | None = None
        self.full_auto_status = tk.StringVar(value="全自动未开启。")
        self.auto_state = tk.StringVar(value="不可开启：未开始跟随")
        self.lamp_status = tk.StringVar(value="待处理")
        self.bound_status = tk.StringVar(value="账号：未绑定 · 雷电：尚未连接")
        self.phase_status = tk.StringVar(value="阶段：尚未开始")
        self.problem_status = tk.StringVar(value="状态：—")

        account_path = self._profile_paths.account
        last_account = (account_path.read_text(encoding="utf-8").strip()
                        if account_path.is_file() else "")
        if self._is_device_placeholder_account(
                last_account, self._profile_paths.serial):
            last_account = ""
        self.account = tk.StringVar(value=last_account)
        self.room_id = tk.StringVar()
        self.round_text = tk.StringVar(value="—")
        self.serial = tk.StringVar(value=self._profile_paths.serial)
        self.mode = tk.StringVar(value="温州茶 · 2人")
        self.caishen = tk.StringVar()
        self.caishen_title = tk.StringVar(value="财神：—")
        self.always_on_top = tk.BooleanVar(value=bool(state.get("topmost", True)))
        self.manual_caishen = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="选择玩法、绑定雷电设备；开始跟随会自动找位、坐下、凑齐后开局，再进入持续推荐。")
        self.hand = tk.StringVar(value="手牌：—")
        self.suggestion = tk.StringVar(value="尚未开始")
        self.reason = tk.StringVar(value="离线预检结果不能用于当前对局。")
        self.latency = tk.StringVar(value="最近一帧：—")
        self.fresh = tk.StringVar(value="尚未更新")
        self.period = tk.StringVar(value=f"周期 {BASE_INTERVAL_MS} ms")
        self.live_info = tk.StringVar(
            value="注意：点“开始跟随”后会真的代你点击——自动找空位、坐下、"
                  "点“开始游戏”并消耗房卡；进入牌局后只给建议、不再点击。"
                  "绝不充值、洗切或离桌。")
        self.preview_title = tk.StringVar(value="实时跟随")
        self.preview_flags = tk.StringVar(value="开始跟随会自动找温州茶2人空座并开局；绝不充值、洗切或离桌。")
        self.preview_meta = tk.StringVar(
            value="")
        self.offline_notice = tk.StringVar(value="")
        self.hud_wall = tk.StringVar(value="余牌：未识别")
        self.hud_countdown = tk.StringVar(value="倒计时：未识别")
        self.hud_turn = tk.StringVar(value="当前行动：未识别")
        self.hud_river = tk.StringVar(value="牌河：未识别")
        # Extended unified-path facts (玩法/房号局数/财神/暗手/副露/最近出牌).
        self.hud_mode = tk.StringVar(value="玩法：未识别")
        self.hud_room_round = tk.StringVar(value="房号/局数：未识别")
        self.match_summary = tk.StringVar(
            value=format_match_summary(round_total=16))
        self.hud_caishen = tk.StringVar(value="财神：未识别")
        self.hud_concealed = tk.StringVar(value="暗手：未识别")
        self.hud_melds = tk.StringVar(value="副露：未识别")
        self.hud_meld_types = tk.StringVar(value="副露类型：未识别")
        self.hud_last_discard = tk.StringVar(value="最近出牌：未识别")
        self.hud_claim_buttons = tk.StringVar(value="鸣牌按钮：未识别")
        self._follow_pipeline = None
        self._last_stop_reason = ""
        self.display_source: str | None = None
        self._floating_bar: tk.Toplevel | None = None
        #: Lazy right-click menu of the floating bar (created on first use).
        self._floating_menu: tk.Menu | None = None
        #: The small "×" quit control inside the floating bar, when built.
        self._floating_close_label: tk.Label | None = None
        #: Guards :meth:`quit_app` against a second teardown pass.
        self._quitting = False
        self._collapsed_geometry = format_window_geometry(width, height, x, y)
        self._collapsed_topmost = bool(state.get("topmost", True))
        self._floating_status = tk.StringVar(value="茶2 · 待命")
        self._restore_binding()
        self._build()
        self._warn_when_adb_missing()
        self._refresh_binding_views()
        self._apply_preview_layout(False)
        self.recorder_timer = root.after(500, self._poll_recorder)
        self.ticker = root.after(200, self._tick)
        # Closing the main window hides it into the right-bottom floating bar
        # instead of exiting: following/logging/auto-play keep running there.
        self._bind_window_close_protocol()

    # -- window close semantics -------------------------------------------
    def _bind_window_close_protocol(self) -> None:
        """Bind the window-manager close box (X) to the collapse action.

        Split out of ``__init__`` so an offline test double can verify the
        binding target without creating a Tk root.
        """
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

    def window_close_action(self) -> str:
        """Return the action the main window's close box triggers.

        Returns:
            str: always :data:`CLOSE_ACTION_COLLAPSE`.  Quitting is a separate
            explicit action (:meth:`quit_app`) and is never reachable by
            accidentally hitting the close box.
        """
        return CLOSE_ACTION_COLLAPSE

    def on_window_close(self) -> str:
        """Handle the main window's close box: collapse, never quit.

        The process keeps running -- the continuous-observation thread, the
        log tailers, the full-auto thread and the 200 ms ``_tick`` cadence are
        untouched, and the serial lease held by ``__main__`` stays valid
        because this window is only withdrawn.

        Returns:
            str: :data:`CLOSE_ACTION_COLLAPSE`, so callers/tests can assert the
            close box never maps to :data:`CLOSE_ACTION_QUIT`.
        """
        self.collapse_to_floating_bar()
        return CLOSE_ACTION_COLLAPSE

    # -- layout ----------------------------------------------------------
    def _build(self) -> None:
        controls = ttk.Frame(self.root, padding=(8, 6))
        controls.pack(fill="x")
        ttk.Label(controls, text="玩法").grid(row=0, column=0, sticky="w")
        self.mode_choice = ttk.Combobox(
            controls, textvariable=self.mode, values=tuple(MODES),
            state="readonly", width=13,
        )
        self.mode_choice.grid(row=0, column=1, padx=(2, 12), sticky="w")
        self.mode_choice.bind("<<ComboboxSelected>>", self._on_mode_change)
        ttk.Label(controls, text="玩家昵称").grid(row=0, column=2, sticky="w")
        self.account_entry = ttk.Entry(
            controls, textvariable=self.account, width=12)
        self.account_entry.grid(row=0, column=3, padx=(2, 10), sticky="w")
        ttk.Label(controls, text="雷电").grid(row=0, column=4, sticky="w")
        self.serial_label = ttk.Label(
            controls, textvariable=self.serial, width=16)
        self.serial_label.grid(row=0, column=5, padx=(2, 6), sticky="w")
        self.bind_button = ttk.Button(
            controls, text="连接并绑定", width=10, command=self._bind)
        self.bind_button.grid(row=0, column=6, padx=2)
        self.unbind_button = ttk.Button(
            controls, text="断开绑定", width=9, command=self._unbind)
        self.unbind_button.grid(row=0, column=7, padx=2)
        self.start_button = tk.Button(
            controls, text="开始跟随", width=10, command=self._toggle)
        self.start_button.grid(row=0, column=8, padx=4)
        self.preflight_button = ttk.Button(
            controls, text="身份/玩法核验", width=12,
            command=self._verify_identity_and_mode)
        self.preflight_button.grid(row=0, column=9, padx=2)
        self.collapse_button = ttk.Button(
            controls, text="收起到右下角", width=13,
            command=self.collapse_to_floating_bar)
        self.collapse_button.grid(row=0, column=10, padx=2)
        ttk.Button(controls, text="更多", width=5, command=self._open_menu).grid(
            row=0, column=11, sticky="e")
        controls.grid_columnconfigure(11, weight=1)
        ttk.Label(
            controls,
            text=("填写当前游戏玩家昵称；程序会读取左下本人昵称并匹配，"
                  "一致后才允许推荐和代打。雷电编号由启动器自动读取，无需手填。"),
            foreground="#a55700", wraplength=1040,
        ).grid(row=1, column=0, columnspan=12, sticky="w", pady=(3, 0))

        # Always-visible operational summary: identity, stage and the latest
        # actionable problem.  Color is semantic, not decorative.
        self.operational_strip = tk.Frame(self.root, bg="#f3f5f7", padx=8, pady=5)
        self.operational_strip.pack(fill="x", padx=8, pady=(0, 4))
        self.bound_label = tk.Label(
            self.operational_strip, textvariable=self.bound_status,
            bg="#f3f5f7", fg="#263238", anchor="w")
        self.bound_label.pack(side="left", padx=(0, 18))
        self.phase_label = tk.Label(
            self.operational_strip, textvariable=self.phase_status,
            bg="#f3f5f7", fg="#a55700", anchor="w")
        self.phase_label.pack(side="left", padx=(0, 18))
        self.problem_label = tk.Label(
            self.operational_strip, textvariable=self.problem_status,
            bg="#f3f5f7", fg="#a55700", anchor="w")
        self.problem_label.pack(side="left", fill="x", expand=True)
        self.lamp_label = tk.Label(
            self.operational_strip, textvariable=self.lamp_status,
            bg="#f3f5f7", fg="#a55700", anchor="e")
        self.lamp_label.pack(side="right")
        self.lamp_dot = tk.Label(
            self.operational_strip, text="●", bg="#f3f5f7", fg="#a55700")
        self.lamp_dot.pack(side="right")

        # Bottom strip: read-only follow_session.jsonl (gate/abstain/decision).
        # Pack before main so the strip stays at the window bottom.
        self.follow_log_frame = ttk.LabelFrame(
            self.root, text="运行日志 · 始终显示（阶段 / 动作 / 弃权 / 错误）", padding=(4, 2)
        )
        self.follow_log_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 6))

        main = ttk.Frame(self.root, padding=(8, 2))
        main.pack(fill="both", expand=True)
        self.preview_panel = ttk.Frame(main)
        self.preview_panel.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(self.preview_panel, textvariable=self.preview_title,
                  font=("Microsoft YaHei UI", 12, "bold")).pack(fill="x")
        ttk.Label(self.preview_panel, textvariable=self.preview_flags,
                  foreground="#a55700", wraplength=550).pack(fill="x", pady=(2, 4))
        self.picture = ttk.Label(
            self.preview_panel,
            text="绑定雷电设备后点“开始跟随”，可先观察大厅。",
            anchor="center", justify="center", wraplength=500,
        )
        self.picture.pack(fill="both", expand=True)
        self.live_label = ttk.Label(
            self.preview_panel, textvariable=self.live_info,
            foreground="#666666", font=("Microsoft YaHei UI", 8),
            wraplength=640, justify="left")
        self.live_label.pack(fill="x", pady=(2, 0))
        sidebar = ttk.Frame(main, width=440)
        sidebar.pack(side="right", fill="y")
        sidebar.pack_propagate(False)
        self.caishen_row = ttk.Frame(sidebar, padding=(6, 0))
        self.caishen_row.pack(fill="x")
        ttk.Label(self.caishen_row, textvariable=self.caishen_title,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Label(self.caishen_row, textvariable=self.round_text).pack(side="right")
        self.room_identity_label = ttk.Label(
            sidebar, textvariable=self.hud_room_round,
            font=("Microsoft YaHei UI", 10), foreground="#455a64")
        self.room_identity_label.pack(fill="x", padx=6, pady=(3, 4))
        self.match_summary_label = ttk.Label(
            sidebar, textvariable=self.match_summary,
            font=("Microsoft YaHei UI", 10, "bold"), foreground="#263238")
        self.match_summary_label.pack(fill="x", padx=6, pady=(0, 4))
        self.tap_panel = ttk.Frame(sidebar)
        self.tap_panel.pack(fill="x", padx=6, pady=(2, 6))
        auto_row = ttk.Frame(self.tap_panel)
        auto_row.pack(anchor="w")
        self.auto_button = ttk.Button(
            auto_row, text="开启全自动代打", width=18,
            command=self._toggle_full_auto)
        self.auto_button.pack(side="left")
        self.auto_state_label = tk.Label(
            auto_row, textvariable=self.auto_state, fg="#a55700",
            font=("Microsoft YaHei UI", 9))
        self.auto_state_label.pack(side="left", padx=(6, 0))
        self.tap_status_label = ttk.Label(
            self.tap_panel, textvariable=self.full_auto_status,
            foreground="#555555", wraplength=360,
            font=("Microsoft YaHei UI", 9))
        self.tap_status_label.pack(fill="x", pady=(2, 0))
        self.offline_label = ttk.Label(
            sidebar, textvariable=self.offline_notice, foreground="#c62828",
            font=("Microsoft YaHei UI", 9, "bold"), wraplength=380,
        )
        self.offline_label.pack(fill="x", padx=6)

        # Read-only unified table HUD — never invents values; CLAIM stays off.
        self.hud_panel = ttk.Frame(sidebar, padding=(6, 2))
        self.hud_idle_label = ttk.Label(
            sidebar, text="开始跟随后显示牌局识别", foreground="#666666",
            font=("Microsoft YaHei UI", 9))
        self.hud_idle_label.pack(fill="x", padx=6, pady=(2, 0))
        self._hud_expanded = False
        # The full technical facts remain in model state and logs.

        self.advice_label = ttk.Label(
            sidebar, textvariable=self.suggestion, foreground="#146c2e",
            font=("Microsoft YaHei UI", 22, "bold"), wraplength=380,
        )
        self.advice_label.pack(fill="x", padx=6)
        self.reason_label = ttk.Label(
            sidebar, textvariable=self.reason, wraplength=380,
            font=("Microsoft YaHei UI", 10),
        )
        self.reason_label.pack(fill="x", padx=6)
        self.info_label = ttk.Label(
            sidebar, textvariable=self.status, wraplength=380,
            font=("Microsoft YaHei UI", 9),
        )
        self.info_label.pack(fill="x", padx=6, pady=(2, 0))

        self.refresh_label = ttk.Label(
            sidebar, textvariable=self.fresh, foreground="#555555",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.refresh_label.pack(fill="x", padx=6)
        self.hand_label = ttk.Label(sidebar, textvariable=self.hand,
                                    wraplength=380)
        # Raw hand codes are hidden; hand-face tile image remains visible.
        self.hand_panel = ttk.Frame(sidebar)
        self.hand_panel.pack(fill="x", padx=6)
        self.hand_face = ttk.Label(self.hand_panel)
        self.hand_face.pack(anchor="w")
        self.advice_panel = ttk.Frame(sidebar)
        self.advice_panel.pack(fill="x", padx=6, pady=(2, 0))
        self.advice_face = ttk.Label(self.advice_panel)
        self.advice_face.pack(anchor="w")
        # Full-auto is a separate, explicit hand-off after recommendation
        # follow.  The previous one-shot/spacebar semi-auto is intentionally
        # removed from the primary workflow.
        self.candidate_face = ttk.Label(sidebar)
        self.candidate_face.pack(anchor="w", padx=6)
        self.latency_label = ttk.Label(sidebar, textvariable=self.latency,
                                       font=("Consolas", 8))
        self.latency_label.pack(fill="x", padx=6, pady=(2, 0))

        bottom = ttk.Frame(sidebar, padding=(6, 2))
        bottom.pack(side="bottom", fill="x")
        # One-click round flag only.  Per-hand correction entries were
        # removed by product decision; the auto review finds problems.
        self.flag_button = ttk.Button(
            bottom, text="标记本局有问题", width=16,
            command=self.mark_round_issue)
        self.flag_button.grid(row=0, column=0, padx=2, sticky="w")
        ttk.Button(bottom, text="打开记录", width=8,
                   command=self.open_folder).grid(row=0, column=1, padx=1)

        self.menu = tk.Menu(self.root, tearoff=0)
        # 设置组：设备由启动器固定；账号只在主区编辑。
        self.menu.add_command(label="打开下一个雷电辅助器",
                              command=self._open_next_device_window)
        self.menu.add_command(label="房间号", command=self._edit_room)
        self.menu.add_checkbutton(label="窗口置顶", variable=self.always_on_top,
                                  command=self._set_always_on_top)
        self.menu.add_separator()
        # 财神组：自动识别模板缺口的逃生门，保持原 radio 结构与门控语义。
        self.menu.add_checkbutton(label="手动指定财神", variable=self.manual_caishen,
                                  command=self._on_manual_caishen)
        self.caishen_menu = tk.Menu(self.menu, tearoff=0)
        for text, label in TILE_CHOICES.items():
            self.caishen_menu.add_radiobutton(
                label=f"{text}", value=label, variable=self.caishen,
                command=self._on_caishen_pick,
            )
        self.menu.add_cascade(label="财神牌（茶）", menu=self.caishen_menu)
        self.menu.add_separator()
        # 诊断与记录组。
        self.menu.add_command(label="离线预检（旧截图）", command=self.preflight_selected_mode)
        self.menu.add_command(label="抓图并分析", command=self.capture)
        self.menu.add_command(label="导入截图", command=self.import_image)
        self.menu.add_command(label="修正手牌并重算", command=self.manual_correction)
        self.menu.add_command(label="历史逐帧复核", command=self.open_recognition_review)
        self.menu.add_command(label="导入录像复核（每秒1帧）", command=self.import_video_review)
        self.menu.add_command(label="停止录像复核", command=self.stop_video_review)
        self.menu.add_command(label="历史会话日志", command=self.open_follow_session_log)
        self.menu.add_separator()
        self.menu.add_command(label="下一局", command=self.next_round)
        # Explicit quit: the main window's X only collapses to the floating
        # bar, so a never-collapsed window still needs a visible exit here.
        self._append_quit_menu_entry(self.menu)

        self._mount_embedded_follow_log()

    def _append_quit_menu_entry(self, menu) -> None:
        """Append the explicit "退出程序" row to ``menu``.

        Split out so the wiring (label -> :meth:`quit_app`) can be verified
        offline against a recording menu double instead of a real Tk menu.
        """
        menu.add_separator()
        menu.add_command(label=QUIT_MENU_LABEL, command=self.quit_app)

    def _follow_log_search_roots(self) -> list:
        tea_root = TEA_ROOT
        return [
            tea_root / "采集" / "dev_auto_play",
            tea_root / "采集" / "assist_sessions",
        ]

    def _mount_embedded_follow_log(self) -> None:
        """Compact read-only log strip in the main window bottom (red-box region)."""
        from follow_session_log import FollowSessionLog

        self.embedded_follow_log = FollowSessionLog(
            self.root,
            self.session_dir,
            # Main window must never silently display an older session.
            search_roots=[],
            container=self.follow_log_frame,
            compact=True,
        )
        self.embedded_follow_log.set_detach_callback(self.open_follow_session_log)

    def _open_menu(self) -> None:
        refresh = getattr(self, "_refresh_menu_state", None)
        if callable(refresh):
            refresh()
        try:
            self.menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            self.menu.grab_release()


    def open_follow_session_log(self) -> None:
        """Open the detached full follow_session.jsonl viewer (More menu / 弹出)."""
        from follow_session_log import FollowSessionLog

        viewer = getattr(self, "follow_session_log", None)
        if viewer is not None and viewer.window is not None:
            try:
                if viewer.window.winfo_exists():
                    viewer.window.lift()
                    return
            except tk.TclError:
                pass
        self.follow_session_log = FollowSessionLog(
            self.root,
            self.session_dir,
            search_roots=self._follow_log_search_roots(),
            compact=False,
        )

    def open_recognition_review(self) -> None:
        from recognition_review import RecognitionReview

        review = getattr(self, "recognition_review", None)
        if review is not None and review.window.winfo_exists():
            review.window.lift()
            return
        self.recognition_review = RecognitionReview(self.root, self.session_dir)

    def import_video_review(self) -> None:
        if self.video_future is not None or self.future is not None:
            self.status.set("当前分析尚未完成，请稍后再导入录像。")
            return
        selected = filedialog.askopenfilename(
            title="选择现有 MP4 录像", filetypes=[("MP4 录像", "*.mp4")])
        if not selected:
            return
        self.stop()
        self.video_stop.clear()
        self.video_count = 0
        audit_path = self.session_dir / "recognition_audit.jsonl"
        self.video_audit_offset = audit_path.stat().st_size if audit_path.is_file() else 0
        from recognition_video import audit_video

        self.video_future = self.executor.submit(
            audit_video, selected, mode=self._mode_key(),
            session_dir=self.session_dir, stop=self.video_stop,
            progress=lambda count: setattr(self, "video_count", count))
        self.status.set("正在复核录像：每秒抽取并识别 1 帧，原录像保持不变。")
        self.root.after(500, self._poll_video_review)

    def _poll_video_review(self) -> None:
        future = self.video_future
        if future is None:
            return
        added, self.video_audit_offset = read_appended_events(
            self.session_dir / "recognition_audit.jsonl", self.video_audit_offset)
        video_frames = [row for row in added
                        if row.get("source") == "mp4_1fps" and
                        row.get("mode") == self._mode_key()]
        if video_frames:
            row = video_frames[-1]
            result = {**row, "image": row["frame"],
                      "vision_diagnostics": row.get("diagnostics") or [],
                      "audit_event": row}
            self._show(result, time.perf_counter())
        if not future.done():
            self.status.set(f"录像复核中：已处理 {self.video_count} 帧；复核窗可实时查看。")
            self.root.after(500, self._poll_video_review)
            return
        self.video_future = None
        try:
            outcome = future.result()
        except Exception as exc:
            self.status.set(f"录像复核失败：{exc}")
        else:
            word = "已停止" if outcome["stopped"] else "完成"
            self.status.set(f"录像复核{word}：{outcome['sampled_frames']} 帧，"
                            f"识别报错 {outcome['analysis_errors']} 帧。")
            self.preview_title.set(f"录像复核{word} · 最后一帧")

    def preflight_selected_mode(self) -> None:
        """Exercise the chosen mode before a match, without ADB or recording decisions."""
        if self.future is not None or self.video_future is not None:
            self.status.set("当前分析尚未完成，请稍后再预检。")
            return
        if self.continuous:
            self.stop()
        mode = self._mode_key()
        relative, expected_sha = PREFLIGHT_FRAMES[mode]
        source = PROJECT_ROOT / relative
        if not source.is_file():
            self.status.set(f"离线预检样本缺失：{source.name}")
            return
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            self.status.set("离线预检样本校验失败；请核查原始截图。")
            return
        self.future_context = self._context()
        self.status.set("正在用已有截图预检识牌与建议；不连接模拟器。")

        def work() -> dict:
            started = time.perf_counter()
            folder = self.session_dir / "preflight" / mode
            folder.mkdir(parents=True, exist_ok=True)
            frame = save_frame(folder, raw)
            result, timing = capture_result(
                raw, mode=mode, image_path=frame, log_dir=folder / "logs",
                folder=str(frame))
            result["frame_path"] = str(frame)
            result["record_frame"] = False
            result["source"] = "offline_preflight"
            event = build_frame_event(raw, frame, result, mode=mode,
                                      source="offline_preflight")
            result["audit_event"] = event
            result["audit_path"] = str(append_frame_event(self.session_dir, event))
            return {"result": result, "capture_started_at": started,
                    "timing": timing}

        self.future = self.executor.submit(work)
        self.root.after(40, self._poll)

    def stop_video_review(self) -> None:
        if self.video_future is not None:
            self.video_stop.set()
            self.status.set("正在停止录像复核；已保存的截图与记录会保留。")

    def _edit_serial(self) -> None:
        if getattr(self, "_profile_paths", None) is not None:
            self.status.set(
                "本窗口设备已固定。多开或切换设备请用“启动多开辅助器.bat”，"
                "每个雷电设备将打开独立窗口。")
            return
        if self._is_bound() or self.continuous or self.full_auto_running:
            self.status.set("解绑并停止当前窗口后才能切换设备。")
            return
        value = simpledialog.askstring("模拟器", "ADB 序列号", initialvalue=self.serial.get())
        if value:
            try:
                profile = profile_paths(TEA_ROOT / "采集" / "assist_sessions", value)
            except ValueError as exc:
                self.status.set(str(exc))
                return
            self._profile_paths = profile
            self.serial.set(profile.serial)
            self._binding = None
            self._reset_identity_check()
            self._check_state = "unchecked"
            self._check_mode_key = None
            self.account.set(profile.account.read_text(encoding="utf-8").strip()
                             if profile.account.is_file() else "")
            self._restore_binding()
            self.root.title(f"温州麻将辅助器 · {profile.serial}")
            refresh = getattr(self, "_refresh_binding_views", None)
            if callable(refresh):
                refresh()

    def _open_next_device_window(self) -> None:
        """Open the next free online emulator without asking for its serial."""
        from app_entry import activate_window, choose_device, existing_window_pids
        from launch_multi import connected_devices, start_window
        try:
            devices = connected_devices(get_adb_path())
            occupied = existing_window_pids(DEFAULT_SERIAL)
            serial, pid = choose_device(devices, occupied)
            if serial is not None:
                proc = start_window(serial)
                self.status.set(f"已打开下一台雷电辅助器：{serial}（PID {proc.pid}）。")
                return
            if pid is not None:
                activate_window(pid)
                self.status.set("所有在线雷电都已有辅助器窗口，已切换到现有窗口。")
                return
            self.status.set("当前没有发现新的在线雷电；无需手填设备编号。")
        except Exception as exc:
            self.status.set(f"打开下一台雷电辅助器失败：{exc}")

    def _edit_room(self) -> None:
        value = simpledialog.askstring("房间号（可空）", "房间号",
                                       initialvalue=self.room_id.get())
        if value is not None:
            self.room_id.set(value.strip())

    # -- mode / settings -------------------------------------------------
    def _mode_key(self) -> str:
        return MODES.get(self.mode.get(), "tea_2p")

    @staticmethod
    def _result_source(result: dict) -> str:
        return frame_result_source(result)

    def _set_offline_notice(self) -> None:
        notice = getattr(self, "offline_notice", None)
        if notice is None:
            return
        notice.set(OFFLINE_NOTICES.get(getattr(self, "display_source", None), ""))

    def _invalidate_live_advice(self) -> None:
        """A stopped live view must not keep presenting its last advice as current."""
        refresh = getattr(self, "refresh", None)
        if refresh is None or not refresh.state.actionable:
            return
        refresh.state = replace(
            refresh.state, actionable=False, advice_is_stale=True,
            status="abstain", title="已停止跟随", suggestion=None,
            suggestion_label=None, candidates=(),
            reason="连续观察已停止；最后一帧不再代表当前牌局",
        )
        suggestion = getattr(self, "suggestion", None)
        if suggestion is not None:
            suggestion.set("已停止跟随")
        reason = getattr(self, "reason", None)
        if reason is not None:
            reason.set("最后一帧可能已过期，不能用于当前出牌。")
        notice = getattr(self, "offline_notice", None)
        if notice is not None:
            notice.set("已停止跟随：最后一帧可能已过期，不能用于当前出牌。")
        title = getattr(self, "preview_title", None)
        if title is not None and getattr(self, "display_source", None) == "live":
            title.set("实时跟随 · 已停止（最后一帧）")
        render = getattr(self, "_refresh_view", None)
        if render is not None:
            render()

    def _forget_manual_caishen(self) -> None:
        """换局时作废上一局的手选财神。

        手选财神是"操作者已肉眼核对"的一等证据，可以顶替画面识别；
        但它只对当前这一局成立。房号或局数一变就必须清空，否则会拿
        上一局的财神去算新一局，比弃权危险得多。
        """
        caishen = getattr(self, "caishen", None)
        if caishen is not None:
            caishen.set("")
        manual = getattr(self, "manual_caishen", None)
        if manual is not None:
            manual.set(False)
        button = getattr(self, "caishen_pick_button", None)
        if button is not None and button.winfo_exists():
            button.configure(text="选择财神牌")

    def _accept_continuous_result(self, result: dict) -> bool:
        """Cross-check a tea frame against the last accepted room/round HUD.

        局身份证据有两路，互相独立：
        1. 房号/局数 HUD 像素指纹（头像未遮挡时可用，像素级、最精确）；
        2. 桌面财神小牌识别（不受头像遮挡，0.98 阈值，逐帧可读）。
        任一路给出"与已核对局矛盾"的硬证据即判定换局：清除上一局手选财神，
        自动采用本帧识出的新财神，并跳过过渡帧建议。

        "局身份更新"与"本帧能否给建议"分开：advise / abstain 都先读本帧
        HUD 与财神证据并更新局基线。abstain 只表示本帧不能用于决策——
        撤销建议、保留当前截图与识牌证据，但绝不能挡住换局检测。
        同一局的 abstain 不得清除已确认的本局财神；两路都不可读时
        fail-closed：清旧建议、不猜新局或新财神。
        """
        mode = getattr(getattr(self, "mode", None), "get", lambda: "")()
        if not (getattr(self, "continuous", False) and str(mode).startswith("温州茶")):
            return True
        is_abstain = result.get("status") == "abstain"
        image = result.get("image")
        fingerprint = tea_round_fingerprint(image)
        card_label, card_score = tea_round_caishen_evidence(image)
        first = getattr(self, "continuous_round", None)
        stored_card = getattr(self, "continuous_caishen", None)
        pick, manual = self._caishen_selection()
        # 手选开关是开的、但存的标签解析不出来（旧版 UI 值、或 TILE_CHOICES
        # 改过之后的残留）。`_caishen_selection` 会安全地返回 ("", False)，
        # 但"换局必须清空手选财神"是运行时红线：这种值不能因为解析失败就被
        # 降级成"自动财神"而跨局留存。这里单独记一笔，换局时照样清空。
        stale_manual = False
        if not manual:
            try:
                stale_manual = bool(self.manual_caishen.get()) and bool(self.caishen.get())
            except Exception:  # noqa: BLE001 - 替身对象没有这两个控件时按无手选处理
                stale_manual = False
        manual_evidence = manual or stale_manual

        def invalidate_abstain_advice() -> None:
            """Abstain revokes advice but keeps the current live evidence."""
            invalidate = getattr(self, "_invalidate_live_advice", None)
            if callable(invalidate):
                invalidate()
            live_info = getattr(self, "live_info", None)
            if live_info is not None:
                live_info.set("本帧识别未完整；已停用建议并保留当前识牌证据。")

        def adopt_automatic_round(reason: str, *, drop_manual: bool = False) -> bool:
            """Keep following when automatic evidence proves a new round.

            A manual caishen is only a fallback for the round in which the user
            selected it. New-round HUD/card evidence invalidates that value;
            the follower keeps running and adopts the newly read card when one
            is available. One transition frame is still skipped so no advice
            can leak across rounds.
            """
            if drop_manual:
                self._forget_manual_caishen()
            self.continuous_round = fingerprint
            self.continuous_caishen = card_label
            self._reset_round_display(reason)
            status = getattr(self, "status", None)
            if status is not None:
                status.set(
                    f"{reason}；已自动建立新局基线，跳过过渡帧并继续跟随。")
            live_info = getattr(self, "live_info", None)
            if live_info is not None:
                live_info.set("新局基线已更新；等待下一张稳定画面。")
            return False

        # 硬证据一：HUD 指纹与已核对局不一致（原判定，保留）。
        if (first is not None and fingerprint is not None
                and not same_tea_round(fingerprint, first)):
            if not manual_evidence:
                return adopt_automatic_round("房号或局数已变化")
            return adopt_automatic_round(
                "房号或局数已变化；上一局手选财神已清空",
                drop_manual=True)
        # 硬证据二：桌面财神小牌与已核对局不一致（头像遮挡时仍可用）。
        if stored_card is not None and card_label is not None and card_label != stored_card:
            if not manual_evidence:
                return adopt_automatic_round(
                    f"财神牌已变化（{stored_card}→{card_label}）")
            return adopt_automatic_round(
                f"财神牌已变化（{stored_card}→{card_label}）；上一局手选财神已清空",
                drop_manual=True)
        # 硬证据三：手选财神与画面财神牌矛盾——要么上局遗留、要么选错。
        # 两者都必须作废，绝不能带着矛盾给出可执行建议。
        if manual and pick and card_label is not None and card_label != pick:
            self._forget_manual_caishen()
            return adopt_automatic_round(
                f"手选财神({pick})已由画面财神({card_label})自动更正")
        # 手选是画面不可读时的本局人工证据；HUD 仍为同一局时允许继续。
        if manual and pick and card_label is None:
            live_info = getattr(self, "live_info", None)
            if live_info is not None:
                live_info.set("桌面财神暂不可读；使用本局手选值并继续观察。")
            if is_abstain:
                invalidate_abstain_advice()
            return True
        # 采纳本帧证据：指纹首次出现即锁定，牌面确认即记录局财神。
        # 同一局 abstain 只在本帧识出牌面时更新；不可读时绝不把已确认财神清掉。
        if fingerprint is not None and first is None:
            self.continuous_round = fingerprint
        if card_label is not None:
            self.continuous_caishen = card_label
        # 挡板二（Codex 行为保留）：HUD 被遮挡且牌面也不可读 → 无证据。
        if fingerprint is None and card_label is None:
            if is_abstain:
                # fail-closed：清旧建议，但不猜测新局/新财神，也不抹掉当前证据。
                invalidate_abstain_advice()
                return True
            self._reset_round_display("画面未核对")
            status = getattr(self, "status", None)
            if status is not None:
                status.set("暂时看不清房号和局数；已清除旧建议，继续观察。")
            live_info = getattr(self, "live_info", None)
            if live_info is not None:
                live_info.set("画面未核对；当前没有可执行的出牌建议。")
            return False
        if is_abstain:
            invalidate_abstain_advice()
            return True
        if fingerprint is None:
            # 头像遮挡但牌面证实仍同一局：不再因遮挡长期停摆。
            live_info = getattr(self, "live_info", None)
            if live_info is not None:
                live_info.set(
                    f"房号被头像遮挡；桌面财神（{card_label}）与已核对局一致，继续观察。")
        return True

    def _reset_round_display(self, reason: str) -> None:
        refresh = getattr(self, "refresh", None)
        if refresh is not None:
            refresh.reset(reason)
        self.current = None
        self.last_render_key = None
        self.hold_hand = None
        self.display_source = None
        notice = getattr(self, "offline_notice", None)
        if notice is not None:
            notice.set("")
        if getattr(self, "hand", None) is None:
            return
        self.hand.set("手牌：—")
        if getattr(self, "suggestion", None) is not None:
            self.suggestion.set("等待摸牌")
        reason_var = getattr(self, "reason", None)
        if reason_var is not None:
            reason_var.set(reason)
        if getattr(self, "hand_panel", None) is not None:
            self._render_hand(())
        if getattr(self, "advice_face", None) is not None:
            self._render_advice()
        if getattr(self, "candidate_face", None) is not None:
            self._render_candidates(())
        if hasattr(self, "_apply_visibility"):
            self._apply_visibility()
        picture = getattr(self, "picture", None)
        if picture is not None and hasattr(picture, "configure"):
            picture.configure(image="")
            if isinstance(picture, ttk.Label):
                picture.configure(text="等待新画面。")
        images = getattr(self, "images", None)
        if images is not None:
            images.pop("preview", None)
        if getattr(self, "preview_title", None) is not None:
            self.preview_title.set("实时跟随 · 等待新一局")
        if getattr(self, "preview_flags", None) is not None:
            self.preview_flags.set(reason)

    def _on_mode_change(self, _event=None) -> None:
        self.stop()
        self.stop_video_review()
        self.continuous_round = None
        self.continuous_caishen = None
        self._autostart_done = False
        self._check_state = "unchecked"
        self._check_mode_key = None
        self._check_detail = ""
        self._check_detected = None
        self._check_pending = None
        self._reset_round_display("已切换玩法，开始跟随时将自动核对")
        self.status.set(
            "已切换玩法；开始跟随时会自动核对牌桌玩法，不一致将阻止开始。")
        self._set_operational_status(
            "待核对", "玩法已切换，开始时自动核对", level="wait")
        refresh = getattr(self, "_refresh_binding_views", None)
        if callable(refresh):
            refresh()

    # -- binding + automatic mode check --------------------------------
    @staticmethod
    def _default_instance_label(serial: str) -> str:
        """Optional human-readable nickname, never an account credential."""
        serial = str(serial or "").strip()
        if serial.startswith("emulator-"):
            return "雷电" + serial.removeprefix("emulator-")
        return "设备 " + serial if serial else "本地设备"

    @classmethod
    def _is_device_placeholder_account(cls, account: str, serial: str) -> bool:
        """Reject the short-lived UI regression that stored a device as a player."""
        value = str(account or "").strip()
        serial = str(serial or "").strip()
        return bool(value and serial and value in {
            serial, cls._default_instance_label(serial), f"设备 {serial}",
        })

    def _binding_file(self) -> Path:
        profile = getattr(self, "_profile_paths", None)
        return profile.binding if profile is not None else BINDING_PATH

    def _last_account_file(self) -> Path:
        profile = getattr(self, "_profile_paths", None)
        return profile.account if profile is not None else LAST_ACCOUNT_PATH

    def _geometry_file(self) -> Path:
        profile = getattr(self, "_profile_paths", None)
        return profile.geometry if profile is not None else WINDOW_STATE_PATH

    def _restore_binding(self) -> None:
        """Only restore the binding belonging to this serial."""
        profile = getattr(self, "_profile_paths", None)
        binding = load_binding(self._binding_file())
        if (binding is not None and profile is not None
                and binding.get("serial") != profile.serial):
            # A copied/misplaced profile must never silently switch this window
            # to another emulator. Discard it and keep the constructor serial.
            clear_binding(self._binding_file())
            binding = None
        if (binding is not None and profile is not None
                and self._is_device_placeholder_account(
                    binding.get("account"), profile.serial)):
            # Older UI builds wrote "雷电5556" as if it were a player.
            # That corrupts attribution, so require a real local player tag.
            clear_binding(self._binding_file())
            binding = None
        if binding is None and profile is not None:
            legacy = read_binding_for_serial(BINDING_PATH, profile.serial)
            if (legacy is not None and self._is_device_placeholder_account(
                    legacy.get("account"), profile.serial)):
                clear_binding(BINDING_PATH)
                legacy = None
            if legacy is not None and save_binding(legacy, self._binding_file()):
                # One-time migration: otherwise unbind would resurrect legacy.
                clear_binding(BINDING_PATH)
                binding = legacy
        if not binding:
            return
        self._binding = binding
        account = str(binding.get("account") or "").strip()
        if account and getattr(self, "account", None) is not None:
            self.account.set(account)
        serial = str(binding.get("serial") or "").strip()
        if serial and getattr(self, "serial", None) is not None:
            self.serial.set(serial)
        label = next((name for name, key in MODES.items()
                      if key == binding.get("mode")), None)
        if label and getattr(self, "mode", None) is not None:
            self.mode.set(label)

    def _current_binding_fields(self) -> dict:
        try:
            mode_key = self._mode_key()
        except Exception:
            mode_key = ""
        return {
            "account": (self.account.get().strip()
                        if getattr(self, "account", None) is not None else ""),
            "serial": (self.serial.get().strip()
                       if getattr(self, "serial", None) is not None else ""),
            "mode": mode_key,
        }

    def _is_bound(self) -> bool:
        binding = getattr(self, "_binding", None)
        if not binding:
            return False
        current = self._current_binding_fields()
        return all(binding.get(key) == current[key]
                   for key in ("account", "serial", "mode"))

    def _binding_mismatch_reason(self) -> str:
        binding = getattr(self, "_binding", None)
        if not binding:
            return "未绑定"
        current = self._current_binding_fields()
        names = {"account": "账号已变更", "serial": "设备已变更",
                 "mode": "玩法已变更"}
        diff = [names[key] for key in ("account", "serial", "mode")
                if binding.get(key) != current[key]]
        return "、".join(diff) if diff else ""

    def _bound_text(self) -> str:
        if self._is_bound():
            current = self._current_binding_fields()
            state = getattr(self, "_identity_state", "unchecked")
            if self._identity_gate_ok():
                identity = "画面已核验"
            elif state == "mismatch":
                detected = getattr(self, "_identity_detected", None) or "其他玩家"
                identity = f"画面是{detected}，不一致"
            else:
                identity = "待画面核验"
            return (f"账号：{current['account']}（{identity}） · "
                    f"雷电：{current['serial']} 已连接")
        return "账号：未绑定 · 雷电：尚未连接"

    def _bind(self) -> None:
        fields = self._current_binding_fields()
        if (not fields["account"] or self._is_device_placeholder_account(
                fields["account"], fields["serial"])):
            self.status.set("玩家昵称为空：请填写游戏内显示的本人昵称。")
            self._set_operational_status("未绑定", "缺少玩家昵称", level="error")
            return
        try:
            serial = safe_serial(fields["serial"])
        except ValueError as exc:
            self.status.set(f"雷电设备编号无效：{exc}")
            self._set_operational_status("未连接", "设备编号无效", level="error")
            return
        profile = getattr(self, "_profile_paths", None)
        if profile is not None:
            if serial != profile.serial:
                self.serial.set(profile.serial)
                self._open_other_device(serial)
                return
            if getattr(self, "_binding_fields_pending", None) is not None:
                return
            self._binding_fields_pending = fields.copy()
            self.status.set(f"正在连接 {serial}，检查ADB及实时截图…")
            self._set_operational_status("连接中", serial, level="wait")
            self._binding_future = self._check_executor.submit(
                self._probe_device, serial)
            self._refresh_bind_controls()
            self.root.after(100, self._poll_binding_probe)
            return
        # Minimal offline test doubles have no live profile.
        self._save_verified_binding(fields)

    def _probe_device(self, serial: str):
        """Worker-thread binding probe: resolve adb first, then verify."""
        adb_path = get_adb_path()
        if not adb_path:
            return DeviceConnection(
                False, serial,
                f"无法连接ADB/读取画面：{ADB_RESOLVE_NOTE}")
        return verify_device(serial, adb_path)

    def _adb_diagnostic_text(self) -> str:
        """Read-only ``adb_inventory`` notes for the *failed* bind branch."""
        try:
            from adb_runtime import adb_inventory

            inventory = adb_inventory()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            return f"adb 体检失败：{exc}"
        if not isinstance(inventory, dict):
            return "adb 体检返回了意外结构，已跳过。"
        notes = inventory.get("notes") or []
        if isinstance(notes, str):
            notes = [notes]
        text = "；".join(str(note) for note in notes if str(note).strip())
        return text or "adb 体检未给出额外原因。"

    def _schedule_adb_diagnostic(self) -> None:
        """Run ``adb_inventory()`` on a worker thread after a failed bind.

        ``adb_inventory()`` costs up to ~10 s (version + devices + process
        scan), so the Tk thread only schedules it; the notes arrive through
        :meth:`_poll_adb_diagnostic` on the existing ``after`` cadence.
        """
        executor = getattr(self, "_check_executor", None)
        if executor is None:
            return
        self._adb_diagnostic_events = Queue()
        try:
            executor.submit(self._run_adb_diagnostic)
        except Exception:  # noqa: BLE001 - diagnostics are best-effort
            return
        if getattr(self, "_adb_diagnostic_timer", None) is None:
            try:
                self._adb_diagnostic_timer = self.root.after(
                    150, self._poll_adb_diagnostic)
            except Exception:  # noqa: BLE001
                self._adb_diagnostic_timer = None

    def _run_adb_diagnostic(self) -> None:
        """Worker thread: collect read-only adb notes; never touch Tk."""
        events = getattr(self, "_adb_diagnostic_events", None)
        if events is None:
            return
        events.put(self._adb_diagnostic_text())

    def _poll_adb_diagnostic(self) -> None:
        self._adb_diagnostic_timer = None
        events = getattr(self, "_adb_diagnostic_events", None)
        if events is None:
            return
        try:
            notes = events.get_nowait()
        except Empty:
            notes = None
        status = getattr(self, "status", None)
        if notes and status is not None:
            status.set(f"{status.get()} adb体检：{notes}")
        if getattr(self, "_adb_diagnostic_timer", None) is None and \
                notes is None:
            try:
                self._adb_diagnostic_timer = self.root.after(
                    300, self._poll_adb_diagnostic)
            except Exception:  # noqa: BLE001
                self._adb_diagnostic_timer = None

    def _open_other_device(self, serial: str) -> None:
        """Open or focus another UI for the chosen device; never switch this UI's lease."""
        from app_entry import activate_window, existing_window_pids
        from launch_multi import connected_devices, start_window
        try:
            if serial not in connected_devices(get_adb_path()):
                self.status.set(f"{serial} 未连接，未打开新窗口。")
                return
            pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
            if pid is not None:
                activate_window(pid)
                self.status.set(f"{serial} 的辅助器已经打开，已尝试切换到该窗口。")
                return
            proc = start_window(serial)
            self.status.set(f"已为 {serial} 打开另一辅助器窗口，PID {proc.pid}。")
        except Exception as exc:
            self.status.set(f"打开设备窗口失败：{exc}")

    def _poll_binding_probe(self) -> None:
        pending = getattr(self, "_binding_fields_pending", None)
        future = getattr(self, "_binding_future", None)
        if pending is None or future is None:
            return
        if not future.done():
            self.root.after(100, self._poll_binding_probe)
            return
        self._binding_future = None
        self._binding_fields_pending = None
        try:
            result = future.result()
        except Exception as exc:
            self.status.set(f"连接失败：{exc}；未保存绑定。")
            self._set_operational_status("连接失败", str(exc), level="error")
            self._refresh_binding_views()
            return
        if (not result.success or
                pending["serial"] != self.serial.get().strip() or
                pending["serial"] != self._profile_paths.serial or
                pending["account"] != self.account.get().strip()):
            detail = (result.detail if not result.success
                      else "连接期间编号或备注改变，请重新连接")
            if not result.success:
                self._schedule_adb_diagnostic()
            self.status.set(f"连接失败：{detail}；未保存绑定。")
            self._set_operational_status("连接失败", detail, level="error")
            self._refresh_binding_views()
            return
        if self._save_verified_binding(pending):
            self.status.set(f"{result.detail}；配置已保存，正在核验画面本人昵称。")
            self._set_operational_status(
                "设备连接成功", "等待画面本人昵称与配置匹配", level="wait")
            self._begin_identity_check(pending=None)
        self._refresh_binding_views()

    def _save_verified_binding(self, fields: dict) -> bool:
        if (not fields["account"] or self._is_device_placeholder_account(
                fields["account"], fields["serial"])):
            self.status.set("玩家昵称为空：未保存绑定。")
            self._set_operational_status("未绑定", "缺少玩家昵称", level="error")
            return False
        binding = {
            "account": fields["account"],
            "serial": fields["serial"],
            "mode": fields["mode"],
            "mode_label": (self.mode.get()
                           if getattr(self, "mode", None) is not None else ""),
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
        if not save_binding(binding, self._binding_file()):
            self.status.set("绑定保存失败：本地文件写入出错。")
            self._set_operational_status(
                "未绑定", "绑定写入失败", level="error")
            return False
        self._binding = binding
        self._reset_identity_check()
        self.status.set(
            f"已绑定：{binding['serial']} · {binding['mode_label']}；"
            "已保存；画面本人昵称核验通过后才允许跟随。")
        self._set_operational_status(
            "已绑定", f"账号 {binding['account']} · 等待画面核验", level="wait")
        self._refresh_binding_views()
        return True

    def _unbind(self) -> None:
        self._binding_fields_pending = None
        # Revoking the local identity also revokes click authority.
        if getattr(self, "full_auto_running", False):
            self._stop_full_auto()
        elif getattr(self, "continuous", False):
            self.stop()
        clear_binding(self._binding_file())
        profile = getattr(self, "_profile_paths", None)
        if profile is not None and read_binding_for_serial(
                BINDING_PATH, profile.serial) is not None:
            clear_binding(BINDING_PATH)
        self._binding = None
        self._reset_identity_check()
        self._check_state = "unchecked"
        self._check_mode_key = None
        self._check_pending = None
        self.status.set("已解绑并申请停手；已发出的动作完成点后验证才算停止。")
        self._set_operational_status("未绑定", "绑定已清除", level="wait")
        self._refresh_binding_views()

    def _refresh_identity(self) -> None:
        bound_status = getattr(self, "bound_status", None)
        if bound_status is not None:
            bound_status.set(self._bound_text())

    @staticmethod
    def _channel_ready() -> bool:
        try:
            from action_policy import CLAIM_TAP_CHANNEL_READY
        except Exception:
            return False
        return bool(CLAIM_TAP_CHANNEL_READY)

    def _refresh_lamp(self) -> None:
        """Readiness lamp: green ready / orange pending / red abnormal."""
        var = getattr(self, "lamp_status", None)
        if var is None:
            return
        colors = {"ok": "#146c2e", "wait": "#a55700", "error": "#c62828"}
        if getattr(self, "_identity_state", "unchecked") == "mismatch":
            expected = self.account.get().strip()
            detected = getattr(self, "_identity_detected", None) or "其他玩家"
            text, level = f"异常：玩家不一致（配置{expected}，画面{detected}）", "error"
        elif getattr(self, "_check_state", "unchecked") == "mismatch":
            detected = getattr(self, "_check_detected", None)
            label = next((name for name, key in MODES.items()
                          if key == detected), detected or "其它玩法")
            text, level = f"异常：玩法不一致（牌桌是{label}）", "error"
        else:
            missing = []
            if getattr(self, "_binding", None) is None:
                missing.append("未绑定")
            elif not self._is_bound():
                missing.append(
                    f"绑定已失效（{self._binding_mismatch_reason()}）")
            if self._is_bound() and not self._identity_gate_ok():
                state = getattr(self, "_identity_state", "unchecked")
                missing.append({
                    "checking": "玩家核验中",
                    "unknown": "本人昵称未显示",
                    "unsupported": "玩家昵称无模板",
                }.get(state, "玩家未核验"))
            if not self._mode_gate_ok():
                state = getattr(self, "_check_state", "unchecked")
                missing.append({"checking": "玩法核对中",
                                "unknown": "玩法未识别"}.get(state, "玩法未核对"))
            channel_ok = self._channel_ready()
            if not channel_ok:
                missing.append("动作通道未就绪")
            if not missing:
                text, level = "就绪", "ok"
            elif not channel_ok and len(missing) == 1:
                text, level = "异常：自动动作通道未就绪", "error"
            else:
                text, level = f"待处理：{'、'.join(missing)}", "wait"
        var.set(text)
        for widget in (getattr(self, "lamp_dot", None),
                       getattr(self, "lamp_label", None)):
            if widget is not None:
                try:
                    widget.configure(fg=colors[level])
                except Exception:
                    pass

    def _refresh_auto_gate(self, *, force: bool = False) -> None:
        """Full-auto button state: never looks clickable when it would refuse."""
        button = getattr(self, "auto_button", None)
        var = getattr(self, "auto_state", None)
        label = getattr(self, "auto_state_label", None)
        colors = {"ok": "#146c2e", "wait": "#a55700", "error": "#c62828"}
        if getattr(self, "full_auto_running", False):
            stopping = bool(getattr(getattr(self, "_auto_loop", None),
                                    "_stop_requested_reason", None))
            text, level = ("● 正在安全停手", "wait") if stopping else ("● 代打中", "ok")
            if force and button is not None:
                try:
                    button.configure(state="normal")
                except Exception:
                    pass
        else:
            missing = []
            if self._mode_key() != "tea_2p":
                missing.append("全自动仅开放温州茶 2 人")
            if not self._is_bound():
                missing.append("未绑定" if getattr(self, "_binding", None) is None
                               else "绑定已失效")
            if self._is_bound() and not self._identity_gate_ok():
                state = getattr(self, "_identity_state", "unchecked")
                missing.append("玩家不一致" if state == "mismatch" else "玩家未核验")
            if not self._mode_gate_ok():
                missing.append("玩法未核对")
            if not getattr(self, "continuous", False):
                missing.append("未开始跟随")
            if not self._channel_ready():
                missing.append("动作通道未就绪")
            if missing:
                level = ("error" if "玩家不一致" in missing else "wait")
                text = f"不可开启：{'、'.join(missing)}"
                state = "disabled"
            else:
                text, level, state = "● 可开启", "ok", "normal"
            if button is not None:
                try:
                    button.configure(state=state)
                except Exception:
                    pass
        if var is not None:
            var.set(text)
        if label is not None:
            try:
                label.configure(fg=colors[level])
            except Exception:
                pass

    def _refresh_start_gate(self) -> None:
        """开始跟随 follows the same grey-out standard as the auto button."""
        button = getattr(self, "start_button", None)
        if button is None:
            return
        running = (bool(getattr(self, "continuous", False))
                   or bool(getattr(self, "full_auto_running", False))
                   or bool(getattr(self, "_lobby_active", False)))
        try:
            button.configure(
                state="normal" if (running or self._is_bound()) else "disabled")
        except Exception:
            pass

    def _refresh_bind_controls(self) -> None:
        """Bind/unbind are mutually exclusive; a bound account is locked."""
        bound = self._is_bound()
        pending = getattr(self, "_binding_fields_pending", None) is not None
        bind_button = getattr(self, "bind_button", None)
        if bind_button is not None:
            try:
                bind_button.configure(
                    text=("连接中…" if pending else
                          "已连接/重检" if bound else "连接并绑定"),
                    state="disabled" if pending else "normal")
            except Exception:
                pass
        unbind_button = getattr(self, "unbind_button", None)
        if unbind_button is not None:
            try:
                unbind_button.configure(
                    state=("normal"
                           if getattr(self, "_binding", None) is not None
                           else "disabled"))
            except Exception:
                pass
        entry = getattr(self, "account_entry", None)
        if entry is not None:
            try:
                entry.configure(state="readonly" if bound else "normal")
            except Exception:
                pass

    def _refresh_flag_button(self) -> None:
        button = getattr(self, "flag_button", None)
        if button is None:
            return
        active = (bool(getattr(self, "continuous", False))
                  or bool(getattr(self, "full_auto_running", False)))
        try:
            button.configure(state="normal" if active else "disabled")
        except Exception:
            pass

    def _apply_preview_layout(self, running: bool) -> None:
        """Keep the compact log compact; reserve workspace for the game frame."""
        picture = getattr(self, "picture", None)
        log_frame = getattr(self, "follow_log_frame", None)
        if picture is None or log_frame is None:
            return
        try:
            picture.pack_configure(fill="both", expand=True)
            log_frame.pack_configure(fill="x", expand=False)
        except Exception:
            pass

    def _refresh_binding_views(self) -> None:
        self._refresh_identity()
        self._refresh_lamp()
        self._refresh_auto_gate()
        self._refresh_start_gate()
        self._refresh_bind_controls()
        self._refresh_flag_button()

    def _identity_context(self) -> tuple[str, str]:
        account = (self.account.get().strip()
                   if getattr(self, "account", None) is not None else "")
        serial = (self.serial.get().strip()
                  if getattr(self, "serial", None) is not None else "")
        return account, serial

    def _reset_identity_check(self) -> None:
        self._identity_generation = getattr(self, "_identity_generation", 0) + 1
        self._identity_state = "unchecked"
        self._identity_key = None
        self._identity_detail = ""
        self._identity_detected = None
        self._identity_verified_at = None
        self._identity_pending = None

    def _identity_gate_ok(self, *, max_age_seconds: float | None = None) -> bool:
        matched = (
            getattr(self, "_identity_state", "unchecked") == "match"
            and getattr(self, "_identity_key", None) == self._identity_context()
        )
        if not matched or max_age_seconds is None:
            return matched
        verified_at = getattr(self, "_identity_verified_at", None)
        return (verified_at is not None
                and time.perf_counter() - verified_at <= max_age_seconds)

    def _begin_identity_check(self, pending: str | None = None) -> None:
        """Read the local-seat nickname before any advice or click authority."""
        context = self._identity_context()
        if getattr(self, "_identity_state", "unchecked") == "checking" \
                and getattr(self, "_identity_key", None) == context:
            if pending:
                self._identity_pending = pending
            return
        self._identity_state = "checking"
        self._identity_generation = getattr(self, "_identity_generation", 0) + 1
        generation = self._identity_generation
        self._identity_key = context
        self._identity_detail = ""
        self._identity_detected = None
        self._identity_pending = pending
        events = getattr(self, "_identity_events", None)
        if events is None:
            self._identity_events = events = Queue()
        else:
            while True:
                try:
                    events.get_nowait()
                except Empty:
                    break
        status = getattr(self, "status", None)
        if status is not None:
            status.set("正在读取左下本人昵称，核对配置账号与当前雷电画面…")
        self._set_operational_status(
            "玩家核验", "只读抓图，不发送点击", level="wait")
        self._refresh_binding_views()
        account, serial = context
        reader = getattr(self, "_identity_reader", None)
        if not callable(reader):
            reader = read_player_identity

        def work() -> None:
            began = time.perf_counter()
            try:
                result = reader(serial, account)
            except Exception as exc:  # noqa: BLE001 - failure must fail closed
                result = PlayerIdentity(
                    "unknown", account, None, 0.0, 0.0,
                    f"身份核验失败：{exc}",
                )
            elapsed = int((time.perf_counter() - began) * 1000)
            events.put((generation, context, result, elapsed))

        executor = (getattr(self, "_check_executor", None)
                    or getattr(self, "executor", None))
        if executor is None:
            events.put((generation, context, PlayerIdentity(
                "unknown", account, None, 0.0, 0.0,
                "身份核验执行器不可用"), 0))
        else:
            executor.submit(work)
        root = getattr(self, "root", None)
        if root is not None:
            self._identity_poll_timer = root.after(
                120, self._poll_identity_check)

    def _poll_identity_check(self) -> None:
        self._identity_poll_timer = None
        events = getattr(self, "_identity_events", None)
        if events is None:
            return
        latest = None
        while True:
            try:
                candidate = events.get_nowait()
                if candidate[0] == getattr(self, "_identity_generation", 0):
                    latest = candidate
            except Empty:
                break
        if latest is None:
            if getattr(self, "_identity_state", None) == "checking":
                root = getattr(self, "root", None)
                if root is not None:
                    self._identity_poll_timer = root.after(
                        120, self._poll_identity_check)
            return
        _generation, context, result, elapsed = latest
        if context != self._identity_context() or not self._is_bound():
            self._reset_identity_check()
            self._refresh_binding_views()
            return
        self._identity_key = context
        self._identity_state = result.status
        self._identity_detail = result.detail
        self._identity_detected = result.detected
        pending = getattr(self, "_identity_pending", None)
        self._identity_pending = None
        if result.matched:
            self._identity_verified_at = time.perf_counter()
            self.status.set(
                f"玩家身份已核验：{result.detail}（只读 {elapsed}ms，未点击）。")
            self._set_operational_status(
                "玩家已核验", f"{context[0]} 与当前画面一致", level="ok")
            self._refresh_binding_views()
            if pending == "follow":
                self.start()
            elif pending == "full_auto":
                self._start_full_auto()
            elif pending == "mode_check":
                self._begin_mode_check(pending=None)
            return
        if result.status == "mismatch":
            self.status.set(f"玩家身份不一致：{result.detail}；已阻止跟随和代打。")
            self._set_operational_status(
                "身份不一致", result.detail, level="error")
        else:
            self.status.set(
                f"玩家身份未核验：{result.detail}；已阻止推荐和代打。"
                "请关闭遮挡本人昵称的面板或进入牌桌后重试。")
            self._set_operational_status(
                "身份待核验", result.detail, level="wait")
        self._refresh_binding_views()

    def _verify_identity_and_mode(self) -> None:
        """Manual read-only recheck; identity must pass before mode reading."""
        if not self._is_bound():
            self.status.set("先填写玩家昵称并完成连接绑定，再核验当前画面。")
            self._set_operational_status(
                "未绑定", "无法核验玩家与玩法", level="wait")
            return
        self._reset_identity_check()
        self._check_state = "unchecked"
        self._check_mode_key = None
        self._begin_identity_check(pending="mode_check")

    def _mode_gate_ok(self) -> bool:
        if getattr(self, "_check_state", "unchecked") != "match":
            return False
        try:
            return getattr(self, "_check_mode_key", None) == self._mode_key()
        except Exception:
            return False

    def _begin_mode_check(self, pending: str | None = None) -> None:
        """Kick off the pre-start table-mode check in the background."""
        if getattr(self, "_check_state", "unchecked") == "checking":
            if pending:
                self._check_pending = pending
            return
        self._check_state = "checking"
        try:
            self._check_mode_key = self._mode_key()
        except Exception:
            self._check_mode_key = None
        self._check_detail = ""
        self._check_detected = None
        self._check_pending = pending
        status = getattr(self, "status", None)
        if status is not None:
            status.set("正在自动核对牌桌玩法（读取规则文字）…")
        self._refresh_lamp()
        serial = (self.serial.get().strip()
                  if getattr(self, "serial", None) is not None else "")
        mode_key = self._check_mode_key
        reader = getattr(self, "_mode_reader", None)
        if not callable(reader):
            reader = read_table_mode
        events = getattr(self, "_check_events", None)
        if events is None:
            self._check_events = events = Queue()

        def work() -> None:
            began = time.perf_counter()
            try:
                detected, detail = reader(serial)
            except Exception as exc:  # noqa: BLE001 - 读失败按未识别处理
                detected, detail = None, f"核对读取失败：{exc}"
            detail = f"{detail}（只读自检 {int((time.perf_counter()-began)*1000)}ms，未点击）"
            events.put((mode_key, detected, detail))

        executor = (getattr(self, "_check_executor", None)
                    or getattr(self, "executor", None))
        if executor is None:
            events.put((mode_key, None, "核对执行器不可用"))
        else:
            executor.submit(work)
        root = getattr(self, "root", None)
        if root is not None:
            self._check_poll_timer = root.after(120, self._poll_mode_check)

    def _poll_mode_check(self) -> None:
        self._check_poll_timer = None
        events = getattr(self, "_check_events", None)
        if events is None:
            return
        latest = None
        while True:
            try:
                latest = events.get_nowait()
            except Empty:
                break
        if latest is None:
            if getattr(self, "_check_state", None) == "checking":
                root = getattr(self, "root", None)
                if root is not None:
                    self._check_poll_timer = root.after(
                        120, self._poll_mode_check)
            return
        mode_key, detected, detail = latest
        try:
            current_key = self._mode_key()
        except Exception:
            current_key = None
        if mode_key != current_key:
            self._check_state = "unchecked"
            self._check_pending = None
            return
        self._check_mode_key = mode_key
        self._check_detail = detail
        self._check_detected = detected
        pending = getattr(self, "_check_pending", None)
        self._check_pending = None
        status = getattr(self, "status", None)
        if detected is not None and detected == mode_key:
            self._check_state = "match"
            if status is not None:
                status.set(f"已自动核对：牌桌玩法与所选一致（{detail}）。")
            self._set_operational_status(
                "已核对", "玩法自动核对一致", level="ok")
            self._refresh_binding_views()
            if pending == "follow":
                self.start()
            elif pending == "full_auto":
                self._start_full_auto()
            return
        if detected is None:
            self._check_state = "unknown"
            if status is not None:
                status.set(
                    f"玩法未识别：{detail}；已阻止开始。"
                    "确认已进入牌桌后，再点“开始跟随”重试。")
            self._set_operational_status(
                "待核对", "玩法未识别，已阻止开始", level="wait")
        else:
            self._check_state = "mismatch"
            label = next((name for name, key in MODES.items()
                          if key == detected), detected)
            if status is not None:
                status.set(
                    f"所选玩法与牌桌不一致：牌桌是“{label}”，已阻止开始；"
                    "请改选玩法或换房。")
            self._set_operational_status(
                "核对不一致", f"牌桌是{label}，与所选不一致", level="error")
        self._refresh_binding_views()

    def _menu_entry_enabled(self, label: str) -> bool:
        if label == "停止录像复核":
            return getattr(self, "video_future", None) is not None
        if label == "下一局":
            return (bool(getattr(self, "continuous", False))
                    and not getattr(self, "full_auto_running", False))
        return True

    def _refresh_menu_state(self) -> None:
        menu = getattr(self, "menu", None)
        if menu is None:
            return
        try:
            end = menu.index("end")
        except Exception:
            return
        if end is None:
            return
        for index in range(int(end) + 1):
            try:
                label = menu.entrycget(index, "label")
            except Exception:
                continue
            if label in ("停止录像复核", "下一局"):
                try:
                    menu.entryconfigure(
                        index, state=("normal"
                                      if self._menu_entry_enabled(label)
                                      else "disabled"))
                except Exception:
                    pass

    def _style_start_button(self, running: bool) -> None:
        """The stop action is always in the main area and red while running."""
        button = getattr(self, "start_button", None)
        if button is None:
            return
        try:
            if running:
                button.configure(fg="#c62828",
                                 font=("Microsoft YaHei UI", 9, "bold"))
            else:
                button.configure(fg="#000000",
                                 font=("Microsoft YaHei UI", 9))
        except Exception:
            pass

    def _set_hud_expanded(self, expanded: bool) -> None:
        panel = getattr(self, "hud_panel", None)
        idle = getattr(self, "hud_idle_label", None)
        if panel is None or idle is None:
            return
        if getattr(self, "_hud_expanded", None) == bool(expanded):
            return
        self._hud_expanded = bool(expanded)
        anchor = getattr(self, "offline_label", None)
        if expanded:
            idle.pack_forget()
            if anchor is not None:
                panel.pack(fill="x", after=anchor)
            else:
                panel.pack(fill="x")
        else:
            panel.pack_forget()
            if anchor is not None:
                idle.pack(fill="x", padx=6, pady=(2, 0), after=anchor)
            else:
                idle.pack(fill="x", padx=6, pady=(2, 0))

    def _on_manual_caishen(self) -> None:
        if not self.manual_caishen.get():
            self.caishen.set("")
            button = getattr(self, "caishen_pick_button", None)
            if button is not None and button.winfo_exists():
                button.configure(text="选择财神牌")
            self._reset_round_display("手选财神已取消，等待画面识别")
            if self.continuous and self.future is None:
                self._schedule(0)

    def _capture_caishen_candidate(self) -> None:
        """手选那一刻，把画不出来的财字小牌存成待复核候选模板。

        只在用户手选这个动作里触发（每局最多几次），不是每帧都跑；
        采集/入库失败绝不能影响识别主链路，所以整个调用被吞掉。
        """
        try:
            label = TILE_CHOICES.get(self.caishen.get(), "")
            current = getattr(self, "current", None) or {}
            frame_path = current.get("image")
            if not label or not frame_path:
                return
            from caishen_collector import collect_pending_from_file
            outcome = collect_pending_from_file(frame_path, label)
            status = getattr(self, "status", None)
            if outcome.get("status") == "pending" and status is not None:
                status.set("已捕获财字小牌样本，待人工复核后入库。")
        except Exception:
            return

    def _on_caishen_pick(self) -> None:
        self.manual_caishen.set(True)
        # Capture the labelled sprite before clearing the old frame from UI.
        self._capture_caishen_candidate()
        button = getattr(self, "caishen_pick_button", None)
        if button is not None and button.winfo_exists():
            name = next((name for name, label in TILE_CHOICES.items()
                         if label == self.caishen.get()), self.caishen.get())
            button.configure(text=f"已选：{name}")
        self._reset_round_display("财神已手选，等待新截图复核")
        self.status.set("已手选本局财神；正在用新画面重新识牌。")
        if self.continuous and self.future is None:
            self._schedule(0)

    def _open_caishen_menu(self) -> None:
        """Use the existing native menu; a topmost window can cover a combobox popup."""
        button = self.caishen_pick_button
        try:
            self.caishen_menu.tk_popup(button.winfo_rootx(),
                                       button.winfo_rooty() + button.winfo_height())
        finally:
            self.caishen_menu.grab_release()

    def _set_always_on_top(self) -> None:
        self.root.attributes("-topmost", self.always_on_top.get())

    def _warn_when_adb_missing(self) -> None:
        """Surface a missing adb instead of silently using a stale path.

        Never auto-restarts the adb server: ``ensure_single_server`` stays
        read-only unless an operator explicitly asks for a restart.
        """
        if not ADB_RESOLVE_NOTE:
            return
        status = getattr(self, "status", None)
        if status is None:
            return
        status.set(f"{ADB_RESOLVE_NOTE}：请先安装雷电/平台工具或指定 adb 路径，再连接设备。")

    def _floating_status_text(self) -> str:
        """Return compact state text for the right-bottom floating bar."""
        try:
            mode_name = str(self.mode.get()) if hasattr(self, "mode") else ""
        except Exception:  # noqa: BLE001 - a status label must never raise
            mode_name = ""
        mode_short = {
            "温州茶 · 2人": "茶2",
            "温州茶 · 4人": "茶4",
            "温州熟 · 4人": "熟4",
        }.get(mode_name, "麻将")
        if getattr(self, "full_auto_running", False):
            state = "全自动"
        elif getattr(self, "continuous", False):
            state = "跟随中"
        else:
            state = "待命"
        return f"{mode_short} · {state}"

    def _refresh_floating_status(self) -> None:
        """Refresh the floating text from the existing Tk tick cadence."""
        status = getattr(self, "_floating_status", None)
        if status is None:
            return
        try:
            status.set(self._floating_status_text())
        except tk.TclError:
            # The bar was destroyed between ticks; the next collapse recreates
            # it, so a stale variable must not break the main tick loop.
            pass

    def _destroy_floating_bar(self) -> None:
        """Destroy this serial window's floating bar if it exists."""
        bar = getattr(self, "_floating_bar", None)
        self._floating_bar = None
        # The "×" control dies with its parent; drop the stale reference so a
        # later collapse rebuilds it instead of resurrecting a dead widget.
        self._floating_close_label = None
        if bar is None:
            return
        try:
            if bar.winfo_exists():
                bar.destroy()
        except tk.TclError:
            pass

    def _floating_menu_command(self, action: str):
        """Return the callable bound to a floating-bar menu ``action``."""
        if action == FLOATING_BAR_MENU_QUIT:
            return self.quit_app
        return self.restore_from_floating_bar

    def _ensure_floating_menu(self) -> tk.Menu | None:
        """Create (once) the floating bar's right-click menu.

        Returns:
            tk.Menu | None: the reusable menu, or ``None`` when Tk refused it
            (e.g. the root is already being torn down).
        """
        menu = getattr(self, "_floating_menu", None)
        if menu is not None:
            try:
                if menu.winfo_exists():
                    return menu
            except tk.TclError:
                self._floating_menu = None
        try:
            menu = tk.Menu(self.root, tearoff=0)
            for label, action in FLOATING_BAR_MENU_ITEMS:
                menu.add_command(label=label,
                                 command=self._floating_menu_command(action))
        except tk.TclError:
            self._floating_menu = None
            return None
        self._floating_menu = menu
        return menu

    def _popup_floating_menu(self, event=None) -> str:
        """Show the floating bar's menu (right click).  Consumes the click."""
        menu = self._ensure_floating_menu()
        if menu is None:
            return "break"
        try:
            x = getattr(event, "x_root", None) if event is not None else None
            y = getattr(event, "y_root", None) if event is not None else None
            if x is None or y is None:
                x = self.root.winfo_pointerx()
                y = self.root.winfo_pointery()
            menu.tk_popup(int(x), int(y))
        except tk.TclError:
            self._floating_menu = None
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _build_floating_close_control(self, bar) -> tk.Label | None:
        """Add the tiny "×" quit control to the top-right of ``bar``.

        It is a separate child widget using ``place`` (independent of the
        packed status label) and it binds ``<Button-1>`` at *widget* level,
        which Tk dispatches before the toplevel binding that restores the
        window; :meth:`_on_floating_close_click` returns ``"break"`` to stop
        that later tag, so a click on "×" can never restore the window.

        Returns:
            tk.Label | None: the control, or ``None`` when Tk refused it.
        """
        width, height = FLOATING_BAR_CLOSE_SIZE
        try:
            close = tk.Label(
                bar, text="×", background="#263238", foreground="#90a4ae",
                cursor="hand2", font=("Microsoft YaHei UI", 8, "bold"),
                width=1, height=1, borderwidth=0, highlightthickness=0)
            close.place(relx=1.0, x=-1, y=0, anchor="ne",
                        width=width, height=height)
            # Widget-level binding runs first and "break" short-circuits the
            # toplevel (bar) <Button-1> restore binding.
            close.bind("<Button-1>", self._on_floating_close_click)
            close.bind("<Enter>",
                       lambda _e: close.configure(foreground="#ff8a80"))
            close.bind("<Leave>",
                       lambda _e: close.configure(foreground="#90a4ae"))
            close.bind("<Button-3>", self._popup_floating_menu)
        except tk.TclError:
            return None
        self._floating_close_label = close
        return close

    def _on_floating_close_click(self, _event=None) -> str:
        """Really quit from the floating bar's "×" control.

        Returns:
            str: ``"break"``, so Tk stops after the widget-level binding and
            the bar's "click to restore" binding never fires.
        """
        self.quit_app()
        return "break"

    def _show_floating_bar(self) -> None:
        """Create one borderless, topmost floating bar for this window."""
        existing = getattr(self, "_floating_bar", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                self._floating_bar = None

        width, height = FLOATING_BAR_SIZE
        x, y = calculate_bottom_right_position(
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            width, height, FLOATING_BAR_MARGIN)
        bar = tk.Toplevel(self.root)
        self._floating_bar = bar
        bar.overrideredirect(True)
        bar.configure(background="#263238", cursor="hand2")
        bar.geometry(format_window_geometry(width, height, x, y))
        bar.resizable(False, False)
        bar.attributes("-topmost", True)
        label = tk.Label(
            bar, textvariable=self._floating_status, background="#263238",
            foreground="#ffffff", cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=4)
        label.pack(fill="both", expand=True)
        for widget in (bar, label):
            widget.bind("<Button-1>", self.restore_from_floating_bar)
            # Right click anywhere on the bar opens the explicit menu.
            widget.bind("<Button-3>", self._popup_floating_menu)
        # An (invisible) window-manager close on the bar still means "restore";
        # the real quit entry points are the "×" control and the context menu.
        bar.protocol("WM_DELETE_WINDOW", self.restore_from_floating_bar)
        # Built last so it sits on top of the packed status label.
        self._build_floating_close_control(bar)
        self._refresh_floating_status()
        bar.lift()

    def collapse_to_floating_bar(self) -> None:
        """Hide the main window while keeping all follow work running."""
        existing = getattr(self, "_floating_bar", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                self._floating_bar = None
        try:
            self.root.update_idletasks()
            current_geometry = self.root.geometry()
            if parse_window_geometry(current_geometry) is None:
                current_geometry = format_window_geometry(
                    self.root.winfo_width(), self.root.winfo_height(),
                    self.root.winfo_x(), self.root.winfo_y())
            self._collapsed_geometry = current_geometry
            self._collapsed_topmost = bool(
                self.root.attributes("-topmost"))
            self.always_on_top.set(self._collapsed_topmost)
            self._remember_geometry()
            self.root.withdraw()
            self._show_floating_bar()
        except tk.TclError:
            # A bar creation failure must not strand the application hidden.
            self._destroy_floating_bar()
            try:
                self.root.deiconify()
            except tk.TclError:
                pass

    def restore_from_floating_bar(self, _event=None) -> None:
        """Restore geometry/topmost state and remove the floating bar."""
        try:
            screen = (self.root.winfo_screenwidth(),
                      self.root.winfo_screenheight())
            geometry = restore_window_geometry(
                getattr(self, "_collapsed_geometry", ""), screen)
            topmost = bool(getattr(self, "_collapsed_topmost", True))
            self._destroy_floating_bar()
            if geometry is not None:
                self.root.geometry(geometry)
                self._collapsed_geometry = geometry
            self.root.deiconify()
            self.always_on_top.set(topmost)
            self.root.attributes("-topmost", topmost)
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            self._floating_bar = None

    def _ready(self, pending: str | None = None) -> bool:
        """Start gate: binding + visual player identity + table-mode check.

        ``pending`` names the action to resume once a fresh check passes
        (``"follow"`` / ``"full_auto"``); when a check first has to run,
        this call returns False and the action completes asynchronously.
        """
        if pending is None:
            pending = getattr(self, "_pending_action", None)
        if not self._is_bound():
            if getattr(self, "_binding", None) is None:
                self.status.set(
                    "尚未连接雷电：确认设备编号并点顶部“连接并绑定”。")
                self._set_operational_status(
                    "未绑定", "开始跟随前先完成绑定", level="wait")
            else:
                reason = self._binding_mismatch_reason()
                self.status.set(
                    f"绑定已失效（{reason}）；点“绑定”更新后再开始跟随。")
                self._set_operational_status(
                    "未绑定", f"绑定已失效：{reason}", level="wait")
            self._refresh_lamp()
            return False
        if not self._identity_gate_ok():
            state = getattr(self, "_identity_state", "unchecked")
            fresh = getattr(self, "_identity_key", None) == self._identity_context()
            if state == "checking" and fresh:
                if pending:
                    self._identity_pending = pending
                self.status.set("正在核对当前画面中的本人昵称…")
                return False
            self._begin_identity_check(pending=pending)
            return False
        if self._mode_gate_ok():
            try:
                self._ensure_recorder()
            except ValueError as exc:
                self.status.set(str(exc))
                self._set_operational_status("已停手", str(exc), level="error")
                return False
            self._refresh_identity()
            return True
        state = getattr(self, "_check_state", "unchecked")
        try:
            fresh = getattr(self, "_check_mode_key", None) == self._mode_key()
        except Exception:
            fresh = False
        if state == "mismatch" and fresh:
            self.status.set(
                "所选玩法与牌桌不一致，已阻止开始；请改选玩法或换房后重试。")
            return False
        if state == "checking" and fresh:
            if pending:
                self._check_pending = pending
            self.status.set("正在自动核对牌桌玩法…")
            return False
        self._begin_mode_check(pending=pending)
        return False

    def _set_operational_status(self, phase: str, problem: str,
                                *, level: str = "wait") -> None:
        """Update the fixed identity/stage/problem strip with semantic color."""
        if not all(hasattr(self, name) for name in (
                "bound_status", "phase_status", "problem_status")):
            return
        self.bound_status.set(self._bound_text())
        self.phase_status.set(f"阶段：{phase}")
        self.problem_status.set(f"状态：{problem}")
        colors = {"ok": "#146c2e", "wait": "#a55700", "error": "#c62828"}
        color = colors.get(level, colors["wait"])
        for widget in (getattr(self, "phase_label", None),
                       getattr(self, "problem_label", None)):
            if widget is not None:
                widget.configure(fg=color)

    def _context(self) -> tuple:
        caishen = getattr(self, "caishen", None)
        manual = getattr(self, "manual_caishen", None)
        caishen_value = ""
        if caishen and manual and getattr(manual, "get", lambda: False)():
            try:
                caishen_value = caishen.get()
            except Exception:
                caishen_value = ""
        account = getattr(self, "account", None)
        room_id = getattr(self, "room_id", None)
        serial = getattr(self, "serial", None)
        mode = getattr(self, "mode", None)
        gate_ok = False
        gate = getattr(self, "_mode_gate_ok", None)
        if callable(gate):
            try:
                gate_ok = bool(gate())
            except Exception:
                gate_ok = False
        return (mode.get() if mode else "温州茶 · 2人", caishen_value,
                serial.get().strip() if serial else "",
                gate_ok,
                account.get().strip() if account else "",
                room_id.get().strip() if room_id else "")

    def _ensure_recorder(self) -> AssistRecorder:
        account = self.account.get().strip()
        room_id = self.room_id.get().strip() or None
        if self.recorder is None:
            self.recorder = AssistRecorder(
                self.session_dir / "assist_record.jsonl", account=account,
                room_id=room_id, round_no=self.round_no,
            )
            last = self._last_account_file()
            last.parent.mkdir(parents=True, exist_ok=True)
            last.write_text(account, encoding="utf-8")
        elif account != self.recorder.account:
            raise ValueError("本次记录会话账号已锁定；更换账号请重新开窗。")
        else:
            self.recorder.set_room_id(room_id)
        return self.recorder

    def next_round(self) -> None:
        """切局不再停手：作废上一局证据并继续跟随，新局财神由画面自动重读。

        2026-10-24 BOSS 指令：无论点本窗口的"下一局"还是游戏内的"下一局"，
        局一变必须立即重读新局财神，不允许停在旧帧等手选。画面侧换局由
        `_accept_continuous_result` 的 HUD 指纹/财字小牌双路硬证据接管；
        这里只负责作废旧证据、推进记录器、保持跟随。
        """
        was_following = bool(self.continuous)
        # 只作废局证据，不调用 stop()：保持 timer/跟随状态，下一帧自动重建基线。
        self.continuous_round = None
        self.continuous_caishen = None
        self._forget_manual_caishen()
        if self.recorder is not None:
            self.round_no = self.recorder.next_round()
        else:
            self.round_no += 1
        self.round_text.set(f"第 {self.round_no} 局")
        self.tracker = LatencyTracker()
        self.latency_log = []
        self._reset_round_display(f"记录器已切换到第 {self.round_no} 局")
        self._autostart_done = True
        if was_following:
            self.status.set(
                f"第 {self.round_no} 局：继续跟随，正在读取新局财神；旧建议已作废。")
            self.live_info.set(
                "新一局开始：下一帧重建基线，财字小牌读出来之前不出建议。")
        else:
            self.status.set(
                f"第 {self.round_no} 局：当前未在跟随；点开始跟随即自动读取本局财神。")

    def _poll_recorder(self) -> None:
        self.recorder_timer = None
        if self.recorder is not None:
            record = self.recorder.tick()
            if record is not None:
                self.live_info.set(f"决策 {record['decision_id']} 已 timeout；只读不点击。")
        self._maybe_autostart()
        try:
            self._poll_full_auto_control()
        except Exception as exc:  # noqa: BLE001 - 控制文件异常不影响跟随
            self.live_info.set(f"全自动控制文件处理失败：{exc}")
        self.recorder_timer = self.root.after(500, self._poll_recorder)

    # -- 局间「下一局」与全自动续接 -----------------------------------------
    @staticmethod
    def _is_round_end_stop(summary: dict) -> bool:
        gate = str(summary.get("stop_gate") or "")
        reason = str(summary.get("stop_reason") or "")
        return gate == "gate_turn" and ("结算" in reason or "settled" in reason)

    def _begin_round_transition(self, summary: dict) -> None:
        """全自动因局终结算停手后：后台单次执行「下一局」，结果回 UI 线程。"""
        if getattr(self, "_round_transition_future", None) is not None:
            return
        from round_transition import RoundTransitionExecutor
        from lobby_follow import _capture_image, _tap_lobby
        serial = self.serial.get().strip()
        session_dir = Path(self.session_dir)
        executor = getattr(self, "_round_transition", None)
        if executor is None or getattr(executor, "_serial", None) != serial:
            executor = RoundTransitionExecutor(
                capture=lambda: _capture_image(serial, get_adb_path(), session_dir),
                tap=lambda x, y: _tap_lobby(serial, get_adb_path(), x, y),
                report_dir=session_dir)
            executor._serial = serial
            self._round_transition = executor
        round_no = int(getattr(self, "round_no", 0) or 0) or None
        self.full_auto_status.set("本局结束：正在核验局间页「下一局」按钮（单次点击）。")
        self._round_transition_future = self.auto_executor.submit(
            executor.attempt, round_no=round_no)
        self.root.after(200, self._poll_round_transition)

    def _poll_round_transition(self) -> None:
        future = getattr(self, "_round_transition_future", None)
        if future is None:
            return
        if not future.done():
            self.root.after(200, self._poll_round_transition)
            return
        self._round_transition_future = None
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            self._full_auto_wanted = False
            self.full_auto_status.set(f"下一局执行异常，已停手：{exc}")
            return
        self._on_round_transition_result(result)

    def _on_round_transition_result(self, result) -> None:
        status = getattr(result, "status", "")
        reason = getattr(result, "reason", "")
        if status in ("advanced", "advanced_without_click"):
            self.next_round()
            self._auto_resume_pending = True
            self.full_auto_status.set(f"{reason}；新局重读财神与手牌后自动恢复全自动。")
            if not self.continuous:
                self.start()
            return
        self._full_auto_wanted = False
        self._auto_resume_pending = False
        self.full_auto_status.set(f"本房 BOT 已停止：{reason}（报告见 round_transition.jsonl）")
        self._set_operational_status("已停手", reason, level="wait")

    def _poll_full_auto_control(self) -> None:
        from full_auto_control import consume_command
        command = consume_command(self.serial.get().strip())
        if command == "stop" and getattr(self, "full_auto_running", False):
            self._stop_full_auto()
        elif command == "resume" and not getattr(self, "full_auto_running", False):
            self._full_auto_wanted = True
            self._auto_resume_pending = True
            self.full_auto_status.set("收到控制文件 resume：门禁通过后恢复全自动。")
            if not self.continuous:
                self.start()
        if (getattr(self, "_auto_resume_pending", False)
                and not getattr(self, "full_auto_running", False)
                and self.continuous
                and getattr(self, "continuous_caishen", None) is not None):
            self._auto_resume_pending = False
            self._start_full_auto()

    def _maybe_autostart(self) -> None:
        """Do not auto-start: recommendation and autoplay need explicit clicks."""
        return

    def _record_result(self, result: dict) -> dict | None:
        recorder = self._ensure_recorder()
        observation = dict(result)
        observation["mode"] = result.get("mode") or self._mode_key()
        record = recorder.observe(observation)
        if record is not None:
            result["record"] = str(recorder.output_path)
            self.live_info.set(
                f"已记录 {record['decision_id']}（{record['status']}）；只读不点击。")
        elif self.recorder is not None and self.recorder.output_path.is_file():
            result.setdefault("record", str(self.recorder.output_path))
        return record

    # -- continuous refresh ---------------------------------------------
    def _toggle(self) -> None:
        if getattr(self, "full_auto_running", False):
            self._stop_full_auto()
            return
        if self.continuous or getattr(self, "_lobby_active", False):
            self.stop()
        else:
            self.start()

    def _begin_lobby_follow(self) -> None:
        """Guarded tea-2p seating/start; never recharge/wash/cut/leave."""
        self._lobby_active = True
        self._lobby_in_game_verified = False
        self._lobby_room_cards = None
        self._lobby_card_status = "unknown"
        self._lobby_future = None
        self.start_button.configure(text="停止找位")
        self.status.set(
            "正在查找温州茶2人空座并自动入座；凑齐且身份/玩法核验通过后自动开始游戏。")
        self._set_operational_status(
            "自动找位", "只允许已复核的空座、规则关闭和开始游戏；绝不充值", level="wait")
        self._schedule_lobby_probe(0)

    def _schedule_lobby_probe(self, delay_ms: int) -> None:
        if getattr(self, "_lobby_active", False):
            self._lobby_timer = self.root.after(delay_ms, self._poll_lobby_probe)

    def _poll_lobby_probe(self) -> None:
        self._lobby_timer = None
        if not getattr(self, "_lobby_active", False):
            return
        if self._lobby_future is None:
            from lobby_follow import advance
            self._lobby_future = self.executor.submit(
                advance, self.serial.get().strip(), get_adb_path(),
                self.session_dir,
                allow_start=True,
                expected_account=self.account.get().strip(),
                room_cards_snapshot=getattr(self, "_lobby_room_cards", None))
        if not self._lobby_future.done():
            self._schedule_lobby_probe(120)
            return
        future, self._lobby_future = self._lobby_future, None
        try:
            progress = future.result()
        except Exception as exc:
            self._lobby_active = False
            self.start_button.configure(text="开始跟随")
            self.status.set(f"自动找位失败：{exc}；已停止后续点击。")
            self._set_operational_status("自动找位失败", str(exc), level="error")
            return
        if progress.room_cards is not None:
            self._lobby_room_cards = progress.room_cards
            self._lobby_card_status = progress.room_card_status
        if progress.state == "in_game":
            self._lobby_active = False
            self._lobby_in_game_verified = True
            self.start_button.configure(text="开始跟随")
            self.status.set("已自动进入牌局，转交现有实时跟随识牌流程。")
            self.start()
            return
        self.status.set(progress.message)
        level = "error" if progress.terminal else "ok" if progress.seated else "wait"
        self._set_operational_status("大厅/等候室", progress.message, level=level)
        # Any post-click uncertainty is terminal: never blindly retry.
        if progress.terminal:
            self._lobby_active = False
            self.start_button.configure(text="开始跟随")
            return
        if progress.state not in ("lobby", "waiting_room"):
            self._lobby_active = False
            self.start_button.configure(text="开始跟随")
            return
        self._schedule_lobby_probe(1200)

    def _stop_lobby_follow(self) -> None:
        self._lobby_active = False
        self._lobby_in_game_verified = False
        self._lobby_future = None
        timer = self._lobby_timer
        self._lobby_timer = None
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass

    def start(self) -> None:
        if self.continuous or getattr(self, "video_future", None) is not None:
            return
        if getattr(self, "_lobby_active", False):
            return
        if (getattr(self, "_lobby_bootstrap_enabled", False)
                and self._mode_key() == "tea_2p"
                and not getattr(self, "_lobby_in_game_verified", False)):
            if not self._is_bound():
                self.status.set("先绑定当前账号与雷电设备，再开始跟随。")
                return
            self._begin_lobby_follow()
            return
        self._pending_action = "follow"
        try:
            ready = self._ready()
        finally:
            self._pending_action = None
        if not ready:
            return
        # 清空轮次锁，重新开始本轮观察
        self.continuous_round = None
        self.continuous_caishen = None
        self._last_capture_at = None
        self._last_render_done_at = None
        # Fail loud BEFORE arming continuous: every live follow must own jsonl.
        try:
            attach = getattr(self, "_attach_follow_pipeline", None)
            if not callable(attach):
                raise RuntimeError(
                    "本窗口缺少跟随管线 _attach_follow_pipeline；"
                    "请关闭助手后重新打开以加载新代码"
                )
            attach()
            pipeline = getattr(self, "_follow_pipeline", None)
            log_path = getattr(pipeline, "follow_log_path", None) if pipeline else None
            if log_path is None or not Path(log_path).is_file():
                raise RuntimeError(
                    f"follow_session.jsonl 未创建：{log_path}"
                )
        except Exception as exc:
            self._follow_pipeline = None
            self.continuous = False
            msg = f"无法开始跟随：{exc}"
            if getattr(self, "status", None) is not None:
                self.status.set(msg)
            if getattr(self, "live_info", None) is not None:
                self.live_info.set(msg)
            return
        self.continuous = True
        if getattr(self, "start_button", None) is not None:
            self.start_button.configure(text="停止跟随")
        style = getattr(self, "_style_start_button", None)
        if callable(style):
            style(True)
        hud = getattr(self, "_set_hud_expanded", None)
        if callable(hud):
            hud(True)
        layout = getattr(self, "_apply_preview_layout", None)
        if callable(layout):
            layout(True)
        refresh = getattr(self, "_refresh_binding_views", None)
        if callable(refresh):
            refresh()
        self.next_run = time.perf_counter()
        log_path = self._follow_pipeline.follow_log_path
        self.status.set(
            f"实时跟随已启动；日志 {Path(log_path).name}；界面按实际处理周期刷新。"
        )
        self.live_info.set(
            f"实时跟随中：只读画面，不会代点；已写入 {log_path}"
        )
        full_auto_status = getattr(self, "full_auto_status", None)
        if full_auto_status is not None:
            full_auto_status.set(
                "推荐跟随已启动；确认识牌和建议正常后，可开启全自动代打。")
        set_operational = getattr(self, "_set_operational_status", None)
        if callable(set_operational):
            set_operational(
                "实时推荐", "正常观察中；尚未授权 BOT 点击", level="ok")
        title = getattr(self, "preview_title", None)
        if title is not None:
            title.set("实时跟随 · 等待当前画面")
        flags = getattr(self, "preview_flags", None)
        if flags is not None and getattr(self, "display_source", None) in OFFLINE_SOURCES:
            flags.set("左侧仍是旧截图，仅作对照；等待第一张实时画面。")
        self._schedule(0)

    def _append_stop_record(self) -> None:
        """Append one ``event="stop"`` row to ``follow_session.jsonl``.

        A follow run blocked by the start gate used to stop without writing
        anything, so a session holding only the header and 0 frames gave no
        clue afterwards which gate (binding / identity / mode / recorder)
        blocked it. The reason is whatever the status bar says right now —
        ``_start_frame`` stashes it there before calling ``stop()``.

        ``SessionLogger.append`` normalises the row through
        ``build_follow_record``, which keeps only its own fixed field set:
        the stop marker must be ``event="stop"`` (top-level ``kind`` is not
        carried over — only the session header has one), and ``ts`` lands
        in ``at``. The reason survives as ``vision.reason``, which is
        exactly where ``auto_review._screen_stops`` looks for it.
        """
        pipeline = getattr(self, "_follow_pipeline", None)
        logger = getattr(pipeline, "logger", None)
        if logger is None or not getattr(logger, "enabled", False):
            return
        reason = str(getattr(self, "_last_stop_reason", "") or "")
        self._last_stop_reason = ""
        if not reason and getattr(self, "status", None) is not None:
            try:
                reason = str(self.status.get())
            except Exception:
                reason = ""
        try:
            logger.append({
                "event": "stop", "kind": "stop", "reason": reason,
                "ts": datetime.now().astimezone().isoformat(),
            })
        except Exception:
            # 台账是证据，不能反过来阻断停止流程。
            pass

    def stop(self) -> None:
        if getattr(self, "_lobby_active", False):
            self._stop_lobby_follow()
        self._lobby_in_game_verified = False
        if getattr(self, "full_auto_running", False):
            self._stop_full_auto()
        follow_session = None
        was_continuous = bool(self.continuous)
        if self.continuous:
            resolve_dir = getattr(self, "_active_follow_session_dir", None)
            if callable(resolve_dir):
                follow_session = resolve_dir()
        stopped_live = (
            self.continuous and getattr(self, "display_source", None) == "live")
        # 停止原因必须先落台账：跟随被门禁拦下时，这一行是事后唯一的复盘证据。
        if was_continuous:
            self._append_stop_record()
        self.continuous = False
        self.continuous_round = None
        self.continuous_caishen = None
        if (not getattr(self, "full_auto_running", False)
                and not getattr(self, "_handoff_to_auto", False)):
            reset_identity = getattr(self, "_reset_identity_check", None)
            if callable(reset_identity):
                reset_identity()
        # Keep follow_session.jsonl; only drop the live writer handle.
        self._follow_pipeline = None
        if getattr(self, "start_button", None) is not None:
            self.start_button.configure(text="开始跟随")
        if getattr(self, "display_source", None) in OFFLINE_SOURCES:
            self.live_info.set("离线预检已停止；结果为旧截图，不能用于当前对局。")
        else:
            self.live_info.set(
                "连续观察已停止；上方是最后一帧，可能已过期，不能用于当前出牌。")
        if stopped_live:
            self._invalidate_live_advice()
        set_operational = getattr(self, "_set_operational_status", None)
        if (not getattr(self, "full_auto_running", False)
                and callable(set_operational)):
            set_operational(
                "已停止", "没有自动点击；最后一帧已作废", level="wait")
        if not getattr(self, "full_auto_running", False):
            style = getattr(self, "_style_start_button", None)
            if callable(style):
                style(False)
            hud = getattr(self, "_set_hud_expanded", None)
            if callable(hud):
                hud(False)
            layout = getattr(self, "_apply_preview_layout", None)
            if callable(layout):
                layout(False)
            refresh = getattr(self, "_refresh_binding_views", None)
            if callable(refresh):
                refresh()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if follow_session is not None:
            schedule = getattr(self, "_schedule_session_review", None)
            if callable(schedule):
                schedule(follow_session)

    def _schedule(self, delay_ms: int) -> None:
        if not self.continuous:
            return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.next_run = time.perf_counter() + delay_ms / 1000
        self.timer = self.root.after(max(0, int(delay_ms)), self._fire)

    def _fire(self) -> None:
        self.timer = None
        if not self.continuous or self.future is not None:
            return
        if getattr(self, "_tap_pending", False):
            # 单次代点正在验证期间暂停抓帧，避免与 ADB 截图互相穿插。
            self._schedule(300)
            return
        self._start_frame(record=True)

    def _start_frame(self, *, record: bool) -> None:
        if not self._ready():
            # 门禁原因只写在状态栏；先存下来，stop() 会把它写进 follow_session.jsonl。
            self._last_stop_reason = self.status.get()
            self.stop()
            return
        mode = self._mode_key()
        self.future_context = self._context()
        caishen_label, manual = self._caishen_selection()
        self.future_started = time.perf_counter()
        self.future = self.executor.submit(
            self._process_frame, mode,
            self.serial.get().strip(), self.session_dir, record,
            caishen_label, manual,
        )
        self.root.after(40, self._poll)

    def _caishen_selection(self) -> tuple[str, bool]:
        """Manual pick only applies when the user enabled it."""
        try:
            if not self.manual_caishen.get():
                return "", False
            selected = self.caishen.get()
        except Exception:
            return "", False
        label = TILE_CHOICES.get(selected, selected)
        if label and label not in TILE_CHOICES.values():
            return "", False
        return label, bool(label)

    def _process_frame(self, mode: str, serial: str, session_dir: Path,
                       record: bool, caishen_label: str = "",
                       manual_caishen: bool = False) -> dict:
        """Worker: capture, then recognize + advise, each timed separately."""
        folder = session_folder(session_dir, mode)
        started = time.perf_counter()
        capture = getattr(self, "_capture", None)
        if capture is None or capture.serial != serial:
            capture = LiveCapture(get_adb_path(), serial)
            self._capture = capture
        raw = capture.grab()
        capture_ms = (time.perf_counter() - started) * 1000
        path = save_frame(folder, raw)
        try:
            result, timing = capture_result(
                raw, mode=mode, image_path=path, log_dir=folder / "logs",
                folder=str(path), caishen_label=caishen_label,
                manual_caishen=manual_caishen,
            )
        except Exception as exc:
            event = build_frame_event(raw, path, {"status": "error", "reason": str(exc)},
                                      mode=mode)
            append_frame_event(session_dir, event)
            raise
        result["frame_path"] = str(path)
        result["record_frame"] = record
        event = build_frame_event(raw, path, result, mode=mode)
        result["audit_event"] = event
        result["audit_path"] = str(append_frame_event(session_dir, event))
        return {"result": result, "capture_started_at": started,
                "timing": FrameTiming(
            capture_ms=capture_ms, recognize_ms=timing.recognize_ms,
            advise_ms=timing.advise_ms,
            cycle_ms=(time.perf_counter() - started) * 1000,
            folder=timing.folder,
        )}

    def _poll(self) -> None:
        if self.future is None:
            return
        if not self.future.done():
            self.root.after(40, self._poll)
            return
        future, self.future = self.future, None
        try:
            outcome = future.result()
        except Exception as exc:  # noqa: BLE001 - report, never crash the window
            self.status.set(f"采集停止：{exc}")
            self.refresh.mark_stale(time.perf_counter(), reason=f"采集失败：{exc}")
            self._apply_visibility()
            if self.continuous:
                self._schedule(max(1000, self.tracker.interval_ms))
            return
        current_context = self._context()
        if self.future_context != current_context:
            # A manual caishen pick changes only context item 1. Discard the
            # in-flight result and rescan without stopping the live follower.
            previous = self.future_context
            if (self.continuous and isinstance(previous, tuple)
                    and len(previous) == len(current_context)
                    and previous[:1] == current_context[:1]
                    and previous[2:] == current_context[2:]):
                self._schedule(0)
                return
            self.stop()
            self._reset_round_display("设置已变化，请重新开始观察")
            self.status.set("设置已变化；旧结果已保存，请按当前设置重新开始。")
            return
        self._on_frame(outcome["result"], outcome["timing"],
                       capture_started_at=outcome["capture_started_at"])
        if outcome["result"].get("source") in {"offline_preflight", "imported_image"}:
            result = outcome["result"]
            preflight = result.get("source") == "offline_preflight"
            label = "离线预检（旧截图）" if preflight else "导入截图（非实时）"
            self.status.set(
                f"{label}：{result.get('status')}，识牌证据 "
                f"{len(result.get('vision_diagnostics') or [])} 处；"
                "仅用于检查识牌与建议链路，不能用于当前对局，也不代表识牌已核准。")

    def _on_frame(self, result: dict, timing: FrameTiming, *,
                  capture_started_at: float) -> None:
        """Display path: render this frame immediately, then feed the recorder.

        The status bar reports the real capture-to-capture period: the time
        from this frame's capture start to the previous frame's capture start,
        which includes the poll wait and the previous frame's Tk render.
        """
        render_started = time.perf_counter()
        current_capture_at = capture_started_at
        previous_capture_at = getattr(self, "_last_capture_at", None)
        measured_period = (
            (current_capture_at - previous_capture_at) * 1000.0
            if previous_capture_at is not None else 0.0
        )
        self._last_capture_at = current_capture_at
        source = self._result_source(result)
        self.display_source = source
        if source in OFFLINE_SOURCES:
            # Offline evidence may render in the live panel but must never seed
            # the continuous-round lock or be recorded as a live decision.
            self._show(result, render_started)
            accepted = False
        else:
            accepted = self._accept_continuous_result(result)
            if accepted:
                self._show(result, render_started)
            else:
                self._render_live_frame(result)
        self.root.after_idle(lambda: self._finish_frame(
            result, timing, current_capture_at, render_started,
            measured_period, accepted))

    def _finish_frame(self, result: dict, timing: FrameTiming,
                      capture_started_at: float, render_started: float,
                      measured_period: float, accepted: bool) -> None:
        """Tk has processed the pending draw before latency is recorded."""
        self.root.update_idletasks()
        render_done = time.perf_counter()
        capture_to_render_ms = max(0.0, (render_done - capture_started_at) * 1000)
        previous_render_done = getattr(self, "_last_render_done_at", None)
        poll_wait_ms = (max(0.0, (capture_started_at - previous_render_done) * 1000)
                        if previous_render_done is not None else 0.0)
        self._last_render_done_at = render_done
        measured = replace(timing, cycle_ms=capture_to_render_ms,
                           capture_to_render_ms=capture_to_render_ms,
                           refresh_ms=poll_wait_ms + capture_to_render_ms)
        interval = self.tracker.record(
            measured, period_ms=measured_period if measured_period > 0 else None)
        self.latency_log.append({
            "at": datetime.now().astimezone().isoformat(), **measured.as_dict(),
            "interval_ms": interval, "period_ms": round(measured_period, 1),
            "poll_wait_ms": round(poll_wait_ms, 1),
            "poll_to_render_ms": round((render_done - capture_started_at)
                                       * 1000.0, 1),
            "render_ms": round((render_done - render_started) * 1000.0, 1),
            "status": result.get("status"),
        })
        self._update_latency_bar()
        if accepted and result.get("record_frame") and self.recorder is not None:
            self._record_result(result)
        if self.continuous:
            self._schedule(interval)

    def _show(self, result: dict, now: float) -> None:
        self.display_source = self._result_source(result)
        offline = self.display_source in OFFLINE_SOURCES
        # Codex must-fix: FollowPipeline.observe / gates FIRST, then refresh + render.
        # Missing decision facts must clear advise before the main UI can show "打 X".
        observe = getattr(self, "_observe_follow_frame", None)
        if callable(observe):
            import types as _types
            pre = _types.SimpleNamespace(
                status=result.get("status"),
                actionable=(
                    str(result.get("status") or "") == "advise"
                    and result.get("suggestion") is not None
                ),
            )
            try:
                observe(result, pre)
            except Exception as exc:
                # Fail-closed on realtime pipeline errors: never keep a stale advise.
                result["status"] = "abstain"
                result["suggestion"] = None
                result["suggestion_id"] = None
                result["reason"] = f"实时管线异常：{exc}"
                result["follow_error"] = str(exc)
                result["follow_abstain"] = True
                result["follow_display"] = None
        state = self.refresh.update(result, now=now)
        self.current = result
        render_preview = getattr(self, "_render_live_frame", None)
        if render_preview is not None:
            render_preview(result)
        hand_text = "手牌：" + (" ".join(state.hand_labels) or "未完整识别")
        kong_text = stacked_kong_summary(result)
        if kong_text:
            hand_text += f"；已识别明牌：{kong_text}"
        self.hand.set(hand_text)
        if offline:
            self.suggestion.set(state.title)
            self.reason.set(
                f"{OFFLINE_NOTICES.get(self.display_source, '')}；"
                "下方明细仅供检查识牌链路。")
        else:
            self.suggestion.set(
                f"打 {state.suggestion}" if state.actionable and state.status == "advise"
                else state.title)
            self.reason.set(state.reason or self._advice_facts(state))
        set_notice = getattr(self, "_set_offline_notice", None)
        if set_notice is not None:
            set_notice()
        self.caishen_title.set(self._caishen_title(state))
        update_hud = getattr(self, "_update_follow_hud", None)
        if callable(update_hud):
            update_hud(result, state)
        self._update_latency_bar()
        if getattr(self, "full_auto_running", False):
            phase = "全自动代打"
        else:
            phase = "实时推荐"
        set_operational = getattr(self, "_set_operational_status", None)
        if callable(set_operational):
            if result.get("status") == "advise" and state.actionable:
                set_operational(
                    phase, f"建议：打 {state.suggestion}", level="ok")
            elif result.get("status") in {"error", "stopped"}:
                set_operational(
                    "安全停手", str(result.get("reason") or "识别/管线异常"),
                    level="error")
            else:
                set_operational(
                    phase, str(result.get("reason") or state.reason or "等待稳定证据"),
                    level="wait")
        self._refresh_view(state)

    def _render_live_frame(self, result: dict) -> None:
        """Keep the latest captured evidence in the primary window, even on abstain."""
        event = result.get("audit_event")
        picture = getattr(self, "picture", None)
        if event is None or picture is None:
            return
        source = event.get("source")
        self.display_source = source
        title = {"live": "实时跟随 · 当前截图",
                 "imported_image": "导入截图 · 非实时",
                 "offline_preflight": "离线预检 · 旧截图",
                 "mp4_1fps": "录像跟随 · 抽取帧"}.get(source, "识牌画面")
        self.preview_title.set(title)
        flags = [FLAG_NAMES.get(flag, flag) for flag in event.get("flags") or []]
        if source in OFFLINE_SOURCES:
            self.preview_flags.set(OFFLINE_NOTICES.get(source, "非实时画面"))
        elif flags:
            self.preview_flags.set("需复核：" + "、".join(flags))
        else:
            self.preview_flags.set("自动疑点：无；识牌结果仍需人工核对。")
        self.preview_meta.set(
            f"{event.get('mode', '')} · {event.get('at', '')} · "
            f"{len(event.get('diagnostics') or [])} 个识牌区域"
            " · 牌框：绿=预测，橙=未识别，红=当前建议")
        set_notice = getattr(self, "_set_offline_notice", None)
        if set_notice is not None:
            set_notice()
        try:
            with Image.open(event["frame"]) as original:
                image = draw_recognition_boxes(original, event)
        except OSError as exc:
            picture.configure(image="", text=f"画面无法读取：{exc}")
            return
        panel = getattr(self, "preview_panel", None)
        width = max(400, (panel.winfo_width() if panel is not None else 540) - 16)
        height = max(300, (panel.winfo_height() if panel is not None else 550) - 100)
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.images["preview"] = self._to_tk(image)
        picture.configure(image=self.images["preview"], text="")

    def _attach_follow_pipeline(self) -> None:
        """Bind SessionLogger/follow_session.jsonl for this 开始跟随 run."""
        from follow_enrich import (
            WALL_CARD_READER_CONNECTED,
            river_read_enabled,
        )
        from follow_pipeline import FollowPipeline

        mode = self._mode_key() if hasattr(self, "_mode_key") else None
        session_dir = Path(self.session_dir)
        # 余牌: no reliable assist card_reader yet — leave unconnected (未识别).
        # 牌河: only tea_2p is calibrated.
        pipeline = FollowPipeline(
            session_dir,
            mode=mode,
            enabled=True,
            card_reader=None,  # WALL_CARD_READER_CONNECTED=False
            read_river=river_read_enabled(mode),
            read_room=True,
            read_claims_display=True,
        )
        assert pipeline.card_reader is None
        assert pipeline.wall_card_reader_connected is False
        assert WALL_CARD_READER_CONNECTED is False
        log_path = pipeline.start({
            "account": (self.account.get().strip()
                        if getattr(self, "account", None) is not None else ""),
            "room_id": (self.room_id.get().strip()
                        if getattr(self, "room_id", None) is not None else ""),
            "round_no": getattr(self, "round_no", None),
            "serial": (self.serial.get().strip()
                       if getattr(self, "serial", None) is not None else ""),
        })
        if log_path is None or not Path(log_path).is_file():
            from session_logger import FollowSessionLogError
            raise FollowSessionLogError(
                f"开始跟随未能创建 follow_session.jsonl：{log_path}"
            )
        self._follow_pipeline = pipeline
        # Point embedded / detached viewers at this session's jsonl.
        for attr in ("embedded_follow_log", "follow_session_log"):
            viewer = getattr(self, attr, None)
            if viewer is None:
                continue
            try:
                viewer.path = pipeline.follow_log_path
                if hasattr(viewer, "refresh"):
                    viewer.refresh()
                elif hasattr(viewer, "_refresh"):
                    viewer._refresh()
            except Exception:
                pass

    def _observe_follow_frame(self, result: dict, state) -> dict | None:
        """TableState + safety abstain + follow_session.jsonl on one path."""
        from follow_pipeline import FollowPipeline
        from assist_hud import load_result_bgr

        pipeline = getattr(self, "_follow_pipeline", None)
        live = bool(getattr(self, "continuous", False))
        if pipeline is None and live:
            self._attach_follow_pipeline()
            pipeline = getattr(self, "_follow_pipeline", None)
        if live:
            if pipeline is None:
                from session_logger import FollowSessionLogError
                raise FollowSessionLogError(
                    "实时跟随中无 FollowPipeline；拒绝无 follow_session.jsonl 继续"
                )
            log_path = getattr(pipeline, "follow_log_path", None)
            if log_path is None or not Path(log_path).is_file():
                from session_logger import FollowSessionLogError
                raise FollowSessionLogError(
                    f"实时跟随中 follow_session.jsonl 缺失：{log_path}"
                )
        elif pipeline is None:
            # Offline / one-shot / stopped: HUD facts only, no jsonl writer.
            mode = self._mode_key() if hasattr(self, "_mode_key") else None
            session = getattr(self, "session_dir", None) or Path(".")
            from follow_enrich import river_read_enabled as _river_ok

            pipeline = FollowPipeline(
                Path(session),
                mode=mode,
                enabled=False,
                card_reader=None,
                read_river=_river_ok(mode),
            )
        image = load_result_bgr(result)
        room = ""
        try:
            room = self.room_id.get().strip() if getattr(self, "room_id", None) else ""
        except Exception:
            room = ""
        outcome = pipeline.observe(
            result,
            image,
            frame_path=result.get("frame_path") or result.get("image"),
            room_id=room or None,
            round_no=getattr(self, "round_no", None),
            round_seq=getattr(self, "round_no", None),
            wall_cards=result.get("wall_cards"),
            turn_status=getattr(state, "status", None) or result.get("status"),
            turn_actionable=bool(getattr(state, "actionable", False)),
        )
        result["table_state"] = outcome.table.ledger()
        result["frame_facts"] = outcome.facts.ledger()
        result["follow_display"] = outcome.display
        result["follow_abstain"] = bool(outcome.abstain)
        if outcome.abstain and result.get("status") == "advise":
            result["status"] = "abstain"
            result["reason"] = outcome.event.get("reason") or result.get("reason")
            result["suggestion"] = None
            result["suggestion_id"] = None
        return outcome.display

    def _update_follow_hud(self, result: dict, state) -> None:
        """Refresh unified read-only HUD; persist jsonl only while 开始跟随."""
        display = result.get("follow_display")
        live = bool(getattr(self, "continuous", False))
        pipeline_error = result.get("follow_error")
        if live and not isinstance(display, dict) and not pipeline_error:
            # Standalone HUD refresh (observe not yet run this frame).
            try:
                display = self._observe_follow_frame(result, state)
            except Exception as exc:
                pipeline_error = str(exc)
                result["follow_error"] = pipeline_error
                result["status"] = "abstain"
                result["suggestion"] = None
                result["suggestion_id"] = None
                result["reason"] = f"实时管线异常：{pipeline_error}"
                result["follow_abstain"] = True
                display = None
        if pipeline_error:
            # Fail-closed: clear main suggestion and surface the error reason.
            try:
                self.suggestion.set("识别中")
            except Exception:
                pass
            try:
                self.reason.set(f"实时管线异常：{pipeline_error}")
            except Exception:
                pass
            if not isinstance(display, dict):
                display = {
                    "mode": "玩法：未识别",
                    "room_round": "房号/局数：未识别",
                    "caishen": "财神：未识别",
                    "concealed": "暗手：未识别",
                    "melds": "副露：未识别",
                    "meld_types": "副露类型：未识别",
                    "wall_cards": "余牌：未识别",
                    "countdown": "倒计时：未识别",
                    "turn": f"当前行动：管线异常（{pipeline_error}）",
                    "river": "牌河：未识别",
                    "last_discard": "最近出牌：未识别",
                    "claim_buttons": "鸣牌按钮：未识别",
                }
        if not isinstance(display, dict):
            # Offline / one-shot / pipeline miss: light HUD only (no jsonl).
            from assist_hud import build_follow_hud
            from follow_pipeline import format_frame_facts_display, build_frame_facts
            from table_state import build_table_state
            # Offline: do not re-run countdown CV (keeps preflight snappy under suite load).
            image = None
            try:
                table = build_table_state(
                    result, None, wall_cards=result.get("wall_cards"))
                room = ""
                try:
                    room = self.room_id.get().strip() if getattr(self, "room_id", None) else ""
                except Exception:
                    room = ""
                facts = build_frame_facts(
                    result, table,
                    room_id=room or None,
                    round_no=getattr(self, "round_no", None),
                    turn_status=getattr(state, "status", None),
                    turn_actionable=bool(getattr(state, "actionable", False)),
                )
                display = format_frame_facts_display(facts)
                result["table_state"] = table.ledger()
                result["frame_facts"] = facts.ledger()
            except Exception:
                hud = build_follow_hud(
                    result,
                    image=image,
                    status=getattr(state, "status", None),
                    actionable=bool(getattr(state, "actionable", False)),
                    wall_cards=result.get("wall_cards"),
                    run_countdown=True,
                )
                display = {
                    "wall_cards": hud.wall,
                    "countdown": hud.countdown,
                    "turn": hud.turn,
                    "river": hud.river,
                }
        mapping = {
            "mode": "hud_mode",
            "room_round": "hud_room_round",
            "caishen": "hud_caishen",
            "concealed": "hud_concealed",
            "melds": "hud_melds",
            "meld_types": "hud_meld_types",
            "wall_cards": "hud_wall",
            "countdown": "hud_countdown",
            "turn": "hud_turn",
            "river": "hud_river",
            "last_discard": "hud_last_discard",
            "claim_buttons": "hud_claim_buttons",
        }
        for key, attr in mapping.items():
            var = getattr(self, attr, None)
            if var is None:
                continue
            value = display.get(key)
            if value:
                try:
                    var.set(value)
                except Exception:
                    pass
        facts = result.get("frame_facts")
        if isinstance(facts, dict):
            round_no = facts.get("round_no")
            round_total = facts.get("round_total")
        else:
            round_no = round_total = None
        if not isinstance(round_no, int):
            round_no = getattr(self, "round_no", None)
        try:
            mode_key = self._mode_key()
        except Exception:
            mode_key = ""
        if not isinstance(round_total, int) and mode_key == "tea_2p":
            round_total = 16
        summary = getattr(self, "match_summary", None)
        if summary is not None:
            summary.set(format_match_summary(round_no, round_total))
        # Keep embedded follow log scrolling only while 开始跟随 is live.
        if getattr(self, "continuous", False):
            emb = getattr(self, "embedded_follow_log", None)
            if emb is not None:
                try:
                    pipe = getattr(self, "_follow_pipeline", None)
                    if pipe is not None and pipe.follow_log_path is not None:
                        emb.path = pipe.follow_log_path
                    if hasattr(emb, "refresh"):
                        emb.refresh()
                    elif hasattr(emb, "_refresh"):
                        emb._refresh()
                except Exception:
                    pass

    def _refresh_view(self, state=None) -> None:
        state = state or self.refresh.state
        self._render_hand(state.hand_labels)
        self._render_advice(state)
        self._render_candidates(state.candidates if state.actionable else ())
        self._apply_visibility()

    def _caishen_title(self, state) -> str:
        if state.caishen_label:
            source = {"image": "画面", "manual": "手选", "rule": "规则"}.get(
                state.caishen_source, "规则")
            name = tile_name(TILE_IDS.get(state.caishen_label, 0))
            return f"财神：{name if name and not name.startswith('?') else state.caishen_label}（{source}）"
        return "财神：识别中"

    def _advice_facts(self, state) -> str:
        if not state.actionable:
            return ""
        if state.candidates:
            top = state.candidates[0]
            return (f"向听{top.get('shanten_after', '?')} · "
                    f"进张{top.get('ukeire_count', '?')}张")
        if state.shanten is not None:
            return f"向听{state.shanten}"
        return "依据现有 advisor 输出"

    def _apply_visibility(self) -> None:
        refresh = getattr(self, "refresh", None)
        if refresh is None or getattr(self, "candidate_face", None) is None:
            return
        state = refresh.state
        offline = getattr(self, "display_source", None) in OFFLINE_SOURCES
        actionable = state.actionable and not offline
        self.advice_label.configure(foreground="#146c2e" if actionable else "#9a9a9a")
        self.info_label.configure(foreground="#146c2e" if actionable else "#a55700")
        self._refresh_caishen_rows(show=self._mode_key().startswith("tea_"))

    def _refresh_caishen_rows(self, show: bool) -> None:
        """Keep the tea picker stable while live frames refresh."""
        widgets = getattr(self, "_caishen_widgets", [])
        if show and not widgets:
            row = ttk.Frame(self.caishen_row)
            row.pack(side="bottom", fill="x")
            ttk.Label(row, text="本局财神看不清时：").pack(side="left")
            self.caishen_pick_button = ttk.Button(
                row, text="选择财神牌", command=self._open_caishen_menu)
            self.caishen_pick_button.pack(side="left", padx=2)
            self._caishen_widgets = [row]
        elif not show and widgets:
            for widget in widgets:
                widget.destroy()
            self._caishen_widgets = []

    def _render_hand(self, labels) -> None:
        if not labels:
            self.hand_face.configure(image="")
            self.hand_panel.pack_forget()
            return
        key = ("hand", tuple(labels), self.refresh.state.suggestion_label)
        if key != self.last_render_key:
            self.images["hand"] = self._to_tk(render_hand(
                labels, suggestion_label=self.refresh.state.suggestion_label,
                caishen_label=self.refresh.state.caishen_label, max_width=420,
            ))
            self.last_render_key = key
            self.hand_face.configure(image=self.images["hand"])
        self.hand_panel.pack(fill="x", padx=6, before=self.advice_panel)

    def _render_advice(self, state=None) -> None:
        state = state or self.refresh.state
        self.images["advice"] = self._to_tk(render_advice(state, width=424))
        self.advice_face.configure(image=self.images["advice"])
        self._placeholder = self.images["advice"]

    def _render_candidates(self, candidates) -> None:
        self.images["candidates"] = self._to_tk(render_candidates(candidates))
        self.candidate_face.configure(image=self.images["candidates"])

    def _to_tk(self, image):
        return ImageTk.PhotoImage(image)

    def _update_latency_bar(self) -> None:
        summary = self.tracker.summary()
        if not self.latency_log:
            self.latency.set("最近一帧：—")
        else:
            last = self.latency_log[-1]
            self.latency.set(
                f"刷新上界 {last['total_ms']:.0f} ms "
                f"(抓{last['capture_ms']:.0f}/识{last['recognize_ms']:.0f}"
                f"/算{last['advise_ms']:.0f})")
        period_ms = self.tracker.last_period_ms
        self.period.set(f"实测周期 {period_ms:.0f} ms" if period_ms
                        else f"周期 {self.tracker.interval_ms} ms")
        if summary["total_p50"] is not None:
            self.latency.set(self.latency.get() + f" · p50 {summary['total_p50']:.0f} p95 {summary['total_p95']:.0f} ms")

    def _tick(self) -> None:
        self.ticker = None
        now = time.perf_counter()
        source = getattr(self, "display_source", None)
        if source in OFFLINE_SOURCES:
            self.fresh.set(OFFLINE_FRESH.get(source, "非实时画面"))
            self.refresh_label.configure(foreground="#a55700")
        elif self.refresh.last_frame_at is not None:
            age = now - self.refresh.last_frame_at
            self.fresh.set(f"{describe_age(age)} · {self.period.get()}")
            stale = age > STALE_SECONDS or not self.continuous
            self.refresh_label.configure(foreground="#c62828" if stale else "#555555")
            if stale:
                before = self.refresh.state
                state = self.refresh.mark_stale(now)
                if (before.actionable or before.candidates
                        or not state.actionable):
                    self.suggestion.set(state.title)
                    self.reason.set(state.reason or "")
                    self._refresh_view(state)
                if self.continuous:
                    self.fresh.set(f"⚠ {describe_age(age)}（超过 3 秒无新帧）")
                else:
                    self.fresh.set("已停止跟随 · 最后一帧不作为当前依据")
        else:
            self.fresh.set(describe_age(None))
        if getattr(self, "_floating_bar", None) is not None:
            # Only meaningful while the collapsed bar is on screen; this also
            # lets window test doubles that bind ``_tick`` alone skip it.
            self._refresh_floating_status()
        self.ticker = self.root.after(200, self._tick)

    # -- one-shot actions ------------------------------------------------
    def capture(self) -> None:
        if self.future is not None:
            return
        if self.continuous:
            self.stop()
        if not self._ready():
            return
        self.status.set("正在抓图、识牌与计算……")
        self._start_frame(record=True)

    def import_image(self) -> None:
        if self.future is not None:
            return
        if self.continuous:
            self.stop()
        record_decision = bool(self._mode_gate_ok() and self._is_bound())
        if record_decision and not self._ready():
            return
        selected = filedialog.askopenfilename(
            title="导入游戏截图", filetypes=[("截图", "*.png *.jpg *.jpeg")],
        )
        if not selected:
            return
        self.status.set("正在识别导入截图……")
        mode = self._mode_key()
        self.future_context = self._context()
        caishen_label, manual = self._caishen_selection()

        def work() -> dict:
            started = time.perf_counter()
            source = Path(selected)
            raw = source.read_bytes()
            read_done = time.perf_counter()
            folder = session_folder(self.session_dir, mode)
            copy = folder / (datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                             + "_" + source.suffix.lower())
            copy.write_bytes(raw)
            try:
                result, timing = capture_result(
                    raw, mode=mode, image_path=copy, log_dir=folder / "logs",
                    folder=str(copy), caishen_label=caishen_label,
                    manual_caishen=manual,
                )
            except Exception as exc:
                event = build_frame_event(raw, copy, {"status": "error", "reason": str(exc)},
                                          mode=mode, source="imported_image")
                append_frame_event(self.session_dir, event)
                raise
            result["frame_path"] = str(copy)
            result["record_frame"] = record_decision
            result["source"] = "imported_image"
            event = build_frame_event(raw, copy, result, mode=mode, source="imported_image")
            result["audit_event"] = event
            result["audit_path"] = str(append_frame_event(self.session_dir, event))
            return {"result": result, "capture_started_at": started,
                    "timing": type(timing)(
                capture_ms=(read_done - started) * 1000,
                recognize_ms=timing.recognize_ms, advise_ms=timing.advise_ms,
                cycle_ms=(time.perf_counter() - started) * 1000,
                folder=timing.folder,
            )}

        self.future = self.executor.submit(work)
        self.root.after(40, self._poll)

    def manual_correction(self) -> None:
        if self.future is not None:
            return
        if self.continuous:
            self.stop()
        if not self._ready():
            return
        labels = " ".join((self.current or {}).get("hand") or [])
        typed = simpledialog.askstring(
            "修正手牌", "按顺序输入牌名，空格分隔，例如 w1 t3 s9 hongzhong",
            initialvalue=labels, parent=self.root,
        )
        if typed is None:
            return
        previous = self.current or {}
        mode = self._mode_key()
        caishen_label = previous.get("caishen") or (
            TILE_CHOICES.get(self.caishen.get(), "") if self.manual_caishen.get() else "")
        if mode.startswith("tea_") and not caishen_label:
            self.status.set("先导入或抓取一张能看清本局财神的画面。")
            return
        self.status.set("正在按修正手牌重算……")
        self.future_context = self._context()
        two, four = (mode == "tea_2p"), (mode == "tea_4p")

        def work() -> dict:
            started = time.perf_counter()
            if mode.startswith("tea_"):
                result = analyze_manual_hand(
                    typed, caishen_label=caishen_label,
                    confirmed_two_player=two, confirmed_four_player=four,
                    session_dir=self.session_dir, image_path=previous.get("image"),
                    corrected_from=previous.get("record"), write_sidecar=False,
                )
            else:
                result = analyze_shu_manual_hand(
                    typed, confirmed_four_player=True,
                    meld_count=previous.get("meld_count", 0),
                    session_dir=self.session_dir, image_path=previous.get("image"),
                    corrected_from=previous.get("record"), write_sidecar=False,
                )
            result["mode"] = mode
            result["record_frame"] = False
            if result.get("suggestion"):
                result["suggestion_id"] = TILE_IDS.get(result["suggestion"])
            return {"result": result, "timing": None, "elapsed": time.perf_counter() - started}

        self.future = self.executor.submit(work)
        self.root.after(40, self._poll_manual)

    def _poll_manual(self) -> None:
        if self.future is None:
            return
        if not self.future.done():
            self.root.after(60, self._poll_manual)
            return
        future, self.future = self.future, None
        try:
            outcome = future.result()
        except Exception as exc:  # noqa: BLE001
            self.status.set(f"重算失败：{exc}")
            return
        result = outcome["result"]
        self._record_result(result)
        self._show(result, time.perf_counter())
        self.status.set(f"已按修正手牌重算（{outcome['elapsed']:.2f} 秒）。")

    # -- explicit full-auto hand-off -----------------------------------
    def _toggle_full_auto(self) -> None:
        if self.full_auto_running:
            self._stop_full_auto()
        else:
            self._start_full_auto()

    def _start_full_auto(self) -> None:
        """Start the whole lobby-to-game flow, then hand off to the BOT."""
        if not self._channel_ready():
            self.full_auto_status.set("真实动作通道未就绪；已拒绝接管。")
            return
        if self._mode_key() != "tea_2p":
            self.full_auto_status.set("全自动目前只开放温州茶 2 人；该玩法保持推荐模式。")
            self._set_operational_status(
                "推荐模式", "当前玩法未完成全自动实机验收", level="wait")
            return
        if not self.continuous:
            self._full_auto_wanted = True
            self._auto_resume_pending = True
            self.full_auto_status.set(
                "全自动已排队：先自动找位、坐下并开局；发牌后通过门禁即接管出牌。")
            self._set_operational_status(
                "自动开局", "正在启动开始跟随全流程", level="wait")
            if not getattr(self, "_lobby_active", False):
                self.start()
            return
        if not self._identity_gate_ok(max_age_seconds=10.0):
            self._begin_identity_check(pending="full_auto")
            self.full_auto_status.set(
                "全自动开启前正在重新核验当前画面本人昵称。")
            self._set_operational_status(
                "玩家核验", "重新确认当前账号后才交出点击权", level="wait")
            return
        if not self._ready():
            if getattr(self, "_identity_state", None) == "checking":
                self._identity_pending = "full_auto"
                self.full_auto_status.set(
                    "正在核对当前画面本人昵称；一致后才会开启全自动。")
                return
            if getattr(self, "_check_state", None) == "checking":
                self._check_pending = "full_auto"
                self.full_auto_status.set(
                    "正在自动核对牌桌玩法；核对一致后自动开启全自动。")
                return
            self._set_operational_status(
                "门禁拦截", self.status.get(), level="error")
            return
        if not self._mode_gate_ok():
            self._begin_mode_check(pending="full_auto")
            self.full_auto_status.set(
                "正在自动核对牌桌玩法；核对一致后才会开启全自动。")
            self._set_operational_status(
                "核对中", "全自动开启前自动核对玩法", level="wait")
            return

        account = self.account.get().strip()
        serial = self.serial.get().strip()
        safe_account = "".join(ch for ch in account if ch.isalnum())[:16] or "account"
        safe_serial = "".join(ch for ch in serial if ch.isalnum())[-12:] or "device"
        auto_session = (
            TEA_ROOT / "采集" / "dev_auto_play" /
            f"ui_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_serial}_{safe_account}"
        )

        # Stop the duplicate read-only ADB capture before the BOT takes over.
        # The BOT's own analyzed frames are streamed back through _auto_events.
        self._suppress_review = True
        self._handoff_to_auto = True
        try:
            self.stop()
        finally:
            self._handoff_to_auto = False
        from action_policy import recommended_runner_config
        from auto_play_loop import AutoPlayLoop

        self._auto_loop = AutoPlayLoop(
            enabled=True,
            serial=serial,
            adb_path=get_adb_path(),
            mode="tea_2p",
            caishen_label="",
            session_dir=auto_session,
            max_actions=5000,
            duration_s=12 * 60 * 60,
            interval_s=1.2,
            claim_auto_click=True,
            claim_policy=recommended_runner_config(
                auto_click_enabled=True, include_chi=True),
            event_callback=lambda event, analysis: self._auto_events.put(
                (event, analysis)),
        )
        self.session_dir = auto_session
        self.full_auto_running = True
        self._auto_future = self.auto_executor.submit(self._auto_loop.run)
        self.start_button.configure(text="停止全部")
        self.auto_button.configure(text="关闭全自动（恢复只读推荐）")
        self._style_start_button(True)
        self._set_hud_expanded(True)
        self._apply_preview_layout(True)
        gate_refresh = getattr(self, "_refresh_auto_gate", None)
        if callable(gate_refresh):
            gate_refresh(force=True)
        self.full_auto_status.set("全自动跟随中 · BOT 已接管识牌/决策/点击")
        self._full_auto_wanted = True
        self.live_info.set(
            "BOT 接管中；开局已由专用门禁完成；绝不点击结算、离桌、充值或分享。")
        self._set_operational_status(
            "全自动代打", "正在做启动门禁自检", level="wait")
        for attr in ("embedded_follow_log", "follow_session_log"):
            viewer = getattr(self, attr, None)
            if viewer is not None:
                viewer.session_dir = auto_session
                viewer.path = self._auto_loop.follow_log_path
                viewer._last_signature = None
        self._auto_poll_timer = self.root.after(150, self._poll_full_auto)

    def _stop_full_auto(self) -> None:
        loop = getattr(self, "_auto_loop", None)
        if loop is not None:
            loop.request_stop("用户在辅助器中关闭全自动代打")
        self._full_auto_wanted = False
        self._auto_resume_pending = False
        self.full_auto_status.set("正在安全停手：若有点击正在验证，会先完成验证再退出。")
        self._set_operational_status(
            "正在停手", "等待当前动作验证完成", level="wait")
        if getattr(self, "auto_button", None) is not None:
            self.auto_button.configure(state="disabled")

    def _poll_full_auto(self) -> None:
        self._auto_poll_timer = None
        while True:
            try:
                event, analysis = self._auto_events.get_nowait()
            except Empty:
                break
            try:
                self._on_auto_event(event, analysis)
            except Exception as exc:  # noqa: BLE001 - UI must not break BOT
                self._set_operational_status(
                    "界面异常", f"BOT 仍按台账运行；界面刷新失败：{exc}",
                    level="error")

        future = self._auto_future
        if future is not None and future.done():
            self._auto_future = None
            self.full_auto_running = False
            self._auto_loop = None
            self._reset_identity_check()
            ended_session = Path(self.session_dir)
            schedule = getattr(self, "_schedule_session_review", None)
            if callable(schedule):
                schedule(ended_session)
            self.start_button.configure(text="开始跟随")
            self._style_start_button(False)
            self._set_hud_expanded(False)
            self._apply_preview_layout(False)
            self.auto_button.configure(text="开启全自动代打")
            gate_refresh = getattr(self, "_refresh_auto_gate", None)
            if callable(gate_refresh):
                gate_refresh(force=True)
            refresh = getattr(self, "_refresh_binding_views", None)
            if callable(refresh):
                refresh()
            try:
                summary = future.result()
            except Exception as exc:  # noqa: BLE001 - surface and keep stopped
                self.full_auto_status.set(f"全自动启动/运行失败：{exc}")
                self._set_operational_status(
                    "已停手", f"全自动异常：{exc}", level="error")
                return
            reason = summary.get("stop_reason") or "运行已结束"
            user_stop = "用户" in reason
            if (not user_stop and getattr(self, "_full_auto_wanted", False)
                    and self._is_round_end_stop(summary)):
                self._begin_round_transition(summary)
            self.full_auto_status.set(
                f"全自动已停止；已验证动作 {summary.get('actions', 0)} 次。{reason}")
            self._set_operational_status(
                "已停手", reason, level="wait" if user_stop else "error")
            return
        if self.full_auto_running:
            self._auto_poll_timer = self.root.after(200, self._poll_full_auto)

    def _on_auto_event(self, event: dict, analysis: dict | None) -> None:
        """Render BOT evidence and update fixed stage/problem status."""
        kind = str(event.get("event") or "idle")
        reason = str(event.get("reason") or "")
        if event.get("round_seq"):
            self.round_no = int(event["round_seq"])
            self.round_text.set(f"第 {self.round_no} 局")
        if kind == "stop":
            level, phase = "error", "安全停手"
        elif kind == "error":
            level, phase = "error", "异常复核"
        elif kind in {"action", "invitation_declined"}:
            level, phase = "ok", "动作已验证"
        elif kind == "round_reset":
            level, phase = "wait", "换局重建"
        else:
            level, phase = "wait", "观察/弃权"
        self._set_operational_status(
            phase, reason or f"事件：{kind}", level=level)

        frame = event.get("frame")
        if isinstance(analysis, dict) and frame and Path(frame).is_file():
            result = dict(analysis)
            result.setdefault("mode", "tea_2p")
            result["source"] = "live"
            result["image"] = str(frame)
            result["frame_path"] = str(frame)
            result["record_frame"] = False
            loop = getattr(self, "_auto_loop", None)
            if loop is not None and loop.ledger_path.is_file():
                result["record"] = str(loop.ledger_path)
            try:
                raw = Path(frame).read_bytes()
                result["audit_event"] = build_frame_event(
                    raw, Path(frame), result, mode="tea_2p", source="live")
            except OSError:
                pass
            self._show(result, time.perf_counter())

        loop = getattr(self, "_auto_loop", None)
        if loop is not None and loop.follow_log_path.is_file():
            viewer = getattr(self, "embedded_follow_log", None)
            if viewer is not None:
                viewer.path = loop.follow_log_path
                viewer._last_signature = None
                viewer.refresh()
        # Rendering may update the generic recommendation state; restore the
        # authoritative autoplay event status last.
        self._set_operational_status(
            phase, reason or f"事件：{kind}", level=level)

    # -- session flag / auto review / misc -------------------------------
    def _active_follow_session_dir(self) -> Path | None:
        pipeline = getattr(self, "_follow_pipeline", None)
        log_path = (
            getattr(pipeline, "follow_log_path", None) if pipeline else None)
        if log_path:
            return Path(log_path).parent
        session = getattr(self, "session_dir", None)
        return Path(session) if session else None

    def _active_session_dir(self) -> Path | None:
        """Folder of the live follow/auto session, or None when idle."""
        if getattr(self, "full_auto_running", False):
            session = getattr(self, "session_dir", None)
            return Path(session) if session else None
        if getattr(self, "continuous", False):
            return self._active_follow_session_dir()
        return None

    def mark_round_issue(self) -> None:
        """One-click 'this round looked wrong' flag; no per-hand form."""
        session_dir = self._active_session_dir()
        if session_dir is None:
            self.status.set("当前无进行中的会话")
            return
        account = getattr(self, "account", None)
        serial = getattr(self, "serial", None)
        room = getattr(self, "room_id", None)
        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "serial": (serial.get().strip()
                       if serial is not None else "") or None,
            "account": (account.get().strip()
                        if account is not None else "") or None,
            "mode": self._mode_key() if hasattr(self, "_mode_key") else None,
            "room_id": (room.get().strip()
                        if room is not None else "") or None,
            "round_no": getattr(self, "round_no", None),
            "source": "assist_window",
        }
        try:
            append_user_flag(session_dir, event)
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            self.status.set(f"标记本局失败：user_flags.jsonl 写入出错：{exc}")
            return
        self.status.set("已标记本局有问题；将随本场自动复盘一并筛查。")

    def _schedule_session_review(self, session_dir) -> None:
        """Kick off the Line-A auto review for a just-ended session."""
        if getattr(self, "_suppress_review", False):
            self._suppress_review = False
            return
        if not session_dir:
            return
        target = Path(session_dir)
        thread = getattr(self, "_review_thread", None)
        if (thread is not None and thread.is_alive()
                and getattr(self, "_review_target", None) == target):
            return
        self._review_target = target
        thread = Thread(
            target=self._run_session_review, args=(target,), daemon=True)
        self._review_thread = thread
        thread.start()
        if getattr(self, "_review_poll_timer", None) is None:
            try:
                self._review_poll_timer = self.root.after(
                    150, self._poll_review)
            except Exception:
                self._review_poll_timer = None

    def _run_session_review(self, session_dir: Path) -> None:
        """Worker thread: call the review core; never touch Tk here."""
        try:
            from auto_review import review_session

            result = review_session(session_dir)
            if not isinstance(result, dict):
                raise TypeError(
                    f"review_session 应返回 dict，实际 {type(result).__name__}")
            outcome = ("ok", result)
        except Exception as exc:  # noqa: BLE001 - reported on the strip
            outcome = ("error", exc)
        events = getattr(self, "_review_events", None)
        if events is not None:
            events.put((session_dir, outcome))

    def _poll_review(self) -> None:
        self._review_poll_timer = None
        events = getattr(self, "_review_events", None)
        if events is not None:
            while True:
                try:
                    _target, outcome = events.get_nowait()
                except Empty:
                    break
                self._apply_review_result(outcome)
        thread = getattr(self, "_review_thread", None)
        if thread is not None and thread.is_alive():
            try:
                self._review_poll_timer = self.root.after(
                    200, self._poll_review)
            except Exception:
                self._review_poll_timer = None

    def _apply_review_result(self, outcome) -> None:
        kind, payload = outcome
        phase = getattr(self, "phase_status", None)
        phase_text = ""
        if phase is not None:
            phase_text = str(phase.get()).removeprefix("阶段：")
        if kind == "error":
            self._set_operational_status(
                phase_text or "会话结束", f"自动复盘失败：{payload}",
                level="error")
            return
        text, level = format_review_summary(payload)
        self._set_operational_status(
            phase_text or "会话结束", text, level=level)

    def open_folder(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.session_dir)

    def _remember_geometry(self) -> None:
        state = load_window_state(self._geometry_file())
        try:
            is_withdrawn = self.root.state() == "withdrawn"
            geometry = (getattr(self, "_collapsed_geometry", "")
                        if is_withdrawn else self.root.geometry())
            parsed = parse_window_geometry(geometry)
            if parsed is None:
                parsed = (
                    self.root.winfo_width(), self.root.winfo_height(),
                    self.root.winfo_x(), self.root.winfo_y())
            width, height, x, y = parsed
            topmost = (bool(getattr(self, "_collapsed_topmost", True))
                       if is_withdrawn else bool(self.always_on_top.get()))
            state.update({
                "geometry": format_window_geometry(width, height, x, y),
                "x": x, "y": y, "w": width, "h": height,
                "topmost": topmost,
            })
        except tk.TclError:
            return
        save_window_state(state, self._geometry_file())

    def close(self) -> None:
        """Deprecated alias of :meth:`quit_app` (kept for explicit shutdown).

        The main window's close box (X) does **not** call this; it collapses
        to the floating bar instead (see :meth:`on_window_close`).
        """
        self.quit_app()

    def quit_app(self) -> None:
        """Really exit the assistant: stop work, flush logs, destroy the root.

        Reached only through explicit entry points: the floating bar's "×"
        control, the floating bar's context menu and the main window's
        更多 menu.  Everything below is the former ``close()`` body, kept
        verbatim so quitting still tears the process down cleanly.
        """
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        self._destroy_floating_bar()
        if getattr(self, "_adb_diagnostic_timer", None) is not None:
            try:
                self.root.after_cancel(self._adb_diagnostic_timer)
            except tk.TclError:
                pass
            self._adb_diagnostic_timer = None
        self._suppress_review = True
        self.stop()
        self.stop_video_review()
        if getattr(self, "_auto_poll_timer", None) is not None:
            try:
                self.root.after_cancel(self._auto_poll_timer)
            except tk.TclError:
                pass
            self._auto_poll_timer = None
        if getattr(self, "_check_poll_timer", None) is not None:
            try:
                self.root.after_cancel(self._check_poll_timer)
            except tk.TclError:
                pass
            self._check_poll_timer = None
        if getattr(self, "_review_poll_timer", None) is not None:
            try:
                self.root.after_cancel(self._review_poll_timer)
            except tk.TclError:
                pass
            self._review_poll_timer = None
        emb = getattr(self, "embedded_follow_log", None)
        if emb is not None:
            emb.stop()
        det = getattr(self, "follow_session_log", None)
        if det is not None:
            det.close()
        for handle in (self.recorder_timer, self.ticker):
            if handle is not None:
                try:
                    self.root.after_cancel(handle)
                except tk.TclError:
                    pass
        self.recorder_timer = self.ticker = None
        self._remember_geometry()
        if self.latency_log:
            target = self.session_dir / "live_latency.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                for row in self.latency_log:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.auto_executor.shutdown(wait=False, cancel_futures=True)
        check_executor = getattr(self, "_check_executor", None)
        if check_executor is not None:
            check_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


#: Whether ``adb_runtime`` is importable in this checkout.  Importing the
#: helper costs nothing (no subprocess); only :func:`get_adb_path` probes.
_ADB_SOURCE_READY = True
try:
    from adb_runtime import resolve_adb_path_cached  # noqa: F401 - lazy use
except Exception:  # noqa: BLE001 - older checkout without the helper module
    _ADB_SOURCE_READY = False


def get_adb_path(refresh: bool = False) -> str:
    """Resolve the single adb binary through ``adb_runtime`` (never hard-coded).

    The lookup spawns short ``adb version`` probes, so it is resolved lazily
    at first real use instead of at import time: several assist windows can
    start at once and every one of them would otherwise pay the probe cost
    before painting.  Results come from ``resolve_adb_path_cached``, which
    only caches non-empty hits, so a one-off probe failure is retried later
    instead of being remembered forever.

    Args:
        refresh: discard the cached path and probe again (a user-visible
            action such as choosing another adb should pass ``True``).

    Returns:
        str: an absolute adb path, or ``""`` when none is usable.  Callers
        must treat ``""`` as "no adb" and surface ``ADB_RESOLVE_NOTE`` rather
        than substituting a hard-coded path (two adb builds fight port 5037).
    """
    global DEFAULT_ADB_PATH
    global ADB_RESOLVE_NOTE
    path = ""
    if _ADB_SOURCE_READY:
        try:
            path = resolve_adb_path_cached(refresh=refresh)
        except Exception:  # noqa: BLE001 - resolver must fail open
            path = ""
    if not path:
        # Historical single-source fallback: only used when ``adb_runtime``
        # is unavailable (older checkout), and only when that file really
        # exists on disk.  It is never combined with a second adb build.
        try:
            from harvest_tea import DEFAULT_ADB as _DEFAULT_ADB

            if _DEFAULT_ADB and Path(_DEFAULT_ADB).is_file():
                path = str(_DEFAULT_ADB)
        except Exception:  # noqa: BLE001 - fallback must not raise either
            path = ""
    if path:
        DEFAULT_ADB_PATH = path
        ADB_RESOLVE_NOTE = ""
    else:
        DEFAULT_ADB_PATH = ""
        ADB_RESOLVE_NOTE = "未找到可用 adb（adb_runtime 未探测到可用 adb）"
    return path


#: Last resolved adb path; ``""`` until :func:`get_adb_path` has succeeded.
DEFAULT_ADB_PATH = ""
#: Empty while an adb was resolved; otherwise the operator-facing reason.
ADB_RESOLVE_NOTE = ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="温州麻将辅助器：每个serial独立窗口")
    parser.add_argument("--serial", default=None)
    args = parser.parse_args()
    from app_entry import activate_window, choose_device, existing_window_pids
    from launch_multi import connected_devices
    try:
        discovered = connected_devices(get_adb_path())
        occupied = existing_window_pids(DEFAULT_SERIAL)
        chosen, open_pid = choose_device(discovered, occupied, args.serial)
    except (OSError, RuntimeError, ImportError, ValueError) as exc:
        # An unavailable device listing or process scanner must not produce a
        # second unguarded operator for the same emulator.
        print(f"雷电设备检查失败：{exc}", flush=True)
        raise SystemExit(2) from exc
    if open_pid is not None:
        activate_window(open_pid)
        print(f"现有辅助器 PID {open_pid} 已打开；不重复运行。", flush=True)
        raise SystemExit(0)
    if chosen is None:
        print("没有空闲在线雷电窗口。已有窗口请从任务栏切换。", flush=True)
        raise SystemExit(0)
    if args.serial is None:
        # A desktop shortcut has no --serial. Spawn the selected serial as an
        # explicit child process: otherwise every window looks like 5556 to
        # the process scanner and reopening falsely reports "already open".
        from launch_multi import start_window
        proc = start_window(chosen)
        print(f"已打开 {chosen} 辅助器，PID={proc.pid}", flush=True)
        raise SystemExit(0)
    from serial_lease import SerialBusy, hold_serial
    try:
        # Separate namespace from the BOT live-click lease.
        with hold_serial("ui:" + chosen):
            app_root = tk.Tk()
            TeaAssistWindow(app_root, serial=chosen)
            app_root.mainloop()
    except SerialBusy:
        pid = existing_window_pids(DEFAULT_SERIAL).get(chosen)
        if pid is not None:
            activate_window(pid)
        print(f"{chosen} 已被其他辅助器窗口使用，不再重复启动。", flush=True)
