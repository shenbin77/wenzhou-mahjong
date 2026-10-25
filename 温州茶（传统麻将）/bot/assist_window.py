"""Single front end for recommendation follow and explicitly armed autoplay.

``开始跟随`` is read-only observation/recommendation.  ``开启全自动代打`` is
the separate hand-off to the guarded ``AutoPlayLoop`` and is currently limited
to the development target (tea 2-player; full-match acceptance is pending).
Both paths keep immutable
evidence; neither ever clicks start-game, payment, leave-table or settlement UI.
"""

from __future__ import annotations

import json
import hashlib
import os
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


TEA_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = Path(__file__).resolve().parent / "assets" / "温州麻将辅助器.ico"
LAST_ACCOUNT_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".last_account"
WINDOW_STATE_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".window_state.json"
BINDING_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".binding.json"
DEFAULT_SIZE = (1180, 860)
SIZE_RANGE = {"w": (900, 1600), "h": (760, 1200)}
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
    if result.get("placeholder"):
        return "自动复盘：复盘核心尚未就绪（占位），本场未做自动筛查", "wait"
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
            mode="tea_2p", adb_path=DEFAULT_ADB_PATH, serial=serial,
            session_dir=TEA_ROOT / "采集" / "assist_sessions")
    except Exception as exc:  # noqa: BLE001 - 抓不到图就是读不出，按未识别处理
        return None, f"抓图失败：{exc}"
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None, "抓图无法解码"
    return detect_table_mode(image)


def clamp_geometry(x: int, y: int, width: int, height: int,
                   screen: tuple[int, int]) -> tuple[int, int, int, int]:
    """Keep a remembered window at least partly on the current screen."""
    screen_w, screen_h = screen
    width = max(SIZE_RANGE["w"][0], min(SIZE_RANGE["w"][1], int(width)))
    height = max(SIZE_RANGE["h"][0], min(SIZE_RANGE["h"][1], int(height)))
    x = max(0, min(int(x), max(0, screen_w - width)))
    y = max(0, min(int(y), max(0, screen_h - height)))
    return x, y, width, height


class TeaAssistWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("温州麻将辅助器 · 推荐跟随 / 全自动代打")
        if ICON_PATH.is_file():
            root.iconbitmap(str(ICON_PATH))
        state = load_window_state()
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
            datetime.now().strftime("%Y%m%d_%H%M%S")
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
        self._auto_future: Future | None = None
        self._auto_poll_timer: str | None = None
        self._auto_events: Queue = Queue()
        self._suppress_review = False
        self._review_thread: Thread | None = None
        self._review_target: Path | None = None
        self._review_events: Queue = Queue()
        self._review_poll_timer: str | None = None
        self._binding: dict | None = None
        self._check_state = "unchecked"
        self._check_mode_key: str | None = None
        self._check_detail = ""
        self._check_detected: str | None = None
        self._check_pending: str | None = None
        self._check_events: Queue = Queue()
        self._check_poll_timer: str | None = None
        self._check_executor = ThreadPoolExecutor(max_workers=1)
        self.full_auto_status = tk.StringVar(value="全自动未开启。")
        self.auto_state = tk.StringVar(value="不可开启：未开始跟随")
        self.lamp_status = tk.StringVar(value="待处理")
        self.bound_status = tk.StringVar(value="账号：未绑定 · 设备：未确认")
        self.phase_status = tk.StringVar(value="阶段：尚未开始")
        self.problem_status = tk.StringVar(value="状态：—")

        last_account = (LAST_ACCOUNT_PATH.read_text(encoding="utf-8").strip()
                        if LAST_ACCOUNT_PATH.is_file() else "")
        self.account = tk.StringVar(value=last_account)
        self.room_id = tk.StringVar()
        self.round_text = tk.StringVar(value="—")
        self.serial = tk.StringVar(value=DEFAULT_SERIAL)
        self.mode = tk.StringVar(value="温州茶 · 2人")
        self.caishen = tk.StringVar()
        self.caishen_title = tk.StringVar(value="财神：—")
        self.always_on_top = tk.BooleanVar(value=bool(state.get("topmost", True)))
        self.manual_caishen = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="先选玩法、填写账号标记并点“绑定”；开始跟随时将自动核对牌桌玩法。")
        self.hand = tk.StringVar(value="手牌：—")
        self.suggestion = tk.StringVar(value="尚未开始")
        self.reason = tk.StringVar(value="离线预检结果不能用于当前对局。")
        self.latency = tk.StringVar(value="最近一帧：—")
        self.fresh = tk.StringVar(value="尚未更新")
        self.period = tk.StringVar(value=f"周期 {BASE_INTERVAL_MS} ms")
        self.live_info = tk.StringVar(value="只读画面，不会代点。")
        self.preview_title = tk.StringVar(value="实时跟随")
        self.preview_flags = tk.StringVar(value="进入牌桌后点“开始跟随”；离线预检在“更多”中，仅检查旧截图。")
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
        self.hud_caishen = tk.StringVar(value="财神：未识别")
        self.hud_concealed = tk.StringVar(value="暗手：未识别")
        self.hud_melds = tk.StringVar(value="副露：未识别")
        self.hud_meld_types = tk.StringVar(value="副露类型：未识别")
        self.hud_last_discard = tk.StringVar(value="最近出牌：未识别")
        self.hud_claim_buttons = tk.StringVar(value="鸣牌按钮：未识别")
        self._follow_pipeline = None
        self.display_source: str | None = None
        self._restore_binding()
        self._build()
        self._refresh_binding_views()
        self._apply_preview_layout(False)
        self.recorder_timer = root.after(500, self._poll_recorder)
        self.ticker = root.after(200, self._tick)
        root.protocol("WM_DELETE_WINDOW", self.close)

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
        ttk.Label(controls, text="绑定账号（本地标记）").grid(row=0, column=2, sticky="w")
        self.account_entry = ttk.Entry(
            controls, textvariable=self.account, width=14)
        self.account_entry.grid(row=0, column=3, padx=(2, 6), sticky="w")
        self.bind_button = ttk.Button(
            controls, text="绑定", width=6, command=self._bind)
        self.bind_button.grid(row=0, column=4, padx=2)
        self.unbind_button = ttk.Button(
            controls, text="解绑", width=6, command=self._unbind)
        self.unbind_button.grid(row=0, column=5, padx=2)
        self.start_button = tk.Button(
            controls, text="开始跟随", width=10, command=self._toggle)
        self.start_button.grid(row=0, column=6, padx=4)
        ttk.Button(controls, text="更多", width=5, command=self._open_menu).grid(
            row=0, column=7, sticky="e")
        controls.grid_columnconfigure(7, weight=1)
        ttk.Label(
            controls,
            text="绑定保存账号标记、设备与玩法，重启自动恢复；"
                 "开始前自动核对牌桌玩法，一致才放行，"
                 "不一致或读不出都阻止开始。",
            foreground="#a55700", wraplength=1040,
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(3, 0))

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
            text="进入真实牌桌后点“开始跟随”。",
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
        for var in (
            self.hud_mode, self.hud_room_round, self.hud_caishen,
            self.hud_concealed, self.hud_melds, self.hud_meld_types, self.hud_wall,
            self.hud_countdown, self.hud_turn, self.hud_river,
            self.hud_claim_buttons,
            self.hud_last_discard,
        ):
            ttk.Label(
                self.hud_panel, textvariable=var,
                font=("Microsoft YaHei UI", 9), wraplength=380,
            ).pack(anchor="w")

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
        self.hand_label.pack(fill="x", padx=6, pady=(2, 0))
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
        self.tap_panel = ttk.Frame(sidebar)
        self.tap_panel.pack(fill="x", padx=6, after=self.advice_panel)
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
        # 设置组：绑定相关的唯一编辑入口（serial 在此，账号只走主区绑定）。
        self.menu.add_command(label="设备序列号…", command=self._edit_serial)
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

        self._mount_embedded_follow_log()

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
        value = simpledialog.askstring("模拟器", "ADB 序列号", initialvalue=self.serial.get())
        if value:
            self.serial.set(value.strip())
            refresh = getattr(self, "_refresh_binding_views", None)
            if callable(refresh):
                refresh()

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
    def _restore_binding(self) -> None:
        """Re-apply the persisted binding (single identity source)."""
        binding = load_binding()
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
            return (f"账号：已绑定（{current['account']}） · "
                    f"设备：已确认（{current['serial']}）")
        return "账号：未绑定 · 设备：未确认"

    def _bind(self) -> None:
        fields = self._current_binding_fields()
        if not fields["account"]:
            self.status.set(
                "账号标记为空，无法绑定；请先在顶部输入框填写账号标记。")
            self._set_operational_status(
                "未绑定", "账号标记为空，绑定未写入", level="wait")
            return
        if not fields["serial"]:
            self.status.set(
                "设备序列号为空，无法绑定；请在“更多 → 设备序列号…”中填写。")
            self._set_operational_status(
                "未绑定", "设备序列号为空，绑定未写入", level="wait")
            return
        binding = {
            "account": fields["account"],
            "serial": fields["serial"],
            "mode": fields["mode"],
            "mode_label": (self.mode.get()
                           if getattr(self, "mode", None) is not None else ""),
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
        if not save_binding(binding):
            self.status.set("绑定保存失败：本地文件写入出错。")
            self._set_operational_status(
                "未绑定", "绑定写入失败", level="error")
            return
        self._binding = binding
        self.status.set(
            f"已绑定并保存：{binding['account']} · {binding['serial']} · "
            f"{binding['mode_label']}；重启自动恢复。")
        self._set_operational_status(
            "已绑定", "账号/设备/玩法已绑定", level="ok")
        self._refresh_binding_views()

    def _unbind(self) -> None:
        clear_binding()
        self._binding = None
        self.status.set("已解绑并清除本地保存；开始跟随前请重新绑定。")
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
        if getattr(self, "_check_state", "unchecked") == "mismatch":
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
            text, level = "● 代打中", "ok"
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
            if not self._mode_gate_ok():
                missing.append("玩法未核对")
            if not getattr(self, "continuous", False):
                missing.append("未开始跟随")
            if missing:
                text, level = f"不可开启：{'、'.join(missing)}", "wait"
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
                   or bool(getattr(self, "full_auto_running", False)))
        try:
            button.configure(
                state="normal" if (running or self._is_bound()) else "disabled")
        except Exception:
            pass

    def _refresh_bind_controls(self) -> None:
        """Bind/unbind are mutually exclusive; a bound account is locked."""
        bound = self._is_bound()
        bind_button = getattr(self, "bind_button", None)
        if bind_button is not None:
            try:
                bind_button.configure(text="重新绑定" if bound else "绑定")
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
        """Idle: the log strip takes the slack. Running: the frame does."""
        picture = getattr(self, "picture", None)
        log_frame = getattr(self, "follow_log_frame", None)
        if picture is None or log_frame is None:
            return
        try:
            picture.pack_configure(fill="both", expand=bool(running))
            log_frame.pack_configure(fill="both" if not running else "x",
                                     expand=not running)
        except Exception:
            pass

    def _refresh_binding_views(self) -> None:
        self._refresh_identity()
        self._refresh_lamp()
        self._refresh_auto_gate()
        self._refresh_start_gate()
        self._refresh_bind_controls()
        self._refresh_flag_button()

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
            try:
                detected, detail = reader(serial)
            except Exception as exc:  # noqa: BLE001 - 读失败按未识别处理
                detected, detail = None, f"核对读取失败：{exc}"
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

    def _ready(self, pending: str | None = None) -> bool:
        """Start gate: bound identity + automatic table-mode check.

        ``pending`` names the action to resume once a fresh check passes
        (``"follow"`` / ``"full_auto"``); when a check first has to run,
        this call returns False and the action completes asynchronously.
        """
        if pending is None:
            pending = getattr(self, "_pending_action", None)
        if not self._is_bound():
            if getattr(self, "_binding", None) is None:
                self.status.set(
                    "尚未绑定：填写账号标记后点顶部“绑定”，再开始跟随。")
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
            LAST_ACCOUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_ACCOUNT_PATH.write_text(account, encoding="utf-8")
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
        self.recorder_timer = self.root.after(500, self._poll_recorder)

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
        if self.continuous:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.continuous or getattr(self, "video_future", None) is not None:
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

    def stop(self) -> None:
        if getattr(self, "full_auto_running", False):
            self._stop_full_auto()
        follow_session = None
        if self.continuous:
            resolve_dir = getattr(self, "_active_follow_session_dir", None)
            if callable(resolve_dir):
                follow_session = resolve_dir()
        stopped_live = (
            self.continuous and getattr(self, "display_source", None) == "live")
        self.continuous = False
        self.continuous_round = None
        self.continuous_caishen = None
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
            capture = LiveCapture(DEFAULT_ADB_PATH, serial)
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
        """Hand the current tea-2p table to the guarded continuous BOT."""
        if self._mode_key() != "tea_2p":
            self.full_auto_status.set("全自动目前只开放温州茶 2 人；该玩法保持推荐模式。")
            self._set_operational_status(
                "推荐模式", "当前玩法未完成全自动实机验收", level="wait")
            return
        if not self.continuous:
            self.full_auto_status.set("请先点“开始跟随”，核对实时识牌和推荐后再开启全自动。")
            self._set_operational_status(
                "等待推荐", "尚未启动实时跟随，拒绝交出点击权", level="error")
            return
        if not self._ready():
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
        self.stop()
        from action_policy import recommended_runner_config
        from auto_play_loop import AutoPlayLoop

        self._auto_loop = AutoPlayLoop(
            enabled=True,
            serial=serial,
            adb_path=DEFAULT_ADB_PATH,
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
        self.auto_button.configure(text="关闭全自动（仅推荐）")
        self._style_start_button(True)
        self._set_hud_expanded(True)
        self._apply_preview_layout(True)
        gate_refresh = getattr(self, "_refresh_auto_gate", None)
        if callable(gate_refresh):
            gate_refresh(force=True)
        self.full_auto_status.set(
            "全自动已接管：出牌、过和安全鸣牌会自动执行；证据不足立即弃权或停手。")
        self.live_info.set(
            "BOT 接管中；绝不点击开始游戏、结算、离桌、充值或分享。")
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
        state = load_window_state()
        try:
            state.update({
                "x": self.root.winfo_x(), "y": self.root.winfo_y(),
                "w": self.root.winfo_width(), "h": self.root.winfo_height(),
                "topmost": bool(self.always_on_top.get()),
            })
        except tk.TclError:
            return
        save_window_state(state)

    def close(self) -> None:
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


DEFAULT_ADB_PATH = str(Path(__file__).resolve().parent.parent / "采集" / "scrcpy" / "adb.exe")
if not Path(DEFAULT_ADB_PATH).is_file():
    from harvest_tea import DEFAULT_ADB as _DEFAULT_ADB

    DEFAULT_ADB_PATH = _DEFAULT_ADB


if __name__ == "__main__":
    app_root = tk.Tk()
    TeaAssistWindow(app_root)
    app_root.mainloop()
