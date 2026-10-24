"""One compact, always-on-top viewer for Wenzhou tea 2/4 and Shu 4.

Read-only: no click, no key event, no emulator input.  Display and recording
are decoupled: every processed frame is rendered immediately, while the
``AssistRecorder`` keeps its own stable-frame state machine and frozen
SPEC-002 JSONL contract.
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
from threading import Event
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import cv2
import numpy as np

from assist_shu import analyze_shu_manual_hand, import_shu_and_analyze
from assist_tea import analyze_manual_hand, import_and_analyze
from caishen_vision import detect_caishen
from harvest_tea import DEFAULT_SERIAL
from live_capture import (LiveCapture, analyze_bytes, capture_result,
                          session_folder, save_frame)
from live_state import (BASE_INTERVAL_MS, FrameTiming, LatencyTracker,
                        LiveRefreshState, describe_age)
from recognition_audit import (append_frame_event, build_frame_event,
                               read_appended_events)
from recognition_review import FLAG_NAMES, draw_recognition_boxes
from recorder import AssistRecorder
from tap_executor import TapAborted, TapExecutor
from tea_bot import tile_name, vision_to_engine
from tile_vision import TARGET_IDS, TILE_IDS
from tile_render import render_advice, render_candidates, render_hand


TEA_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = Path(__file__).resolve().parent / "assets" / "温州麻将辅助器.ico"
LAST_ACCOUNT_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".last_account"
WINDOW_STATE_PATH = TEA_ROOT / "采集" / "assist_sessions" / ".window_state.json"
DEFAULT_SIZE = (1100, 740)
SIZE_RANGE = {"w": (840, 1600), "h": (600, 1200)}
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


def append_feedback(record_path: str | Path, verdict: str, actual_tile: str | None = None) -> Path:
    """Keep user corrections separate from immutable capture results."""
    if verdict not in {"useful", "wrong_advice", "wrong_vision"}:
        raise ValueError("未知反馈类型")
    record = Path(record_path)
    if not record.is_file() or record.suffix not in {".json", ".jsonl"}:
        raise FileNotFoundError("本次截图记录不存在")
    event = {
        "at": datetime.now().astimezone().isoformat(),
        "record": record.name,
        "verdict": verdict,
        "actual_tile": actual_tile,
    }
    target = record.parent / "feedback.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


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
        root.title("温州麻将辅助器 · 实时跟随（只读）")
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
        # 半自动代点（单次点击）：默认完全关闭，见 _build 的 tap_panel。
        self.tap_armed = tk.BooleanVar(value=False)
        self.tap_status = tk.StringVar(
            value="半自动代点：未启用（勾选后按空格或点按钮，每帧只点一张）")
        self._tap_executor: TapExecutor | None = None
        self._tap_future: Future | None = None
        self._tap_pending = False
        self._tap_frame_key: str | None = None

        last_account = (LAST_ACCOUNT_PATH.read_text(encoding="utf-8").strip()
                        if LAST_ACCOUNT_PATH.is_file() else "")
        self.account = tk.StringVar(value=last_account)
        self.room_id = tk.StringVar()
        self.round_text = tk.StringVar(value="第 1 局")
        self.serial = tk.StringVar(value=DEFAULT_SERIAL)
        self.mode = tk.StringVar(value="温州茶 · 2人")
        self.caishen = tk.StringVar()
        self.caishen_title = tk.StringVar(value="财神：识别中")
        self.confirm_mode = tk.BooleanVar(value=False)
        self.always_on_top = tk.BooleanVar(value=bool(state.get("topmost", True)))
        self.manual_caishen = tk.BooleanVar(value=False)
        self.actual_tile = tk.StringVar()
        self.status = tk.StringVar(value="先选玩法并核对眼前牌局；填写账号标记后勾选“已核对本局玩法”会自动跟随。")
        self.hand = tk.StringVar(value="手牌：—")
        self.suggestion = tk.StringVar(value="尚未开始")
        self.reason = tk.StringVar(value="离线预检结果不能用于当前对局。")
        self.latency = tk.StringVar(value="最近一帧：—")
        self.fresh = tk.StringVar(value="尚未更新")
        self.period = tk.StringVar(value=f"周期 {BASE_INTERVAL_MS} ms")
        self.live_info = tk.StringVar(value="只读画面，不会代点；停止后最后一帧可能已过期。")
        self.preview_title = tk.StringVar(value="实时跟随 · 尚未开始")
        self.preview_flags = tk.StringVar(value="可先点“离线预检”检查所选玩法；预检使用旧截图，不能用于当前对局。")
        self.preview_meta = tk.StringVar(value="绿色=识牌预测；橙色=未识别；红色=建议牌。")
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
        self._build()
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
        ttk.Label(controls, text="账号标记（必填）").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.account, width=14).grid(
            row=0, column=3, padx=(2, 10), sticky="w")
        ttk.Checkbutton(
            controls, text="已核对本局玩法", variable=self.confirm_mode,
            command=self._on_confirm,
        ).grid(row=0, column=4, padx=2)
        self.start_button = ttk.Button(controls, text="开始跟随", command=self._toggle)
        self.start_button.grid(row=0, column=5, padx=4)
        ttk.Button(controls, text="离线预检", command=self.preflight_selected_mode).grid(
            row=0, column=6, padx=4)
        ttk.Button(controls, text="更多", width=5, command=self._open_menu).grid(
            row=0, column=7, sticky="e")
        controls.grid_columnconfigure(7, weight=1)
        ttk.Label(
            controls,
            text="“已核对本局玩法”只表示眼前牌局与所选玩法一致，不代表识牌或建议已核准；"
                 "填写账号标记（仅本地记录）后才会自动跟随。",
            foreground="#a55700", wraplength=1040,
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(3, 0))

        # Bottom strip: read-only follow_session.jsonl (gate/abstain/decision).
        # Pack before main so the strip stays at the window bottom.
        self.follow_log_frame = ttk.LabelFrame(
            self.root, text="跟随会话日志 · 只读（可折叠）", padding=(4, 2)
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
            text="尚未开始实时跟随。打牌前可先点“离线预检”检查保存样本。",
            anchor="center", justify="center", wraplength=500,
        )
        self.picture.pack(fill="both", expand=True)
        ttk.Label(self.preview_panel, textvariable=self.preview_meta,
                  foreground="#666666", wraplength=550).pack(fill="x", pady=(4, 2))
        sidebar = ttk.Frame(main, width=440)
        sidebar.pack(side="right", fill="y")
        sidebar.pack_propagate(False)
        self.caishen_row = ttk.Frame(sidebar, padding=(6, 0))
        self.caishen_row.pack(fill="x")
        ttk.Label(self.caishen_row, textvariable=self.caishen_title,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Label(self.caishen_row, textvariable=self.round_text).pack(side="right")
        ttk.Label(
            sidebar, textvariable=self.offline_notice, foreground="#c62828",
            font=("Microsoft YaHei UI", 9, "bold"), wraplength=380,
        ).pack(fill="x", padx=6)

        # Read-only unified table HUD — never invents values; CLAIM stays off.
        self.hud_panel = ttk.Frame(sidebar, padding=(6, 2))
        self.hud_panel.pack(fill="x")
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
        # 半自动代点：置顶窗口内一个大按钮 + 空格键，见 confirm_tap()。
        self.tap_panel = ttk.Frame(sidebar)
        self.tap_panel.pack(fill="x", padx=6, after=self.advice_panel)
        self.tap_armed_check = ttk.Checkbutton(
            self.tap_panel, text="允许代点（半自动）", variable=self.tap_armed,
            command=self._on_tap_arm_toggle)
        self.tap_armed_check.pack(side="left")
        self.tap_button = ttk.Button(
            self.tap_panel, text="确认出这张（空格）", width=16,
            command=self.confirm_tap)
        self.tap_button.pack(side="left", padx=4)
        self.tap_status_label = ttk.Label(
            self.tap_panel, textvariable=self.tap_status,
            foreground="#555555", wraplength=360,
            font=("Microsoft YaHei UI", 9))
        self.tap_status_label.pack(fill="x", padx=(4, 0))
        self.root.bind("<space>", self._on_space)
        self.candidate_face = ttk.Label(sidebar)
        self.candidate_face.pack(anchor="w", padx=6)
        self.latency_label = ttk.Label(sidebar, textvariable=self.latency,
                                       font=("Consolas", 8))
        self.latency_label.pack(fill="x", padx=6, pady=(2, 0))

        bottom = ttk.Frame(sidebar, padding=(6, 2))
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, text="我实际打的牌").grid(row=0, column=0, padx=2)
        self.actual_choice = ttk.Combobox(
            bottom, textvariable=self.actual_tile, state="readonly", width=11,
        )
        self.actual_choice.grid(row=0, column=1, padx=2)
        ttk.Button(bottom, text="可用", width=5,
                   command=lambda: self.feedback("useful")).grid(row=0, column=2, padx=1)
        ttk.Button(bottom, text="不对", width=5,
                   command=lambda: self.feedback("wrong_advice")).grid(row=0, column=3, padx=1)
        ttk.Button(bottom, text="识错", width=5,
                   command=lambda: self.feedback("wrong_vision")).grid(row=0, column=4, padx=1)
        ttk.Button(bottom, text="记录", width=5,
                   command=self.open_folder).grid(row=0, column=5, padx=1)
        ttk.Label(bottom, textvariable=self.live_info, wraplength=430,
                  font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=6, sticky="w")

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="填入模拟器序列号", command=self._edit_serial)
        self.menu.add_command(label="账号", command=self._edit_account)
        self.menu.add_command(label="房间号", command=self._edit_room)
        self.menu.add_checkbutton(label="窗口置顶", variable=self.always_on_top,
                                  command=self._set_always_on_top)
        self.caishen_menu = tk.Menu(self.menu, tearoff=0)
        for text, label in TILE_CHOICES.items():
            self.caishen_menu.add_radiobutton(
                label=f"{text}", value=label, variable=self.caishen,
                command=self._on_caishen_pick,
            )
        self.menu.add_checkbutton(label="手动指定财神", variable=self.manual_caishen,
                                  command=self._on_manual_caishen)
        self.menu.add_cascade(label="财神牌（茶）", menu=self.caishen_menu)
        self.menu.add_separator()
        self.menu.add_command(label="抓图并分析", command=self.capture)
        self.menu.add_command(label="导入截图", command=self.import_image)
        self.menu.add_command(label="历史逐帧复核", command=self.open_recognition_review)
        self.menu.add_command(label="跟随会话日志", command=self.open_follow_session_log)
        self.menu.add_command(label="导入录像复核（每秒1帧）", command=self.import_video_review)
        self.menu.add_command(label="停止录像复核", command=self.stop_video_review)
        self.menu.add_command(label="修正手牌并重算", command=self.manual_correction)
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
            search_roots=self._follow_log_search_roots(),
            container=self.follow_log_frame,
            compact=True,
        )
        self.embedded_follow_log.set_detach_callback(self.open_follow_session_log)

    def _open_menu(self) -> None:
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

    def _edit_account(self) -> None:
        value = simpledialog.askstring(
            "账号标记（必填）", "仅本地记录用，不是游戏登录账号",
            initialvalue=self.account.get())
        if value is None:
            return
        self.account.set(value.strip())
        if not self.account.get():
            if self.confirm_mode.get():
                self.status.set(
                    "账号标记为空；填写后才会自动跟随（仅本地记录）。")
            return
        self._autostart_done = False
        self._maybe_autostart()

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
                picture.configure(text="等待新画面；可先做离线预检。")
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
        self.confirm_mode.set(False)
        self._reset_round_display("已切换玩法，请重新核对本局")
        self.status.set(
            "已切换玩法；“已核对本局玩法”已取消。请核对眼前牌局后重新勾选。")

    def _on_confirm(self) -> None:
        if not self.confirm_mode.get():
            self.stop()
            self._reset_round_display("等待核对本局玩法")
            status = getattr(self, "status", None)
            if status is not None:
                status.set("已取消核对；连续观察已停止。")
            return
        self._autostart_done = False
        account = getattr(self, "account", None)
        if account is None or not account.get().strip():
            self.status.set(
                "已核对玩法，但账号标记为空；填写后才会自动跟随（仅本地记录）。")
            return
        self._maybe_autostart()

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

    def _ready(self) -> bool:
        if not self.confirm_mode.get():
            self.status.set(
                "先勾选“已核对本局玩法”（确认眼前牌局与所选玩法一致），再开始跟随。")
            return False
        account = getattr(self, "account", None)
        if account is not None and not account.get().strip():
            self.status.set(
                "先填写账号标记（仅本地记录，不是登录账号），再开始跟随。")
            return False
        try:
            if getattr(self, "account", None) is not None:
                self._ensure_recorder()
        except ValueError as exc:
            self.status.set(str(exc))
            return False
        return True

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
        confirm = getattr(self, "confirm_mode", None)
        return (mode.get() if mode else "温州茶 · 2人", caishen_value,
                serial.get().strip() if serial else "",
                bool(confirm.get()) if confirm else False,
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
            self.live_info.set(
                "“已核对本局玩法”勾选保留；它只代表玩法核对，不代表识牌准确。")

    def _poll_recorder(self) -> None:
        self.recorder_timer = None
        if self.recorder is not None:
            record = self.recorder.tick()
            if record is not None:
                self.live_info.set(f"决策 {record['decision_id']} 已 timeout；只读不点击。")
        self._maybe_autostart()
        self.recorder_timer = self.root.after(500, self._poll_recorder)

    def _maybe_autostart(self) -> None:
        """开窗完成前置输入后，自动进入连续观察（仍受账号/玩法门禁约束）。"""
        if getattr(self, "_autostart_done", False) or self.continuous:
            return
        account = getattr(self, "account", None)
        confirm = getattr(self, "confirm_mode", None)
        if account is None or not account.get().strip():
            return
        if confirm is None or not confirm.get():
            return
        self._autostart_done = True
        self.start()

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
        if self.continuous:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.continuous or getattr(self, "video_future", None) is not None or not self._ready():
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
        self.next_run = time.perf_counter()
        log_path = self._follow_pipeline.follow_log_path
        self.status.set(
            f"实时跟随已启动；日志 {Path(log_path).name}；界面按实际处理周期刷新。"
        )
        self.live_info.set(
            f"实时跟随中：只读画面，不会代点；已写入 {log_path}"
        )
        title = getattr(self, "preview_title", None)
        if title is not None:
            title.set("实时跟随 · 等待当前画面")
        flags = getattr(self, "preview_flags", None)
        if flags is not None and getattr(self, "display_source", None) in OFFLINE_SOURCES:
            flags.set("左侧仍是旧截图，仅作对照；等待第一张实时画面。")
        self._schedule(0)

    def stop(self) -> None:
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
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None

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
        initial = self.actual_choice.get() == ""
        self.actual_choice["values"] = sorted(
            {label for label in state.hand_labels if label})
        if initial:
            self.actual_tile.set("")
        else:
            self.actual_tile.set("")
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
            " · 绿=预测，橙=未识别，红=建议")
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
            from assist_hud import build_follow_hud, load_result_bgr
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
        self.info_label.configure(foreground="#555555" if actionable else "#c62828")
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
        record_decision = bool(self.confirm_mode.get() and self.account.get().strip())
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

    # -- 半自动代点（单次点击，人工确认后才执行） ------------------------
    def _on_tap_arm_toggle(self) -> None:
        if self.tap_armed.get():
            self.tap_status.set(
                "已允许代点：出现绿色建议后按空格或点“确认出这张”，每帧只点一张。")
            self.tap_status_label.configure(foreground="#a55700")
        else:
            self.tap_status.set("已关闭代点：本窗口回到纯只读状态。")
            self.tap_status_label.configure(foreground="#555555")

    def _on_space(self, event) -> str | None:
        """空格 = 点一次“确认出这张”；输入框聚焦时不拦截。"""
        if not self.tap_armed.get():
            return None
        try:
            focus = self.root.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if isinstance(focus, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        self.confirm_tap()
        return "break"

    def _tap_target_result(self) -> dict | None:
        """只允许对实时、新鲜、有完整建议的 advise 帧代点。"""
        result = self.current or {}
        if self.display_source in OFFLINE_SOURCES:
            return None
        if not self.continuous or self._tap_pending:
            return None
        if result.get("status") != "advise":
            return None
        if not result.get("suggestion_id") or not result.get("suggested_boxes"):
            return None
        return result

    def confirm_tap(self) -> None:
        """人工确认入口：对当前这一帧的建议牌执行一次受控点击。"""
        if self._tap_pending:
            self.tap_status.set("上一次点击还在验证中，请等待结果。")
            return
        result = self._tap_target_result()
        if result is None:
            self.tap_status.set("当前没有可执行的建议（需要实时 advise 帧）。")
            return
        if not self.tap_armed.get():
            self.tap_status.set("先勾选“允许代点（半自动）”，再按空格确认。")
            return
        mode = str(result.get("mode") or self._mode_key())
        if not mode.startswith("tea_"):
            self.tap_status.set("代点目前只支持温州茶局面。")
            return
        frame_key = (f"{result.get('image') or result.get('frame_path')}"
                     f"#{result.get('captured_at')}")
        if frame_key == self._tap_frame_key:
            self.tap_status.set("这一帧已经执行过代点；等待下一帧建议。")
            return
        serial = self.serial.get().strip()
        if not serial:
            self.tap_status.set("先在“更多”里填写模拟器序列号，才能代点。")
            return
        if self._tap_executor is None or self._tap_executor.serial != serial:
            # enabled=True 只由这行代码显式传入；勾选框本身不碰执行器开关。
            self._tap_executor = TapExecutor(
                enabled=True, adb_path=DEFAULT_ADB_PATH, serial=serial)
        # 放在 if 外面：执行器会被复用，而会话目录每局都会变。这样 tap 证据
        # 落进当前这一局的会话目录，而不是执行器第一次被造出来时的那一局。
        self._tap_executor.session_dir = self.session_dir
        self._tap_frame_key = frame_key
        self._tap_pending = True
        self.tap_status.set("正在重新截屏并核对建议牌……")
        self.tap_status_label.configure(foreground="#a55700")
        self._tap_future = self.executor.submit(
            self._tap_executor.execute, dict(result))
        self.root.after(80, self._poll_tap)

    def _poll_tap(self) -> None:
        future = self._tap_future
        if future is None:
            return
        if not future.done():
            self.root.after(80, self._poll_tap)
            return
        self._tap_future = None
        self._tap_pending = False
        try:
            outcome = future.result()
        except TapAborted as exc:
            self.tap_status.set(f"❌ 已中止，未重试：{exc}")
            self.tap_status_label.configure(foreground="#c62828")
            self.status.set(f"代点中止：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - report, never crash the window
            self.tap_status.set(f"❌ 代点出错：{exc}")
            self.tap_status_label.configure(foreground="#c62828")
            return
        x, y = outcome["tap"]
        self.tap_status.set(
            f"✅ 已点出 {outcome['tile']} @({x},{y})，"
            f"手牌 {outcome['before_count']}→{outcome['after_count']}，验证通过。")
        self.tap_status_label.configure(foreground="#146c2e")

    # -- feedback / misc -------------------------------------------------
    def feedback(self, verdict: str) -> None:
        record = (self.current or {}).get("record")
        if not record:
            messagebox.showinfo("温州茶辅助", "先抓图，生成本次记录。")
            return
        if verdict in {"useful", "wrong_advice"} and self.refresh.state.status != "advise":
            messagebox.showinfo("温州茶辅助", "本次没有出牌建议。")
            return
        actual = self.actual_tile.get() or None
        path = append_feedback(record, verdict, actual)
        self.status.set(f"反馈已存本地：{path.name}")

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
        self.stop()
        self.stop_video_review()
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
        self.root.destroy()


DEFAULT_ADB_PATH = str(Path(__file__).resolve().parent.parent / "采集" / "scrcpy" / "adb.exe")
if not Path(DEFAULT_ADB_PATH).is_file():
    from harvest_tea import DEFAULT_ADB as _DEFAULT_ADB

    DEFAULT_ADB_PATH = _DEFAULT_ADB


if __name__ == "__main__":
    app_root = tk.Tk()
    TeaAssistWindow(app_root)
    app_root.mainloop()
