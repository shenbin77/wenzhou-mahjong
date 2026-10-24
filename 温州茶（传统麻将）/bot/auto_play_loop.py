# -*- coding: utf-8 -*-
"""auto_play_loop.py — 连续代打循环（开发局专用，默认 dry_run）。

一帧一周期：
    截图 → assist_tea 建议 → TapExecutor（四道闸，不重造）→ dev 台账

主理人 2026-10-24 开发局授权：仅 2 人测试房且双号均为用户本人时允许实测
半自动/全自动动作，数据标 ``dev`` 不进训练语料。**全自动首次实战尚未验证**，
本模块不做任何托管承诺。

门禁（任一不满足立即停手并记录原因，不重试、不兜底点击）：
  * 前台包必须是 ``com.xm.zjgamecenter``；
  * ``enabled`` 只认显式 True（默认 False → dry_run：只记录不点）；
  * 连续 3 次截图或识牌异常 → 停止；点击异常或点后验证失败立即停止；
  * 换局信号（HUD 指纹变化或财神 label 变化）→ 跳过该帧，手选财神时停手；
  * HUD 与画面财神都不可读 → 拒绝真实点击；
  * 牌墙/房卡读数 ≤ 16 → 停止（P0-5）；不可读记 unknown，不阻断开发局，但绝不点充值；
  * 启用且本帧财神未知 → 停止（P0-5 ``gate_unknown_caishen``）；
  * **回合所有权**（``turn_gate``，dry_run 与真点都要过）：轮到我方、已摸牌、
    张数守恒（17 = 暗手 + 副露）、画面连续两帧一致、手牌未被压暗；
    ``wait`` 只等待并记 ``idle``，``blocked``（证据自相矛盾）立即停手；
  * **结算/牌局结束/模态弹窗**（``turn_gate`` 第 0 条判据，最优先）：命中即
    ``_stop("gate_turn")``（``turn_gate.code="gate_settled"``），绝不点
    「确定/返回大厅/分享战绩/开始游戏/充值」；
  * 几何白名单沿用 TapExecutor（只允许手牌 ROI，y≥720，充值/开始游戏/
    游戏中心永不触碰）。
  * **行动行（吃/碰/杠/胡/过）**：``action_guard`` 判定行动界面可见时，调用
    ``action_policy.decide_action`` 做 L1 几何 + L2 算牌引擎决策，**默认只
    落台账不点击**（``claim_auto_click=False``，且每个动作还须人工确认）；
    点击通道本身尚未接线（TapExecutor 白名单不放行动圆盘所在的 y≈600）。
    来牌由 ``discard_vision`` 每帧读取（``read_discards_sequential``，需要
    前后两帧才认得出，换局会 ``forget_discards``）；读不出时 ``incoming_tile``
    为 ``None``，此时 L2 只能出"过"。

本模块只新增，不修改任何既有文件；识别与点击链路全部 import 复用。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from action_guard import ActionGuardResult, detect_action_ui
from action_policy import (
    CLAIM_TAP_CHANNEL_READY,
    ActionPolicyConfig,
    HandState,
    decide_action,
)
from claim_vision import read_claims
from harvest_tea import DEFAULT_SERIAL, EXPECTED_PACKAGE
from tap_executor import TapAborted, TapExecutor
from table_state import WALL_CARDS_STOP_AT, build_table_state
from turn_gate import (
    MAX_HISTORY as TURN_HISTORY_LIMIT,
    HandObservation,
    TurnVerdict,
    evaluate_turn,
    hand_observation,
)

# 本机实测路径优先，其次回落到 harvest_tea 的默认（两处是同一份 adb）。
ADB_CANDIDATES = ("D:/leidian/LDPlayer14/adb.exe",
                  "C:/leidian/LDPlayer14/adb.exe")
DEFAULT_ADB = next((path for path in ADB_CANDIDATES if Path(path).is_file()),
                   ADB_CANDIDATES[-1])

DEV_ROOT = Path(__file__).resolve().parent.parent / "采集" / "dev_auto_play"
MAX_CONSECUTIVE_ERRORS = 3
FRAME_SIZE = (900, 1600)          # 已验证的 ADB 原生截图尺寸
DEFAULT_INTERVAL_S = 1.2
# 与 tap_executor 同口径：mCurrentFocus / mFocusedApp / topResumedActivity。
FOCUS_PATTERNS = (
    r"mCurrentFocus=.*?\s([A-Za-z0-9_.]+)/",
    r"mFocusedApp=.*?\s([A-Za-z0-9_.]+)/",
    r"topResumedActivity=.*?\s([A-Za-z0-9_.]+)/",
    r"ResumedActivity:.*?\s([A-Za-z0-9_.]+)/",
)


class GateBlocked(RuntimeError):
    """门禁不放行：立即停止，不计入"连续异常"预算。"""


@dataclass
class RoundState:
    """一局的身份与计数；换局时整体重置（序号从 1 起，0 不代表任何一局）。"""

    seq: int = 1
    hud_key: bytes | None = None
    caishen: str | None = None

    def same_round(self, hud_key: bytes | None, caishen: str | None) -> bool:
        """HUD 指纹优先，其次财神小牌；两者都未知时视为同一局（方案保守）。"""
        if hud_key is not None and self.hud_key is not None:
            if not _same_hud(self.hud_key, hud_key):
                return False
        if caishen and self.caishen and caishen != self.caishen:
            return False
        return True


def _same_hud(first: bytes, second: bytes) -> bool:
    """等价于 assist_window.same_tea_round 的容差（≤8 个量化字节不同）。"""
    if len(first) != len(second):
        return False
    return sum(a != b for a, b in zip(first, second)) <= 8


def hud_fingerprint(image_path: str | Path | None) -> bytes | None:
    """房号/局数 HUD 指纹；被头像遮挡或读数失败时返回 None。"""
    if not image_path:
        return None
    path = Path(image_path)
    if not path.is_file():
        return None
    try:
        from assist_window import tea_round_fingerprint
        return tea_round_fingerprint(path)
    except Exception:  # noqa: BLE001 - 裸像素兜底会绕过 HUD 遮挡门禁
        return None


class AutoPlayLoop:
    """单帧一周期的连续代打循环（默认 dry_run）。

    Usage:
        loop = AutoPlayLoop(enabled=False)          # dry_run：只记录
        summary = loop.run(max_actions=3)           # 或 enabled=True 实测
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        serial: str = DEFAULT_SERIAL,
        adb_path: str = DEFAULT_ADB,
        mode: str = "tea_2p",
        caishen_label: str = "",
        session_dir: str | Path | None = None,
        max_actions: int = 20,
        duration_s: float = 600.0,
        interval_s: float = DEFAULT_INTERVAL_S,
        tap_executor: TapExecutor | None = None,
        shot_fn: Callable[[], bytes] | None = None,
        analyze_fn: Callable[[bytes, Path], dict] | None = None,
        foreground_fn: Callable[[], str] | None = None,
        card_reader: Callable[[np.ndarray], int | None] | None = None,
        action_guard_fn: Callable[[np.ndarray], ActionGuardResult] | None = None,
        round_key_fn: Callable[[Path], tuple[bytes | None, str | None]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
        claim_auto_click: bool = False,
        claim_policy: ActionPolicyConfig | None = None,
        read_discards: bool = True,
        discard_key: str | None = None,
        discard_fn: Callable[[np.ndarray], Any] | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是显式 bool；True 才允许真实点击")
        if mode != "tea_2p":
            raise ValueError("连续代打目前只支持 tea_2p（4 人局未验证）")
        self.enabled = enabled
        self.dry_run = not enabled
        self.serial = str(serial)
        self.adb_path = str(adb_path)
        self.mode = mode
        self.caishen_label = str(caishen_label or "")
        self.max_actions = max(1, int(max_actions))
        self.duration_s = max(0.0, float(duration_s))
        self.interval_s = max(0.0, float(interval_s))
        self._tap = tap_executor or TapExecutor(
            enabled=enabled, adb_path=self.adb_path, serial=self.serial)
        self._shot = shot_fn or self._adb_screenshot
        self._analyze = analyze_fn or self._default_analyze
        self._foreground = foreground_fn or self._adb_foreground
        self._card_reader = card_reader
        self._action_guard = action_guard_fn or detect_action_ui
        self._round_key = round_key_fn or self._default_round_key
        self._sleep = sleep_fn or time.sleep
        self._clock = clock_fn or time.monotonic

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.session_dir = Path(session_dir) if session_dir else DEV_ROOT / stamp
        self.frame_dir = self.session_dir / "frames"
        self.ledger_path = self.session_dir / "actions.jsonl"
        # session_dir 在 self._tap（161 行）之后才确定，所以只能在这里回填：
        # 让 tap 证据落进本局会话目录而不是 TapExecutor 的默认目录。
        # self._tap 可能是测试注入的替身/mock，普通属性赋值对它们无害。
        if self._tap is not None:
            self._tap.session_dir = self.session_dir
        # 目录延迟到第一次真正落盘时才创建：门禁不放行时不留空会话目录。
        self._dirs_ready = False

        # 行动（吃/碰/杠/胡/过）决策闸门。**默认只落证据，不自动点**：
        # ``claim_auto_click`` 打开也只是把决策标成 ready，真正点击还要等
        # ``CLAIM_TAP_CHANNEL_READY``（TapExecutor 白名单只放行手牌 ROI y≥720，
        # 行动圆盘在 y≈600，通道尚未接线）。
        self.claim_auto_click = bool(claim_auto_click)
        # Advisory claim ledger may run L2 without button glyphs; auto-click
        # still forces require_template_match inside decide_action (P0-2).
        self._claim_policy = claim_policy or ActionPolicyConfig(
            auto_click_enabled=self.claim_auto_click,
            require_template_match=bool(self.claim_auto_click))
        # 上一帧的识牌结果：行动行出现时用它构造 L2 的局面快照。
        self._last_result: dict | None = None
        # 牌河/来牌读取：行动决策（碰/吃/杠/胡）要拿"别家刚打出的那张"。
        # discard_vision 需要前后两帧才能确认来牌，所以用它的 sequential
        # 登记处每帧喂一次；换局必须 forget，否则会拿上一局的牌河当基线。
        self.read_discards = bool(read_discards)
        self.discard_key = str(discard_key or f"auto_play:{self.serial}")
        self._discard = discard_fn or self._read_discards_sequential
        self._discard_state: Any = None
        # 本帧的牌河读数能不能信：换局没确认过的帧，它的"来牌"是拿上一局
        # 的牌河做差分算出来的，绝不能喂给 L2。
        self._discard_trusted: bool = True

        self.round = RoundState()
        self.consecutive_errors = 0
        self.actions_done = 0
        self.stopped = False
        self.stop_reason = ""
        self.card_count: int | None = None
        self._ledger_started = False
        # 回合所有权门禁的帧历史（只在本局内有效，换局即清空）。
        self.turn_history: list[HandObservation] = []
        self.tick_index = 0

    # ------------------------------------------------------------------
    # 只读门禁
    # ------------------------------------------------------------------
    def check_gates(self) -> dict:
        """启动前自检：设备/前台包/房卡。任一硬门禁失败抛 GateBlocked。"""
        report: dict = {"adb": self.adb_path, "adb_exists": Path(self.adb_path).is_file(),
                        "serial": self.serial, "mode": self.mode,
                        "enabled": self.enabled, "dry_run": self.dry_run}
        if not report["adb_exists"]:
            raise GateBlocked(f"ADB 不存在: {self.adb_path}")
        if not self.serial or any(ch.isspace() for ch in self.serial):
            raise GateBlocked(f"模拟器序列号无效: {self.serial!r}")
        package = self._foreground()
        report["foreground"] = package
        if package != EXPECTED_PACKAGE:
            raise GateBlocked(f"前台包不是游戏: {package}（要求 {EXPECTED_PACKAGE}）")
        report["card_count"] = self._read_card_count(None)
        return report

    def _read_card_count(self, image: np.ndarray | None) -> int | None:
        """房卡读数只有显式注入的 reader 才可信，否则记 unknown。"""
        if self._card_reader is None:
            self.card_count = None
            return None
        try:
            self.card_count = self._card_reader(image)
        except Exception:  # noqa: BLE001 - 读数失败按 unknown 处理
            self.card_count = None
        return self.card_count

    def _adb_foreground(self) -> str:
        output = self._run_adb("shell", "dumpsys", "window", "windows",
                               timeout=15).decode(errors="replace")
        for pattern in FOCUS_PATTERNS:
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        output = self._run_adb("shell", "dumpsys", "activity", "activities",
                               timeout=15).decode(errors="replace")
        for pattern in FOCUS_PATTERNS:
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        return ""

    def _run_adb(self, *args: str, timeout: float = 20.0) -> bytes:
        command = [self.adb_path, "-s", self.serial, *args]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=timeout, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"ADB 调用失败: {exc}") from exc
        return result.stdout

    def _adb_screenshot(self) -> bytes:
        raw = self._run_adb("exec-out", "screencap", "-p")
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("模拟器截图不是有效 PNG")
        if image.shape[:2] != FRAME_SIZE:
            raise RuntimeError(
                f"画面尺寸 {image.shape[1]}x{image.shape[0]} 不是已验证的 "
                f"{FRAME_SIZE[1]}x{FRAME_SIZE[0]}")
        return raw

    # ------------------------------------------------------------------
    # 分析（复用 live_capture.analyze_bytes，不重造识别链路）
    # ------------------------------------------------------------------
    def _default_analyze(self, raw: bytes, image_path: Path) -> dict:
        from live_capture import analyze_bytes
        manual = bool(self.caishen_label)
        return analyze_bytes(
            raw, mode=self.mode, image_path=image_path,
            log_dir=self.session_dir / "logs",
            caishen_label=self.caishen_label, manual_caishen=manual)

    def _default_round_key(self, image_path: Path) -> tuple[bytes | None, str | None]:
        """Use independent image evidence, never a possibly stale manual pick."""
        from assist_window import tea_round_caishen_evidence

        label, _score = tea_round_caishen_evidence(image_path)
        return hud_fingerprint(image_path), label

    # ------------------------------------------------------------------
    # 台账
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        """Create the session folder on first write; frames only when saving."""
        if self._dirs_ready:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._dirs_ready = True

    def _write_event(self, event: dict) -> None:
        """Append one dev-tagged ledger line; header line marks the corpus."""
        self._ensure_dirs()
        if not self._ledger_started:
            header = {
                "dev": True,
                "note": "开发局数据（BOSS 20261024 规则），不进训练语料",
                "session": str(self.session_dir),
                "serial": self.serial, "mode": self.mode,
                "enabled": self.enabled, "dry_run": self.dry_run,
                "caishen": self.caishen_label or "auto",
                "started_at": datetime.now().astimezone().isoformat(),
            }
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
            self._ledger_started = True
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _save_frame(self, raw: bytes, tag: str) -> Path:
        self._ensure_dirs()
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.frame_dir / f"{stamp}_{tag}_{uuid.uuid4().hex[:6]}.png"
        with path.open("xb") as handle:
            handle.write(raw)
        return path

    # ------------------------------------------------------------------
    # 单帧一周期
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """跑一个周期并返回本周期的事件记录。任何异常都不重试本帧。"""
        started = self._clock()
        if self.stopped:
            return {"event": "skip", "reason": self.stop_reason}
        # 门禁一：前台包。不放行立即停手（不计异常预算）。
        try:
            package = self._foreground()
        except Exception as exc:  # noqa: BLE001 - 确认不了前台就不许点
            return self._stop("gate_foreground", f"无法确认前台应用: {exc}")
        if package != EXPECTED_PACKAGE:
            return self._stop("gate_foreground",
                              f"前台包不是游戏: {package!r}")
        try:
            raw = self._shot()
            frame_path = self._save_frame(raw, "before")
        except Exception as exc:  # noqa: BLE001 - 截图失败计入异常预算
            return self._anomaly("capture_failed", f"{exc}", started)
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        # 换局判定必须在读牌河**之前**：牌河基线一旦跨局，差分出来的"来牌"
        # 会是上一局的牌。guard 早退分支（行动行可见）在正式的换局判定之前
        # 就会用到 _discard_state，所以这里先判一次。本帧结果存下来复用，
        # 后面不再重复调 _round_key。
        hud_key, key_caishen = self._probe_round_key(frame_path)
        # 换局没确认过的帧，本帧读出的"来牌"是跨局差分的结果，标记不可信：
        # 基线照样作废，且本帧的读数不许进 L2（见 _claim_policy_event）。
        self._discard_trusted = self.round.same_round(hud_key, key_caishen)
        if not self._discard_trusted:
            self._forget_discards()
        # 每帧喂一次牌河：来牌要前后两帧才认得出，缺了它行动决策只能出"过"。
        self._read_discards(image)
        try:
            action_guard = self._action_guard(image)
        except Exception as exc:  # noqa: BLE001 - 门禁异常必须 fail closed
            action_guard = ActionGuardResult(
                "uncertain", reason=f"行动界面检测异常: {exc}")
        if action_guard.blocks_discard:
            guard_event = {
                "ts": datetime.now().astimezone().isoformat(),
                "round_seq": self.round.seq, "frame": str(frame_path),
                "status": "guard", "suggestion": None,
                "suggestion_id": None, "caishen": None,
                "event": "idle", "guard": action_guard.state,
                "guard_boxes": [list(box) for box in action_guard.boxes],
                "reason": action_guard.reason,
            }
            # P0-5: refresh hand from THIS frame before claim decide.
            try:
                claim_analysis = self._analyze(raw, frame_path)
                if isinstance(claim_analysis, dict):
                    self._last_result = claim_analysis
                else:
                    claim_analysis = None
            except Exception:  # noqa: BLE001 - claim path fail-closed
                claim_analysis = None
            guard_event.update(self._claim_policy_event(
                image, frame_path, analysis=claim_analysis))
            if action_guard.state == "uncertain":
                return self._anomaly(
                    "action_guard_uncertain", action_guard.reason, started,
                    guard_event,
                )
            return self._event(guard_event)
        wall_cards = None
        if self._card_reader is not None:
            wall_cards = self._read_card_count(image)
            if wall_cards is not None and wall_cards <= WALL_CARDS_STOP_AT:
                return self._stop(
                    "card_exhausted",
                    f"牌墙/房卡={wall_cards}≤{WALL_CARDS_STOP_AT}，停止代打",
                )
        try:
            result = self._analyze(raw, frame_path)
        except Exception as exc:  # noqa: BLE001 - 识牌异常计入异常预算
            return self._anomaly("analyze_failed", f"{exc}", started)
        # 行动行出现时要用它构造 L2 的局面快照（可能拿不到完整手牌）。
        self._last_result = result if isinstance(result, dict) else None
        # 门禁二：换局（HUD 指纹 / 财神 label）。指纹在本帧开头已经算过一次
        # （读牌河之前要先作废跨局基线），这里直接复用，不重复调用。
        if self.enabled and hud_key is None and key_caishen is None:
            return self._stop("gate_round_identity", "房号局数与画面财神都不可读，拒绝自动点击")
        if (self.enabled and self.caishen_label and key_caishen
                and key_caishen != self.caishen_label):
            return self._stop("gate_manual_caishen", "手选财神与当前画面不一致，须重新确认")
        caishen = key_caishen or result.get("caishen")
        changed_round = not self.round.same_round(hud_key, caishen)
        if changed_round:
            self.round.seq += 1
            self.round.hud_key = hud_key
            self.round.caishen = caishen
            # 换局：稳定帧必须重新攒（换局后的第一帧绝不放行）。
            self.turn_history.clear()
            # 牌河基线也要作废：拿上一局的牌河做差分会白丢一轮（后果是弃权，
            # 不会误报，但没必要）。
            self._forget_discards()
            self._write_event({
                "ts": datetime.now().astimezone().isoformat(),
                "event": "round_reset", "round_seq": self.round.seq,
                "frame": str(frame_path), "caishen": caishen,
                "reason": "换局信号（HUD 指纹或财神变化），不沿用上局建议",
            })
        else:
            if hud_key is not None:
                self.round.hud_key = hud_key
            if caishen:
                self.round.caishen = caishen
        base = {
            "ts": datetime.now().astimezone().isoformat(),
            "round_seq": self.round.seq, "frame": str(frame_path),
            "status": result.get("status"), "suggestion": result.get("suggestion"),
            "suggestion_id": result.get("suggestion_id"),
            "caishen": result.get("caishen"),
        }
        # P0-5: refresh unified TableState from THIS frame before decide/discard.
        incoming_tile = None
        incoming_source = None
        if self._discard_state is not None and self._discard_trusted:
            incoming_tile = getattr(self._discard_state, "incoming_tile", None)
            incoming_source = getattr(self._discard_state, "incoming_source", None)
        result_for_table = dict(result)
        if caishen:
            result_for_table["caishen"] = caishen
        table = build_table_state(
            result_for_table, image,
            wall_cards=wall_cards,
            discards_trusted=bool(self._discard_trusted),
            incoming_tile=incoming_tile,
            incoming_source=incoming_source,
        )
        base["table_state"] = table.ledger()
        if self.enabled and table.should_stop_for_unknown_caishen:
            return self._stop(
                "gate_unknown_caishen",
                "本帧财神未知：停止代打（P0-5）",
                base,
            )
        # 门禁三：回合所有权。dry_run 与真点都必须先过这一关，台账逐帧留证。
        verdict = self._evaluate_turn_gate(
            result, image, record=not changed_round)
        base["turn_gate"] = verdict.ledger()
        if verdict.state == "blocked":
            # 证据自相矛盾，或结算/模态弹窗画面 → fail closed：不重试、不补点。
            # 台账统一记 gate="gate_turn"；具体归类在 turn_gate.code
            # （如 "gate_settled"），同一行即可看出"其实是牌局结束了"。
            return self._stop("gate_turn", verdict.reason, base)
        if changed_round:
            if self.enabled and self.caishen_label:
                return self._stop("gate_manual_caishen", "检测到换局；手选财神须重新确认",
                                  base)
            return self._event({**base, "event": "idle", "reason": "换局帧跳过，等待下一张清晰画面"})
        # 无建议/弃权 → 只等待，绝不兜底点击。
        if result.get("status") != "advise" or not result.get("suggested_boxes"):
            return self._event({**base, "event": "idle",
                                "reason": result.get("reason") or "无可执行建议"})
        if verdict.state == "wait":
            # 正常等待：不点击、不计异常、不停机。
            return self._event({**base, "event": "idle", "reason": verdict.reason})
        if self.actions_done >= self.max_actions:
            return self._stop("max_actions", f"达到 --max-actions={self.max_actions}", base)
        if self.dry_run:
            try:
                plan = self._tap.plan(result)
            except TapAborted as exc:
                return self._anomaly("plan_rejected", str(exc), started, base)
            self.actions_done += 1
            return self._event({**base, "event": "dry_run_plan",
                                "tap": [plan.x, plan.y], "box": list(plan.box),
                                "verified": None,
                                "elapsed_ms": round((self._clock() - started) * 1000, 1)})
        # 真点：TapExecutor 四道闸（前台/重截屏重定位/白名单/点后 −1 验证）。
        try:
            outcome = self._tap.execute(result)
        except TapAborted as exc:
            return self._stop("tap_aborted", f"点击或点后验证未通过：{exc}", base)
        except Exception as exc:  # noqa: BLE001 - 未预期异常同样停手
            return self._stop("tap_failed", f"点击结果未知：{exc}", base)
        self.actions_done += 1
        after_path = None
        try:
            after_raw = self._shot()
            after_path = str(self._save_frame(after_raw, "after"))
        except Exception as exc:  # noqa: BLE001 - 台阶账不影响已验证的点击
            after_path = f"<截图失败: {exc}>"
        event = self._event({
            **base, "event": "action", "after_frame": after_path,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed") and outcome.get("after_count")
                             == (outcome.get("before_count") or 0) - 1),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })
        if not event["verified"]:
            self._stop("tap_unverified", "点击后手牌数量未验证，停止自动点击", base)
        return event

    # ------------------------------------------------------------------
    # 牌河/来牌读取（供行动决策取"别家刚打出的那张"）
    # ------------------------------------------------------------------
    def _probe_round_key(self, frame_path: Path) -> tuple[bytes | None, str | None]:
        """算本帧的换局指纹；识别失败按未知处理（fail closed）。"""
        try:
            return self._round_key(frame_path)
        except Exception:  # noqa: BLE001 - 换局识别失败按未知处理
            return None, None

    def _read_discards_sequential(self, image: np.ndarray) -> Any:
        """调 ``discard_vision`` 读本帧牌河，并登记为下一帧的基线。"""
        from discard_vision import read_discards_sequential

        return read_discards_sequential(image, key=self.discard_key)

    def _read_discards(self, image: np.ndarray) -> None:
        """每帧读一次牌河；失败只丢"来牌"，不许拖垮循环。"""
        self._discard_state = None
        if not self.read_discards:
            return
        try:
            self._discard_state = self._discard(image)
        except Exception:  # noqa: BLE001 - 读牌河失败不影响其它门禁
            self._discard_state = None
            # 基线也一起作废：异常帧之后不该再拿更早的帧做差分——那样报出的
            # "来牌"事实可能对，但时效已过（可能早被鸣走）。宁可下一帧弃权一次。
            self._forget_discards()

    def _forget_discards(self) -> None:
        """换局时作废牌河基线（``discard_vision`` 的登记处按 key 隔离）。"""
        self._discard_state = None
        if not self.read_discards:
            return
        try:
            from discard_vision import forget_discards

            forget_discards(self.discard_key)
        except Exception:  # noqa: BLE001 - 清不掉也只是少一层保险
            pass

    # ------------------------------------------------------------------
    # 行动（吃/碰/杠/胡/过）决策闸门
    # ------------------------------------------------------------------
    def _claim_policy_event(self, image: np.ndarray, frame_path: Path,
                             analysis: dict | None = None) -> dict:
        """行动行可见时跑一次 ``action_policy.decide_action``，只落证据。

        默认**不点击**：``claim_auto_click=False`` 时每个动作都还要人工确认；
        即便确认过，``TapExecutor`` 的几何白名单只放行手牌 ROI（y≥720），
        行动圆盘在 y≈600，点击通道尚未接线，所以这里最多记一条
        ``auto_click`` 意向，绝不发 ADB 命令。

        Args:
            image: 本帧 BGR 图（已确认 1600x900）。
            frame_path: 本帧落盘路径，写进台账便于回放。

        Returns:
            ``{"claim": {...}}``，可直接并进本帧事件。
        """
        try:
            claim_state = read_claims(image)
        except Exception as exc:  # noqa: BLE001 - 读不出即记异常，不放行
            return {"claim": {"status": "read_failed", "reason": f"{exc}",
                              "frame": str(frame_path)}}
        # 来牌从本帧的牌河读数里取（discard_vision 给的是 DiscardState）。
        # 换局没确认过的帧：本帧"来牌"可能是跨局差分出来的，不许进 L2。
        # P0-5: never reuse a prior frame's analysis for claim decisions.
        payload: dict = dict(analysis) if isinstance(analysis, dict) else {}
        if self._discard_state is not None and self._discard_trusted:
            payload["discards"] = self._discard_state
        hand = HandState.from_analysis(payload)
        decision = decide_action(claim_state, hand, self._claim_policy, image=image)
        if decision is None:
            return {"claim": {
                "status": "abstain",
                "frame": str(frame_path),
                "known": bool(claim_state.known),
                "buttons": [button.kind for button in claim_state.buttons],
                "reason": claim_state.reason,
            }}
        payload = decision.ledger()
        payload["frame"] = str(frame_path)
        payload["status"] = ("blocked" if decision.blocked_reason else
                             "ready" if decision.executable else
                             "awaiting_human_confirm")
        payload["claim_reason"] = claim_state.reason
        if decision.executable and self.claim_auto_click:
            payload["auto_click"] = {
                "requested": True,
                "executed": False,
                "channel_ready": CLAIM_TAP_CHANNEL_READY,
                "reason": ("点击通道未接线：TapExecutor 几何白名单只放行手牌 "
                           "ROI（y≥720），行动圆盘在 y≈600"
                           if not CLAIM_TAP_CHANNEL_READY else "通道已接线，待实机验证"),
            }
        return {"claim": payload}

    # ------------------------------------------------------------------
    # 回合所有权门禁
    # ------------------------------------------------------------------
    def _evaluate_turn_gate(self, result: dict, image, *,
                            record: bool = True) -> TurnVerdict:
        """跑一次 ``turn_gate.evaluate_turn`` 并维护「连续帧」历史。

        Args:
            result: 本帧分析结果（只读）。
            image: 本帧 BGR 图（只读，测手牌面亮度）。
            record: 是否把本帧手牌记进历史。换局帧必须 ``False``——
                换局后的第一帧不放行，也不参与下一帧的"连续一致"。

        Returns:
            本帧的门禁判定。
        """
        self.tick_index += 1
        verdict = evaluate_turn(result, image, self.turn_history,
                                round_seq=self.round.seq, tick=self.tick_index)
        observation = hand_observation(result, round_seq=self.round.seq,
                                       tick=self.tick_index)
        if verdict.state == "blocked":
            # 证据自相矛盾：历史同样作废，绝不拿来自相矛盾的帧当稳定基线。
            self.turn_history.clear()
            return verdict
        if record and observation is not None:
            self.turn_history.append(observation)
            del self.turn_history[:-TURN_HISTORY_LIMIT]
        return verdict

    # ------------------------------------------------------------------
    # 事件/停机
    # ------------------------------------------------------------------
    def _event(self, event: dict) -> dict:
        """Write a normal (non-error) event and clear the error streak.

        "连续 3 次异常" 必须是**连续**：任何一次正常周期都清零计数，
        否则偶发失败会累积成误停机。
        """
        self.consecutive_errors = 0
        event.setdefault("card_count", self.card_count)
        event["consecutive_errors"] = self.consecutive_errors
        self._write_event(event)
        return event

    def _anomaly(self, kind: str, reason: str, started: float,
                 base: dict | None = None) -> dict:
        self.consecutive_errors += 1
        event = {**(base or {}), "ts": datetime.now().astimezone().isoformat(),
                 "event": "error", "error": kind, "reason": reason,
                 "consecutive_errors": self.consecutive_errors,
                 "elapsed_ms": round((self._clock() - started) * 1000, 1)}
        self._write_event(event)
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.stopped = True
            self.stop_reason = f"连续 {self.consecutive_errors} 次异常：{reason}"
            stop = {"ts": datetime.now().astimezone().isoformat(),
                    "event": "stop", "reason": self.stop_reason,
                    "consecutive_errors": self.consecutive_errors}
            self._write_event(stop)
        return event

    def _stop(self, kind: str, reason: str, extra: dict | None = None) -> dict:
        """停手并记台账；``extra`` 用于带上本帧的 ``turn_gate`` 证据。"""
        self.stopped = True
        self.stop_reason = reason
        event = {**(extra or {}), "ts": datetime.now().astimezone().isoformat(),
                 "event": "stop", "gate": kind, "reason": reason}
        self._write_event(event)
        return event

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> dict:
        """直到停手条件满足；返回本次运行摘要。"""
        self.check_gates()
        began = self._clock()
        while not self.stopped:
            self.tick()
            if self.stopped:
                break
            if self.actions_done >= self.max_actions:
                self._stop("max_actions", f"达到 --max-actions={self.max_actions}")
                break
            if self.duration_s and self._clock() - began >= self.duration_s:
                self._stop("duration", f"达到运行时长 {self.duration_s:.0f}s")
                break
            if self.interval_s:
                self._sleep(self.interval_s)
        return {
            "session": str(self.session_dir),
            "ledger": str(self.ledger_path),
            "actions": self.actions_done,
            "dry_run": self.dry_run,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "consecutive_errors": self.consecutive_errors,
            "rounds": self.round.seq,
            "card_count": self.card_count,
        }
