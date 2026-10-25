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
  * 换局/HUD/财神冲突采用连续帧防抖，候选期零点击；自动财神不沿用旧局值；
  * HUD 与画面财神连续 3 帧都不可读 → 停止；期间拒绝真实点击；
  * 牌墙/房卡读数 ≤ 16 → 停止（P0-5）；不可读记 unknown，不阻断开发局，但绝不点充值；
  * 启用且本帧财神未知 → 最多等待 2 帧有界重拍，连续第 3 帧仍未知才停止；
    等待期间零点击且不沿用旧财神（P0-5 ``gate_unknown_caishen``）；
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
    落台账不点击**（``claim_auto_click=False``）；显式开启后经独立圆盘白名单
    单击，并用 L3 盘面迁移验证，失败立即停机。吃的二次选组仍保持关闭。
    来牌由 ``discard_vision`` 每帧读取（``read_discards_sequential``，需要
    前后两帧才认得出，换局会 ``forget_discards``）；读不出时 ``incoming_tile``
    为 ``None``，此时 L2 只能出"过"。

  * **胡提示面板（听牌提示条）**：``turn_gate`` 判 ``code="hint_panel"``（中央面板
    单信号、手牌带可读、结算双信号未成立）时只等待并计数；同局内存在面板前
    advise 好读数且面板持续 ≥3 帧时，兜底摸切「刚摸的牌」（手牌最右分离位，
    ``locate_drawn_tile`` 校验），目标交 TapExecutor 正常路径；定位不到/读数
    过期一律继续等待，双信号结算永远 blocked 停手。
识别与点击链路全部 import 复用；两个设备按 serial 启动独立实例，共用本模块。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
import uuid
from collections import Counter
from statistics import median
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

import panel_recovery
from action_guard import ActionGuardResult, detect_action_ui
from action_policy import (
    CLAIM_TAP_CHANNEL_READY,
    ACTION_CHI,
    ACTION_PASS,
    ActionPolicyConfig,
    BoardState,
    HandState,
    choose_chi_panel_option,
    decide_action,
    recommended_runner_config,
    validate_runner_config,
    verify_after_click,
)
from claim_vision import KIND_CANCEL, read_claims
from chi_panel_vision import read_chi_panel
from invite_dialog import read_invitation
from harvest_tea import DEFAULT_SERIAL, EXPECTED_PACKAGE
from single_bright_cut import SingleBrightCutTracker, detect_single_bright_cut
from tap_executor import (
    TapAborted,
    TapRejected,
    TapExecutor,
    TapPrecheckRetryExhausted,
    TapUnverified,
    label_for_tile,
)
from table_state import WALL_CARDS_STOP_AT, build_table_state
from follow_pipeline import build_frame_facts, missing_decision_facts
from session_logger import SessionLogger
from serial_lease import guard_live_run
from turn_gate import (
    MAX_HISTORY as TURN_HISTORY_LIMIT,
    HandObservation,
    TurnVerdict,
    evaluate_turn,
    hand_observation,
    known_caishen_id,
    HINT_PANEL_CODE,
    detect_settled_screen,
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



# 胡提示面板兜底：面板连续命中至少这么多帧才允许尝试摸切（防一闪误触）。
HINT_PANEL_MIN_TICKS = 3
# 兜底尝试节奏：达到门槛后每隔这么多帧再试一次（执行器在面板未消失前
# 会在点击前中止，反复尝试只会刷证据文件）。
HINT_PANEL_RETRY_EVERY = 10
# 换局/财神/HUD 都不再用单帧作决定。未知身份给三帧预算；明确冲突或换局
# 信号要求连续两帧相同，既挡瞬时坏读，也不沿用旧财神跨局点击。
ROUND_CHANGE_CONFIRM_TICKS = 2
ROUND_IDENTITY_UNKNOWN_BUDGET = 3
MANUAL_CAISHEN_CONFLICT_TICKS = 2
CAISHEN_UNKNOWN_BUDGET = 3
# 吃链点吃后等候选面板的有界轮询：次数 × 间隔（另加每帧截图/识图耗时）。
CHI_PANEL_POLL_ATTEMPTS = 6
CHI_PANEL_POLL_INTERVAL_S = 0.4
# 吃直接完成时客户端会把暗手整体压暗；实机真帧 p70(V)=100、亮像素≈6.7%，
# 正常手牌 p70(V)=214、亮像素≈77%。这里只用于“吃后迁移验证”，不改全局识牌阈值。
CHI_DIMMED_HAND_ROI = (250, 760, 1590, 895)  # x0,y0,x1,y1
CHI_DIMMED_P70_MAX = 125.0
CHI_DIMMED_BRIGHT_V = 150
CHI_DIMMED_BRIGHT_RATIO_MAX = 0.15
# claim 点后验证的落定轮询（2026-10-25 实机 Bug 2）：首帧决定性读数
# 直接采用；含糊则每隔 INTERVAL 重拍，直到验证通过、连续两帧签名
# 一致（落定）或 ATTEMPTS 耗尽（fail-closed）。
CLAIM_SETTLE_ATTEMPTS = 8
CLAIM_SETTLE_INTERVAL_S = 0.4


def locate_drawn_tile(result: dict, good_hand_ids) -> dict | None:
    """在面板帧里定位「刚摸的牌」：暗手最右、且处于分离位的那一张。

    Args:
        result: 本帧（面板帧）的 analyze 结果（只读）。
        good_hand_ids: 面板出现前最后一帧好读数（advise）的暗手多重集。

    Returns:
        ``{"tile_id": int, "box": [x0, y0, x1, y1]}``；任一校验不过返回
        ``None``（fail-closed，调用方继续等待）。

    校验（全部满足才定位）：
      * 本帧 diagnostics 中 accepted 暗手的多重集与 hand_ids 完全一致；
      * 与好读数相比，多重集增/删各至多 1 张（实测：面板下缘会遮住中间
        一张牌的牌框 → 删 1；面板在摸牌前出现时则增 1 = 刚摸的牌）；
      * 若增 1 张，它必须正是几何最右张；
      * 最右张与左邻的间距大于其余相邻间距的中位数（摸牌的分离位特征，
        实测 101px vs 常规 92px）。
    """
    items: list[tuple[int, int, list[int]]] = []
    for entry in result.get("vision_diagnostics") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("region") not in (None, "concealed"):
            continue
        if not entry.get("accepted"):
            continue
        tile_id = entry.get("tile_id")
        box = entry.get("box")
        if not isinstance(tile_id, int) or isinstance(tile_id, bool):
            continue
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        items.append((int(box[0]), int(tile_id), [int(v) for v in box]))
    if len(items) < 4:
        return None
    items.sort(key=lambda item: item[0])
    panel_ids = sorted(tile for _x, tile, _box in items)
    try:
        hand_ids = sorted(int(tile) for tile in (result.get("hand_ids") or []))
    except (TypeError, ValueError):
        return None
    if panel_ids != hand_ids:
        return None
    try:
        good = sorted(int(tile) for tile in (good_hand_ids or []))
    except (TypeError, ValueError):
        return None
    if not good:
        return None
    panel_counter = Counter(panel_ids)
    good_counter = Counter(good)
    added = list((panel_counter - good_counter).elements())
    removed = list((good_counter - panel_counter).elements())
    if len(added) > 1 or len(removed) > 1:
        return None
    rightmost_tile = items[-1][1]
    if added and added[0] != rightmost_tile:
        return None
    diffs = [items[i + 1][0] - items[i][0] for i in range(len(items) - 1)]
    if diffs[-1] <= median(diffs[:-1]):
        return None
    return {"tile_id": rightmost_tile, "box": items[-1][2]}


def direct_discard_redraw_proof(
    before_result: dict,
    after_result: dict,
    expected_tile: int,
) -> dict:
    """证明第一击已直接出牌，随后又摸入一张牌。

    必须同时满足：前后完整手牌、玩法/财神一致、暗手张数不变、
    目标牌恰好少 1 张、同时恰好多 1 张新牌。任一条件不满足都拒绝。
    """
    out = {
        "discarded": False, "after_count": None, "reason": "",
        "removed": [], "added": [],
    }
    if not isinstance(before_result, dict) or not isinstance(after_result, dict):
        out["reason"] = "缺少前后整手分析"
        return out
    before = before_result.get("hand_ids")
    after = after_result.get("hand_ids")
    if not isinstance(before, (list, tuple)) or not isinstance(after, (list, tuple)):
        out["reason"] = "缺少完整 hand_ids"
        return out
    try:
        before = [int(v) for v in before]
        after = [int(v) for v in after]
    except (TypeError, ValueError):
        out["reason"] = "hand_ids 含非法值"
        return out
    out["after_count"] = len(after)
    if not before or len(before) != len(after):
        out["reason"] = f"前后暗手张数不等（{len(before)}→{len(after)}）"
        return out
    if before_result.get("mode") and after_result.get("mode"):
        if before_result.get("mode") != after_result.get("mode"):
            out["reason"] = "前后玩法不一致"
            return out
    before_cs = known_caishen_id(before_result)
    after_cs = known_caishen_id(after_result)
    if before_cs is None or after_cs is None or before_cs != after_cs:
        out["reason"] = "前后财神未知或不一致"
        return out
    removed = list((Counter(before) - Counter(after)).elements())
    added = list((Counter(after) - Counter(before)).elements())
    out["removed"] = removed
    out["added"] = added
    if removed != [int(expected_tile)]:
        out["reason"] = (
            f"目标牌变化不唯一：期望移除 {expected_tile}，实际移除 {removed}")
        return out
    if len(added) != 1:
        out["reason"] = f"补摸变化不唯一：新增 {added}"
        return out
    out["discarded"] = True
    out["reason"] = (
        f"第一击后目标牌 {expected_tile} 已从完整手牌移除，同时新摸入 "
        f"{added[0]}，暗手仍为 {len(after)} 张；判定第一击已完成出牌，禁止第二击")
    return out


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
        event_callback: Callable[[dict, dict | None], None] | None = None,
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
        # UI observer only: receives a copy after the append-only ledgers are
        # safely written.  Callback failures must never affect the play loop.
        self._event_callback = event_callback
        self._stop_requested_reason: str | None = None
        # Immediate revocation: every physical tap (including second taps)
        # checks this, not just the next run-loop iteration.
        try:
            self._tap.authorization_check = (
                lambda: self.enabled and not self.stopped
                and self._stop_requested_reason is None
            )
        except (AttributeError, TypeError):
            # Injected dry-run test doubles need not expose this hook.
            if self.enabled and isinstance(self._tap, TapExecutor):
                raise

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.session_dir = Path(session_dir) if session_dir else DEV_ROOT / stamp
        self.frame_dir = self.session_dir / "frames"
        self.ledger_path = self.session_dir / "actions.jsonl"
        # Structured follow-session debug log (vision/gates/caishen/decisions).
        # Never flips CLAIM_TAP_CHANNEL_READY; dual-writes beside actions.jsonl.
        self.follow_log_path = self.session_dir / "follow_session.jsonl"
        self._follow_log = SessionLogger(self.session_dir)
        # session_dir 在 self._tap（161 行）之后才确定，所以只能在这里回填：
        # 让 tap 证据落进本局会话目录而不是 TapExecutor 的默认目录。
        # self._tap 可能是测试注入的替身/mock，普通属性赋值对它们无害。
        if self._tap is not None:
            self._tap.session_dir = self.session_dir
        # 目录延迟到第一次真正落盘时才创建：门禁不放行时不留空会话目录。
        self._dirs_ready = False

        # 行动（吃/碰/杠/胡/过）决策闸门。默认只落证据；显式打开后仍须
        # CLAIM_TAP_CHANNEL_READY + 模板/L2/L3 全部通过。行动圆盘使用独立
        # 白名单，不放宽普通出牌 y>=720 的硬边界。
        self.claim_auto_click = bool(claim_auto_click)
        # Advisory claim ledger may run L2 without button glyphs; auto-click
        # still forces require_template_match inside decide_action (P0-2).
        self._claim_policy = claim_policy or (
            recommended_runner_config(auto_click_enabled=True)
            if self.claim_auto_click else
            ActionPolicyConfig(auto_click_enabled=False,
                               require_template_match=False)
        )
        config_problems = validate_runner_config(self._claim_policy)
        if config_problems:
            raise ValueError("行动策略配置不安全: " + "; ".join(config_problems))
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
        self.stop_gate = ""
        self.card_count: int | None = None
        self._ledger_started = False
        # 回合所有权门禁的帧历史（只在本局内有效，换局即清空）。
        self.turn_history: list[HandObservation] = []
        self.tick_index = 0
        # 胡提示面板兜底状态：面板前最后一次 advise 好读数 + 面板连续帧数。
        self._last_advise_read: dict | None = None
        self._hint_panel_streak = 0
        self._hint_panel_round: int | None = None
        # 待二击恢复：状态文件按 serial 固定（跨进程/跨会话目录可找回），
        # 执行器落盘、循环在面板帧上判定恢复（panel_recovery）。
        self._panel_recovery_path = panel_recovery.state_path_for(self.serial)
        self._single_bright = SingleBrightCutTracker()
        # 吃链在本行动窗弃权过的标记（局序+来牌+闭手），防重复点吃。
        self._chi_abstain_key: tuple | None = None
        self._round_candidate: tuple[bytes | None, str | None] | None = None
        self._round_candidate_ticks = 0
        self._round_identity_unknown_ticks = 0
        self._unknown_caishen_ticks = 0
        self._manual_caishen_conflict: str | None = None
        self._manual_caishen_conflict_ticks = 0

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
        # 双端共用同一套代码时，每一行都必须能独立追溯到设备、玩法、局和帧；
        # 不依赖读者先找到文件头，方便把两端同一局配对复盘。
        event.setdefault("device_serial", self.serial)
        event.setdefault("mode", self.mode)
        event.setdefault("tick", self.tick_index)
        event.setdefault("round_seq", self.round.seq)
        if not self._ledger_started:
            header = {
                "dev": True,
                "note": "开发局数据（BOSS 20261024 规则），不进训练语料",
                "session": str(self.session_dir),
                "serial": self.serial, "mode": self.mode,
                "enabled": self.enabled, "dry_run": self.dry_run,
                "caishen": self.caishen_label or "auto",
                "started_at": datetime.now().astimezone().isoformat(),
                "claim_tap_channel_ready": CLAIM_TAP_CHANNEL_READY,
            }
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
            self._follow_log.write_header({
                "serial": self.serial, "mode": self.mode,
                "enabled": self.enabled, "dry_run": self.dry_run,
                "caishen": self.caishen_label or "auto",
                "dev": True,
            })
            self._ledger_started = True
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Structured follow-session row for post-hoc debug (no taps).
        analysis = self._last_result if isinstance(self._last_result, dict) else None
        self._follow_log.append(event, tick=self.tick_index, analysis=analysis)
        if self._event_callback is not None:
            try:
                self._event_callback(dict(event),
                                     dict(analysis) if analysis is not None else None)
            except Exception:  # noqa: BLE001 - UI/observer must not break autoplay
                pass

    def request_stop(self, reason: str = "用户停止全自动代打") -> None:
        """Request a graceful stop at the next loop boundary.

        This is intentionally not a forceful process kill: any in-flight tap
        keeps its normal post-click verification, then the loop records a
        terminal ``user_stop`` event before returning.
        """
        self._stop_requested_reason = str(reason or "用户停止全自动代打")

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
        # 入桌邀请优先级高于行动行、换局和识牌：弹窗盖住牌桌时继续分析会把
        # 弹窗元素误读成游戏状态。只允许“邀请您进入”文案 + 矩形“取消”按钮
        # 同帧完整命中后拒绝；这条通道绝不复用吃牌面板的蓝色圆形取消。
        invitation = read_invitation(image)
        if invitation.present:
            invitation_event = {
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path),
                "invitation": invitation.ledger(),
                "reason": invitation.reason,
            }
            if not invitation.complete or invitation.cancel_center is None:
                return self._stop(
                    "gate_invitation_unresolved",
                    "发现入桌邀请，但矩形取消按钮证据不完整；停手且不点其它位置",
                    invitation_event,
                )
            try:
                outcome = self._tap.execute_invitation_cancel(
                    invitation.cancel_center, image=image,
                    session_dir=self.session_dir)
            except TapAborted as exc:
                return self._stop(
                    "invitation_cancel_aborted",
                    f"邀请取消未通过安全复核：{exc}", invitation_event)
            except Exception as exc:  # noqa: BLE001 - 邀请遮挡下任何未知结果都停手
                return self._stop(
                    "invitation_cancel_failed",
                    f"邀请取消结果未知：{exc}", invitation_event)
            invitation_event["invitation_cancel"] = outcome
            if self.dry_run:
                invitation_event["reason"] = (
                    "dry_run 已识别邀请及矩形取消按钮；只记录计划，不点击")
                return self._event(invitation_event)
            invitation_event["event"] = "invitation_declined"
            invitation_event["reason"] = "已点矩形取消并验证邀请文案消失"
            return self._event(invitation_event)
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
            claim_event = self._claim_policy_event(
                image, frame_path, analysis=claim_analysis)
            terminal = claim_event.pop("_terminal", None)
            guard_event.update(claim_event)
            if terminal:
                return self._stop(
                    terminal["gate"], terminal["reason"], guard_event)
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
        caishen = key_caishen or result.get("caishen")
        round_signal = self._debounce_round_signal(
            hud_key, key_caishen, caishen)
        if round_signal["state"] == "identity_unknown":
            if self.enabled and round_signal["ticks"] >= ROUND_IDENTITY_UNKNOWN_BUDGET:
                return self._stop(
                    "gate_round_identity",
                    f"房号局数与画面财神连续 {round_signal['ticks']} 帧都不可读，拒绝自动点击",
                    {"round_signal": round_signal, "frame": str(frame_path)},
                )
            return self._event({
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path), "round_signal": round_signal,
                "reason": "局身份单帧不可读，等待有界重拍",
            })
        if round_signal["state"] == "manual_conflict":
            if round_signal["ticks"] >= MANUAL_CAISHEN_CONFLICT_TICKS:
                return self._stop(
                    "gate_manual_caishen",
                    f"手选财神与画面连续 {round_signal['ticks']} 帧不一致，须重新确认",
                    {"round_signal": round_signal, "frame": str(frame_path)},
                )
            return self._event({
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path), "round_signal": round_signal,
                "reason": "财神单帧冲突，等待连续帧确认",
            })
        if round_signal["state"] == "round_suspect":
            self.turn_history.clear()
            self._single_bright.reset()
            return self._event({
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path), "caishen": caishen,
                "round_signal": round_signal,
                "reason": "检测到单帧换局信号，等待第二帧确认；期间禁止点击",
            })
        changed_round = round_signal["state"] == "round_changed"
        if changed_round:
            self.round.seq += 1
            self.round.hud_key = hud_key
            self.round.caishen = caishen
            self._unknown_caishen_ticks = 0
            # 换局：稳定帧必须重新攒（换局后的第一帧绝不放行）。
            self.turn_history.clear()
            # 牌河基线也要作废：拿上一局的牌河做差分会白丢一轮（后果是弃权，
            # 不会误报，但没必要）。
            self._forget_discards()
            self._single_bright.reset()
            # 换局确认：上局若有待二击状态一并作废，防跨局误用。
            panel_recovery.clear_state(self._panel_recovery_path)
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
            "round_signal": round_signal,
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
        # Unified path parity with assist_window FollowPipeline (read-only facts).
        facts = build_frame_facts(
            result_for_table, table,
            round_seq=self.round.seq,
            turn_status=result.get("status"),
            turn_actionable=(result.get("status") == "advise"
                             and bool(result.get("suggested_boxes"))),
        )
        base["frame_facts"] = facts.ledger()
        missing = missing_decision_facts(result_for_table, table)
        # dry_run / observe: abstain on missing decision facts.
        # enabled: keep hard stop via should_stop_for_unknown_caishen below.
        if (missing and result.get("status") == "advise" and not self.enabled):
            return self._event({
                **base,
                "event": "idle",
                "status": "abstain",
                "suggestion": None,
                "suggestion_id": None,
                "reason": f"决策事实不足({','.join(missing)})；本帧弃权",
            })
        # P0-5 状态门控（2026-10-25 实机 Bug 1）：未知计数只在本帧
        # 确认「已发牌在局中」时才累计。等候室（未发牌）与局间过渡期
        # 财神指示本就不存在/不可读，此时累计会在 3 帧（≈3.6s）内
        # 误杀循环——实机当晚等候室启动 3 次、局间过渡 1 次皆中招，
        # 过渡误杀后牌桌无人操作、整场被系统解散。门控判据 = 本帧
        # 读到已发牌手牌（TableState.concealed_count 非空）；读不到
        # 手牌的帧只冻结计数（不清零：局中偶发一帧手牌读残不洗掉
        # 已积累的预算；换局确认在上方照旧清零，财神恢复已知时
        # 下方照旧清零）。
        if (table.should_stop_for_unknown_caishen
                and table.concealed_count is None):
            base["unknown_caishen_ticks"] = self._unknown_caishen_ticks
            base["unknown_caishen_budget"] = CAISHEN_UNKNOWN_BUDGET
            return self._event({
                **base,
                "event": "idle",
                "status": "abstain",
                "suggestion": None,
                "suggestion_id": None,
                "reason": (
                    "财神未知但本帧未读到已发牌手牌（等候室/局间过渡），"
                    "P0-5 计数冻结；期间零点击且不沿用旧财神"
                ),
            })
        if table.should_stop_for_unknown_caishen:
            self._unknown_caishen_ticks += 1
            base["unknown_caishen_ticks"] = self._unknown_caishen_ticks
            base["unknown_caishen_budget"] = CAISHEN_UNKNOWN_BUDGET
            if (self.enabled and self._unknown_caishen_ticks
                    >= CAISHEN_UNKNOWN_BUDGET):
                return self._stop(
                    "gate_unknown_caishen",
                    (f"财神连续 {self._unknown_caishen_ticks} 帧未知："
                     "停止代打（P0-5）"),
                    base,
                )
            return self._event({
                **base,
                "event": "idle",
                "status": "abstain",
                "suggestion": None,
                "suggestion_id": None,
                "reason": (
                    f"财神连续 {self._unknown_caishen_ticks}/"
                    f"{CAISHEN_UNKNOWN_BUDGET} 帧未知，等待有界重拍；"
                    "期间零点击且不沿用旧财神"
                ),
            })
        self._unknown_caishen_ticks = 0
        # 门禁三：回合所有权。dry_run 与真点都必须先过这一关，台账逐帧留证。
        verdict = self._evaluate_turn_gate(
            result, image, record=not changed_round)
        base["turn_gate"] = verdict.ledger()
        if verdict.state == "blocked":
            # 证据自相矛盾，或结算/模态弹窗画面 → fail closed：不重试、不补点。
            # 台账统一记 gate="gate_turn"；具体归类在 turn_gate.code
            # （如 "gate_settled"），同一行即可看出"其实是牌局结束了"。
            return self._stop("gate_turn", verdict.reason, base)
        bright = self._single_bright.feed(
            image, result, caishen_label=caishen, mode=self.mode)
        base["single_bright_cut"] = bright.ledger()
        # 胡提示面板：维护兜底所需状态（换局帧一律作废，面板帧绝不进快照）。
        if changed_round:
            self._last_advise_read = None
            self._hint_panel_streak = 0
            self._hint_panel_round = None
        else:
            self._track_hint_panel_state(result, verdict)
        if changed_round:
            if self.enabled and self.caishen_label:
                return self._stop("gate_manual_caishen", "检测到换局；手选财神须重新确认",
                                  base)
            return self._event({**base, "event": "idle", "reason": "换局帧跳过，等待下一张清晰画面"})
        # 无建议/弃权 → 只等待，绝不兜底点击。
        if verdict.code == HINT_PANEL_CODE:
            # 待二击恢复优先于摸切兜底：已有一张牌处于已选状态时，
            # 绝不能让兜底去点别的牌。无状态/状态失效才回落兜底。
            recovery_event = self._panel_recovery_attempt(
                result, base, started, hud_key=hud_key, caishen=caishen)
            if recovery_event is not None:
                return recovery_event
            fallback_event = self._hint_panel_fallback(
                result, verdict, base, started)
            if fallback_event is not None:
                return fallback_event
            return self._event({**base, "event": "idle",
                                "reason": verdict.reason})
        if bright.confirmed:
            bright_event = self._single_bright_fallback(
                result, bright, base, started)
            if bright_event is not None:
                return bright_event
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
        # 待二击落盘上下文：局身份 + 本帧完整手牌（进程重启恢复用）。
        self._tap.pending_context = self._pending_context(
            result.get("hand_ids"), result)
        expected_tile = int(result.get("suggestion_id"))

        def validate_post_select(selected_image: np.ndarray,
                                 fresh_analysis: dict,
                                 _selected_gate: dict) -> dict:
            selected_result = self._analyze_fresh_image(
                selected_image, "post_select_direct_discard")
            return direct_discard_redraw_proof(
                fresh_analysis, selected_result, expected_tile)

        try:
            outcome = self._tap.execute(
                result, post_select_validator=validate_post_select)
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
            "verified": bool(outcome.get(
                "verified",
                outcome.get("executed") and outcome.get("after_count")
                == (outcome.get("before_count") or 0) - 1)),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })
        panel_recovery.clear_state(self._panel_recovery_path)
        if not event["verified"]:
            self._stop("tap_unverified", "点击后手牌数量未验证，停止自动点击", base)
        return event

    def _analyze_fresh_image(self, image: np.ndarray, tag: str) -> dict:
        """把 TapExecutor 的 fresh 帧送回同一分析链，且保留可审计原图。"""
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise TapAborted("点击前 fresh 帧 PNG 编码失败")
        raw = encoded.tobytes()
        path = self._save_frame(raw, tag)
        result = self._analyze(raw, path)
        if not isinstance(result, dict):
            raise TapAborted("点击前 fresh 分析没有返回结构化结果")
        return result

    @staticmethod
    def _synthetic_discard(result: dict, tile_id: int, box,
                           *, before_count: int | None = None) -> dict:
        synthetic = dict(result)
        synthetic.update({
            "status": "advise",
            "suggestion": label_for_tile(tile_id),
            "suggestion_id": int(tile_id),
            "suggested_boxes": [[int(v) for v in box]],
            "_locked_tile": True,
        })
        if before_count is not None:
            synthetic["_locked_before_count"] = int(before_count)
        return synthetic

    def _single_bright_fallback(self, result: dict, bright, base: dict,
                                started: float) -> dict | None:
        """连续两帧单亮态只摸切该亮牌；fresh 帧必须再次独立命中。"""
        marker = base.setdefault("single_bright_cut", bright.ledger())
        if bright.tile_id is None or bright.bright_box is None:
            marker["outcome"] = "tile_identity_unknown"
            return None
        synthetic = self._synthetic_discard(
            result, bright.tile_id, bright.bright_box,
            before_count=(bright.evidence or {}).get("expected_concealed"))
        if self.actions_done >= self.max_actions:
            return self._stop("max_actions",
                              f"达到 --max-actions={self.max_actions}", base)
        if self.dry_run:
            try:
                plan = self._tap.plan(synthetic)
            except TapAborted as exc:
                marker["outcome"] = "plan_aborted"
                marker["reason"] = str(exc)
                return None
            self.actions_done += 1
            marker["outcome"] = "dry_run_plan"
            return self._event({
                **base, "event": "dry_run_plan", "tap": [plan.x, plan.y],
                "box": list(plan.box), "verified": None,
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })

        expected_tile = int(bright.tile_id)
        expected_point = tuple(int(v) for v in bright.tap_point)

        def validate_fresh(fresh_image: np.ndarray, _original: dict) -> dict:
            fresh_result = self._analyze_fresh_image(fresh_image, "preclick_bright")
            fresh = detect_single_bright_cut(
                fresh_image, fresh_result,
                caishen_label=self.round.caishen or result.get("caishen"),
                mode=self.mode)
            if not fresh.hit or fresh.tile_id != expected_tile or fresh.bright_box is None:
                raise TapAborted("fresh 帧不再是同一张单亮牌")
            if (fresh.tap_point is None
                    or abs(fresh.tap_point[0] - expected_point[0]) > 20
                    or abs(fresh.tap_point[1] - expected_point[1]) > 20):
                raise TapAborted("fresh 帧单亮牌位置漂移，拒绝点击")
            return self._synthetic_discard(
                fresh_result, expected_tile, fresh.bright_box,
                before_count=(fresh.evidence or {}).get("expected_concealed"))

        self._tap.pending_context = self._pending_context(
            result.get("hand_ids"), result)
        marker["outcome"] = "attempt"
        try:
            outcome = self._tap.execute(synthetic, fresh_validator=validate_fresh)
        except TapPrecheckRetryExhausted as exc:
            marker["outcome"] = "precheck_retry_exhausted"
            return self._anomaly("single_bright_precheck", str(exc), started, base)
        except TapUnverified as exc:
            return self._stop("tap_aborted", f"单亮摸切点击后无法验证：{exc}", base)
        except TapAborted as exc:
            return self._stop("tap_aborted", f"单亮摸切被执行器拒绝：{exc}", base)
        except Exception as exc:  # noqa: BLE001
            return self._stop("tap_failed", f"单亮摸切结果未知：{exc}", base)
        self.actions_done += 1
        self._single_bright.reset()
        marker["outcome"] = "verified"
        return self._event({
            **base, "event": "action", "tap": outcome.get("tap"),
            "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed") and
                             outcome.get("after_count") ==
                             (outcome.get("before_count") or 0) - 1),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })

    # ------------------------------------------------------------------
    # 牌河/来牌读取（供行动决策取"别家刚打出的那张"）
    # ------------------------------------------------------------------
    def _track_hint_panel_state(self, result: dict, verdict: TurnVerdict) -> None:
        """维护胡提示面板兜底的状态：面板连续帧计数 + 面板前 advise 快照。

        快照只收「有完整建议的好读数」（status=advise 且带建议框）：它证明
        面板出现前确实轮到我方出牌。面板帧本身（code=hint_panel）只计数、
        绝不更新快照；任何非面板帧都把连续计数清零。
        """
        if verdict.code == HINT_PANEL_CODE:
            if self._hint_panel_round != self.round.seq:
                self._hint_panel_round = self.round.seq
                self._hint_panel_streak = 0
            self._hint_panel_streak += 1
            return
        self._hint_panel_streak = 0
        if (result.get("status") == "advise" and result.get("suggested_boxes")
                and result.get("hand_ids")):
            try:
                hand_ids = sorted(int(tile) for tile in result["hand_ids"])
            except (TypeError, ValueError):
                return
            self._last_advise_read = {
                "hand_ids": hand_ids,
                "caishen_id": known_caishen_id(result),
                "round_seq": self.round.seq,
                "tick": self.tick_index,
                "actions_done": self.actions_done,
            }

    def _pending_context(self, hand_ids, result: dict | None = None) -> dict:
        """给执行器的待二击落盘上下文：局身份指纹/财神 + 点击前手牌。

        进程在两击之间重启时，新进程靠这份上下文 + 唯一抬起牌定位恢复
        第二击（panel_recovery）；缺项就恢复不了，判定侧按未知弃权。
        """
        ids: list[int] | None = None
        if hand_ids:
            try:
                ids = sorted(int(tile) for tile in hand_ids)
            except (TypeError, ValueError):
                ids = None
        hud = self.round.hud_key
        return {
            "hud_key": hud.hex() if hud else None,
            "caishen": self.round.caishen or (result or {}).get("caishen"),
            "caishen_id": known_caishen_id(result) if result else None,
            "round_seq": self.round.seq,
            "hand_ids": ids,
        }

    def _panel_recovery_attempt(self, result: dict, base: dict,
                                started: float, *, hud_key,
                                caishen) -> dict | None:
        """进程重启后的待二击恢复：状态文件 + 唯一抬起牌定位，缺一不可。

        触发前提（调用方已保证）：本帧 turn_gate 判 hint_panel。恢复条件
        全部走 panel_recovery.decide_recovery（状态合法/未过期、局身份
        一致、整手可完整读、已选抬起牌唯一定位）；点击只走执行器的
        execute_recovery_second_tap（fresh 帧重定位 + 白名单 + 点后 −1
        验证），本方法不直接碰 ADB。

        Returns:
            已处理本帧的事件 dict（恢复出手 / 继续等待）；``None`` = 无
            待二击状态，或状态已失效并清除，调用方回落原摸切兜底。
        """
        state, error = panel_recovery.read_state(self._panel_recovery_path)
        if error is not None:
            base["panel_recovery"] = {"outcome": "state_unreadable",
                                      "reason": error}
            panel_recovery.clear_state(self._panel_recovery_path)
            return None
        if state is None:
            return None
        marker: dict = {"tile_id": state.get("tile_id"),
                        "saved_at": state.get("saved_at")}
        base["panel_recovery"] = marker
        if state.get("serial") != self.serial:
            marker["outcome"] = "serial_mismatch"
            panel_recovery.clear_state(self._panel_recovery_path)
            return None
        decision = panel_recovery.decide_recovery(
            state, result, hud_key=hud_key,
            caishen=self.round.caishen or caishen, now=time.time())
        marker["decision"] = decision.reason
        if not decision.allowed:
            marker["outcome"] = "abstain"
            marker["reason"] = decision.reason
            if decision.reason.startswith(
                    ("identity_mismatch", "state_stale", "state_invalid")):
                # 状态已失效：清除后交回原兜底（其快照自带新鲜度校验）。
                panel_recovery.clear_state(self._panel_recovery_path)
                marker["state_cleared"] = True
                return None
            # 状态仍像本局待二击、只是本帧定位不唯一：已有一张牌处于
            # 已选状态，绝不让摸切兜底去点别的牌，继续等待下一帧。
            return self._event({
                **base, "event": "idle",
                "reason": "胡提示面板待二击恢复条件未满足，继续弃权等待"})
        marker["outcome"] = "attempt"
        marker["case"] = decision.case
        marker["box"] = decision.box
        if self.actions_done >= self.max_actions:
            return self._stop("max_actions",
                              f"达到 --max-actions={self.max_actions}", base)
        if self.dry_run:
            marker["outcome"] = "dry_run_ready"
            return self._event({
                **base, "event": "dry_run_plan",
                "tap": list(decision.tap_point), "box": decision.box,
                "verified": None,
                "elapsed_ms": round((self._clock() - started) * 1000, 1)})
        expected_tile = int(decision.tile_id)
        expected_point = tuple(int(value) for value in decision.tap_point)

        def fresh_locator(fresh_image: np.ndarray) -> dict:
            fresh_result = self._analyze_fresh_image(
                fresh_image, "preclick_recovery")
            settled, settled_evidence = detect_settled_screen(fresh_image)
            signals = settled_evidence.get("settled_signals") or []
            if settled or signals != ["central_panel"]:
                raise TapAborted("fresh 帧不再是单信号胡提示面板")
            fresh_guard = self._action_guard(fresh_image)
            if fresh_guard.blocks_discard:
                raise TapAborted("fresh 帧出现行动圆盘/未知控件，禁止恢复第二击")
            fresh_decision = panel_recovery.decide_recovery(
                state, fresh_result, hud_key=self.round.hud_key,
                caishen=self.round.caishen, now=time.time())
            if (not fresh_decision.allowed
                    or fresh_decision.tile_id != expected_tile):
                raise TapAborted(
                    f"fresh 帧恢复判定未通过: {fresh_decision.reason}")
            fresh_point = tuple(int(value)
                                 for value in fresh_decision.tap_point)
            if (abs(fresh_point[0] - expected_point[0]) > 20
                    or abs(fresh_point[1] - expected_point[1]) > 20):
                raise TapAborted("fresh 帧抬起牌位置漂移，拒绝恢复第二击")
            synthetic = self._synthetic_discard(
                fresh_result, expected_tile, fresh_decision.box,
                before_count=int(state["before_count"]))
            synthetic["_recovery_concealed_boxes"] = [
                list(item["box"]) for item in
                panel_recovery.accepted_concealed_items(fresh_result)]
            return synthetic

        try:
            outcome = self._tap.execute_recovery_second_tap(
                state, fresh_locator)
        except TapUnverified as exc:
            return self._stop(
                "tap_aborted", f"胡提示面板恢复第二击后无法验证：{exc}", base)
        except TapAborted as exc:
            marker["outcome"] = "aborted_pre_tap"
            marker["reason"] = str(exc)
            return self._event({
                **base, "event": "idle",
                "reason": "恢复第二击在点击前被中止，继续等待"})
        except Exception as exc:  # noqa: BLE001 - 未预期异常与常规路径同口径停手
            return self._stop("tap_failed", f"恢复第二击结果未知：{exc}", base)
        self.actions_done += 1
        panel_recovery.clear_state(self._panel_recovery_path)
        marker["outcome"] = "recovered"
        after_path = None
        try:
            after_raw = self._shot()
            after_path = str(self._save_frame(after_raw, "after"))
        except Exception as exc:  # noqa: BLE001 - 台账不影响已验证的点击
            after_path = f"<截图失败: {exc}>"
        event = self._event({
            **base, "event": "action", "after_frame": after_path,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed")
                             and outcome.get("after_count")
                             == (outcome.get("before_count") or 0) - 1),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })
        if not event["verified"]:
            self._stop("tap_unverified",
                       "恢复第二击后手牌数量未验证，停止自动点击", base)
        return event

    def _hint_panel_fallback(self, result: dict, verdict: TurnVerdict,
                             base: dict, started: float) -> dict | None:
        """胡提示面板持续多帧时的兜底：只摸切「刚摸的牌」，其余一律等待。

        触发条件（缺一不可）：面板连续 ≥ HINT_PANEL_MIN_TICKS 帧（之后每
        HINT_PANEL_RETRY_EVERY 帧重试一次）；存在同局、未过期（期间无任何
        已执行动作）、财神一致的面板前 advise 快照；本帧行动圆盘不可见；
        locate_drawn_tile 能在本帧定位刚摸的牌。

        摸切目标以合成 advise 结果交给 TapExecutor 正常路径（重定位+验证）。
        注意执行器的 fresh 复核会重跑 analyze：面板未消失前必然弃权并在
        **点击前**中止（离线夹具实测）——这类 TapAborted 只记台账、继续
        等待，绝不能按常规路径停机；TapUnverified（点击后无法验证）则与
        常规路径同口径立即停手。

        Returns:
            本帧的事件 dict（已落台账）；``None`` = 本帧不尝试/尝试未发出
            点击，调用方按面板 wait 记录 idle。
        """
        marker: dict = {"streak": self._hint_panel_streak}
        base["hint_panel"] = marker
        streak = self._hint_panel_streak
        if (streak < HINT_PANEL_MIN_TICKS
                or (streak - HINT_PANEL_MIN_TICKS) % HINT_PANEL_RETRY_EVERY):
            marker["outcome"] = "waiting_streak"
            return None
        snapshot = self._last_advise_read
        if snapshot is None:
            marker["outcome"] = "no_prior_advise_read"
            return None
        if (snapshot["round_seq"] != self.round.seq
                or snapshot["actions_done"] != self.actions_done
                or snapshot["caishen_id"] != known_caishen_id(result)):
            marker["outcome"] = "prior_advise_read_stale"
            return None
        action_ui = (verdict.evidence or {}).get("action_ui") or {}
        if action_ui.get("action_discs_seen"):
            marker["outcome"] = "action_ui_visible"
            return None
        located = locate_drawn_tile(result, snapshot["hand_ids"])
        if located is None:
            marker["outcome"] = "drawn_tile_not_located"
            return None
        marker.update({
            "outcome": "attempt",
            "drawn_tile_id": located["tile_id"],
            "box": located["box"],
            "prior_advise_tick": snapshot["tick"],
        })
        synthetic = dict(result)
        synthetic.update({
            "status": "advise",
            "suggestion": label_for_tile(located["tile_id"]),
            "suggestion_id": located["tile_id"],
            "suggested_boxes": [list(located["box"])],
        })
        if self.actions_done >= self.max_actions:
            return self._stop("max_actions",
                              f"达到 --max-actions={self.max_actions}", base)
        if self.dry_run:
            try:
                plan = self._tap.plan(synthetic)
            except TapAborted as exc:
                marker["outcome"] = "plan_aborted"
                marker["reason"] = str(exc)
                return None
            self.actions_done += 1
            marker["outcome"] = "dry_run_plan"
            return self._event({
                **base, "event": "dry_run_plan",
                "tap": [plan.x, plan.y], "box": list(plan.box),
                "verified": None,
                "elapsed_ms": round((self._clock() - started) * 1000, 1)})
        expected_tile = int(located["tile_id"])
        expected_point = ((int(located["box"][0]) + int(located["box"][2])) // 2,
                          (int(located["box"][1]) + int(located["box"][3])) // 2)

        def validate_fresh(fresh_image: np.ndarray, _original: dict) -> dict:
            fresh_result = self._analyze_fresh_image(fresh_image, "preclick_hint")
            settled, settled_evidence = detect_settled_screen(fresh_image)
            signals = settled_evidence.get("settled_signals") or []
            if settled or signals != ["central_panel"]:
                raise TapAborted("fresh 帧不再是单信号胡提示面板")
            fresh_guard = self._action_guard(fresh_image)
            if fresh_guard.blocks_discard:
                raise TapAborted("fresh 帧出现行动圆盘/未知控件，禁止摸切")
            if known_caishen_id(fresh_result) != snapshot["caishen_id"]:
                raise TapAborted("fresh 帧财神与面板前快照不一致")
            fresh_located = locate_drawn_tile(fresh_result, snapshot["hand_ids"])
            if fresh_located is None or int(fresh_located["tile_id"]) != expected_tile:
                raise TapAborted("fresh 帧未定位到同一张刚摸牌")
            fresh_box = fresh_located["box"]
            fresh_point = ((int(fresh_box[0]) + int(fresh_box[2])) // 2,
                           (int(fresh_box[1]) + int(fresh_box[3])) // 2)
            if (abs(fresh_point[0] - expected_point[0]) > 20
                    or abs(fresh_point[1] - expected_point[1]) > 20):
                raise TapAborted("fresh 帧刚摸牌位置漂移，拒绝点击")
            return self._synthetic_discard(
                fresh_result, expected_tile, fresh_box,
                before_count=len(snapshot["hand_ids"]))

        self._tap.pending_context = self._pending_context(
            snapshot["hand_ids"], result)
        try:
            outcome = self._tap.execute(synthetic, fresh_validator=validate_fresh)
        except TapPrecheckRetryExhausted as exc:
            marker["outcome"] = "precheck_retry_exhausted"
            return self._anomaly("hint_panel_precheck", str(exc), started, base)
        except TapUnverified as exc:
            return self._stop("tap_aborted",
                              f"胡提示面板兜底点击后无法验证：{exc}", base)
        except TapAborted as exc:
            marker["outcome"] = "executor_aborted_pre_tap"
            marker["reason"] = str(exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 未预期异常与常规路径同口径停手
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
        self._last_advise_read = None
        self._hint_panel_streak = 0
        if not event["verified"]:
            self._stop("tap_unverified", "点击后手牌数量未验证，停止自动点击", base)
        return event

    def _probe_round_key(self, frame_path: Path) -> tuple[bytes | None, str | None]:
        """算本帧的换局指纹；识别失败按未知处理（fail closed）。"""
        try:
            return self._round_key(frame_path)
        except Exception:  # noqa: BLE001 - 换局识别失败按未知处理
            return None, None

    @staticmethod
    def _same_round_candidate(
        first: tuple[bytes | None, str | None] | None,
        second: tuple[bytes | None, str | None],
    ) -> bool:
        if first is None:
            return False
        first_hud, first_caishen = first
        second_hud, second_caishen = second
        hud_same = ((first_hud is None and second_hud is None)
                    or (first_hud is not None and second_hud is not None
                        and _same_hud(first_hud, second_hud)))
        return hud_same and first_caishen == second_caishen

    def _debounce_round_signal(
        self,
        hud_key: bytes | None,
        visual_caishen: str | None,
        resolved_caishen: str | None,
    ) -> dict:
        """连续帧确认局身份；任何不稳定期只等待，绝不沿用旧财神点击。"""
        if hud_key is None and visual_caishen is None:
            self._round_identity_unknown_ticks += 1
            self._round_candidate = None
            self._round_candidate_ticks = 0
            return {"state": "identity_unknown",
                    "ticks": self._round_identity_unknown_ticks}
        self._round_identity_unknown_ticks = 0

        if (self.caishen_label and visual_caishen
                and visual_caishen != self.caishen_label):
            if self._manual_caishen_conflict == visual_caishen:
                self._manual_caishen_conflict_ticks += 1
            else:
                self._manual_caishen_conflict = visual_caishen
                self._manual_caishen_conflict_ticks = 1
            return {"state": "manual_conflict",
                    "ticks": self._manual_caishen_conflict_ticks,
                    "expected": self.caishen_label,
                    "observed": visual_caishen}
        self._manual_caishen_conflict = None
        self._manual_caishen_conflict_ticks = 0

        if self.round.hud_key is None and self.round.caishen is None:
            self._round_candidate = None
            self._round_candidate_ticks = 0
            return {"state": "same", "ticks": 1, "initial": True}
        if self.round.same_round(hud_key, resolved_caishen):
            self._round_candidate = None
            self._round_candidate_ticks = 0
            return {"state": "same", "ticks": 1}

        candidate = (hud_key, resolved_caishen)
        if self._same_round_candidate(self._round_candidate, candidate):
            self._round_candidate_ticks += 1
        else:
            self._round_candidate = candidate
            self._round_candidate_ticks = 1
        state = ("round_changed" if self._round_candidate_ticks
                 >= ROUND_CHANGE_CONFIRM_TICKS else "round_suspect")
        return {"state": state, "ticks": self._round_candidate_ticks,
                "caishen": resolved_caishen}

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
    # claim 点后落定验证（2026-10-25 实机 Bug 2）
    # ------------------------------------------------------------------
    @staticmethod
    def _claim_board_signature(board: BoardState) -> tuple:
        """点后落定签名：连续两帧签名一致才算画面落定（执行器同口径）。"""
        return (
            board.concealed_count,
            board.meld_group_count,
            board.total,
            board.claim_buttons,
            board.settled,
            board.selection_panel,
        )

    def _verify_claim_settled(
        self,
        before: BoardState,
        action: str,
        *,
        tag: str,
        note: str,
        extra_check=None,
    ) -> tuple[bool, str, "BoardState | None", list]:
        """claim 点击后的落定语义验证，替代旧的单帧 ``verify_after_click``。

        旧实现点击后只截一帧就判：首帧正处圆盘消失动画/手牌压暗状态，
        分割计数不可信——实机三例全是「过」实际已生效却被误杀停机
        （暗手 14→17、16→10、16→10，其中 16→10 在全新进程同数字
        复现，是确定性坏读而非状态残留）。改法与出牌执行器
        ``_await_settled_hand`` 同口径：

        * 首帧即验证通过（决定性读数）→ 直接采用，不多截屏；
        * 未通过 → 每隔 ``CLAIM_SETTLE_INTERVAL_S`` 重拍一帧，直到某
          帧验证通过、或连续两帧落定签名一致（状态已落定，此时的
          不符是真不符，照旧判未证实）、或 ``CLAIM_SETTLE_ATTEMPTS``
          耗尽（未落定 → fail-closed，照旧停机）；
        * 采样异常只记样本（签名取唯一哨兵、永不算落定），不外抛。

        基线 ``before`` 不变：仍取点击前同帧 fresh 状态（既有暗手列
        白名单口径保留）。``extra_check(board, verified, reason)`` 可
        在每帧验证后追加判据（吃链用它核对新增副露组 == 所选组）。

        Returns:
            ``(verified, reason, last_board, samples)``；samples 逐帧
            记录计数与判定，调用方随台账落盘供复核。
        """
        samples: list[dict] = []
        board: BoardState | None = None
        reason = "点后未取得任何复核帧"
        previous_signature: tuple | None = None
        for attempt in range(CLAIM_SETTLE_ATTEMPTS):
            if attempt:
                self._sleep(CLAIM_SETTLE_INTERVAL_S)
            try:
                raw = self._shot()
                path = self._save_frame(raw, tag)
                image = cv2.imdecode(
                    np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                board = self._board_from_frame(raw, path, image, note=note)
                verified, reason = verify_after_click(before, board, action)
                if extra_check is not None:
                    verified, reason = extra_check(board, verified, reason)
                signature = self._claim_board_signature(board)
            except Exception as exc:  # noqa: BLE001 - 坏帧只记样本
                verified = False
                reason = f"点后复核帧异常：{exc}"
                samples.append({
                    "attempt": attempt, "error": str(exc),
                    "verified": False, "reason": reason,
                })
                previous_signature = ("error", attempt)
                continue
            samples.append({
                "attempt": attempt,
                "concealed": board.concealed_count,
                "meld_groups": board.meld_group_count,
                "total": board.total,
                "claim_buttons": board.claim_buttons,
                "settled": board.settled,
                "selection_panel": board.selection_panel,
                "verified": verified,
                "reason": reason,
                "frame": str(path),
            })
            if verified:
                return True, reason, board, samples
            if (previous_signature is not None
                    and signature == previous_signature):
                # 连续两帧签名一致 = 已落定；落定后仍不符是真未证实。
                return False, reason, board, samples
            previous_signature = signature
        return False, (f"点后状态 {CLAIM_SETTLE_ATTEMPTS} 帧内未落定，"
                       f"fail-closed：{reason}"), board, samples

    # ------------------------------------------------------------------
    # 行动（吃/碰/杠/胡/过）决策闸门
    # ------------------------------------------------------------------
    def _claim_policy_event(self, image: np.ndarray, frame_path: Path,
                             analysis: dict | None = None) -> dict:
        """行动行可见时决策；显式启用后走独立圆盘通道并做 L3 语义验证。

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
        # 吃牌二次候选面板的蓝色圆盘：用户明确要求 BOT 能自动点“取消”。
        # 只接受“本帧唯一圆盘就是 cancel”，邀请弹窗的矩形取消不走此通道。
        if (len(claim_state.buttons) == 1
                and claim_state.buttons[0].kind == KIND_CANCEL):
            button = claim_state.buttons[0]
            payload = {
                "status": "ready" if self.claim_auto_click else "observed",
                "action": "cancel_selection",
                "frame": str(frame_path),
                "known": True,
                "buttons": [KIND_CANCEL],
                "tap_point": list(button.center),
                "reason": ("本帧唯一实测圆盘为候选面板取消；"
                           "显式开启行动点击后允许关闭候选面板"),
            }
            if not self.claim_auto_click:
                return {"claim": payload}
            payload["auto_click"] = {
                "requested": True, "executed": False,
                "channel_ready": CLAIM_TAP_CHANNEL_READY,
            }
            if not CLAIM_TAP_CHANNEL_READY:
                payload["auto_click"]["reason"] = "行动圆盘点击通道未启用"
                return {"claim": payload}
            try:
                outcome = self._tap.execute_claim(
                    button.center, action="cancel_selection", kind=KIND_CANCEL,
                    image=image, session_dir=self.session_dir)
            except TapRejected as exc:
                # fresh 帧里面板已自行消失属于零点击安全竞争，不停整条循环。
                payload["auto_click"].update({
                    "reason": str(exc), "stage": "stale_or_rejected",
                })
                return {"claim": payload}
            except TapAborted as exc:
                payload["auto_click"].update({"reason": str(exc), "stage": "aborted"})
                return {"claim": payload, "_terminal": {
                    "gate": "selection_cancel_unverified",
                    "reason": f"候选面板取消点击结果未知：{exc}",
                }}
            payload["auto_click"].update({
                "executed": bool(outcome.get("executed")),
                "stage": outcome.get("stage"), "tap": outcome.get("tap"),
                "semantic_verified": bool(outcome.get("ui_responded")),
                "semantic_reason": outcome.get("reason"),
            })
            return {"claim": payload}
        # 来牌从本帧的牌河读数里取（discard_vision 给的是 DiscardState）。
        # 换局没确认过的帧：本帧"来牌"可能是跨局差分出来的，不许进 L2。
        # P0-5: never reuse a prior frame's analysis for claim decisions.
        payload: dict = dict(analysis) if isinstance(analysis, dict) else {}
        if self._discard_state is not None and self._discard_trusted:
            payload["discards"] = self._discard_state
        hand = HandState.from_analysis(payload)
        decision = decide_action(claim_state, hand, self._claim_policy, image=image)
        if (decision is not None and decision.action == ACTION_CHI
                and self._chi_abstain_key is not None
                and self._chi_abstain_key == self._chi_window_key(hand)):
            # 同一行动窗里吃链已弃权过一次（选组弃权/点组被拒/面板读
            # 不全）：不再重复点吃，改用关吃配置重新决策（通常落到
            # 「过」），避免 点吃→取消→点吃 的死循环。
            decision = decide_action(
                claim_state, hand,
                replace(self._claim_policy, chi_enabled=False), image=image)
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
        if not (decision.executable and self.claim_auto_click):
            return {"claim": payload}
        payload["auto_click"] = {
            "requested": True, "executed": False,
            "channel_ready": CLAIM_TAP_CHANNEL_READY,
        }
        if not CLAIM_TAP_CHANNEL_READY:
            payload["auto_click"]["reason"] = "行动圆盘点击通道未启用"
            return {"claim": payload}

        if decision.action == ACTION_CHI:
            return self._chi_chain_event(
                decision, hand, claim_state, image, frame_path, payload)

        before = BoardState.from_hand_state(
            hand, claim_buttons=len(claim_state.buttons), settled=False,
            note="行动点击前同帧状态")
        try:
            outcome = self._tap.execute_claim(
                decision.tap_point, action=decision.action, image=image,
                session_dir=self.session_dir)
        except TapAborted as exc:
            payload["auto_click"].update({"reason": str(exc), "stage": "aborted"})
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_aborted",
                "reason": f"行动 {decision.action} 点击未通过：{exc}",
            }}
        except Exception as exc:  # noqa: BLE001
            payload["auto_click"].update({"reason": str(exc), "stage": "failed"})
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_failed",
                "reason": f"行动 {decision.action} 点击结果未知：{exc}",
            }}
        payload["auto_click"].update({
            "executed": bool(outcome.get("executed")),
            "stage": outcome.get("stage"), "tap": outcome.get("tap"),
        })
        if self.dry_run:
            payload["auto_click"]["semantic_verified"] = None
            return {"claim": payload}

        # L3 语义复核走落定轮询（_verify_claim_settled）：点后首帧
        # 常处动画/压暗状态，单帧计数不可信（实机三例误杀）。
        verified, verify_reason, _after_board, settle_samples = (
            self._verify_claim_settled(
                before, decision.action, tag="after_claim",
                note="行动点击后 fresh 状态"))
        payload["auto_click"].update({
            "semantic_verified": verified,
            "semantic_reason": verify_reason,
            "settle_samples": settle_samples,
        })
        if settle_samples and "frame" in settle_samples[-1]:
            payload["auto_click"]["after_frame"] = (
                settle_samples[-1]["frame"])
        if not verified:
            return {"claim": payload, "_terminal": {
                "gate": "claim_unverified",
                "reason": verify_reason,
            }}
        self.actions_done += 1
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
        self.stop_gate = kind
        event = {**(extra or {}), "ts": datetime.now().astimezone().isoformat(),
                 "event": "stop", "gate": kind, "reason": reason}
        self._write_event(event)
        return event

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 吃链：点吃圆盘 → 等面板 → 读面板选组 → 点组 → L3 复核
    # ------------------------------------------------------------------
    def _chi_window_key(self, hand: HandState) -> tuple:
        """同一行动窗的身份：局序 + 来牌 + 闭手。任一变化即新窗。"""
        return (self.round.seq, hand.incoming_tile, tuple(hand.concealed))

    @staticmethod
    def _is_engine_sequence(tiles) -> bool:
        """3 张互不相同的引擎牌是否构成同花色顺子。"""
        values = sorted(int(v) for v in tiles)
        if len(values) != 3 or len(set(values)) != 3:
            return False
        return any(low <= values[0] and values[2] <= high
                   and values[2] - values[0] == 2
                   for low, high in ((1, 9), (21, 29), (41, 49)))

    @staticmethod
    def _new_meld_groups(before: BoardState,
                         after: BoardState) -> list[tuple[int, ...]]:
        """after 相对 before 新增的副露组（多重集差，组内升序）。"""
        if before.melds is None or after.melds is None:
            return []
        remaining = Counter(tuple(sorted(int(v) for v in group))
                            for group in before.melds)
        new_groups: list[tuple[int, ...]] = []
        for group in after.melds:
            key = tuple(sorted(int(v) for v in group))
            if remaining.get(key, 0) > 0:
                remaining[key] -= 1
            else:
                new_groups.append(key)
        return new_groups

    def _board_from_frame(self, raw: bytes, path: Path, image,
                          *, note: str) -> BoardState:
        """一帧 → L3 盘面快照（与行动点击后复核同一口径，另带面板标志）。"""
        result = self._analyze(raw, path)
        claims = read_claims(image)
        settled, _evidence = detect_settled_screen(image)
        hand = HandState.from_analysis(result)
        panel = read_chi_panel(image)
        return BoardState.from_hand_state(
            hand, claim_buttons=len(claims.buttons), settled=settled,
            selection_panel=panel.present, note=note)

    @staticmethod
    def _chi_dimmed_hand_evidence(image: np.ndarray) -> dict:
        """吃后专用压暗证据；不参与普通手牌识别。"""
        out = {"dimmed": False, "p70": None, "bright_ratio": None}
        if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
            return out
        x0, y0, x1, y1 = CHI_DIMMED_HAND_ROI
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            return out
        value = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 2]
        p70 = float(np.percentile(value, 70))
        bright_ratio = float((value >= CHI_DIMMED_BRIGHT_V).mean())
        out.update({
            "p70": round(p70, 2),
            "bright_ratio": round(bright_ratio, 4),
            "dimmed": (
                p70 <= CHI_DIMMED_P70_MAX
                and bright_ratio <= CHI_DIMMED_BRIGHT_RATIO_MAX),
        })
        return out

    def _chi_completed_board(self, before: BoardState, raw: bytes,
                             path: Path, image) -> tuple[BoardState, str] | None:
        """面板未出现时，检查吃是否已被游戏直接完成（单候选免面板）。

        正常帧沿用 L3；若客户端把暗手整体压暗导致分类器少读，只允许用
        “副露 +1 组/+3 张 + 合法顺子 + 圆盘消失 + 面板消失 + 压暗证据”
        这一组更强的结构证据确认。绝不降低全局 tile threshold。
        """
        try:
            board = self._board_from_frame(raw, path, image,
                                           note="点吃后轮询帧")
        except Exception:  # noqa: BLE001 - 轮询帧读不全只是"还没等到"
            return None
        new_groups = self._new_meld_groups(before, board)
        if len(new_groups) != 1 or not self._is_engine_sequence(new_groups[0]):
            return None
        ok, why = verify_after_click(before, board, ACTION_CHI)
        if ok:
            return board, why

        dim = self._chi_dimmed_hand_evidence(image)
        if not dim["dimmed"]:
            return None
        if board.claim_buttons != 0:
            return None
        if board.selection_panel is not False:
            return None
        if before.meld_group_count is None or board.meld_group_count is None:
            return None
        if board.meld_group_count - before.meld_group_count != 1:
            return None
        if before.meld_tile_total is None or board.meld_tile_total is None:
            return None
        if board.meld_tile_total - before.meld_tile_total != 3:
            return None
        expected = (before.concealed_count - 2
                    if before.concealed_count is not None else None)
        # 这条分支只处理“明显少读”的压暗态；若暗手读数恰好正常，
        # 应由上面的标准 L3 通过，不在这里兜底。
        if (expected is None or board.concealed_count is None
                or board.concealed_count >= expected):
            return None
        reason = (
            "吃已由结构证据确认：新增合法顺子副露 +1组/+3张、行动圆盘消失、"
            "候选面板消失；暗手处于客户端压暗态 "
            f"(p70={dim['p70']}, bright_ratio={dim['bright_ratio']})，"
            f"当前暗手读数 {board.concealed_count} 低于应有 {expected}，"
            "按已知压暗坏读处理，不用该张数否决真实成吃")
        return board, reason

    def _chi_group_verdict(self, before: BoardState, after: BoardState,
                           group, verified: bool, reason: str):
        """吃链追加判据：L3 通过后，新增副露组必须恰为所选组。"""
        if not verified:
            return verified, reason
        new_groups = self._new_meld_groups(before, after)
        if len(new_groups) != 1:
            return False, (f"吃后新增副露组数 {len(new_groups)} != 1，"
                           "判未证实")
        if (group is not None
                and tuple(new_groups[0]) != tuple(
                    sorted(int(v) for v in group))):
            return False, (f"吃后新增副露组 {list(new_groups[0])} 与所选组 "
                           f"{sorted(int(v) for v in group)} 不一致，判未证实")
        return verified, reason

    def _chi_finish_tail(self, payload: dict, chain: dict,
                         verified: bool, reason: str) -> dict:
        """吃链收尾落账：通过才记一次动作，未证实一律停机落证据。"""
        payload["auto_click"].update({
            "semantic_verified": verified, "semantic_reason": reason})
        chain["stage"] = "verified" if verified else "unverified"
        if not verified:
            return {"claim": payload, "_terminal": {
                "gate": "claim_unverified", "reason": reason}}
        self.actions_done += 1
        return {"claim": payload}

    def _chi_finish_verified(self, payload: dict, chain: dict,
                             before: BoardState, after: BoardState,
                             group) -> dict:
        """吃链收尾：L3 相对变化 + 新增副露组核对，通过才记一次动作。"""
        verified, reason = verify_after_click(before, after, ACTION_CHI)
        verified, reason = self._chi_group_verdict(
            before, after, group, verified, reason)
        return self._chi_finish_tail(payload, chain, verified, reason)

    def _chi_chain_event(self, decision, hand: HandState, claim_state,
                         image, frame_path: Path, payload: dict) -> dict:
        """吃的两段式执行（decision 为 chi 且已过全部闸门时进入）。

        链路：点吃圆盘 → 有界轮询等候选面板 → ``read_chi_panel`` 读组
        → ``choose_chi_panel_option`` 选组（不劣化向听才选、财神不替代
        顺子、证据不足恒弃权） → 面板专用通道点组 → L3 复核（暗手相对
        −2、副露新增组恰为所选组）。任一步证据不足都 fail closed：
        面板读不完整/选组弃权/点组被拒时先点「取消」关面板再退回
        自动「过」（见 :meth:`_chi_cancel_and_pass`）；点组后状态未知
        或 L3 不通过则停机落证据。

        吃成后的出牌不在本方法内做：本 tick 结束后，下一 tick 的常规
        出牌路径会用新手牌重新算牌、打出推荐牌并自带点后 −1 验证
        （与「过」之后续打同一机制）。
        """
        chain: dict[str, Any] = {"stage": "start", "frame": str(frame_path)}
        payload["chi_chain"] = chain
        before = BoardState.from_hand_state(
            hand, claim_buttons=len(claim_state.buttons), settled=False,
            selection_panel=False, note="吃链点击前同帧状态")
        # 第一段：点吃圆盘（独立行动行白名单，与碰/杠/胡/过同一通道）。
        try:
            outcome = self._tap.execute_claim(
                decision.tap_point, action=decision.action, image=image,
                session_dir=self.session_dir)
        except TapAborted as exc:
            payload["auto_click"].update(
                {"reason": str(exc), "stage": "aborted"})
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_aborted",
                "reason": f"行动 chi 点击未通过：{exc}"}}
        except Exception as exc:  # noqa: BLE001
            payload["auto_click"].update(
                {"reason": str(exc), "stage": "failed"})
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_failed",
                "reason": f"行动 chi 点击结果未知：{exc}"}}
        chain["disc_tap"] = outcome
        payload["auto_click"].update({
            "executed": bool(outcome.get("executed")),
            "stage": outcome.get("stage"), "tap": outcome.get("tap")})
        if self.dry_run:
            chain["stage"] = "planned_only"
            payload["auto_click"]["semantic_verified"] = None
            return {"claim": payload}

        # 第二段：有界轮询等面板；面板始终不出现时，看吃是否已被游戏
        # 直接完成（只有一组候选时客户端可能不弹面板、直接成吃）。
        panel_image = None
        panel_state = None
        completed = None
        for attempt in range(CHI_PANEL_POLL_ATTEMPTS):
            if attempt:
                self._sleep(CHI_PANEL_POLL_INTERVAL_S)
            try:
                raw = self._shot()
                path = self._save_frame(raw, "chi_panel")
                shot_image = cv2.imdecode(
                    np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            except Exception as exc:  # noqa: BLE001
                return {"claim": payload, "_terminal": {
                    "gate": "chi_panel_capture_failed",
                    "reason": f"点吃后截屏失败，面板状态未知：{exc}"}}
            if shot_image is None:
                continue
            panel_state = read_chi_panel(shot_image)
            if panel_state.present:
                panel_image = shot_image
            if panel_state.complete:
                break
            completed = self._chi_completed_board(before, raw, path,
                                                  shot_image)
            if completed is not None:
                break
        chain["panel"] = (panel_state.ledger() if panel_state is not None
                          else None)
        if completed is not None:
            completed_board, completed_reason = completed
            chain["stage"] = "panel_skipped_completed"
            chain["direct_completion_reason"] = completed_reason
            return self._chi_finish_tail(
                payload, chain, True, completed_reason)
        if panel_image is None or panel_state is None:
            return {"claim": payload, "_terminal": {
                "gate": "chi_panel_missing",
                "reason": "点吃后有界轮询内既没读到完整候选面板，"
                          "也没观察到成吃迁移，点击结果未知，停手"}}
        if not panel_state.complete:
            chain["stage"] = "panel_incomplete"
            return self._chi_cancel_and_pass(
                payload, chain, panel_state, panel_image, hand)

        # 第三段：选组（纯决策，证据不足恒弃权，绝不盲点第一个组合）。
        choice = choose_chi_panel_option(
            hand, [list(g.tiles) for g in panel_state.groups],
            self._claim_policy)
        chain["choice"] = choice.ledger()
        if not choice.chosen or choice.candidate_index is None:
            chain["stage"] = "choice_abstained"
            self._chi_abstain_key = self._chi_window_key(hand)
            return self._chi_cancel_and_pass(
                payload, chain, panel_state, panel_image, hand)
        group = panel_state.groups[choice.candidate_index]
        if tuple(group.tiles) != tuple(choice.group or ()):
            chain["stage"] = "choice_mismatch"
            self._chi_abstain_key = self._chi_window_key(hand)
            return self._chi_cancel_and_pass(
                payload, chain, panel_state, panel_image, hand)

        # 第四段：点所选组（TapExecutor 面板专用白名单通道）。
        try:
            group_outcome = self._tap.execute_chi_panel_group(
                group.center, group=choice.group, image=panel_image,
                session_dir=self.session_dir)
        except TapRejected as exc:
            # 发出前被拒 = 零点击：面板还开着，取消后退回过。
            chain["stage"] = "group_tap_rejected"
            chain["group_tap_reason"] = str(exc)
            self._chi_abstain_key = self._chi_window_key(hand)
            return self._chi_cancel_and_pass(
                payload, chain, panel_state, panel_image, hand)
        except TapAborted as exc:
            return {"claim": payload, "_terminal": {
                "gate": "chi_group_tap_aborted",
                "reason": f"吃选组点击未通过：{exc}"}}
        except Exception as exc:  # noqa: BLE001
            return {"claim": payload, "_terminal": {
                "gate": "chi_group_tap_failed",
                "reason": f"吃选组点击结果未知：{exc}"}}
        chain["group_tap"] = group_outcome

        # 第五段：L3 语义复核（落定轮询；相对变化 + 新增副露组核对）。
        def _group_check(board, ok, why):
            return self._chi_group_verdict(before, board, choice.group,
                                           ok, why)

        verified, reason, _after_board, settle_samples = (
            self._verify_claim_settled(
                before, ACTION_CHI, tag="after_chi",
                note="吃选组点击后 fresh 状态", extra_check=_group_check))
        chain["settle_samples"] = settle_samples
        return self._chi_finish_tail(payload, chain, verified, reason)

    def _chi_cancel_and_pass(self, payload: dict, chain: dict, panel_state,
                             panel_image, hand: HandState) -> dict:
        """面板已开但吃链走不下去：点「取消」关面板，再退回自动「过」。

        取消走既有 cancel_selection 通道（唯一取消圆盘 + 专用白名单，
        与循环顶层那条候选面板取消路径同一口径）。取消结果未知 →
        停机；取消成功后若行动窗还在，用**关吃配置**重新决策（防再次
        落进吃链），恰为可执行的「过」才点，并照常做 L3 复核。
        """
        cancel = panel_state.cancel if panel_state is not None else None
        if cancel is None:
            return {"claim": payload, "_terminal": {
                "gate": "chi_panel_no_cancel",
                "reason": "候选面板已开但读不到取消圆盘，无法安全退出，停手"}}
        try:
            outcome = self._tap.execute_claim(
                cancel.center, action="cancel_selection", kind=KIND_CANCEL,
                image=panel_image, session_dir=self.session_dir)
        except TapAborted as exc:
            return {"claim": payload, "_terminal": {
                "gate": "selection_cancel_unverified",
                "reason": f"候选面板取消点击结果未知：{exc}"}}
        except Exception as exc:  # noqa: BLE001
            return {"claim": payload, "_terminal": {
                "gate": "selection_cancel_unverified",
                "reason": f"候选面板取消点击结果未知：{exc}"}}
        chain["cancel_tap"] = outcome
        chain["stage"] = "cancelled"
        # 行动窗可能还在：再读一帧，圆盘重现且决策恰为「过」才收尾。
        try:
            raw = self._shot()
            path = self._save_frame(raw, "chi_after_cancel")
            image2 = cv2.imdecode(
                np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:  # noqa: BLE001 - 取消已成，读不到后续帧不算失败
            return {"claim": payload}
        if image2 is None:
            return {"claim": payload}
        claims2 = read_claims(image2)
        if not claims2.buttons:
            return {"claim": payload}
        hand2 = hand
        try:
            result2 = self._analyze(raw, path)
            if isinstance(result2, dict):
                merged = dict(result2)
                if self._discard_state is not None and self._discard_trusted:
                    merged["discards"] = self._discard_state
                hand2 = HandState.from_analysis(merged)
        except Exception:  # noqa: BLE001 - 分析失败就只关面板，不试点过
            return {"claim": payload}
        decision2 = decide_action(
            claims2, hand2, replace(self._claim_policy, chi_enabled=False),
            image=image2)
        if (decision2 is None or decision2.action != ACTION_PASS
                or not decision2.executable):
            return {"claim": payload}
        before2 = BoardState.from_hand_state(
            hand2, claim_buttons=len(claims2.buttons), settled=False,
            note="吃链取消后退回过点击前状态")
        try:
            pass_outcome = self._tap.execute_claim(
                decision2.tap_point, action=ACTION_PASS, image=image2,
                session_dir=self.session_dir)
        except TapAborted as exc:
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_aborted",
                "reason": f"吃链退回过点击未通过：{exc}"}}
        except Exception as exc:  # noqa: BLE001
            return {"claim": payload, "_terminal": {
                "gate": "claim_tap_failed",
                "reason": f"吃链退回过点击结果未知：{exc}"}}
        chain["fallback_pass_tap"] = pass_outcome
        verified, reason, _after2, settle_samples = (
            self._verify_claim_settled(
                before2, ACTION_PASS, tag="after_chi_fallback_pass",
                note="退回过点击后 fresh 状态"))
        chain["fallback_pass_settle_samples"] = settle_samples
        chain["fallback_pass_verified"] = verified
        chain["fallback_pass_reason"] = reason
        chain["stage"] = "cancelled_then_passed" if verified else "unverified"
        if not verified:
            return {"claim": payload, "_terminal": {
                "gate": "claim_unverified", "reason": reason}}
        self.actions_done += 1
        return {"claim": payload}

    @guard_live_run
    def run(self) -> dict:
        """直到停手条件满足；返回本次运行摘要。"""
        self.check_gates()
        began = self._clock()
        while not self.stopped:
            if self._stop_requested_reason:
                self._stop("user_stop", self._stop_requested_reason)
                break
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 先落台账，再保留原异常语义
                self._stop(
                    "unhandled_exception",
                    f"循环未捕获异常：{type(exc).__name__}: {exc}",
                    {"traceback": traceback.format_exc()},
                )
                raise
            if self.stopped:
                break
            if self._stop_requested_reason:
                self._stop("user_stop", self._stop_requested_reason)
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
            "follow_session": str(self.follow_log_path),
            "actions": self.actions_done,
            "dry_run": self.dry_run,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "stop_gate": getattr(self, "stop_gate", ""),
            "consecutive_errors": self.consecutive_errors,
            "rounds": self.round.seq,
            "card_count": self.card_count,
        }
