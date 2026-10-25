# -*- coding: utf-8 -*-
"""tap_executor.py — 把“建议牌的像素框”转换成一次安全的 ADB 点击（单张）。

这是代打功能的基础设施，只负责“点一张已经由人工确认的建议牌”。
连续代打、自动摸牌循环、吃碰决策都不在本模块。

安全设计（四道闸，缺一不可，全部默认生效）：

1. dry_run 默认
   构造时 ``enabled=False``，``execute()`` 只计算坐标、绝不调用 adb。
   ``enabled`` 只能由调用方代码显式传布尔值；环境变量、配置文件、
   序列化字段都无法把它打开，且对外只读（无 setter）。

2. 几何白名单兜底硬黑名单
   充值 / 开始游戏 / 游戏中心 / 房卡不足等控件全部位于手牌横排之外
   （HANDOVER.md 红线第 2、5 条）。因此点击目标必须同时满足：
   y ≥ ``HAND_ROI_MIN_Y``（900 高截图的手牌 ROI 上沿）**且**严格落在
   ``TileVision.count_tiles`` 识别出的手牌框内。任一不满足即
   ``TapRejected``，拒绝且不重试。

3. 点击前必须重新截屏
   绝不用旧的 ``suggested_boxes`` 直接点——牌局在动，两次截图之间
   手牌会变。流程：拿到建议 → 重新截屏 → 在新截图上按牌面
   ``tile_id``（不是像素坐标）重新定位同一张建议牌 → 找不到即
   ``TargetMissing``，零点击中止。

4. 两段确认 + 点击后验证
   浙江游戏大厅需要先点牌使其抬起，再点同一位置确认出牌。第一次点击后
   必须看到目标列画面变化；**两次点击之间还要复核手牌状态（审计 B）**：
   手牌已少一张 → 通常判定牌已离手、**禁止第二击**；但若同时严格命中
   “胡提示面板”单信号，则说明抬起牌被识牌 ROI 漏掉，仍须第二击确认；否则必须张数守恒、
   目标牌仍在原列且几何抬起（``y1`` 低于同帧中位数 ≥
   ``RAISED_BOX_LIFT_PX``），才允许第二击。第二次点击后，目标列还必须
   再次变化，且手牌数量恰好减一。任何一步不满足都 ``TapUnverified``，
   绝不继续点击。

证据落盘（只观察，不改判定）
============================
``enabled=True`` 的每一次 ``execute()`` 都会把 before / selected / after
三帧与 ``target_column.png``（目标列的 2 倍放大裁图）连同 ``evidence.json``
写到 ``<session_dir>/tap_probe/<YYYYmmdd_HHMMSS_ffffff>/``。失败路径
**先落盘再抛异常**（``TapUnverified`` / ``TapRejected`` / ``TapAborted``
以及任何其它异常都覆盖），已有哪几帧就写哪几帧，缺失的记 ``null``。

落盘本身再包一层 ``try/except``：落盘失败只丢证据，绝不吞掉原始异常，
也**绝不**变成放行条件。``dry_run``（``enabled=False``）分支不落盘——
只读路径不允许变成写盘路径。

``evidence.json`` 里的 ``raised_candidates`` 会在 selected 帧上重跑一次
``count_tiles``，列出每个暗手框的 ``y0/y1``，并标出 ``y1`` 比中位数低
≥ ``RAISED_BOX_LIFT_PX``(12) px 的框，用来回答"抬起的牌到底有没有被
算成暗手框"。

行动行点击通道（:meth:`TapExecutor.execute_claim`）
==================================================
行动圆盘（碰/吃/杠/胡/过）在 y≈596..620，落在上面的手牌白名单（y≥720）
之外，所以本模块另开一条**独立**的 :meth:`TapExecutor.execute_claim` 通道：

* **不放宽** :meth:`TapExecutor.validate_target` 的既有白名单——放宽会连带
  放开充值（顶部居中）、游戏中心（右上）、开始游戏（中屏）三个点，它们有
  ``tests/test_tap_executor.py`` 的测试红线盯着；
* 自带独立的行动行几何白名单（:data:`CLAIM_ROI_X_MIN` .. :data:`CLAIM_ROI_X_MAX`、
  :data:`CLAIM_ROI_Y_MIN` .. :data:`CLAIM_ROI_Y_MAX`）；
* 点击坐标必须落在 ``claim_vision.read_claims`` **实测**返回的圆盘连通域框内，
  槽位推断值（``SLOT_CENTER_X``）一律不接受；
* 黑名单（:data:`CLAIM_ACTION_BLACKLIST`，含房卡价的 ``洗牌``/``切牌``）
  按动作名永不放行；``取消`` 只有显式 ``cancel_selection``、本帧唯一蓝盘
  且落在候选面板白名单时放行，普通行动决策仍不能借它冒充“过”；
* fail-closed：读不出圆盘、坐标不在实测框内、任何一项校验不过 → 拒绝点击。

识别链路（tile_vision / tea_bot / assist_tea / claim_vision）只 import，不修改。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

import cv2
import numpy as np

from action_guard import detect_action_ui
from claim_vision import (
    KIND_CANCEL,
    KIND_GOLD,
    KIND_HU,
    KIND_PASS,
    ClaimButton,
    ClaimState,
    read_claims,
)
from harvest_tea import DEFAULT_ADB, DEFAULT_SERIAL, EXPECTED_PACKAGE
from tea_bot import _TEMPLATE_DIR  # noqa: F401  # 仅复用已验证的茶模板路径，不改动
from tile_vision import TILE_IDS, TileVision
from chi_panel_vision import ChiPanelState, read_chi_panel
from invite_dialog import InvitationState, read_invitation
from turn_gate import detect_settled_screen

import panel_recovery

# 1600x900 已验证 ADB 原生截图的手牌横排上沿（count_tiles 默认 roi 0.8*h）。
HAND_ROI_MIN_Y = 720
FRAME_SIZE = (900, 1600)
# 目标点必须离手牌框边缘至少 2px：压边的点不是“稳稳落在牌上”。
BOX_EDGE_MARGIN = 2
# 证据落盘根目录：与 recorder 的会话目录同一条规则
# （assist_window 的 session_dir = 采集/assist_sessions/<YYYYmmdd_HHMMSS>，
#   recorder 的 assist_record.jsonl 就写在它下面）。TapExecutor 不发明新规则；
#  调用方（assist_window / auto_play_loop）可以显式传 session_dir 或事后
#  直接给 executor.session_dir 赋值，把证据并进自己的会话目录。
SESSION_ROOT = Path(__file__).resolve().parent.parent / "采集" / "assist_sessions"
TAP_PROBE_DIRNAME = "tap_probe"
# 兜底目录：会话目录取不到 / 写不进去时，证据至少落到 采集/tap_probe/ 下，
# 绝不因为目录问题丢掉唯一的一手证据。
FALLBACK_PROBE_ROOT = SESSION_ROOT.parent / TAP_PROBE_DIRNAME
# before_count 的出处说明（写死，避免事后复核的人把它当成点击后的值）。
BEFORE_COUNT_SOURCE = (
    "located['before_count'] —— _locate_suggested_tile(fresh) 在**第一次点击之前**、"
    "在 fresh 帧上测得的暗手张数（与 after_count 不同帧、不同时刻）"
)
# “疑似抬起的牌”判据：暗手框 y1 比同帧其它暗手框 y1 中位数低至少这么多 px。
RAISED_BOX_LIFT_PX = 12
# target_column.png 的放大倍数（人眼核对抬牌态用）。
TARGET_COLUMN_SCALE = 2
# 与 harvest_tea.require_game_foreground 相同口径的前台校验正则。
FOREGROUND_PATTERNS = (
    r"topResumedActivity=.*?\s([A-Za-z0-9_.]+)/",
    r"ResumedActivity:.*?\s([A-Za-z0-9_.]+)/",
)


# ---------------------------------------------------------------------------
# 行动行（吃/碰/杠/胡/过）点击通道的独立几何白名单
#
# 这些值与手牌白名单（HAND_ROI_MIN_Y=720）**互不相干**：放宽手牌白名单会
# 连带放开充值 / 游戏中心 / 开始游戏，所以行动行单独开门，手牌那道闸不动。
#
# 实测依据（采集/assist_sessions 离线帧 + action_inventory.md）：
#   过   连通域框 [1383,552,1483,652]，中心 (1433,602)
#   取消 连通域框 [1399,546,1499,646]，中心 (1449,596)
#   金盘（碰/吃/杠）与胡盘 中心 y 588..610，中心 x 754..1501
# 圆盘中心 y 恒在 596..620，因此 [560,660] 上下各留 ~24px 余量。
# ---------------------------------------------------------------------------

CLAIM_ROI_Y_MIN: int = 560
CLAIM_ROI_Y_MAX: int = 660
CLAIM_ROI_X_MIN: int = 740
CLAIM_ROI_X_MAX: int = 1520
# 吃牌候选面板的蓝色圆盘可能高于行动行；只给显式取消动作使用。
SELECTION_CANCEL_ROI_Y_MIN: int = 300
SELECTION_CANCEL_ROI_Y_MAX: int = 720
SELECTION_CANCEL_ROI_X_MIN: int = 740
SELECTION_CANCEL_ROI_X_MAX: int = 1580
# 落点必须离实测圆盘框边缘至少 2px：压边的点不是"稳稳落在圆盘上"。
CLAIM_BOX_EDGE_MARGIN: int = 2

#: 永不放行的圆盘种类。``取消`` 点了会不可逆地中断进行中的选择（吃面板）。
CLAIM_KIND_BLACKLIST: frozenset[str] = frozenset({KIND_CANCEL})

#: 永不放行的动作名。**与 ``action_policy.BLACKLISTED_LABELS`` 同一口径**，
#: 此处独立写死是为了不让 tap_executor 依赖策略层（策略层反过来可以依赖它）。
#: ``洗牌`` 房卡×2、``切牌`` 房卡×4 —— 花钱的按钮任何路径都不许点。
CLAIM_ACTION_BLACKLIST: frozenset[str] = frozenset({
    "洗牌", "切牌",                                  # 花钱（房卡×2 / ×4）
    "充值", "开始游戏", "返回大厅", "分享战绩", "游戏中心",  # 离桌/开新局/进支付
})

#: 实测禁区（xyxy）。``DEFAULT_FORBIDDEN_BOXES`` 尚未测到坐标，故为空；
#: 一旦测到就补进来，与标签黑名单双重生效。空 tuple 表示"只按标签拦"。
CLAIM_FORBIDDEN_BOXES: tuple[tuple[int, int, int, int], ...] = ()

#: 动作名 → 期望的圆盘种类。调用方不给 ``kind`` 时按此推导；推导结果也是
#: 一道闸：说要"碰"而画面上只有蓝盘（过），就没有候选，直接拒绝。
CLAIM_ACTION_KINDS: Mapping[str, str] = {
    "hu": KIND_HU, "胡": KIND_HU,
    "peng": KIND_GOLD, "碰": KIND_GOLD,
    "kong": KIND_GOLD, "杠": KIND_GOLD,
    "chi": KIND_GOLD, "吃": KIND_GOLD,
    "pass": KIND_PASS, "过": KIND_PASS,
    "cancel_selection": KIND_CANCEL, "取消候选": KIND_CANCEL,
}

SELECTION_CANCEL_ACTIONS: frozenset[str] = frozenset({
    "cancel_selection", "取消候选",
})


# 吃候选面板点组通道的独立几何白名单（与 chi_panel_vision.PANEL_ROI 同口径）。
# 候选牌行 y 恒在 [550,655]，组并集框中心 y≈602；3 组面板最左组中心 x≈736，
# 已在行动行 x 下限 740 之外——这正是面板点不能套行动行白名单的原因。
PANEL_ROI_X_MIN: int = 560
PANEL_ROI_X_MAX: int = 1400
PANEL_ROI_Y_MIN: int = 500
PANEL_ROI_Y_MAX: int = 700

#: 吃候选面板点组动作名（只在本模块与吃链台账之间使用）。
PANEL_ACTION: str = "chi_select"


class TapAborted(RuntimeError):
    """安全中止基类：一旦中止就停止，绝不重试。"""


class TapPrecheckRetryExhausted(TapAborted):
    """点击前连续重拍仍无法得到稳定证据；保证尚未发送任何点击。"""


class TapRejected(TapAborted):
    """目标坐标没有通过几何白名单（疑似充值/按钮/弹窗区域）。"""


class TargetMissing(TapAborted):
    """建议牌在新截图中未定位到（按牌面匹配），零点击中止。"""


class TapUnverified(TapAborted):
    """点击后手牌数量没有恰好减一，验证失败，不重试。"""


@dataclass(frozen=True)
class TapPlan:
    """一次出牌点击的完整计划（坐标 + 依据）。"""

    label: str                       # 牌面 label（vision 词汇，如 't3'）
    tile_id: int                     # vision 牌 ID
    box: tuple[int, int, int, int]   # 建议牌像素框 xyxy
    x: int                           # 框中心
    y: int


def label_for_tile(tile_id: int) -> str:
    """Return the vision label for a tile id (fallback keeps the id visible)."""
    for name, tid in TILE_IDS.items():
        if tid == tile_id:
            return name
    return f"tile_{tile_id}"


def box_center(box) -> tuple[int, int]:
    """Return the integer center of an xyxy box."""
    x0, y0, x1, y1 = (int(value) for value in box[:4])
    return (x0 + x1) // 2, (y0 + y1) // 2


@dataclass
class _TapEvidence:
    """一次 ``execute()`` 的证据收集器（只写盘，不参与任何判定）。

    Attributes:
        session_dir: 会话目录；证据写到它下面的 ``tap_probe/<时间戳>/``。
        tile_label / tile_id: 建议牌（进台账便于对照）。
        before / selected / after: 三个关键时刻的原生帧（可为空）。
        frames: 落盘后的文件名映射，缺失的帧记 ``None``。
    """

    session_dir: Path
    tile_label: str = ""
    tile_id: int | None = None
    before: np.ndarray | None = None
    selected: np.ndarray | None = None
    after: np.ndarray | None = None
    before_count: int | None = None
    after_count: int | None = None
    before_count_source: str = BEFORE_COUNT_SOURCE
    action_guard_before: dict | None = None
    action_guard_afterselect: dict | None = None
    tap: list[int] | None = None
    box: list[int] | None = None
    first_click_metrics: dict | None = None
    second_click_metrics: dict | None = None
    hand_boxes_selected: list | None = None
    hand_boxes_after: list | None = None
    raised_candidates: dict | None = None
    # 第一次点击后的手牌复核（审计 B 的闸门结果）：只写盘，不参与判定。
    selected_gate: dict | None = None
    # 点击前完整复核/专用锁定器的每次尝试。只记录，不参与判定。
    preclick_attempts: list[dict] = field(default_factory=list)
    # 行动行点击（execute_claim）专用：动作名、期望/实测圆盘种类、点击前后
    # 的圆盘列表。**只写盘，不参与任何判定**。
    claim: dict | None = None
    frames: dict = field(default_factory=dict)

    @staticmethod
    def _write_png(path: Path, image) -> str | None:
        """写一帧 PNG；图为空或编码失败返回 ``None``（不抛）。"""
        if image is None:
            return None
        try:
            ok, encoded = cv2.imencode(".png", image)
        except cv2.error:
            return None
        if not ok:
            return None
        try:
            path.write_bytes(encoded.tobytes())
        except OSError:
            return None
        return path.name

    def dump(self, outcome: str, reason: str = "") -> str | None:
        """把已有证据写进 ``<session_dir>/tap_probe/<时间戳>/``。

        Args:
            outcome: ``"verified"`` 或异常类名（如 ``"TapUnverified"``）。
            reason: 人类可读依据（成功时是成功依据，失败时是异常文本）。

        Returns:
            落盘目录的字符串路径；落盘失败返回 ``None``。**返回什么都不
            影响控制流**：调用方只是记一笔，绝不用它决定是否放行。
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # 首选会话目录；写不进去就兜底到 采集/tap_probe/，绝不丢证据。
        try:
            return self._dump_into(
                Path(self.session_dir) / TAP_PROBE_DIRNAME / stamp, outcome, reason)
        except Exception:  # noqa: BLE001 — 换兜底目录再试一次
            pass
        try:
            return self._dump_into(FALLBACK_PROBE_ROOT / stamp, outcome, reason)
        except Exception:  # noqa: BLE001 — 落盘失败只丢证据
            return None

    def _dump_into(self, target: Path, outcome: str, reason: str) -> str:
        """把证据写进指定目录（唯一一处真正写盘的地方）。"""
        try:
            target.mkdir(parents=True, exist_ok=True)
            written = {
                "before": self._write_png(target / "before.png", self.before),
                "selected": self._write_png(target / "selected.png", self.selected),
                "after": self._write_png(target / "after.png", self.after),
                "target_column": None,
            }
            # 行动行只点一次，没有 selected 帧，此时用 after 帧裁同一区域，
            # 让人眼仍能核对"点的那一块到底变了没"。
            region = (self.first_click_metrics or {}).get("region")
            crop_source = self.selected if self.selected is not None else self.after
            if crop_source is not None and region:
                x0, y0, x1, y1 = (int(value) for value in region)
                height, width = crop_source.shape[:2]
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(width, x1), min(height, y1)
                crop = crop_source[y0:y1, x0:x1]
                if crop.size:
                    scale = TARGET_COLUMN_SCALE
                    big = cv2.resize(
                        crop, (crop.shape[1] * scale, crop.shape[0] * scale),
                        interpolation=cv2.INTER_NEAREST)
                    written["target_column"] = self._write_png(
                        target / "target_column.png", big)
            payload = {
                "at": datetime.now().astimezone().isoformat(),
                "tap": self.tap,
                "box": self.box,
                "tile_label": self.tile_label,
                "tile_id": self.tile_id,
                "before_count": self.before_count,
                "before_count_source": self.before_count_source,
                "after_count": self.after_count,
                # 原始 detect_action_ui 返回值，不加工：按钮在不在画面上，
                # 是"代点前就已注定"还是"代点后才有"的唯一判据。
                "action_guard_before": self.action_guard_before,
                "action_guard_afterselect": self.action_guard_afterselect,
                "first_click_metrics": self.first_click_metrics,
                "second_click_metrics": self.second_click_metrics,
                "hand_boxes_selected": self.hand_boxes_selected,
                "hand_boxes_after": self.hand_boxes_after,
                "raised_candidates": self.raised_candidates,
                "selected_gate": self.selected_gate,
                "preclick_attempts": self.preclick_attempts,
                "claim": self.claim,
                "frames": written,
                "outcome": outcome,
                "reason": reason,
            }
            (target / "evidence.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self.frames = written
            return str(target)
        except Exception:  # noqa: BLE001 — 落盘失败只丢证据，绝不影响判定
            raise


def _action_guard_record(result) -> dict | None:
    """把 ``detect_action_ui`` 的返回值原样转成 JSON（不加工）。"""
    if result is None:
        return None
    return {
        "state": getattr(result, "state", None),
        "reason": getattr(result, "reason", ""),
        "blocks_discard": bool(getattr(result, "blocks_discard", False)),
        "boxes": [[int(value) for value in box[:4]]
                  for box in (getattr(result, "boxes", ()) or ())],
    }


class TapExecutor:
    """把 analyze_snapshot 的建议结果变成一次经过验证的单次 ADB 点击。

    Usage:
        executor = TapExecutor()                       # 默认 dry_run
        outcome = executor.execute(analysis)           # 只算坐标，零 adb 调用

        executor = TapExecutor(enabled=True, serial="emulator-5556")
        outcome = executor.execute(analysis)           # 真正点击（需人工触发）
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        adb_path: str = DEFAULT_ADB,
        serial: str = DEFAULT_SERIAL,
        template_dir: str | Path | None = None,
        select_delay: float = 0.35,
        verify_delay: float = 1.5,
        settle_deadline: float = 3.0,
        settle_interval: float = 0.4,
        fresh_retry_attempts: int = 3,
        fresh_retry_interval: float = 0.4,
        screenshot_timeout: float = 30.0,
        session_dir: str | Path | None = None,
    ) -> None:
        """``enabled`` 只认显式布尔实参；缺省 False 且对外只读。

        环境变量 / 配置文件在这里根本没有入口，所以“意外打开真点”不可能。

        ``session_dir`` 缺省时按 recorder 的会话目录规则取
        :data:`SESSION_ROOT` / ``<YYYYmmdd_HHMMSS>``；调用方可以显式传，
        也可以在构造后直接给 ``executor.session_dir`` 赋值，把 tap 证据
        并进自己那一局的会话目录。**只有 ``enabled=True`` 才会往这里写。**

        .. warning::
           缺省目录会在真实语料库 ``采集/assist_sessions/`` 下建"假会话"。
           出牌入口 :meth:`execute` 沿用旧行为；新的行动行入口
           :meth:`execute_claim` **拒收**缺省目录（见
           :meth:`_resolve_claim_session_dir`），调用方必须显式给。
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是显式 bool；True 才允许真实点击")
        self._enabled = enabled
        self.adb_path = str(adb_path)
        self.serial = str(serial)
        self.template_dir = Path(template_dir) if template_dir else Path(_TEMPLATE_DIR)
        self.select_delay = max(0.0, float(select_delay))
        self.verify_delay = max(0.0, float(verify_delay))
        self.settle_deadline = max(0.0, float(settle_deadline))
        self.settle_interval = max(0.0, float(settle_interval))
        self.fresh_retry_attempts = max(1, int(fresh_retry_attempts))
        self.fresh_retry_interval = max(0.0, float(fresh_retry_interval))
        self.screenshot_timeout = float(screenshot_timeout)
        # 是否由调用方显式指定过落盘目录：行动行入口拿它挡住缺省目录。
        # 直接写 _session_dir，绕开 setter —— 缺省分支不算"显式指定"。
        self._session_dir_explicit = session_dir is not None
        self._session_dir = (Path(session_dir) if session_dir
                             else SESSION_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S"))
        # 胡提示面板待二击状态（panel_recovery）：调用方（循环）在每次
        # execute() 前把局身份/点击前手牌写进 pending_context，execute()
        # 第一击确认改变目标列后据此落盘、验证成功即清除；None = 本次
        # 不落盘（缺省，零行为变化）。pending_state_path 可覆盖状态文件
        # 位置（测试用），缺省按 serial 固定路径。
        self.pending_context: dict | None = None
        self.pending_state_path: Path | None = None

    @property
    def session_dir(self) -> Path:
        """证据落盘目录；赋值即视为"调用方显式指定"（同上 warning）。"""
        return self._session_dir

    @session_dir.setter
    def session_dir(self, value: str | Path) -> None:
        self._session_dir = Path(value)
        self._session_dir_explicit = True

    @property
    def enabled(self) -> bool:
        """只读：是否允许真实点击（无 setter，代码外无法翻转）。"""
        return self._enabled

    def raised_candidates(self, image) -> dict:
        """在一帧上重跑 ``count_tiles``，标出“疑似抬起的牌”。

        判据沿用既有几何口径：暗手框的 ``y1`` 比同帧其它暗手框 ``y1`` 的
        中位数低 ≥ :data:`RAISED_BOX_LIFT_PX`(12) px。**只用于证据记录，
        不参与任何通过/中止判定。**

        Args:
            image: 一帧 BGR 图（通常是 selected 帧）。

        Returns:
            含 ``boxes``（每框的 ``y0/y1/raised``）、``median_y1``、
            ``lift_px``、``raised`` 的字典；识别失败时带 ``error``。
        """
        empty: dict = {"boxes": [], "raised": [], "median_y1": None,
                       "lift_px": RAISED_BOX_LIFT_PX}
        if image is None:
            return dict(empty, error="frame_missing")
        try:
            _count, boxes = self._make_vision().count_tiles(image)
        except Exception as exc:  # noqa: BLE001 — 诊断失败只记一笔
            return dict(empty, error=f"count_tiles 失败: {exc}")
        items = []
        for box in boxes or []:
            try:
                x0, y0, x1, y1 = (int(value) for value in box[:4])
            except (TypeError, ValueError):
                continue
            items.append({"box": [x0, y0, x1, y1], "y0": y0, "y1": y1,
                          "raised": False})
        if not items:
            return dict(empty, error="no_hand_boxes")
        median_y1 = float(np.median([item["y1"] for item in items]))
        for item in items:
            item["raised"] = item["y1"] <= median_y1 - RAISED_BOX_LIFT_PX
        raised = [item for item in items if item["raised"]]
        return {"boxes": items, "raised": raised, "median_y1": median_y1,
                "lift_px": RAISED_BOX_LIFT_PX}

    # ------------------------------------------------------------------
    # 第 1 步：从 analyze_snapshot 结果计算点击计划（不产生任何 adb 调用）
    # ------------------------------------------------------------------
    def plan(self, analysis: dict) -> TapPlan:
        """Validate an advise result and compute the tap plan from its box."""
        status = analysis.get("status")
        if status != "advise":
            raise TapAborted(f"只有 advise 状态才能出牌，当前 {status!r}")
        tile_id = analysis.get("suggestion_id")
        if not isinstance(tile_id, int) or tile_id not in set(TILE_IDS.values()):
            raise TapAborted(f"建议牌 tile_id 无效: {tile_id!r}")
        boxes = analysis.get("suggested_boxes") or []
        if not boxes:
            raise TapAborted("分析结果没有 suggested_boxes，无法定位建议牌")
        box = tuple(int(value) for value in boxes[0][:4])
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            raise TapAborted(f"建议牌像素框无效: {box}")
        x, y = box_center(box)
        return TapPlan(label=label_for_tile(tile_id), tile_id=tile_id,
                       box=box, x=x, y=y)

    # ------------------------------------------------------------------
    # 第 2 步：几何白名单（硬黑名单兜底）
    # ------------------------------------------------------------------
    def validate_target(
        self,
        point: tuple[int, int],
        hand_boxes,
        frame_shape,
    ) -> tuple[int, int]:
        """只允许点击手牌 ROI 内、且严格落在 count_tiles 手牌框内的点。

        充值 / 开始游戏 / 游戏中心 / 房卡不足等控件都不在手牌横排内，
        这条几何约束就是它们的硬黑名单兜底；日后拿到这些区域的实测
        像素范围，可以在此追加显式禁区，兜底约束不撤。
        """
        x, y = int(point[0]), int(point[1])
        height, width = frame_shape[:2]
        if not (0 <= x < width and 0 <= y < height):
            raise TapRejected(
                f"点击坐标超出截图范围: ({x},{y}) / {width}x{height}")
        if y < HAND_ROI_MIN_Y:
            raise TapRejected(
                f"目标 y={y} 不在手牌 ROI（y≥{HAND_ROI_MIN_Y}）——充值、开始游戏、"
                "游戏中心、房卡不足等按钮/弹窗都在手牌横排之外，拒绝点击")
        for box in hand_boxes or []:
            x0, y0, x1, y1 = (int(value) for value in box[:4])
            if (x0 + BOX_EDGE_MARGIN <= x <= x1 - BOX_EDGE_MARGIN
                    and y0 + BOX_EDGE_MARGIN <= y <= y1 - BOX_EDGE_MARGIN):
                return x, y
        raise TapRejected(
            f"目标 ({x},{y}) 不在 count_tiles 识别出的手牌框内，拒绝点击")

    # ------------------------------------------------------------------
    # 行动行执行入口（吃/碰/杠/胡/过）：独立白名单，不动出牌那道闸
    # ------------------------------------------------------------------
    def execute_claim(
        self,
        point: tuple[int, int] | None = None,
        *,
        action: str = "",
        kind: str | None = None,
        image: np.ndarray | None = None,
        session_dir: str | Path | None = None,
        verify_delay: float | None = None,
    ) -> dict:
        """点一次行动圆盘（碰/吃/杠/胡/过），四道闸缺一不可。

        与 :meth:`execute`（出牌）**完全分离**：这里不开 :meth:`validate_target`
        的口子，只用 :data:`CLAIM_ROI_X_MIN` .. :data:`CLAIM_ROI_X_MAX` /
        :data:`CLAIM_ROI_Y_MIN` .. :data:`CLAIM_ROI_Y_MAX` 这条行动行专属
        白名单，并要求落点在 ``claim_vision.read_claims`` **实测**的圆盘连通域
        框内（槽位推断值一律不接受）。

        Args:
            point: 要点的坐标 ``(x, y)``。为 ``None`` 时由本方法从实测圆盘里
                挑——**只有恰好一个候选**才挑得出来，多于一个即拒绝（不猜）。
                给了坐标就必须落进某个实测圆盘框内，否则拒绝。
            action: 动作名，必填且非空。命中 :data:`CLAIM_ACTION_BLACKLIST`
                （``洗牌``/``切牌``/``充值``/…）立即拒绝，零 adb 调用。
            kind: 期望的圆盘种类（``"gold"``/``"hu"``/``"pass"``）。不给时
                按 :data:`CLAIM_ACTION_KINDS` 从 ``action`` 推导；推导出来
                的种类也是一道闸（要"碰"却只有蓝盘 → 拒绝）。
            image: 只在 ``dry_run``（``enabled=False``）时使用的一帧，用来
                在不截图的前提下复核圆盘几何。``enabled=True`` 时忽略它，
                一律**重新截图**（牌局在动，旧帧的圆盘可能已经消失）。
            session_dir: 证据落盘目录。**行动行入口拒收缺省目录**——缺省值
                会在真实语料库里建假会话，见 :meth:`_resolve_claim_session_dir`。
            verify_delay: 点击后到复核截图的等待秒数；``None`` 用构造参数。

        Returns:
            ``dry_run``：``{"executed": False, "dry_run": True,
            "stage": "plan", ...}``，零 adb 调用、零落盘。
            真点成功：``{"executed": True, "dry_run": False,
            "stage": "clicked", "tap": [x, y], "box": [...],
            "click_metrics": {...}, "ui_responded": True,
            "semantic_verified": False, ...}``。**注意** ``stage`` 是
            ``"clicked"`` 而不是 ``"verified"``：本模块只证明点击送达且
            UI 有响应（圆盘消失），不证明动作语义成功——那归
            ``action_policy.verify_after_click``（审计 A）。

        Raises:
            TapRejected: 黑名单动作 / 坐标不在行动行 ROI / 坐标不在实测圆盘框
                / 读出的是取消盘 / 读不出圆盘。全部零点击。
            TapUnverified: 点击后目标区域画面没变化。已落证据，不重试。
            TapAborted: 前景不是游戏、截图失败、缺 ``session_dir`` 等。
        """
        label = str(action or "").strip()
        if not label:
            raise TapAborted(
                "execute_claim 必须显式给出 action（碰/吃/杠/胡/过），拒绝盲点")
        # 第一道闸：动作名黑名单。洗牌/切牌要花房卡，充值/开始游戏等会离桌
        # 或进支付——这些在任何路径下都不许点，且必须在任何 adb 调用之前拦下。
        if label in CLAIM_ACTION_BLACKLIST:
            raise TapRejected(f"动作 {label!r} 在永不点击黑名单里，拒绝点击")
        expected_kind = kind if kind is not None else CLAIM_ACTION_KINDS.get(label)
        allow_selection_cancel = (label in SELECTION_CANCEL_ACTIONS
                                  and expected_kind == KIND_CANCEL)
        if (expected_kind is not None and expected_kind in CLAIM_KIND_BLACKLIST
                and not allow_selection_cancel):
            raise TapRejected(
                f"动作 {label!r} 期望的圆盘种类 {expected_kind!r} 在黑名单里，拒绝点击")
        # 第二道闸：落盘目录必须是调用方显式给的（见方法 docstring）。
        dump_dir = self._resolve_claim_session_dir(session_dir)
        # 第三道闸：坐标几何白名单。给了坐标就先验，零 adb 调用即可拒绝
        # 充值(800,120) / 游戏中心(1480,60) / 开始游戏(800,430) 这类点。
        if point is not None:
            if allow_selection_cancel:
                self.validate_selection_cancel_point(point)
            else:
                self.validate_claim_point(point)
        if not self._enabled:
            return self._plan_claim(
                image, point, label, expected_kind,
                allow_selection_cancel=allow_selection_cancel)

        evidence = _TapEvidence(dump_dir, tile_label=label)
        try:
            self._require_game_foreground()
            before = self._screenshot()
            evidence.before = before
            # 第四道闸：圆盘必须是从**本帧**像素里实测出来的连通域，
            # 不是槽位推断；读不出来就没有任何可点的东西。
            state = read_claims(before)
            button = self._resolve_claim_button(
                state, point, expected_kind,
                allow_selection_cancel=allow_selection_cancel)
            box = tuple(int(value) for value in button.box[:4])
            if point is None:
                x, y = int(button.center[0]), int(button.center[1])
                if allow_selection_cancel:
                    self.validate_selection_cancel_point((x, y))
                else:
                    self.validate_claim_point((x, y))
            else:
                x, y = int(point[0]), int(point[1])
            claim_record = self._claim_record(
                label, expected_kind, state, button, None)
            # 按钮区/玩法门禁的一手读数（与 read_claims 同源，只读）。失败路径
            # 也要有，所以紧跟截图就取；拿不到记 None，绝不影响控制流。
            claim_record["action_ui_before"] = self._action_ui_evidence(before)
            evidence.claim = claim_record
            evidence.tap = [x, y]
            evidence.box = list(box)
            self._require_game_foreground()
            self._tap(x, y)
            delay = self.verify_delay if verify_delay is None else max(0.0, float(verify_delay))
            if delay > 0:
                time.sleep(delay)
            after = self._screenshot()
            evidence.after = after
            metrics = self._target_column_metrics(before, after, box)
            evidence.first_click_metrics = metrics
            claim_record["buttons_after"] = self._claim_buttons_record(
                read_claims(after))
            claim_record["action_ui_after"] = self._action_ui_evidence(after)
            if not metrics["changed"]:
                raise TapUnverified(
                    f"行动行点击 ({x},{y}) 后目标圆盘区域画面未变化"
                    f"（diff_mean={metrics['diff_mean']}, "
                    f"changed_ratio={metrics['changed_ratio']}），未证明点击生效")
            # 审计 A（2026-10-27 Codex 只读审计）：按钮区域变化/按钮消失都不能
            # 单独证明动作成功——动画结束、被其它 UI 覆盖、游戏超时取消都有
            # 同样的像素表现。本模块只证明"点击送达且 UI 有响应"：目标圆盘
            # 必须从 after 帧的实测里消失。**语义级验证**（胡进结算、碰后
            # 副露+1 等）归 ``action_policy.verify_after_click``（L3），
            # 调用方必须接，这里绝不冒充。
            still_there = [b for b in claim_record["buttons_after"]
                           if self._claim_point_in_box((x, y), b["box"])]
            if still_there:
                raise TapUnverified(
                    f"行动行点击 ({x},{y}) 后目标圆盘仍实测存在于 "
                    f"{still_there[0]['box']}，点击没有生效，不重试")
        except Exception as exc:  # noqa: BLE001 — 原异常照抛，只是先落盘
            evidence.dump(type(exc).__name__, str(exc))
            raise
        outcome = {
            "executed": True, "dry_run": False, "stage": "clicked",
            "action": label, "kind": button.kind,
            "tap": [x, y], "box": list(box),
            "click_metrics": metrics,
            "claim_buttons_before": len(claim_record["buttons_before"]),
            "claim_buttons_after": len(claim_record["buttons_after"]),
            # 只证明到 UI 层：点击送达、画面响应、圆盘消失。
            "ui_responded": True,
            # 语义级（真的碰成了/胡进结算了）本模块证明不了，绝不谎称。
            "semantic_verified": False,
            "semantic_verifier": "action_policy.verify_after_click",
            "reason": (f"行动 {label} 已点击 ({x},{y})，目标圆盘区域画面已变化、"
                       "目标圆盘已从画面消失（UI 响应确认）；语义级验证须由 "
                       "action_policy.verify_after_click 完成"),
        }
        evidence.dump("clicked", outcome["reason"])
        return outcome

    # ------------------------------------------------------------------
    # 入桌邀请拒绝入口：矩形“取消”专用，不借道行动圆盘/候选面板取消
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_invitation_cancel(
        state: InvitationState,
        point: tuple[int, int] | None,
    ) -> tuple[int, int, tuple[int, int, int, int]]:
        if not isinstance(state, InvitationState) or not state.present:
            raise TapRejected("本帧没有实测到‘邀请您进入’文案，拒绝点击")
        if (not state.complete or state.cancel_center is None
                or state.cancel_box is None):
            raise TapAborted(
                f"发现入桌邀请，但矩形取消按钮证据不完整：{state.reason}")
        center = tuple(int(v) for v in state.cancel_center)
        box = tuple(int(v) for v in state.cancel_box)
        if point is not None:
            requested = (int(point[0]), int(point[1]))
            if not TapExecutor._claim_point_in_box(requested, box):
                raise TapRejected(
                    f"邀请取消坐标 {requested} 不在 fresh 帧实测矩形按钮框 "
                    f"{list(box)} 内，拒绝点击")
            if max(abs(requested[0] - center[0]),
                   abs(requested[1] - center[1])) > 12:
                raise TapRejected(
                    f"邀请取消按钮两帧中心漂移超过 12px：旧 {requested} / 新 {center}，"
                    "拒绝点击")
        return center[0], center[1], box

    def execute_invitation_cancel(
        self,
        point: tuple[int, int] | None = None,
        *,
        image: np.ndarray | None = None,
        session_dir: str | Path | None = None,
        verify_delay: float | None = None,
    ) -> dict:
        """拒绝一次入桌邀请；只允许点矩形“取消”按钮。

        dry-run 必须显式传当前帧，只返回计划。真点时重新截屏，要求邀请文案
        与矩形取消按钮仍同帧存在、按钮中心相对旧帧稳定；点击后还要证明邀请
        文案消失。该入口与 ``cancel_selection`` 的蓝色圆盘完全分离。
        """
        dump_dir = self._resolve_claim_session_dir(session_dir)
        if not self._enabled:
            if image is None:
                raise TapAborted("邀请拒绝 dry_run 必须显式传 image，拒绝盲点")
            state = read_invitation(image)
            x, y, box = self._resolve_invitation_cancel(state, point)
            return {
                "executed": False, "dry_run": True, "stage": "plan",
                "action": "decline_invitation", "tap": [x, y],
                "box": list(box), "invitation": state.ledger(),
            }

        evidence = _TapEvidence(dump_dir, tile_label="decline_invitation")
        try:
            self._require_game_foreground()
            before = self._screenshot()
            evidence.before = before
            state = read_invitation(before)
            x, y, box = self._resolve_invitation_cancel(state, point)
            evidence.tap = [x, y]
            evidence.box = list(box)
            evidence.claim = {
                "action": "decline_invitation",
                "control": "rectangular_cancel",
                "source": "invite_dialog.read_invitation",
                "invitation_before": state.ledger(),
                "selection_cancel_channel_reused": False,
            }
            self._require_game_foreground()
            self._tap(x, y)
            delay = self.verify_delay if verify_delay is None else max(
                0.0, float(verify_delay))
            if delay > 0:
                time.sleep(delay)
            after = self._screenshot()
            evidence.after = after
            metrics = self._target_column_metrics(before, after, box)
            evidence.first_click_metrics = metrics
            after_state = read_invitation(after)
            evidence.claim["invitation_after"] = after_state.ledger()
            if not metrics["changed"]:
                raise TapUnverified(
                    f"点邀请取消 ({x},{y}) 后按钮区域画面未变化，未证明点击生效")
            if after_state.present:
                raise TapUnverified(
                    "点邀请取消后‘邀请您进入’文案仍在，拒绝重试第二下")
        except Exception as exc:  # noqa: BLE001 - 先落证据，原异常照抛
            evidence.dump(type(exc).__name__, str(exc))
            raise

        outcome = {
            "executed": True, "dry_run": False, "stage": "verified",
            "action": "decline_invitation", "tap": [x, y],
            "box": list(box), "click_metrics": metrics,
            "verified": True,
            "reason": "邀请文案与矩形取消按钮双帧确认，点击后邀请文案消失",
        }
        evidence.dump("verified", outcome["reason"])
        return outcome

    # ------------------------------------------------------------------
    # 吃候选面板点组入口：面板专用白名单，不借道行动行/手牌任何一道闸
    # ------------------------------------------------------------------
    def validate_chi_panel_point(self, point: tuple[int, int]) -> tuple[int, int]:
        """吃候选面板几何白名单：只放行面板区 x∈[560,1400]、y∈[500,700]。

        与 :meth:`validate_target`（手牌 y≥720）、:meth:`validate_claim_point`
        （行动行 x∈[740,1520]、y∈[560,660]）互不相干：候选组并集框中心
        y≈602；3 组面板最左组并集框中心实测约 x=736，已落在行动行
        x 下限 740 之外，硬套行动行白名单会误杀该组；反过来面板点也
        绝不许借道行动行/手牌通道。
        """
        x, y = int(point[0]), int(point[1])
        if not (PANEL_ROI_X_MIN <= x <= PANEL_ROI_X_MAX
                and PANEL_ROI_Y_MIN <= y <= PANEL_ROI_Y_MAX):
            raise TapRejected(
                f"吃候选面板点击坐标 ({x},{y}) 不在面板 ROI "
                f"x∈[{PANEL_ROI_X_MIN},{PANEL_ROI_X_MAX}]、"
                f"y∈[{PANEL_ROI_Y_MIN},{PANEL_ROI_Y_MAX}] 内，拒绝点击")
        return x, y

    @staticmethod
    def _chi_group_problem(group) -> str:
        """候选组本身合法性：恰 3 张互不相同、同花色连续顺子（引擎编码）。"""
        try:
            tiles = tuple(sorted(int(v) for v in group))
        except (TypeError, ValueError):
            return "候选组不是牌 ID 序列"
        if len(tiles) != 3 or len(set(tiles)) != 3:
            return "候选组不是 3 张互不相同的牌"
        for low, high in ((1, 9), (21, 29), (41, 49)):
            if low <= tiles[0] and tiles[2] <= high:
                return "" if tiles[2] - tiles[0] == 2 else "候选组不是同花色顺子"
        return "候选组不是同花色顺子"

    def execute_chi_panel_group(
        self,
        point: tuple[int, int] | None = None,
        *,
        group: Sequence[int],
        image: np.ndarray | None = None,
        session_dir: str | Path | None = None,
        verify_delay: float | None = None,
    ) -> dict:
        """点一次吃候选面板里选定的那一组，四道闸缺一不可。

        与 :meth:`execute_claim`（行动圆盘）完全分离：候选组不是圆盘，
        ``read_claims`` 测不到它，所以本入口用 ``chi_panel_vision``
        在 **fresh 帧** 上实测候选组，落点必须是所选组（牌面与调用方
        给的 ``group`` 恰好一致的那一组）的实测并集框中心或框内点。

        Args:
            point: 要点的坐标；``None`` 时点实测组框的中心。给了就必须
                落进那一组的实测并集框内（留 :data:`CLAIM_BOX_EDGE_MARGIN`
                边距），否则拒绝。
            group: 选定的顺子（3 张引擎牌 ID）。先由策略层
                ``choose_chi_panel_option`` 选出，再经本方法复核合法性。
            image: 只在 ``dry_run`` 时用的一帧；``enabled=True`` 时忽略，
                一律重新截图（面板在动，旧帧的组框不能信）。
            session_dir: 证据落盘目录，规则同 :meth:`execute_claim`
                （拒收缺省目录）。
            verify_delay: 点击后到复核截图的等待秒数；``None`` 用构造参数。

        Returns:
            ``dry_run``：``{"executed": False, "dry_run": True,
            "stage": "plan", ...}``，零 adb、零落盘。
            真点成功：``{"executed": True, "stage": "clicked",
            "ui_responded": True, "semantic_verified": False, ...}``——
            与行动行入口同一口径：本模块只证明点击送达、组框区域变化、
            候选面板消失；语义级验证（暗手 −2、副露 +1 组且恰为所选组）
            归 ``action_policy.verify_after_click`` 与循环侧复核。

        Raises:
            TapRejected: 组非法 / 坐标不在面板白名单 / fresh 帧面板读不
                完整 / 所选组在 fresh 帧上不存在或不唯一 / 点不在组框内。
                全部零点击。
            TapUnverified: 点击后组框区域没变化，或候选面板仍在画面上。
            TapAborted: 前景不是游戏、截图失败、缺 ``session_dir`` 等。
        """
        problem = self._chi_group_problem(group)
        if problem:
            raise TapRejected(f"吃候选组非法：{problem}，拒绝点击")
        tiles = tuple(sorted(int(v) for v in group))
        dump_dir = self._resolve_claim_session_dir(session_dir)
        if point is not None:
            self.validate_chi_panel_point(point)
        if not self._enabled:
            return self._plan_chi_panel_group(image, point, tiles)

        evidence = _TapEvidence(dump_dir, tile_label=PANEL_ACTION)
        try:
            self._require_game_foreground()
            before = self._screenshot()
            evidence.before = before
            panel = read_chi_panel(before)
            target = self._resolve_chi_panel_group(panel, tiles, point)
            box = tuple(int(v) for v in target.box[:4])
            if point is None:
                x, y = int(target.center[0]), int(target.center[1])
                self.validate_chi_panel_point((x, y))
            else:
                x, y = int(point[0]), int(point[1])
            record = {
                "action": PANEL_ACTION,
                "group": list(tiles),
                "tapped_box": list(box),
                "tapped_center": [x, y],
                "panel_before": panel.ledger(),
                "panel_after": None,
                "roi": {"x": [PANEL_ROI_X_MIN, PANEL_ROI_X_MAX],
                        "y": [PANEL_ROI_Y_MIN, PANEL_ROI_Y_MAX]},
            }
            evidence.claim = record
            evidence.tap = [x, y]
            evidence.box = list(box)
            self._require_game_foreground()
            self._tap(x, y)
            delay = (self.verify_delay if verify_delay is None
                     else max(0.0, float(verify_delay)))
            if delay > 0:
                time.sleep(delay)
            after = self._screenshot()
            evidence.after = after
            metrics = self._target_column_metrics(before, after, box)
            evidence.first_click_metrics = metrics
            panel_after = read_chi_panel(after)
            record["panel_after"] = panel_after.ledger()
            if not metrics["changed"]:
                raise TapUnverified(
                    f"吃候选组点击 ({x},{y}) 后组框区域画面未变化"
                    f"（diff_mean={metrics['diff_mean']}, "
                    f"changed_ratio={metrics['changed_ratio']}），未证明点击生效")
            if panel_after.present:
                raise TapUnverified(
                    "点组后候选面板仍在画面上（取消圆盘或面板行牌框未消失），"
                    "点击未证明生效，不重试")
        except Exception as exc:  # noqa: BLE001 — 原异常照抛，只是先落盘
            evidence.dump(type(exc).__name__, str(exc))
            raise
        outcome = {
            "executed": True, "dry_run": False, "stage": "clicked",
            "action": PANEL_ACTION, "group": list(tiles),
            "tap": [x, y], "box": list(box),
            "click_metrics": metrics,
            "ui_responded": True,
            "semantic_verified": False,
            "semantic_verifier": "action_policy.verify_after_click",
            "reason": (f"吃候选组 {list(tiles)} 已点击 ({x},{y})，组框区域"
                       "画面已变化、候选面板已消失（UI 响应确认）；语义级"
                       "验证须由 action_policy.verify_after_click 完成"),
        }
        evidence.dump("clicked", outcome["reason"])
        return outcome

    def _resolve_chi_panel_group(self, panel, tiles, point):
        """在 fresh 帧面板读数里定位要点的组：牌面必须恰等于 ``tiles``。

        面板读不完整、命中的组不是恰好一个、或给了坐标却不在该组实测
        并集框内，一律拒绝（零点击、不猜）。
        """
        if not isinstance(panel, ChiPanelState) or not panel.complete:
            raise TapRejected(
                "fresh 帧候选面板读不完整"
                f"（{getattr(panel, 'reason', '无读数')}），拒绝点组")
        matches = [g for g in panel.groups if tuple(g.tiles) == tuple(tiles)]
        if len(matches) != 1:
            raise TapRejected(
                f"fresh 帧面板上与所选组 {list(tiles)} 一致的候选组有 "
                f"{len(matches)} 个，拒绝点组（不猜）")
        target = matches[0]
        if point is not None:
            x, y = int(point[0]), int(point[1])
            x0, y0, x1, y1 = (int(v) for v in target.box[:4])
            if not (x0 + CLAIM_BOX_EDGE_MARGIN <= x <= x1 - CLAIM_BOX_EDGE_MARGIN
                    and y0 + CLAIM_BOX_EDGE_MARGIN <= y
                    <= y1 - CLAIM_BOX_EDGE_MARGIN):
                raise TapRejected(
                    f"点组坐标 ({x},{y}) 不在实测候选组框 "
                    f"{list(target.box)} 内（留 {CLAIM_BOX_EDGE_MARGIN}px "
                    "边距），拒绝点击")
        return target

    def _plan_chi_panel_group(self, image, point, tiles) -> dict:
        """``dry_run`` 分支：用调用方给的一帧复核面板组几何，零 adb。"""
        if image is None:
            raise TapAborted(
                "dry_run 的 execute_chi_panel_group 必须显式传 image"
                "（不截图也能复核候选组几何）")
        panel = read_chi_panel(image)
        target = self._resolve_chi_panel_group(panel, tiles, point)
        if point is None:
            x, y = int(target.center[0]), int(target.center[1])
            self.validate_chi_panel_point((x, y))
        else:
            x, y = int(point[0]), int(point[1])
        return {
            "executed": False, "dry_run": True, "stage": "plan",
            "action": PANEL_ACTION, "group": list(tiles),
            "tap": [x, y], "box": list(target.box),
            "reason": ("dry_run：只复核吃候选组几何，不调用 adb；"
                       "enabled=True 才会点击"),
        }

    def _resolve_claim_session_dir(
        self,
        session_dir: str | Path | None,
    ) -> Path:
        """行动行入口的落盘目录：**必须**是调用方显式给的。

        ``TapExecutor.__init__`` 的缺省目录指向真实语料库
        ``采集/assist_sessions/``，在那里留下的合成 tap_probe 曾经被复核
        误读成 9 次真实出牌成功。行动行是新通道，从一开始就不许有这个口子。
        """
        if session_dir is not None:
            return Path(session_dir)
        if self._session_dir_explicit:
            return Path(self.session_dir)
        raise TapAborted(
            "execute_claim 必须显式传 session_dir（或先赋值 "
            "executor.session_dir），绝不在缺省的 "
            f"{SESSION_ROOT} 下建假会话目录")

    def validate_claim_point(self, point: tuple[int, int]) -> tuple[int, int]:
        """行动行几何白名单：只放行 y∈[560,660]、x∈[740,1520] 的圆盘中心。

        这是充值（顶部居中）、游戏中心（右上）、开始游戏（中屏）等控件的硬
        兜底——它们没有一个落在这条带里。**与 :meth:`validate_target`
        （手牌 y≥720）互不相干**，本方法绝不调用它，也绝不放宽它。
        """
        x, y = int(point[0]), int(point[1])
        if not (CLAIM_ROI_X_MIN <= x <= CLAIM_ROI_X_MAX
                and CLAIM_ROI_Y_MIN <= y <= CLAIM_ROI_Y_MAX):
            raise TapRejected(
                f"行动行点击坐标 ({x},{y}) 不在行动行 ROI "
                f"x∈[{CLAIM_ROI_X_MIN},{CLAIM_ROI_X_MAX}]、"
                f"y∈[{CLAIM_ROI_Y_MIN},{CLAIM_ROI_Y_MAX}] 内——充值、开始游戏、"
                "游戏中心等控件都在这条带之外，拒绝点击")
        for box in CLAIM_FORBIDDEN_BOXES:
            x0, y0, x1, y1 = (int(value) for value in box)
            if x0 <= x <= x1 and y0 <= y <= y1:
                raise TapRejected(
                    f"行动行点击坐标 ({x},{y}) 落在实测禁区 {list(box)} 内，拒绝点击")
        return x, y

    def validate_selection_cancel_point(
        self, point: tuple[int, int]
    ) -> tuple[int, int]:
        """候选面板取消专用白名单；仍须命中 read_claims 的唯一取消圆盘。"""
        x, y = int(point[0]), int(point[1])
        if not (SELECTION_CANCEL_ROI_X_MIN <= x <= SELECTION_CANCEL_ROI_X_MAX
                and SELECTION_CANCEL_ROI_Y_MIN <= y <= SELECTION_CANCEL_ROI_Y_MAX):
            raise TapRejected(
                f"候选面板取消坐标 ({x},{y}) 不在专用 ROI "
                f"x∈[{SELECTION_CANCEL_ROI_X_MIN},{SELECTION_CANCEL_ROI_X_MAX}]、"
                f"y∈[{SELECTION_CANCEL_ROI_Y_MIN},{SELECTION_CANCEL_ROI_Y_MAX}] 内，拒绝点击")
        return x, y

    @staticmethod
    def _claim_point_in_box(point: tuple[int, int], box) -> bool:
        """坐标是否稳稳落在实测圆盘连通域框内（离边至少 2px）。"""
        x, y = int(point[0]), int(point[1])
        x0, y0, x1, y1 = (int(value) for value in box[:4])
        margin = CLAIM_BOX_EDGE_MARGIN
        return (x0 + margin <= x <= x1 - margin
                and y0 + margin <= y <= y1 - margin)

    def _resolve_claim_button(
        self,
        state: ClaimState,
        point: tuple[int, int] | None,
        expected_kind: str | None,
        *,
        allow_selection_cancel: bool = False,
    ) -> ClaimButton:
        """从 ``read_claims`` 的实测结果里挑出要点的那一个圆盘。

        三件事在这里定死：

        1. **取消盘永不点**（点了会不可逆地中断进行中的选择）；
        2. 期望种类对不上就没有候选；
        3. 没给坐标时**候选必须恰好一个**——多于一个说明分不清（例如同时
           有碰和杠两个金盘），此时拒绝而不是猜。

        Raises:
            TapRejected: 任何一项不成立。
        """
        if not isinstance(state, ClaimState):
            raise TapRejected("行动行读数不是 ClaimState，拒绝点击")
        buttons = [button for button in (state.buttons or ())
                   if isinstance(button, ClaimButton)]
        if not buttons:
            raise TapRejected(
                f"本帧 read_claims 没有实测到任何行动圆盘（{state.reason}），拒绝点击")
        if allow_selection_cancel:
            if len(buttons) != 1 or buttons[0].kind != KIND_CANCEL:
                raise TapRejected(
                    "候选面板取消只允许本帧唯一一个取消盘；"
                    f"实测到 {[button.kind for button in buttons]}，拒绝点击")
            eligible = list(buttons)
        else:
            eligible = [button for button in buttons
                        if button.kind not in CLAIM_KIND_BLACKLIST]
        if not eligible:
            raise TapRejected(
                "本帧只实测到取消盘（点它会不可逆地中断进行中的选择），拒绝点击")
        if expected_kind is not None:
            matched = [button for button in eligible
                       if button.kind == expected_kind]
            if not matched:
                raise TapRejected(
                    f"本帧没有 kind={expected_kind!r} 的行动圆盘"
                    f"（实测到 {[b.kind for b in eligible]}），拒绝点击")
            eligible = matched
        if point is None:
            if len(eligible) != 1:
                raise TapRejected(
                    f"未给出坐标，而本帧实测出 {len(eligible)} 个候选圆盘，"
                    "无法唯一定位，拒绝点击（不猜）")
            return eligible[0]
        for button in eligible:
            if self._claim_point_in_box(point, button.box):
                return button
        raise TapRejected(
            f"坐标 ({point[0]},{point[1]}) 不在 read_claims 实测的圆盘框内"
            f"（候选框 {[list(b.box) for b in eligible]}）；"
            "槽位推断值一律不接受，拒绝点击")

    @staticmethod
    def _action_ui_evidence(image) -> dict | None:
        """本帧按钮区/玩法门禁的一手读数（只写盘，不参与任何判定）。

        复用 ``turn_gate.action_ui_evidence``（只读、零副作用，判据与
        ``claim_vision.read_claims`` 同源），避免在本模块再解析一遍按钮区
        而与决策层/门禁层打架。**证据路径上的任何异常都不许影响点击链路**：
        取不到就记 ``None``。
        """
        try:
            from turn_gate import action_ui_evidence

            return action_ui_evidence(image)
        except Exception:  # noqa: BLE001 — 拿不到证据不等于可以放行或拒绝
            return None

    @staticmethod
    def _claim_buttons_record(state: ClaimState) -> list[dict]:
        """把一帧的实测圆盘原样转成 JSON（只写盘，不参与判定）。"""
        if not isinstance(state, ClaimState):
            return []
        return [{"kind": button.kind, "box": list(button.box),
                 "center": list(button.center)}
                for button in (state.buttons or ())]

    def _claim_record(
        self,
        label: str,
        expected_kind: str | None,
        before_state: ClaimState,
        button: ClaimButton,
        after_state: ClaimState | None,
    ) -> dict:
        """行动行点击的证据记录（只写盘，不参与判定）。"""
        return {
            "action": label,
            "expected_kind": expected_kind,
            "tapped_kind": button.kind,
            "tapped_box": list(button.box),
            "tapped_center": list(button.center),
            "claim_reason_before": getattr(before_state, "reason", ""),
            "buttons_before": self._claim_buttons_record(before_state),
            "buttons_after": (None if after_state is None
                              else self._claim_buttons_record(after_state)),
            "roi": {"x": [CLAIM_ROI_X_MIN, CLAIM_ROI_X_MAX],
                    "y": [CLAIM_ROI_Y_MIN, CLAIM_ROI_Y_MAX]},
            "kind_blacklist": sorted(CLAIM_KIND_BLACKLIST),
            "action_blacklist": sorted(CLAIM_ACTION_BLACKLIST),
        }

    def _plan_claim(
        self,
        image: np.ndarray | None,
        point: tuple[int, int] | None,
        label: str,
        expected_kind: str | None,
        *,
        allow_selection_cancel: bool = False,
    ) -> dict:
        """``dry_run`` 分支：用调用方给的一帧复核圆盘几何，零 adb、零落盘。"""
        if image is None:
            raise TapAborted(
                "dry_run 的 execute_claim 必须显式传 image（不截图也能复核圆盘几何）")
        state = read_claims(image)
        button = self._resolve_claim_button(
            state, point, expected_kind,
            allow_selection_cancel=allow_selection_cancel)
        if point is None:
            x, y = int(button.center[0]), int(button.center[1])
            if allow_selection_cancel:
                self.validate_selection_cancel_point((x, y))
            else:
                self.validate_claim_point((x, y))
        else:
            x, y = int(point[0]), int(point[1])
        return {
            "executed": False, "dry_run": True, "stage": "plan",
            "action": label, "kind": button.kind,
            "tap": [x, y], "box": list(button.box),
            "reason": ("dry_run：只复核行动圆盘几何，不调用 adb；"
                       "enabled=True 才会点击"),
        }

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------
    def execute(
        self,
        analysis: dict,
        *,
        fresh_validator: Callable[[np.ndarray, dict], dict] | None = None,
    ) -> dict:
        """Run one safety-gated tap cycle and return a machine-readable outcome.

        dry_run（enabled=False）：只返回坐标，零 adb 调用。
        enabled=True：重截屏 → 重新定位建议牌 → 白名单校验 → 第一次点选
        → 确认牌已抬起 → 第二次确认出牌 → 验证牌真正离手；任一步不满足
        即抛 TapAborted 子类，不再继续点击。
        """
        tap_plan = self.plan(analysis)
        if not self._enabled:
            return {
                "executed": False, "dry_run": True, "stage": "plan",
                "tile": tap_plan.label, "tile_id": tap_plan.tile_id,
                "tap": [tap_plan.x, tap_plan.y], "box": list(tap_plan.box),
                "reason": "dry_run：只计算坐标，不调用 adb；enabled=True 才会点击",
            }
        # 证据收集器：只记不判。dry_run 分支在上面已经 return，所以只读
        # 路径永远不会落盘。
        evidence = _TapEvidence(self.session_dir, tile_label=tap_plan.label,
                                tile_id=tap_plan.tile_id)
        try:
            self._require_game_foreground()
            fresh = self._screenshot()
            fresh, fresh_analysis = self._validate_fresh_with_retry(
                fresh, analysis, evidence, fresh_validator=fresh_validator)
            located = self._locate_suggested_tile(
                fresh, tap_plan.tile_id, fresh_analysis["suggested_boxes"][0],
                caishen_label=fresh_analysis.get("caishen"),
                require_complete=not bool(fresh_analysis.get("_locked_tile")),
            )
            locked_before = fresh_analysis.get("_locked_before_count")
            if locked_before is not None:
                located["before_count"] = int(locked_before)
            x, y = located["x"], located["y"]
            self.validate_target((x, y), located["concealed_boxes"], fresh.shape)
            before = located["before_count"]
            evidence.before_count = before
            evidence.tap = [x, y]
            evidence.box = list(located["box"])
            self._require_game_foreground()
            self._tap(x, y)
            if self.select_delay > 0:
                time.sleep(self.select_delay)
            selected_image = self._screenshot()
            evidence.selected = selected_image
            first_metrics = self._target_column_metrics(
                fresh, selected_image, located["box"])
            evidence.first_click_metrics = first_metrics
            if not first_metrics["changed"]:
                raise TapUnverified("第一次点击后目标牌没有抬起，停止确认出牌")
            # 待二击状态落盘：进程若在两击之间重启，新进程靠它 + 唯一
            # 抬起牌定位恢复第二击（panel_recovery）。落盘失败不挡第二击。
            if self.pending_context is not None:
                self._persist_pending_second_tap(
                    tap_plan, (x, y), located["box"], before)
            selected_guard = detect_action_ui(selected_image)
            evidence.action_guard_afterselect = _action_guard_record(selected_guard)
            if selected_guard.blocks_discard:
                raise TapUnverified(
                    f"第一次点击后出现{selected_guard.reason}，停止确认出牌")
            # 审计 B（2026-10-27 Codex 只读审计）：两次点击之间必须复核手牌
            # 状态——只看"目标列画面变了"会假通过，而且第一击已出牌时绝不能
            # 再点第二下（会点到空牌位或别的控件）。这里多跑一次 count_tiles、
            # 拉长两击间隔是**有意**的安全代价，不再沿用"诊断推迟到点击链
            # 结束"的旧约定（_fill_selected_diagnostics 只剩失败路径兜底）。
            # 落定修正（2026-10-25 实机三次误杀）：第一击已出牌时，
            # selected 帧（第一击后 select_delay）正卡在出牌动画/手牌
            # 重排中，单帧计数少数 2 张被误判成“状态异常”。先进
            # _await_settled_hand 等计数落定再过闸；未落定一律
            # fail-closed，绝不发第二击。
            settled_image, count_samples, settled = self._await_settled_hand(
                selected_image, before)
            if not settled:
                raise TapUnverified(
                    f"第一次点击后手牌计数在 {self.settle_deadline:.1f}s 内"
                    f"未落定（样本 {count_samples}），状态未知，禁止第二击")
            gate = self._selected_hand_gate(
                settled_image, located["box"], before,
                concealed_boxes=located["concealed_boxes"])
            gate["count_samples"] = count_samples
            evidence.selected_gate = gate
            evidence.hand_boxes_selected = gate["boxes"]
            evidence.raised_candidates = self._gate_raised_record(gate)
            if gate["discarded"]:
                outcome = {
                    "executed": True, "dry_run": False,
                    "stage": "discarded_first_click",
                    "tile": tap_plan.label, "tile_id": tap_plan.tile_id,
                    "tap": [x, y], "box": list(located["box"]),
                    "before_count": before, "after_count": gate["count"],
                    "reason": gate["reason"],
                }
                evidence.after_count = gate["count"]
                evidence.dump("discarded_first_click", gate["reason"])
                if self.pending_context is not None:
                    self._clear_pending_second_tap()
                return outcome
            if not gate["ready"]:
                raise TapUnverified(gate["reason"])

            # 实机已验证：第一次只选中并抬牌，第二次点同一列才真正出牌。
            self._require_game_foreground()
            self._tap(x, y)
            if self.verify_delay > 0:
                time.sleep(self.verify_delay)
            after_image = self._screenshot()
            evidence.after = after_image
            after_vision = self._make_vision()
            _raw_after_count, raw_after_boxes = after_vision.count_tiles(after_image)
            after_boxes = self._boxes_in_concealed_columns(
                raw_after_boxes, located["concealed_boxes"])
            after_count = len(after_boxes)
            evidence.after_count = after_count
            evidence.hand_boxes_after = [
                [int(value) for value in box[:4]] for box in after_boxes or []]
            if after_count != before - 1:
                raise TapUnverified(
                    f"确认出牌后手牌数量 {after_count} ≠ 点击前 {before} - 1；"
                    "验证失败，不重试")
            second_metrics = self._target_column_metrics(
                selected_image, after_image, located["box"])
            evidence.second_click_metrics = second_metrics
            if not second_metrics["changed"]:
                raise TapUnverified(
                    "第二次点击后抬起牌仍未消失，未证明真正出牌")
        except Exception as exc:  # noqa: BLE001 — 原异常照抛，只是先落盘
            # 失败路径：先落盘再抛。落盘内部已吞掉自己的异常，绝不掩盖原始
            # 异常，也绝不把“落盘失败”变成放行条件。
            self._fill_selected_diagnostics(evidence)
            evidence.dump(type(exc).__name__, str(exc))
            raise
        outcome = {
            "executed": True, "dry_run": False, "stage": "verified",
            "tile": tap_plan.label, "tile_id": tap_plan.tile_id,
            "tap": [x, y], "box": list(located["box"]),
            "before_count": before, "after_count": after_count,
            "reason": "两次点击完成，抬起牌已消失且手牌恰好少一张",
        }
        self._fill_selected_diagnostics(evidence)
        evidence.dump("verified", outcome["reason"])
        if self.pending_context is not None:
            self._clear_pending_second_tap()
        return outcome

    # ------------------------------------------------------------------
    # 待二击状态（panel_recovery）：落盘 / 清除 / 重启后补发第二击
    # ------------------------------------------------------------------
    def _pending_state_file(self) -> Path:
        """待二击状态文件位置：显式覆盖优先，缺省按 serial 固定路径。"""
        if self.pending_state_path is not None:
            return Path(self.pending_state_path)
        return panel_recovery.state_path_for(self.serial)

    def _persist_pending_second_tap(self, tap_plan, tap_point, box,
                                    before_count: int) -> None:
        """第一击已确认改变目标列：把待二击状态落盘（失败静默）。

        局身份/点击前手牌来自调用方预先写好的 ``pending_context``；
        落盘只是给进程重启留恢复入口，失败绝不挡本次第二击。
        """
        context = self.pending_context or {}
        state = panel_recovery.build_state(
            serial=self.serial,
            tile_id=tap_plan.tile_id,
            tile_label=tap_plan.label,
            tap=tap_point,
            box=box,
            before_count=before_count,
            hand_ids=context.get("hand_ids"),
            hud_key=context.get("hud_key"),
            caishen=context.get("caishen"),
            caishen_id=context.get("caishen_id"),
            round_seq=context.get("round_seq"),
        )
        panel_recovery.write_state(self._pending_state_file(), state)

    def _clear_pending_second_tap(self) -> None:
        """出牌已验证 / 第一击即离手：待二击状态作废（失败静默）。"""
        panel_recovery.clear_state(self._pending_state_file())

    def execute_recovery_second_tap(
        self,
        state: dict,
        fresh_locator: Callable[[np.ndarray], dict],
    ) -> dict:
        """进程重启后补发「待二击」：只点 fresh 帧重新定位的当前中心。

        ``fresh_locator(fresh_image)`` 由调用方（循环）提供：它必须在
        fresh 帧上重新确认胡提示面板单信号 + 唯一抬起牌定位
        （panel_recovery.decide_recovery），任一条件不满足就抛
        TapAborted（点击前中止，本方法一击不发）；返回与 execute()
        同形的合成 advise 结果（``suggested_boxes[0]`` = 重新定位的
        当前牌框、``_locked_before_count`` = 点击前暗手张数、
        ``_recovery_concealed_boxes`` = fresh 帧暗手框白名单）。
        本方法只负责前台确认、几何白名单、点击与点后 −1 验证，
        判定口径不在这里放松。
        """
        tile_id = int(state["tile_id"])
        label = str(state.get("tile_label") or label_for_tile(tile_id))
        if not self._enabled:
            return {
                "executed": False, "dry_run": True,
                "stage": "recovery_plan",
                "tile": label, "tile_id": tile_id,
                "reason": "dry_run：只判定不点击；enabled=True 才会补发第二击",
            }
        evidence = _TapEvidence(self.session_dir, tile_label=label,
                                tile_id=tile_id)
        try:
            self._require_game_foreground()
            fresh = self._screenshot()
            evidence.before = fresh
            located = fresh_locator(fresh)
            box = tuple(int(value) for value
                        in located["suggested_boxes"][0][:4])
            x, y = box_center(box)
            concealed = located.get("_recovery_concealed_boxes") or [list(box)]
            self.validate_target((x, y), concealed, fresh.shape)
            before = int(located.get("_locked_before_count")
                         or state["before_count"])
            evidence.tap = [x, y]
            evidence.box = list(box)
            evidence.before_count = before
            self._require_game_foreground()
            self._tap(x, y)
            if self.verify_delay > 0:
                time.sleep(self.verify_delay)
            after_image = self._screenshot()
            evidence.after = after_image
            after_vision = self._make_vision()
            _raw_after_count, raw_after_boxes = after_vision.count_tiles(
                after_image)
            after_boxes = self._boxes_in_concealed_columns(
                raw_after_boxes, concealed)
            after_count = len(after_boxes)
            evidence.after_count = after_count
            evidence.hand_boxes_after = [
                [int(value) for value in item[:4]]
                for item in after_boxes or []]
            if after_count != before - 1:
                raise TapUnverified(
                    f"恢复第二击后手牌数量 {after_count} ≠ 点击前 {before} - 1；"
                    "验证失败，不重试")
            second_metrics = self._target_column_metrics(
                fresh, after_image, box)
            evidence.second_click_metrics = second_metrics
            if not second_metrics["changed"]:
                raise TapUnverified(
                    "恢复第二击后目标列未变化，未证明真正出牌")
        except Exception as exc:  # noqa: BLE001 — 原异常照抛，只是先落盘
            self._fill_selected_diagnostics(evidence)
            evidence.dump(type(exc).__name__, str(exc))
            raise
        self._clear_pending_second_tap()
        outcome = {
            "executed": True, "dry_run": False,
            "stage": "recovery_second_tap",
            "tile": label, "tile_id": tile_id,
            "tap": [x, y], "box": list(box),
            "before_count": before, "after_count": after_count,
            "reason": "重启恢复第二击完成，抬起牌已消失且手牌恰好少一张",
        }
        self._fill_selected_diagnostics(evidence)
        evidence.dump("recovery_verified", outcome["reason"])
        return outcome

    def _validate_fresh_with_retry(
        self,
        first_image: np.ndarray,
        original: dict,
        evidence: "_TapEvidence",
        *,
        fresh_validator: Callable[[np.ndarray, dict], dict] | None = None,
    ) -> tuple[np.ndarray, dict]:
        """点击前最多重拍三帧；只有同一建议稳定通过才继续。

        失败发生在任何 ``input tap`` 之前。行动圆盘/未知弹窗属于硬阻断，
        不重试；仅识牌、编码、建议一致性这类瞬时失败进入有界重拍。
        """
        image = first_image
        last: Exception | None = None
        for attempt in range(1, self.fresh_retry_attempts + 1):
            evidence.before = image
            guard = detect_action_ui(image)
            evidence.action_guard_before = _action_guard_record(guard)
            if guard.blocks_discard:
                raise TapAborted(
                    f"点击前{guard.reason}；框={list(guard.boxes)}")
            try:
                current = (fresh_validator(image, original)
                           if fresh_validator is not None
                           else self._reanalyze_fresh(image, original))
                if not isinstance(current, dict):
                    raise TapAborted("点击前复核未返回结构化结果")
                evidence.preclick_attempts.append({
                    "attempt": attempt, "ok": True,
                    "status": current.get("status"),
                    "suggestion_id": current.get("suggestion_id"),
                })
                return image, current
            except TapAborted as exc:
                last = exc
                evidence.preclick_attempts.append({
                    "attempt": attempt, "ok": False,
                    "error": str(exc)[:500],
                })
                if attempt >= self.fresh_retry_attempts:
                    break
                if self.fresh_retry_interval:
                    time.sleep(self.fresh_retry_interval)
                self._require_game_foreground()
                image = self._screenshot()
        raise TapPrecheckRetryExhausted(
            f"点击前连续 {self.fresh_retry_attempts} 帧复核失败，未发送点击：{last}")

    def _fill_selected_diagnostics(self, evidence: "_TapEvidence") -> None:
        """把 selected 帧的抬牌诊断补进证据（只剩失败路径兜底）。

        审计 B 之后，``execute()`` 在两次点击**之间**就用
        :meth:`_selected_hand_gate` 跑过 ``count_tiles`` 并把结果写进了
        ``evidence.raised_candidates``；这里只在**还没走到那道闸就失败**
        的路径上补一次。它**只读**，不参与任何判定。
        """
        if evidence.selected is None or evidence.raised_candidates is not None:
            return
        evidence.raised_candidates = self.raised_candidates(evidence.selected)
        evidence.hand_boxes_selected = [
            item["box"] for item in
            (evidence.raised_candidates.get("boxes") or [])]

    def _await_settled_hand(
        self,
        first_image: np.ndarray,
        before_count: int,
    ) -> tuple[np.ndarray, list, bool]:
        """第一击后等手牌计数落定，返回 ``(落定帧, 计数样本, 是否落定)``。

        实机三次误杀（2026-10-25）证明：第一击真正出牌时，出牌动画与手牌
        重排未落定的帧上 ``count_tiles`` 会少数 2 张（11→9 / 14→12 /
        17→15），拿单帧计数直接判「状态异常」是把动画当成了事故。所以进
        :meth:`_selected_hand_gate` 之前先等计数落定：

        * 首帧计数已是决定性读数（``== before_count`` 或
          ``== before_count - 1``）时直接采用首帧、**不多截屏**——与审计 B
          的原时序完全一致，落定轮询只在首帧含糊时才启动；
        * 首帧含糊（其它任何值，或数不出来）→ 每隔
          :attr:`settle_interval` 重截一帧计数，直到**连续两次计数一致**
          （落定）或 :attr:`settle_deadline` 到期；
        * 到期仍未落定 → ``settled=False``，调用方 fail-closed 中止，
          绝不拿未落定的帧去判第二击。

        样本序列（含首帧）原样返回，随闸门结果写进证据供复核。
        """
        vision = self._make_vision()
        samples: list[int | None] = []

        def _count(image) -> int | None:
            try:
                count, _boxes = vision.count_tiles(image)
                return int(count)
            except Exception:  # noqa: BLE001 — 数不出来记 None，不外抛
                return None

        first = _count(first_image)
        samples.append(first)
        if first is not None and first in (before_count, before_count - 1):
            return first_image, samples, True
        image = first_image
        deadline = time.monotonic() + self.settle_deadline
        while time.monotonic() < deadline:
            if self.settle_interval > 0:
                time.sleep(self.settle_interval)
            image = self._screenshot()
            current = _count(image)
            samples.append(current)
            if current is not None and current == samples[-2]:
                return image, samples, True
        return image, samples, False

    def _selected_hand_gate(
        self,
        image: np.ndarray,
        reference_box,
        before_count: int,
        *,
        concealed_boxes=None,
    ) -> dict:
        """第一次点击后的手牌复核（审计 B）：不过这道闸就不发第二击。

        判据（顺序即语义，fail-closed）：

        1. **已少一张** → 通常牌已经离手，``discarded=True``：禁止第二击。
           唯一例外是本帧严格只有 ``central_panel``（胡提示面板）单信号：
           实机上被抬起的已选牌会移出常规手牌 ROI，导致计数假少一张；此时
           ``ready=True``，第二击仍点第一次已经过白名单验证的同一坐标；
        2. **张数既不少一也不守恒** → 状态异常，禁止第二击；
        3. 张数守恒 → 目标牌列上必须还有牌（最近框与建议牌框的 x 重叠
           ≥ 牌宽一半），且它的 ``y1`` 比同帧其它暗手框的中位数低
           ≥ :data:`RAISED_BOX_LIFT_PX`（几何抬起），才允许第二击确认。

        Returns:
            ``{"discarded", "ready", "reason", "count", "boxes",
            "target_box", "median_y1", "lifted", "hint_panel_selection"}``；任何一步拿不到数都
            ``ready=False`` 并写明原因。
        """
        result: dict = {"discarded": False, "ready": False, "reason": "",
                        "count": None, "boxes": [], "target_box": None,
                        "median_y1": None, "lifted": False,
                        "hint_panel_selection": False}
        try:
            vision = self._make_vision()
            raw_count, raw_boxes = vision.count_tiles(image)
        except Exception as exc:  # noqa: BLE001 — 拿不到数就禁止第二击
            result["reason"] = f"第一次点击后重新数牌失败: {exc}；禁止第二击"
            return result
        boxes = self._boxes_in_concealed_columns(raw_boxes, concealed_boxes)
        count = len(boxes) if concealed_boxes else raw_count
        result["raw_count"] = raw_count
        result["raw_boxes"] = [[int(value) for value in box[:4]]
                               for box in raw_boxes or []]
        result["count"] = count
        result["boxes"] = [[int(value) for value in box[:4]]
                           for box in boxes or []]
        if count == before_count - 1:
            try:
                settled, panel_evidence = detect_settled_screen(image)
                signals = panel_evidence.get("settled_signals") or []
            except Exception:  # noqa: BLE001 - 检测失败就走原来的禁止第二击
                settled, signals = False, []
            if not settled and signals == ["central_panel"]:
                result["ready"] = True
                result["hint_panel_selection"] = True
                result["reason"] = (
                    "第一次点击后手牌计数少一张，但严格命中胡提示面板"
                    " central_panel 单信号：已选抬起牌移出常规手牌 ROI，"
                    "允许在原白名单坐标发送第二击确认"
                )
                return result
            result["discarded"] = True
            result["reason"] = ("第一次点击后手牌已少一张"
                                f"（{before_count} → {count}），判定牌已离手；"
                                "禁止第二击（再点同一位置可能点到空牌位/别的控件）")
            return result
        if count != before_count:
            result["reason"] = (f"第一次点击后手牌数量 {count} 既不等于点击前 "
                                f"{before_count} 也不等于少一张；状态异常，禁止第二击")
            return result
        if not result["boxes"]:
            result["reason"] = "第一次点击后没有数到任何手牌框，禁止第二击"
            return result
        ref_x0, _ref_y0, ref_x1, _ref_y1 = (int(v) for v in reference_box[:4])
        ref_cx = (ref_x0 + ref_x1) // 2
        target = min(result["boxes"],
                     key=lambda b: abs((b[0] + b[2]) // 2 - ref_cx))
        result["target_box"] = list(target)
        overlap = min(target[2], ref_x1) - max(target[0], ref_x0)
        if overlap * 2 < (ref_x1 - ref_x0):
            result["reason"] = (f"第一次点击后目标牌列上的框 {target} 与建议牌框 "
                                f"x 重叠只有 {overlap}px（不足牌宽一半），"
                                "目标牌不在原位，禁止第二击")
            return result
        median_y1 = float(np.median([b[3] for b in result["boxes"]]))
        result["median_y1"] = median_y1
        result["lifted"] = target[3] <= median_y1 - RAISED_BOX_LIFT_PX
        if not result["lifted"]:
            result["reason"] = (f"第一次点击后目标牌 y1={target[3]} 未比同帧中位数 "
                                f"{median_y1:.1f} 抬高 {RAISED_BOX_LIFT_PX}px，"
                                "牌没有抬起，禁止第二击")
            return result
        result["ready"] = True
        result["reason"] = ("第一次点击后张数守恒、目标牌仍在原列且几何抬起"
                            f"（y1={target[3]} ≤ 中位数 {median_y1:.1f} - "
                            f"{RAISED_BOX_LIFT_PX}），允许第二击确认")
        return result

    @staticmethod
    def _boxes_in_concealed_columns(boxes, concealed_boxes) -> list[list[int]]:
        """只保留点击前已确认的暗手列，排除左侧既有碰／吃／杠。

        ``count_tiles`` 在中央胡提示面板出现时可能暂时把左侧副露框并入结果，
        同时漏掉被抬出常规 ROI 的目标牌。首次点击不会改变暗手各列的 x 位置，
        所以用 fresh 帧的暗手列作白名单：当前框须与至少一个原暗手框在 x 轴
        重叠达到原框宽度的一半。拿不到 fresh 暗手框时保留旧行为，由上层继续
        fail-closed。
        """
        normalized = [[int(value) for value in box[:4]] for box in boxes or []]
        baseline = [[int(value) for value in box[:4]]
                    for box in concealed_boxes or []]
        if not baseline:
            return normalized
        kept: list[list[int]] = []
        for box in normalized:
            for ref in baseline:
                overlap = min(box[2], ref[2]) - max(box[0], ref[0])
                ref_width = max(1, ref[2] - ref[0])
                if overlap * 2 >= ref_width:
                    kept.append(box)
                    break
        return kept

    @staticmethod
    def _gate_raised_record(gate: dict) -> dict:
        """把 :meth:`_selected_hand_gate` 的结果整理成与 ``raised_candidates()`` 同形的证据。"""
        median_y1 = gate.get("median_y1")
        items = []
        for box in gate.get("boxes") or []:
            items.append({
                "box": list(box), "y0": box[1], "y1": box[3],
                "raised": bool(median_y1 is not None
                               and box[3] <= median_y1 - RAISED_BOX_LIFT_PX),
            })
        return {"boxes": items,
                "raised": [item for item in items if item["raised"]],
                "median_y1": median_y1, "lift_px": RAISED_BOX_LIFT_PX,
                "selected_gate": gate.get("reason", "")}

    @staticmethod
    def _target_column_metrics(
        before: np.ndarray,
        after: np.ndarray,
        box,
    ) -> dict:
        """量出目标牌列变化的实测值（纯仪表化，判定语义与改动前完全一致）。

        只看目标列及其上方 90px，避免桌面计时器、他家动作等无关动画
        让验证误通过。阈值来自 2026-10-24 实机“抬牌→真正打出”帧：
        该区域均值差 9.62、显著变化像素 6.68%。这里取更保守的下限
        （均值差 2.5、显著变化像素比例 0.015），**两个阈值与扩边
        （x -12/+12、y -90/+8）都是冻结值，仪表化时不许顺手改**。

        Args:
            before: 点击前的帧。
            after: 点击后的帧。
            box: 目标牌像素框 xyxy。

        Returns:
            ``{"diff_mean", "changed_ratio", "mean_ok", "ratio_ok",
            "region", "changed"}``；帧尺寸不一致或区域为空时，实测值记
            ``None``、各 ok 记 ``False``、``changed`` 记 ``False``，
            ``region`` 仍尽力给出（便于裁图核对）。
        """
        metrics: dict = {"diff_mean": None, "changed_ratio": None,
                         "mean_ok": False, "ratio_ok": False,
                         "region": None, "changed": False}
        if before.shape != after.shape:
            return metrics
        height, width = before.shape[:2]
        x0, y0, x1, y1 = (int(value) for value in box[:4])
        x0 = max(0, x0 - 12)
        x1 = min(width, x1 + 12)
        y0 = max(0, y0 - 90)
        y1 = min(height, y1 + 8)
        metrics["region"] = [x0, y0, x1, y1]
        if x1 <= x0 or y1 <= y0:
            return metrics
        diff = cv2.absdiff(before[y0:y1, x0:x1], after[y0:y1, x0:x1])
        diff_mean = float(diff.mean())
        changed_ratio = float((diff > 15).mean())
        metrics["diff_mean"] = round(diff_mean, 4)
        metrics["changed_ratio"] = round(changed_ratio, 6)
        metrics["mean_ok"] = diff_mean >= 2.5
        metrics["ratio_ok"] = changed_ratio >= 0.015
        metrics["changed"] = metrics["mean_ok"] and metrics["ratio_ok"]
        return metrics

    @staticmethod
    def _target_column_changed(
        before: np.ndarray,
        after: np.ndarray,
        box,
    ) -> bool:
        """检测目标牌列是否发生了足够大的视觉变化（同名同签名，语义不变）。"""
        return TapExecutor._target_column_metrics(before, after, box)["changed"]

    # ------------------------------------------------------------------
    # 识别与定位（只调用识别链路，不修改它）
    # ------------------------------------------------------------------
    def _make_vision(self) -> TileVision:
        """Build a TileVision on the verified tea template directory."""
        return TileVision(template_dir=self.template_dir)

    def _reanalyze_fresh(self, image: np.ndarray, original: dict) -> dict:
        """Require the same complete hand, caishen, and advice on a fresh frame."""
        from assist_tea import analyze_snapshot

        mode = original.get("mode")
        if mode not in ("tea_2p", "tea_4p"):
            raise TapAborted(f"代点玩法未确认: {mode!r}")
        old_hand = original.get("hand_ids")
        if not isinstance(old_hand, list) or not old_hand:
            raise TapAborted("原建议缺少完整手牌证据，停止点击")
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise TapAborted("新截图编码失败，停止点击")
        with tempfile.TemporaryDirectory(prefix="tea_tap_check_") as dirname:
            frame = Path(dirname) / "fresh.png"
            frame.write_bytes(encoded.tobytes())
            try:
                current = analyze_snapshot(
                    frame,
                    caishen_label=(original.get("caishen") or "")
                    if original.get("caishen_source") == "manual" else "",
                    confirmed_two_player=mode == "tea_2p",
                    confirmed_four_player=mode == "tea_4p",
                    require_visible_caishen=original.get("caishen_source") != "manual",
                    log_dir=dirname,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                raise TapAborted(f"新截图完整复核失败: {exc}") from exc
        if current.get("status") != "advise":
            raise TapAborted(f"新截图无可执行建议: {current.get('reason')}")
        if (current.get("mode") != mode
                or current.get("caishen") != original.get("caishen")
                or current.get("suggestion_id") != original.get("suggestion_id")
                or sorted(current.get("hand_ids") or []) != sorted(old_hand)
                or not current.get("suggested_boxes")):
            raise TapAborted("新截图牌面、财神或建议已变化，停止点击")
        return current

    def _locate_suggested_tile(
        self,
        image: np.ndarray,
        tile_id: int,
        reference_box,
        *,
        caishen_label: str | None,
        require_complete: bool = True,
    ) -> dict:
        """Re-identify the suggested tile on a FRESH frame by face, not pixels.

        Returns the chosen box, its center, all concealed hand boxes and the
        pre-click concealed count (used by the post-tap verification).
        """
        vision = self._make_vision()
        label = caishen_label or None
        if label is not None and TILE_IDS.get(label) is None:
            raise TapAborted(f"本局财神标签无效: {caishen_label!r}，无法复核建议牌")
        vision.caishen_label = label
        try:
            vision.recognize_hand(image, caishen_label=label)
        except (ValueError, RuntimeError) as exc:
            raise TapAborted(f"新截图识别失败，中止点击: {exc}") from exc
        if require_complete and not vision.last_complete:
            raise TapAborted("新截图识牌不完整，中止点击")
        concealed = [item for item in vision.diagnostics
                     if item.get("region") == "concealed"]
        candidates = [item for item in concealed
                      if item.get("accepted") and item.get("tile_id") == tile_id
                      and item.get("box")]
        if not candidates:
            raise TargetMissing(
                f"建议牌 {label_for_tile(tile_id)} 在新截图中未定位到"
                "（按牌面匹配），中止点击")
        reference = box_center(reference_box)
        chosen = min(
            candidates,
            key=lambda item: sum(abs(a - b) for a, b
                                 in zip(box_center(item["box"]), reference)),
        )
        x, y = box_center(chosen["box"])
        return {
            "box": tuple(int(value) for value in chosen["box"]),
            "x": x, "y": y,
            "concealed_boxes": [item["box"] for item in concealed],
            "before_count": len(concealed),
        }

    # ------------------------------------------------------------------
    # ADB 通道（fail closed：任何异常都转成 TapAborted，绝不重试）
    # ------------------------------------------------------------------
    def _adb(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        command = [self.adb_path, "-s", self.serial, *args]
        return subprocess.run(
            command, capture_output=True, check=True,
            timeout=timeout if timeout is not None else self.screenshot_timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _screenshot(self) -> np.ndarray:
        try:
            result = self._adb("exec-out", "screencap", "-p")
        except subprocess.TimeoutExpired as exc:
            raise TapAborted(f"ADB 截图超时: {exc}") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise TapAborted(f"ADB 截图失败: {exc}") from exc
        image = cv2.imdecode(
            np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise TapAborted("模拟器截图不是有效 PNG")
        if image.shape[:2] != FRAME_SIZE:
            raise TapAborted(
                f"画面尺寸 {image.shape[1]}x{image.shape[0]} 不是已验证的 "
                f"{FRAME_SIZE[1]}x{FRAME_SIZE[0]}，拒绝点击")
        return image

    def _require_game_foreground(self) -> None:
        """Fail closed before any game input: only the game package may proceed."""
        for args in (("shell", "dumpsys", "activity", "activities"),
                     ("shell", "dumpsys", "window", "windows")):
            try:
                result = self._adb(*args, timeout=15)
            except (OSError, subprocess.SubprocessError):
                continue
            dump = result.stdout.decode(errors="replace")
            package = None
            for pattern in FOREGROUND_PATTERNS:
                match = re.search(pattern, dump)
                if match:
                    package = match.group(1)
                    break
            if package is None:
                continue
            if package != EXPECTED_PACKAGE:
                raise TapAborted(f"前景应用不是游戏: {package}，拒绝点击")
            return
        raise TapAborted("无法确认游戏在模拟器前台，拒绝点击")

    def _tap(self, x: int, y: int) -> None:
        try:
            self._adb("shell", "input", "tap", str(int(x)), str(int(y)),
                      timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise TapAborted(f"ADB 点击超时: {exc}") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise TapAborted(f"ADB 点击失败: {exc}") from exc
