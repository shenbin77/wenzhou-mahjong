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
import os
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
    ACTION_HU,
    ACTION_KONG,
    ACTION_PASS,
    ACTION_PENG,
    ActionPolicyConfig,
    BoardState,
    HandState,
    choose_chi_panel_option,
    decide_action,
    resolve_incoming,
    recommended_runner_config,
    validate_runner_config,
    verify_after_click,
)
from claim_vision import KIND_CANCEL, read_claims
from chi_panel_vision import read_chi_panel
from hand_ledger import HandLedger
from invite_dialog import read_invitation
from lobby_autostart import plan_start, read_lobby_room
from match_end import read_match_end
from match_score import read_match_score
from promo_popup import read_promo_popup
from timeout_dissolve import read_timeout_dissolve
from harvest_tea import DEFAULT_SERIAL, EXPECTED_PACKAGE
from round_transition import RoundTransitionExecutor
from single_bright_cut import (
    BRIGHT_SUBSET_GATE,
    BrightSubsetCutTracker,
    SingleBrightCutTracker,
    detect_bright_subset_cut,
    detect_single_bright_cut,
)
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
# adb 路径统一：优先用 adb_runtime.resolve_adb_path()（唯一真源），取不到就
# 原样回退到上面的元组逻辑。该模块可能尚未就绪/导入失败，一律 except 兜底。
try:                                                    # pragma: no cover
    from adb_runtime import resolve_adb_path

    _RESOLVED_ADB = str(resolve_adb_path() or "")
    _ADB_SOURCE_READY = True
except Exception:                                       # noqa: BLE001
    _RESOLVED_ADB = ""
    _ADB_SOURCE_READY = False
if _RESOLVED_ADB:
    DEFAULT_ADB = _RESOLVED_ADB
elif _ADB_SOURCE_READY:
    # 统一到 adb_runtime.resolve_adb_path() 单点治理，消除多路径分歧；
    # 真源给不出路径 = 本机确实确认不了 adb，这时不回退硬编码候选，留空后由
    # check_gates() 用 Path(adb).is_file() 判否并抛 GateBlocked("ADB 不存在")
    # 停手——失败是响亮的、可诊断的，而不是带着不确定的 adb 继续跑。
    #
    # 这条分支的真正理由是「真源已确认无解，回退只是把不确定性藏起来」。
    # **它不是**"防止 server 冲突"——多个 adb server 互相冲突只是早期推测，
    # 至今未被证实（今晨 3221225773 崩溃真因未定性），不要把推测写进理由。
    # 已核实的实测事实（2026-10-26）：
    #   * 本机此刻只有 1 个 adb 进程（PID 18368）；
    #   * adb_inventory()["found"] 里的 3 条是**候选路径**，不是进程；
    #     其中 采集/scrcpy/adb.exe 磁盘上并不存在；
    #   * C:\leidian 与 D:\leidian 两份 adb.exe 字节完全相同
    #     （MD5 F1F479BBA21298E758FC22D8D98F8E48，同为 1.0.41）。
    # 写"实测"之前先跑数：以上三条都是跑出来的，不是推的。
    DEFAULT_ADB = ""
else:
    # adb_runtime 还没落地 / 导入失败：完整保留原元组逻辑，与改动前逐字一致。
    DEFAULT_ADB = next((path for path in ADB_CANDIDATES
                        if Path(path).is_file()),
                       ADB_CANDIDATES[-1])

DEV_ROOT = Path(__file__).resolve().parent.parent / "采集" / "dev_auto_play"
MAX_CONSECUTIVE_ERRORS = 3
# 内存熔断阈值：从环境变量读，**未设置 = 只观测不停手**。
# ``limit_mb`` 的 1500 是未经标定的初值，采到真实峰值之前开启 abort 会误停
# 正常代打（误停比漏停更糟）。标定方法：先不带这个变量跑一轮真实代打，
# 读台账里 resource_guard_sample 的 peak_mb，再按 peak+50% 设这个变量。
RSS_LIMIT_ENV_VAR = "WZMJ_RSS_LIMIT_MB"
# 看门狗自身的节流/迟滞：interval_s=5.0（一秒一轮也只真采一次）、
# consecutive=2（连续两次超限才停，抗单次尖峰）。迟滞不自己再实现一套。
RSS_GUARD_INTERVAL_S = 5.0
RSS_GUARD_CONSECUTIVE = 2
RSS_GUARD_DEFAULT_LIMIT_MB = 1500.0
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
# 多亮白名单：算牌建议必须连续这么多帧指向同一张才许点击（与白名单自身的
# 连续帧计数同源；实测多亮帧的 analyze 单帧读数不稳，两帧一致才可信）。
BRIGHT_ADVICE_MIN_TICKS = 2
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
# 「过」后整手压暗的几何恢复证据门（实机 2026-10-27 21:00，会话
# 203044）：压暗帧牌面带 p70=100、亮像素 0.036；正常帧 214、0.794。
# CHI_DIMMED 的 ROI 含大量非牌面像素，对该帧 bright_ratio 实测
# 0.2126 > 0.15 漏检，故另设牌面带尺；只用于恢复激活，不改全局阈值。
PASS_DIM_FACE_ROI = (500, 760, 1500, 895)  # x0,y0,x1,y1
PASS_DIM_P70_MAX = 130.0
PASS_DIM_BRIGHT_RATIO_MAX = 0.15
# claim 点后验证的落定轮询（2026-10-25 实机 Bug 2）：首帧决定性读数
# 直接采用；含糊则每隔 INTERVAL 重拍，直到验证通过、连续两帧签名
# 一致（落定）或 ATTEMPTS 耗尽（fail-closed）。
CLAIM_SETTLE_ATTEMPTS = 8
CLAIM_SETTLE_INTERVAL_S = 0.4
# 吃/碰/杠成功后不是事务终点：必须在同局内用 fresh 帧重新算牌并再出一张。
# 两个预算同时约束，避免进程调度异常时只靠 tick 或只靠时钟无限等待。
POST_CLAIM_DISCARD_ACTIONS = frozenset({ACTION_CHI, ACTION_PENG, ACTION_KONG})
POST_CLAIM_DISCARD_MAX_TICKS = 15
POST_CLAIM_DISCARD_TIMEOUT_S = 20.0
# 同屏促销弹窗的关闭预算：连续这么多次关闭之后弹窗仍在，说明点的不是它
# （或弹窗会立刻重弹），停手留证，绝不无限重试。计数在换局/离桌时清零。
PROMO_CLOSE_BUDGET = 3
# ---- 开新场（比赛场房间 → 点空座入座）门禁预算 ------------------------------
# 这条链路是「打满 16 局返回大厅之后自动开下一场」的最后一段，误点代价是
# **在别人的真局里建桌并扣房卡**，所以默认只算计划、绝不点。
# G7 授权门：真点必须显式打开 ``AutoPlayLoop(lobby_open_enabled=True)``。
LOBBY_OPEN_ENABLED_DEFAULT = False
# G5 预算门：同一屏最多点这么多次。房间画面点了没反应时绝不允许"再补一下"
# ——实测点空座后 1 秒画面仍长得像房间（lobby_probe/011 是 units=2 / seats=3）。
LOBBY_OPEN_BUDGET = 2
# G5：两次入座点击之间至少隔这么多帧。
LOBBY_OPEN_MIN_GAP_FRAMES = 2
# G6 兜底：连续这么多帧「不是房间 / 房间但给不出计划」且期间零动作，才报一次
# lobby_open_blocked: unrecognised —— 降级方向是停机等人工，不是无限静等。
LOBBY_OPEN_UNRECOGNISED_FRAMES = 15


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
    """用前后完整牌集合证明第一击已经完成出牌。

    接受两种真实时序：

    * 目标牌直接离手，暗手 ``N -> N-1``；
    * 目标牌离手后游戏已立即补摸，暗手仍为 ``N``。

    两种都必须满足玩法/财神一致、目标牌恰好少一张、没有其它旧牌
    消失；若前后都提供副露，副露还必须完全一致。任一条件不满足都拒绝。
    这层是对裸 ``count_tiles`` 的语义复核，专门避免副露布局把一段暗手
    误切成第二组副露后，将已经成功的第一击误报为 ``tap_aborted``。
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
    if not before or len(after) not in (len(before), len(before) - 1):
        out["reason"] = (
            f"前后暗手张数不是直接离手或离手后补摸（{len(before)}→{len(after)}）")
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
    before_melds = before_result.get("melds")
    after_melds = after_result.get("melds")
    if before_melds is not None and after_melds is not None:
        if before_melds != after_melds:
            out["reason"] = "前后副露不一致"
            return out
    removed = list((Counter(before) - Counter(after)).elements())
    added = list((Counter(after) - Counter(before)).elements())
    out["removed"] = removed
    out["added"] = added
    if removed != [int(expected_tile)]:
        out["reason"] = (
            f"目标牌变化不唯一：期望移除 {expected_tile}，实际移除 {removed}")
        return out
    if len(after) == len(before) - 1:
        if added:
            out["reason"] = f"直接离手态不应新增牌：实际新增 {added}"
            return out
        out["discarded"] = True
        out["reason"] = (
            f"第一击后目标牌 {expected_tile} 已从完整手牌移除，暗手 "
            f"{len(before)}→{len(after)} 且副露保持；判定第一击已完成出牌，"
            "禁止第二击")
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


@dataclass
class PostClaimContinuation:
    """一次已验证鸣牌之后，尚未完成的“重新算牌并出一张”事务。"""

    action: str
    action_id: str
    target_tile: int | None
    round_seq: int
    started_tick: int
    started_at: float
    supplement_required: bool = False
    observed_ticks: int = 0

    def ledger(self, now: float) -> dict:
        return {
            "action": self.action,
            "action_id": self.action_id,
            "target_tile": self.target_tile,
            "round_seq": self.round_seq,
            "started_tick": self.started_tick,
            "observed_ticks": self.observed_ticks,
            "age_s": round(max(0.0, float(now) - self.started_at), 3),
            "supplement_required": self.supplement_required,
            "status": "awaiting_recomputed_discard",
        }


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


def _read_rss_limit_from_env() -> float | None:
    """读 ``WZMJ_RSS_LIMIT_MB``；**未设置 / 非正数 / 非法 → 返回 None**。

    None 的语义是「未标定 = 只观测，绝不停手」。这是刻意的两阶段设计：
    ``RSS_GUARD_DEFAULT_LIMIT_MB`` 是拍脑袋的初值，采到真实峰值之前开启
    abort 会误停正常代打——误停比漏停更糟。
    """
    raw = os.environ.get(RSS_LIMIT_ENV_VAR, "")
    if not str(raw).strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:        # NaN / 非正数一律当未设置
        return None
    return value


def _build_resource_guard(limit_mb: float | None, *,
                          sampler: Callable[[], dict] | None = None
                          ) -> tuple[object | None, bool]:
    """构造内存看门狗，返回 ``(guard, 是否回退到假阈值)``。

    mem_guard 缺失或构造异常时返回 ``(None, False)``（永不误杀）。

    能力边界（务必按这个理解，别当成 OOM 万能保险）：它测的是**本进程**
    RSS/commit，抓得住自身泄漏，**抓不住**"被别人的进程把系统整体挤爆"——
    后者要看系统可用内存，是本模块目前没覆盖的另一个量。

    ``limit_mb=None`` **原样透传**，不替换成默认阈值：mem_guard 把它解释成
    「纯观测模式」（``over`` 恒 False、``reason="no-limit…"``，但 ``peak_mb``
    照样记）。**绝不能**塞一个假的 1500 进去——假阈值写进遥测会看起来像已
    标定，那正是我们要避免的事（"未标定"必须是可辨认的状态）。

    第二个返回值是「是否回退到了假阈值」：只有旧版 mem_guard 不接受
    ``limit_mb=None`` 时才会为 True。这条回退**绝不能是静默的**——它会把刚
    修掉的问题原样放回（遥测重新出现 ``over: true`` vs 1500，看起来像已标定）。
    所以调用方必须把这个标记写进每一条遥测（见 ``limit_fallback`` 字段）。
    """
    try:
        from mem_guard import ResourceGuard
    except Exception:                       # noqa: BLE001 - 观测件缺失不得影响代打
        return None, False
    try:
        return (ResourceGuard(
            limit_mb=limit_mb,
            interval_s=RSS_GUARD_INTERVAL_S,
            consecutive=RSS_GUARD_CONSECUTIVE,
            sampler=sampler,
        ), False)
    except TypeError:                       # 旧版 mem_guard 不支持 None：回退默认阈值
        try:
            return (ResourceGuard(
                limit_mb=RSS_GUARD_DEFAULT_LIMIT_MB,
                interval_s=RSS_GUARD_INTERVAL_S,
                consecutive=RSS_GUARD_CONSECUTIVE,
                sampler=sampler,
            ), True)
        except Exception:                   # noqa: BLE001 - 构造失败就当没有看门狗
            return None, False
    except Exception:                       # noqa: BLE001 - 构造失败就当没有看门狗
        return None, False


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
        adb_inventory_fn: Callable[[], dict] | None = None,
        rss_sampler_fn: Callable[[], dict] | None = None,
        lobby_open_enabled: bool = LOBBY_OPEN_ENABLED_DEFAULT,
        player_name: str = "",
        match_score_fn: Callable[..., Any] | None = None,
        # 只对白名单 16/16 总结页开放：必须先按昵称识别并持久化双方整场
        # 总分，才允许点返回大厅；提前解散、未知昵称、积分不守恒都不接线。
        allow_return_lobby: bool = False,
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
        # 内存遥测（标定 limit_mb 的依据）：独立文件，不掺进审计台账。
        self._resource_log_path = self.session_dir / "resource_guard.jsonl"
        # Structured follow-session debug log (vision/gates/caishen/decisions).
        # Never flips CLAIM_TAP_CHANNEL_READY; dual-writes beside actions.jsonl.
        self.follow_log_path = self.session_dir / "follow_session.jsonl"
        self.match_result_path = self.session_dir / "match_results.jsonl"
        self._follow_log = SessionLogger(self.session_dir)
        # session_dir 在 self._tap（161 行）之后才确定，所以只能在这里回填：
        # 让 tap 证据落进本局会话目录而不是 TapExecutor 的默认目录。
        # self._tap 可能是测试注入的替身/mock，普通属性赋值对它们无害。
        if self._tap is not None:
            self._tap.session_dir = self.session_dir
        # 目录延迟到第一次真正落盘时才创建：门禁不放行时不留空会话目录。
        self._dirs_ready = False
        self._round_transition = RoundTransitionExecutor(
            capture=self._capture_transition_frame,
            tap=self._tap_round_transition,
            sleep=self._sleep,
            report_dir=self.session_dir,
        )

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
        # 多亮白名单（亮簇 1..N 张）：只在 bright_count >= 2 时才接管，单亮
        # 一律走上面那条已实机验证的路径，行为逐字不变。
        self._bright_subset = BrightSubsetCutTracker()
        # 压暗态补读只供多亮白名单使用；完整手牌须由两张独立原生帧复核。
        self._dim_gain_snapshot: tuple | None = None
        self._dim_gain_evidence: dict | None = None
        # 同局手牌账本：压暗/遮挡帧不重读全手，用上一帧可信读数 + 本帧
        # 亮牌白名单决策（见 hand_ledger 模块头部的实机证据）。
        self._hand_ledger = HandLedger()
        # 牌局结束已返回大厅：本场结束，等人工/找桌链路开新局。
        self._left_table = False
        # 同屏促销弹窗已关闭次数（换局/离桌清零）：用于 PROMO_CLOSE_BUDGET
        # 的“关了还弹就停手”预算，不是异常计数。
        self._promo_close_count = 0
        # ---- 开新场通道的状态（G5 预算 / G6 告警节流，离桌与换局都清零）----
        # G7 授权门：默认关。关着时只把「该点哪个空座」算出来记进台账，零点击。
        self.lobby_open_enabled = bool(lobby_open_enabled)
        self.player_name = str(player_name or "").strip()
        self._match_score = match_score_fn or read_match_score
        self._match_result_recorded = False
        # 返回大厅授权门：只认显式 True——字符串/"1"/1 一律未授权。
        self.allow_return_lobby = allow_return_lobby is True
        # ---- adb 单点治理现场（只读枚举，供故障定性） -------------------
        # ``adb_inventory()`` 只做只读枚举（adb devices / 进程列表），不发任何
        # tap/keyevent。注入点存在是为了离线测试完全不接触真实模拟器。
        self._adb_inventory = adb_inventory_fn or self._default_adb_inventory
        # 最近一次 check_gates 的 report（含 adb 现场），失败时也可回查。
        self._last_gate_report: dict | None = None
        # ---- 内存看门狗（观测优先，默认不停手） -------------------------
        # None = 未标定 → 只记台账；设了 WZMJ_RSS_LIMIT_MB 才启用 abort。
        self._rss_limit_mb: float | None = _read_rss_limit_from_env()
        self._guard, self._rss_limit_fell_back = _build_resource_guard(
            self._rss_limit_mb, sampler=rss_sampler_fn)
        self._guard_last_ts: float | None = None
        # 本屏已真实点击过几次入座（G5 预算）。
        self._lobby_open_taps = 0
        # 上一次入座点击的帧号（G5 间隔）。
        self._lobby_open_last_tick: int | None = None
        # 连续「不是房间 / 房间但给不出计划」的帧数（G6 unrecognised 节流）。
        self._lobby_open_blank_frames = 0
        # 本屏已经报过的开新场事件键：blocked:<reason> / plan:<action>@<point>。
        # 房间画面会一动不动停很多帧，台账按"一屏一次"记，别每帧刷。
        self._lobby_open_seen: set[str] = set()
        # 多亮白名单的「建议牌连续帧」记录：(局序号, 建议牌 id, 连续帧数)。
        # 与白名单自身的连续帧计数同源：换局/不命中/建议变了都清零。
        self._bright_subset_advice: tuple[int, object, int] | None = None
        # 吃链在本行动窗弃权过的标记（局序+来牌+闭手），防重复点吃。
        self._chi_abstain_key: tuple | None = None
        # 吃/碰/杠经 L3 证实后必须继续到“fresh 重算→出牌→离手验证”。
        # 该状态只存在于内存，进程重启不恢复点击授权，也不猜中间事务。
        self._post_claim_pending: PostClaimContinuation | None = None
        self._round_candidate: tuple[bytes | None, str | None] | None = None
        self._round_candidate_ticks = 0
        self._round_identity_unknown_ticks = 0
        self._unknown_caishen_ticks = 0
        self._manual_caishen_conflict: str | None = None
        self._manual_caishen_conflict_ticks = 0

    # ------------------------------------------------------------------
    # 只读门禁
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 内存看门狗（观测优先；只在标定后才停手）
    # ------------------------------------------------------------------
    def _resource_guard_tick(self) -> dict | None:
        """每帧一次内存体检；返回非 None 表示已停手，调用方直接返回该事件。

        两阶段，刻意如此（1500 是未标定的初值，误停比漏停更糟）：

        * ``WZMJ_RSS_LIMIT_MB`` **未设置** → 只往台账写
          ``resource_guard_sample``（含 ``peak_mb``），**绝不停手**；
        * 设置了 → 才启用 ``should_abort()``，连续超限（迟滞 ``consecutive=2``）
          时 ``stop(gate="resource_guard")``，reason 带 rss/commit/limit 实测值。

        只做"停手留证"这一件事：**不**降采样、**不**关预览、**不**重启子进程、
        **不**自愈重试。读数不可用时恒不停手（与 mem_guard 的红线一致）。

        关于口径（别读错 ``peak_mb``）：``ResourceGuard.peak_mb`` 是
        **max(rss, commit)**，不是纯 RSS——熔断判定用的就是这个值。所以标定
        ``WZMJ_RSS_LIMIT_MB`` 要对着 ``peak_mb`` / ``--summarize`` 的
        ``peak_guard_mb``，别对着 ``peak_rss_mb``，否则口径不一致。
        系统级读数（``sys_available_mb`` 等）只进遥测、**不参与** should_abort()。
        """
        guard = self._guard
        if guard is None:
            return None
        try:
            snapshot = guard.sample()
        except Exception:                   # noqa: BLE001 - 看门狗异常不当门禁
            return None
        if not isinstance(snapshot, dict):
            return None
        # 节流命中（ts 未变）的快照不重复落盘：一秒一帧也只 5 秒一行。
        # 单独写 resource_guard.jsonl，不掺进 actions.jsonl —— 后者是"每次动作
        # 一行"的审计口径，被既有用例逐行断言；也不走 follow_session.jsonl，
        # 它的 build_follow_record 只保留固定字段，rss/peak 会被静默丢掉。
        fresh = self._guard_last_ts != snapshot.get("ts")
        self._guard_last_ts = snapshot.get("ts")
        if fresh:
            try:
                self._ensure_dirs()
                self._append_line(
                    self._resource_log_path,
                    json.dumps({
                        "ts": datetime.now().astimezone().isoformat(),
                        "event": "resource_guard_sample",
                        "tick": self.tick_index,
                        "serial": self.serial,
                        "rss_mb": snapshot.get("rss_mb"),
                        "commit_mb": snapshot.get("commit_mb"),
                        # 系统级读数（mem_guard 新增，键名与其保持一致，好让
                        # `mem_guard.py --summarize` 直接算出 min_sys_available_mb）。
                        # **只观测**：不参与 should_abort()，本帧也不会因此停手。
                        "sys_reading_ok": snapshot.get("sys_reading_ok"),
                        "sys_available_mb": snapshot.get("sys_available_mb"),
                        "sys_used_percent": snapshot.get("sys_used_percent"),
                        "over": bool(snapshot.get("over")),
                        "reason": snapshot.get("reason"),
                        "peak_mb": guard.peak_mb,
                        "over_streak": guard.over_streak,
                        "limit_mb": self._rss_limit_mb,      # None = 未标定/只观测
                        "abort_enabled": self._rss_limit_mb is not None,
                        # True = 旧版 mem_guard 不接受 None，看门狗用的是回退的
                        # 假阈值，遥测里的 over 不能当成"已标定且超限"来读。
                        "limit_fallback": bool(self._rss_limit_fell_back),
                    }, ensure_ascii=False) + "\n",
                )
            except Exception:               # noqa: BLE001 - 遥测落盘失败不许停手
                pass
        if self._rss_limit_mb is None:
            return None                     # 未标定：只观测，绝不停手
        try:
            should_abort = guard.should_abort()
        except Exception:                   # noqa: BLE001 - 判定异常不停手
            return None
        if not should_abort:
            return None
        return self._stop(
            "resource_guard",
            (f"连续 {RSS_GUARD_CONSECUTIVE} 次内存超限："
             f"rss={snapshot.get('rss_mb')}MB "
             f"commit={snapshot.get('commit_mb')}MB "
             f"peak={guard.peak_mb}MB "
             f"limit={self._rss_limit_mb}MB（{RSS_LIMIT_ENV_VAR}）；"
             "停手留证，不做降采样/重启/自愈"),
            {
                "event": "resource_guard_sample",
                "rss_mb": snapshot.get("rss_mb"),
                "commit_mb": snapshot.get("commit_mb"),
                "peak_mb": guard.peak_mb,
                "over_streak": guard.over_streak,
                "limit_mb": self._rss_limit_mb,
                "abort_enabled": True,
            },
        )

    # ------------------------------------------------------------------
    # adb 单点治理现场（只读枚举，供故障定性）
    # ------------------------------------------------------------------
    def _default_adb_inventory(self) -> dict:
        """调 ``adb_runtime.adb_inventory()``，任何失败都归一成同构 dict。

        只读枚举（``adb devices`` + 进程列表），**不发** tap/keyevent/sendevent，
        也不调 ``ensure_single_server()``——kill-server 只能是用户显式动作。
        """
        try:
            from adb_runtime import adb_inventory
        except Exception as exc:            # noqa: BLE001 - 模块缺失不得挡住代打
            return {"available": False, "error": f"adb_runtime 不可用: {exc}",
                    "chosen": None, "version": None, "servers": [],
                    "devices": [], "found": [], "notes": []}
        try:
            raw = adb_inventory()
        except Exception as exc:            # noqa: BLE001 - 枚举失败只记为不可用
            return {"available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "chosen": None, "version": None, "servers": [],
                    "devices": [], "found": [], "notes": []}
        if not isinstance(raw, dict):
            return {"available": False,
                    "error": f"adb_inventory() 返回 {type(raw).__name__}，应为 dict",
                    "chosen": None, "version": None, "servers": [],
                    "devices": [], "found": [], "notes": []}
        return {
            "available": True, "error": "",
            "chosen": raw.get("chosen"),
            "version": raw.get("version"),
            "servers": [str(item) for item in (raw.get("servers") or ())],
            "devices": [str(item) for item in (raw.get("devices") or ())],
            "found": [str(item) for item in (raw.get("found") or ())],
            "notes": [str(item) for item in (raw.get("notes") or ())],
        }

    def _gate_blocked(self, report: dict, reason: str) -> GateBlocked:
        """门禁失败：把 adb 现场补进 report 后再抛（失败要响亮且可诊断）。

        ``servers`` / ``devices`` 是「adb 到底起没起来、有几个 server」的现场
        证据。今晨 3221225773 崩溃一直没定性，就是因为故障那一刻没有留下
        任何现场，所以这里**每次失败都自动留一份**。
        """
        self._last_gate_report = dict(report)
        inventory = report.get("adb_inventory")
        # 现场直接进异常消息：哪怕这次没能落盘，stdout / 上层调用方也一定看得到。
        if inventory is not None:
            message = (f"{reason}｜adb 现场: servers={inventory['servers']} "
                       f"devices={inventory['devices']} "
                       f"version={inventory['version']}")
        elif report.get("adb_inventory_error"):
            message = f"{reason}｜adb 现场不可用: {report['adb_inventory_error']}"
        else:
            message = reason
        # 只在会话目录**已经存在**时落盘：绝不为了留证去新建一个目录。
        # 既有用例（test_runner_exits_nonzero_when_adb_missing）钉死的是
        # 「门禁不放行时不留空会话目录」，冷启动自检失败就该什么都不建。
        if self._dirs_ready or self.ledger_path.exists():
            try:
                payload = {
                    "ts": datetime.now().astimezone().isoformat(),
                    "event": "gate_blocked", "reason": reason,
                    "serial": self.serial, "adb": self.adb_path,
                    **{key: report.get(key) for key in
                       ("adb_exists", "adb_inventory", "adb_inventory_error")},
                }
                # 前台读取失败的原始异常也进台账：异常文本里通常带着退出码，
                # 是事后给故障定性的关键一行。只在它存在时才加键，
                # 避免给其它门禁分支塞进无意义的 None。
                if report.get("foreground_error") is not None:
                    payload["foreground_error"] = report["foreground_error"]
                self._write_event(payload)
            except Exception:               # noqa: BLE001 - 落盘失败不许盖掉门禁原因
                pass
        return GateBlocked(message)

    def check_gates(self) -> dict:
        """启动前自检：adb / 设备在线 / 前台包 / 房卡。任一硬门禁失败抛 GateBlocked。"""
        report: dict = {"adb": self.adb_path, "adb_exists": Path(self.adb_path).is_file(),
                        "serial": self.serial, "mode": self.mode,
                        "enabled": self.enabled, "dry_run": self.dry_run}
        if not report["adb_exists"]:
            report.update(self._adb_inventory_evidence())
            raise self._gate_blocked(report, f"ADB 不存在: {self.adb_path}")
        report.update(self._adb_inventory_evidence())
        if not self.serial or any(ch.isspace() for ch in self.serial):
            raise self._gate_blocked(report, f"模拟器序列号无效: {self.serial!r}")
        inventory = report.get("adb_inventory")
        if inventory is not None and self.serial not in inventory["devices"]:
            # 靶机不在线（offline/unauthorized/掉线）→ fail closed，明确停手。
            # 读不到 inventory 时不拦（读不到 ≠ 不在线），避免误杀。
            raise self._gate_blocked(
                report,
                f"靶机不在线：serial={self.serial!r} 不在 adb devices "
                f"{inventory['devices']} 内；不重试、不自愈")
        # 前台包读取失败**必须**走 _gate_blocked：2026-10-26 取证发现这条
        # 裸调用（旧代码：``package = self._foreground()`` 无 try/except）
        # 会在 dumpsys 超时/崩溃时把异常直接冒出去，绕过门禁落盘分支，
        # 前面已经采到的 adb_inventory 现场一行都不留——证据黑洞就是这里。
        # 现在的口径：fail closed 不变，但**先把现场留住再抛**。
        try:
            package = self._foreground()
        except Exception as exc:  # noqa: BLE001 - 确认不了前台就不许点，但要留证
            detail = self._foreground_error_detail(exc)
            report["foreground_error"] = detail
            raise self._gate_blocked(
                report,
                f"无法确认前台应用: {detail['type']}: {detail['message']}"
                f"｜退出码诊断: {detail['diagnosis']}") from exc
        report["foreground"] = package
        if package != EXPECTED_PACKAGE:
            raise self._gate_blocked(
                report, f"前台包不是游戏: {package}（要求 {EXPECTED_PACKAGE}）")
        report["card_count"] = self._read_card_count(None)
        self._last_gate_report = dict(report)
        return report

    @staticmethod
    def _foreground_error_detail(exc: BaseException) -> dict:
        """把"前台包读不到"的异常拆成可落台账的结构 + 退出码人话诊断。

        Args:
            exc: ``self._foreground()`` 抛出的任意异常。

        Returns:
            dict: ``{"type": 异常类名, "message": str(exc),
            "exit_code": int | None, "diagnosis": str}``。
            退出码抠不出来时 ``exit_code`` 为 ``None``、``diagnosis`` 为
            ``"未取到退出码"``——**绝不猜**（WinError 1455 这类文本里就没有退出码）。
            翻译一律走 ``adb_runtime.describe_exit_code`` 这个唯一真源；
            adb_runtime 导入失败时退化成 ``"未取到退出码（adb_runtime 不可用）"``。
        """
        message = str(exc)
        exit_code: int | None = None
        diagnosis = "未取到退出码"
        try:
            from adb_runtime import describe_exit_code, extract_exit_code

            exit_code = extract_exit_code(message)
            if exit_code is not None:
                diagnosis = describe_exit_code(exit_code)
        except Exception as import_exc:  # noqa: BLE001 - 诊断失败不许盖掉门禁原因
            diagnosis = f"未取到退出码（adb_runtime 不可用: {type(import_exc).__name__}）"
        return {"type": type(exc).__name__, "message": message,
                "exit_code": exit_code, "diagnosis": diagnosis}

    def _adb_inventory_evidence(self) -> dict:
        """把只读现场按台账口径摊平：devices/servers/version/chosen + 错误。"""
        snapshot = self._adb_inventory()
        if not isinstance(snapshot, dict):
            snapshot = {"available": False,
                        "error": f"adb_inventory 返回 {type(snapshot).__name__}"}
        available = snapshot.get("available")
        if available is None:
            # 注入的替身可能直接给 adb_runtime 的原始口径（没有 available 键）：
            # 有内容且没报错就当作"读到了"，避免静默退化成 None。
            available = not snapshot.get("error") and bool(snapshot)
        inventory = None
        if available is True:
            inventory = {
                "chosen": snapshot.get("chosen"),
                "version": snapshot.get("version"),
                "servers": [str(v) for v in (snapshot.get("servers") or ())],
                "devices": [str(v) for v in (snapshot.get("devices") or ())],
                "found": [str(v) for v in (snapshot.get("found") or ())],
                "notes": [str(v) for v in (snapshot.get("notes") or ())],
            }
        return {"adb_inventory": inventory,
                "adb_inventory_error": str(snapshot.get("error") or "")}

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
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            observer.call("byte_received", raw)
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

    @staticmethod
    def _append_line(path: Path, text: str, *, attempts: int = 4) -> None:
        """追加一行台账，遇到瞬时占用就重试几次。

        实机教训（2026-10-25 22:40）：杀毒/索引服务会短暂独占正在长大的
        jsonl，一次 ``PermissionError`` 就把整场代打打断（两台机同时
        ``[FAIL] 循环异常终止：[Errno 13]``）。台账是纯追加的审计数据，
        重试几次既不会重复也不会错序，比重启安全得多。
        """
        delay = 0.05
        for attempt in range(1, attempts + 1):
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
                return
            except PermissionError:
                # 只有"被短暂占用"才值得重试；最后仍失败必须抛出，
                # 绝不吞掉——台账写不进去等于证据链断了。
                if attempt >= attempts:
                    raise
                time.sleep(delay)
                delay = min(delay * 3, 0.5)

    def _write_event(self, event: dict) -> None:
        """Append one dev-tagged ledger line; header line marks the corpus."""
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            evidence = observer.call("runtime_event", event)
            if evidence is not None:
                event["incoming_shadow_observer"] = evidence
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
            self._append_line(self.ledger_path,
                              json.dumps(header, ensure_ascii=False) + "\n")
            self._follow_log.write_header({
                "serial": self.serial, "mode": self.mode,
                "enabled": self.enabled, "dry_run": self.dry_run,
                "caishen": self.caishen_label or "auto",
                "dev": True,
            })
            self._ledger_started = True
        self._append_line(self.ledger_path,
                          json.dumps(event, ensure_ascii=False) + "\n")
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

    def _capture_transition_frame(self) -> np.ndarray:
        raw = self._shot()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != FRAME_SIZE:
            raise TapAborted("下一局复核帧不是1600×900原生画面")
        return image

    def _tap_round_transition(self, x: int, y: int) -> None:
        """Use the already-authorised ADB channel after round-transition gates."""
        tap = getattr(self._tap, "_tap", None)
        if not callable(tap):
            raise TapAborted("点击执行器不提供受控下一局通道")
        tap(int(x), int(y))

    def _round_transition_event(
        self,
        image: np.ndarray,
        frame_path: Path,
    ) -> dict | None:
        """Handle a reviewed end-of-round screen before round-identity gates."""
        matched, probe = self._round_transition.probe(image)
        if not matched:
            return None
        base = {
            "ts": datetime.now().astimezone().isoformat(),
            "round_seq": self.round.seq,
            "frame": str(frame_path),
            "round_transition_probe": probe,
        }
        if self._post_claim_pending is not None:
            return self._stop(
                "post_claim_round_transition",
                "鸣牌后的必出牌事务尚未完成却出现下一局页；本帧禁止点击",
                {**base,
                 "post_claim_continuation": self._post_claim_ledger()},
            )
        if self.dry_run:
            return self._event({
                **base, "event": "idle",
                "reason": "dry_run 已确认局间下一局按钮；只记录不点击",
            })
        # ``round.seq`` 是本次进程观察到的换局序号，不是画面上的
        # 实际 ``13/16``。进程中途接管或跨场时两者必然不对齐；
        # 只用它做“同一观测局不重复点”的 key，不把它冒充末局证据。
        # 真正的 16/16 由独立 match-end 场景门处理。
        outcome = self._round_transition.attempt(
            round_no=self.round.seq, round_total=0)
        ledger = outcome.ledger()
        base["round_transition"] = ledger
        if outcome.status == "terminal":
            return self._stop("match_complete", outcome.reason, base)
        # 点后状态不确定时执行器已经永久 halted；若这里只记事件继续循环，
        # 后续每 tick 只会反复得到 halted，形成“程序活着但永远不再动作”的
        # 假运行。显式 stop 才能让日志和 UI 准确暴露首个停手原因。
        if outcome.status in {"post_verify_uncertain", "halted"}:
            return self._stop(
                "round_transition_uncertain", outcome.reason, base)
        # advanced / advanced_without_click / accepted_waiting / no_click 均
        # 继续观察。双号场景下另一端可能先推进；accepted_waiting
        # 表示本端已就绪，no_click 则等下一张 fresh 帧重新过三重门。
        return self._event({
            **base, "event": "round_transition",
            "reason": outcome.reason,
        })

    # ------------------------------------------------------------------
    # 单帧一周期
    # ------------------------------------------------------------------
    def _ensure_incoming_shadow_observer(self):
        if not self._claim_policy.incoming_shadow_enabled:
            return None
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            return observer
        try:
            from incoming_shadow_context import IncomingShadowObserver, ShadowObserverBinding
            observer = IncomingShadowObserver(
                serial=self.serial, session_id=str(self.session_dir))
            binding = ShadowObserverBinding(self, observer)
            self._incoming_shadow_observer = observer
            self._incoming_shadow_binding = binding
            return observer
        except Exception as exc:
            self._incoming_shadow_context_error = type(exc).__name__
            return None

    def tick(self) -> dict:
        """Observe explicit shadow runs without changing the gameplay path."""
        if not self._claim_policy.incoming_shadow_enabled:
            binding = getattr(self, "_incoming_shadow_binding", None)
            if binding is not None:
                binding.detach(self)
                observer = getattr(self, "_incoming_shadow_observer", None)
                if observer is not None:
                    observer.call("break_chain", "shadow_disabled")
                self._incoming_shadow_binding = None
                self._incoming_shadow_observer = None
            return self._tick_without_shadow()
        observer = self._ensure_incoming_shadow_observer()
        if observer is None:
            return self._tick_without_shadow()
        observer.call("begin_cycle", serial=self.serial, session_id=str(self.session_dir))
        result, error = None, None
        try:
            result = self._tick_without_shadow()
            return result
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            # All exits, including capture/foreground errors and unknown scenes.
            # Only a complete normal wait/claim observation may retain a chain.
            observer.call("end_cycle", result, error=error)

    def _tick_without_shadow(self) -> dict:
        """跑一个周期并返回本周期的事件记录。任何异常都不重试本帧。"""
        started = self._clock()
        if self.stopped:
            return {"event": "skip", "reason": self.stop_reason}
        # 门禁零：内存看门狗。默认只观测；标定后（设了 WZMJ_RSS_LIMIT_MB）
        # 才会在连续超限时停手。放在最前面，保证任何一帧都不会漏检。
        guard_event = self._resource_guard_tick()
        if guard_event is not None:
            return guard_event
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
        from lobby_preflight import classify_session_scene
        scene = classify_session_scene(
            image, round_probe=self._round_transition.probe)
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            observer.call("scene", image, scene.ledger(), frame_path)
        scene_base = {
            "ts": datetime.now().astimezone().isoformat(),
            "round_seq": self.round.seq, "frame": str(frame_path),
            "scene": scene.ledger(),
        }
        # “等待时间过长解散比赛！”是独立终态，必须在邀请、换局、牌河和
        # 识牌之前处理。弹窗覆盖牌桌时继续走下层像素会污染所有状态。
        if scene.kind == "timeout_dissolve":
            return self._timeout_dissolve_event(
                image, frame_path, started, state=scene.state)
        # 入桌邀请优先级高于行动行、换局和识牌：弹窗盖住牌桌时继续分析会把
        # 弹窗元素误读成游戏状态。只允许“邀请您进入”文案 + 矩形“取消”按钮
        # 同帧完整命中后拒绝；这条通道绝不复用吃牌面板的蓝色圆形取消。
        invitation = scene.state if scene.kind == "invitation" else None
        if invitation is not None:
            invitation_event = {
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path),
                "invitation": invitation.ledger(),
                "reason": invitation.reason,
            }
            if not invitation.complete or invitation.cancel_center is None:
                return self._event({
                    **invitation_event, "event": "idle", "status": "abstain",
                    "reason": "发现入桌邀请，矩形取消按钮证据暂不完整；零点击继续观察",
                })
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
        # 精确促销弹窗必须在换局、行动行、牌河与识牌之前仲裁。2026-10-26
        # 23:46 真帧证明：弹窗会把 HUD 持续读成 round_suspect；若把本通道
        # 放在换局早退之后，循环会永远停在“等待第二帧确认”，实际已验证的
        # 关闭 X 根本没有执行机会。这里只消费专用 promo_popup 指纹；“使用”
        # 与洗牌/切牌仍在永久黑名单中，且本帧不进入任何牌局状态机。
        promo_base = {
            "ts": datetime.now().astimezone().isoformat(),
            "event": "idle", "round_seq": self.round.seq,
            "frame": str(frame_path),
        }
        # 鸣牌后必出牌事务先推进 watchdog：促销若持续遮挡且 X 不可靠，
        # 也必须在 20 秒内显式停手，不能被 promo 的早退无限挂起。
        post_claim_timeout = self._post_claim_watchdog(frame_path, started)
        if post_claim_timeout is not None:
            return post_claim_timeout
        if scene.kind == "promo":
            return self._promo_dismiss_event(
                image, frame_path, promo_base, started, state=scene.state)
        self._promo_close_count = 0
        # Route the scene before updating any gameplay/HUD/river facts.
        # A lobby has no hand by design; it must never be swallowed by the
        # identity_unknown early return or the legacy lobby-open channel.
        if scene.kind == "match_end":
            if self._post_claim_pending is not None:
                return self._stop(
                    "post_claim_match_end",
                    "鸣牌后续出牌事务未完成就进入整场总结，禁止越过未决动作",
                    scene_base)
            outcome = self._match_end_event(
                image, frame_path, scene_base, started)
            return outcome or self._event({
                **scene_base, "event": "idle",
                "reason": "已处理本场总结，等待新场景；不重复点击"})
        if scene.kind == "next_round":
            transition_event = self._round_transition_event(image, frame_path)
            if transition_event is not None:
                return transition_event
            return self._event({**scene_base, "event": "idle",
                                "reason": "局间候选已变化，零点击重读场景"})
        if scene.kind in {"lobby", "waiting_room"}:
            if self._post_claim_pending is not None:
                return self._stop(
                    "post_claim_scene_changed",
                    "鸣牌后续事务未完成就离开牌桌，拒绝自动重接",
                    scene_base)
            if self.enabled:
                return self._stop(
                    "scene_handoff",
                    f"已确认{scene.kind}；局内worker退出后交还同一大厅流程",
                    scene_base)
            return self._event({**scene_base, "event": "idle",
                                "reason": "dry_run 已确认场次入口；只观察不点击"})
        if scene.kind == "blocked":
            return self._stop("gate_scene", scene.reason, scene_base)
        if scene.kind == "observe":
            return self._event({**scene_base, "event": "idle",
                                "status": "abstain", "reason": scene.reason})
        # 换局判定必须在读牌河**之前**：牌河基线一旦跨局，差分出来的"来牌"
        # 会是上一局的牌。guard 早退分支（行动行可见）在正式的换局判定之前
        # 就会用到 _discard_state，所以这里先判一次。本帧结果存下来复用，
        # 后面不再重复调 _round_key。
        hud_key, key_caishen = self._probe_round_key(frame_path)
        # 换局没确认过的帧，本帧读出的"来牌"是跨局差分的结果，标记不可信：
        # 基线照样作废，且本帧的读数不许进 L2（见 _claim_policy_event）。
        self._discard_trusted = self.round.same_round(hud_key, key_caishen)
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            observer.call("runtime_scope", round_seq=self.round.seq, hud_key=hud_key,
                          caishen=key_caishen, discards_trusted=self._discard_trusted)
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
        # 手牌账本：只有"可信读数"（advise/hu 且手牌读全）才写账；压暗帧
        # 的残读（abstain）一律不进账本，避免把读残的手牌当成事实复用。
        self._hand_ledger.remember(
            result if isinstance(result, dict) else None,
            round_seq=self.round.seq,
            caishen=self.round.caishen or (result.get("caishen")
                                           if isinstance(result, dict) else None),
            frame=str(frame_path),
        )
        # 门禁二：换局（HUD 指纹 / 财神 label）。指纹在本帧开头已经算过一次
        # （读牌河之前要先作废跨局基线），这里直接复用，不重复调用。
        caishen = key_caishen or result.get("caishen")
        round_signal = self._debounce_round_signal(
            hud_key, key_caishen, caishen)
        if round_signal["state"] == "identity_unknown":
            # 不在牌桌（大厅 / 赛后房间 / 结算）：没有 HUD 房号、没有财神、也没有
            # 任何手牌读数。这时根本点不了牌，也**绝不该**把"读不到房号"当成
            # 故障停机——否则「返回大厅」之后进程立刻自杀，新一场永远开不起来
            # （2026-10-25 23:07 实测：两端刚回大厅就 `gate_round_identity` 退出）。
            seated = bool(key_caishen or (result.get("hand_ids") or ()))
            if hud_key is None and not seated:
                base = {
                    "ts": datetime.now().astimezone().isoformat(),
                    "event": "idle", "round_seq": self.round.seq,
                    "frame": str(frame_path), "round_signal": round_signal,
                }
                base["hand_ledger"] = self._hand_ledger.ledger()
                return self._event({
                    **base,
                    "reason": ("不在牌桌（无房号/无财神/无手牌）：不做任何决策，"
                               "等待开新局；本帧零点击"),
                })
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
            self._bright_subset.reset()
            self._dim_gain_snapshot = None
            self._dim_gain_evidence = None
            self._hand_ledger.reset()
            return self._event({
                "ts": datetime.now().astimezone().isoformat(),
                "event": "idle", "round_seq": self.round.seq,
                "frame": str(frame_path), "caishen": caishen,
                "round_signal": round_signal,
                "reason": "检测到单帧换局信号，等待第二帧确认；期间禁止点击",
            })
        changed_round = round_signal["state"] == "round_changed"
        if changed_round:
            if self._post_claim_pending is not None:
                return self._stop(
                    "post_claim_round_changed",
                    "鸣牌后的必出牌事务尚未完成就检测到换局，停止且不沿用旧状态",
                    {"frame": str(frame_path),
                     "post_claim_continuation": self._post_claim_ledger()},
                )
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
            self._bright_subset.reset()
            self._dim_gain_snapshot = None
            self._dim_gain_evidence = None
            # 换局：手牌账本必须清空——上局的完整手牌与新局毫无关系，
            # 张数碰巧相同就会把上局手牌当成新局事实（这是最危险的一类
            # 跨局污染），所以只清不留。
            self._hand_ledger.reset()
            self._left_table = False
            self._match_result_recorded = False
            self._promo_close_count = 0
            # 换局 = 新的一局已经开始，开新场通道的整屏预算随之作废。
            self._reset_lobby_open_budget()
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
        if self._post_claim_pending is not None:
            base["post_claim_continuation"] = self._post_claim_ledger()
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
            if self._left_table:
                return self._event({
                    **base, "event": "idle",
                    "reason": ("已返回大厅/房间：本场已结束，等待开新局"
                               "（自动找桌→入座→开局为下一步开发项）"),
                })
            # 证据自相矛盾，或结算/模态弹窗画面 → fail closed：不重试、不补点。
            # 台账统一记 gate="gate_turn"；具体归类在 turn_gate.code
            # （如 "gate_settled"），同一行即可看出"其实是牌局结束了"。
            return self._stop("gate_turn", verdict.reason, base)
        bright = self._single_bright.feed(
            image, result, caishen_label=caishen, mode=self.mode)
        base["single_bright_cut"] = bright.ledger()
        # 多亮白名单（亮簇 1..N 张）：单亮帧在这里同样会命中，但只有
        # bright_count >= 2 时才由它接管，单亮行为逐字不变。
        subset = self._bright_subset.feed(
            image, result, caishen_label=caishen, mode=self.mode)
        base["bright_subset_cut"] = subset.ledger()
        dim_gain_advice = None if changed_round else self._dim_gain_whitelist_advice(
            image, subset, result, frame=str(frame_path))
        if self._dim_gain_evidence is not None:
            base["dim_gain_read"] = dict(self._dim_gain_evidence)
        if dim_gain_advice is not None:
            base["dim_gain_advice"] = dim_gain_advice
        # 压暗帧兜底建议：本帧 analyze 读残（abstain）时，用账本手牌 + 本帧
        # 亮牌白名单算"该打白名单里的哪一张"。换局帧不参与（账本已清空）。
        ledger_advice = None if changed_round else self._ledger_advice(
            subset, result, frame=str(frame_path))
        base["hand_ledger"] = self._hand_ledger.ledger()
        if ledger_advice is not None:
            base["ledger_advice"] = ledger_advice
        base["bright_subset_advice"] = self._track_bright_subset_advice(
            subset, result,
            advice_id=(dim_gain_advice or ledger_advice or {}).get("tile_id"),
            override=bool((dim_gain_advice or ledger_advice or {}).get("tile_id")))
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
        # 多亮（>=2 张亮牌）：单亮路径表达不了，改由白名单接管——建议牌必
        # 须落在白名单内，否则本帧弃权（gate=bright_subset_unconfirmed）。
        if subset.confirmed and subset.multi_candidate:
            subset_event = self._bright_subset_fallback(
                result, subset, base, started)
            if subset_event is not None:
                return subset_event
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
        except TapPrecheckRetryExhausted as exc:
            # 三张 fresh 帧都没有给出可执行建议，但异常明确发生在任何
            # input tap 之前。它通常是副露/动画的短暂视觉波动；作为有界
            # 异常继续主循环，由“连续 3 次异常”门禁兜底，不能直接杀死
            # 整场。点后不确定仍由 TapUnverified/普通 TapAborted 停机。
            return self._anomaly(
                "discard_precheck_retry_exhausted", str(exc), started, base)
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
        event_data = {
            **base, "event": "action", "after_frame": after_path,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get(
                "verified",
                outcome.get("executed") and outcome.get("after_count")
                == (outcome.get("before_count") or 0) - 1)),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }
        panel_recovery.clear_state(self._panel_recovery_path)
        if event_data["verified"]:
            # 普通直出也必须同步手牌账本。单亮/多亮路径已经这样做；若漏掉
            # 常规路径，下一次摸牌后旧账本会在张数上重新“看似成立”，把已
            # 打出的牌带回行动决策。扣不准时 HandLedger 会主动清空。
            event_data["hand_ledger_discard"] = {
                "tile_id": expected_tile,
                "applied": self._hand_ledger.forget_tile(expected_tile),
            }
            event_data = self._complete_post_claim_after_discard(event_data)
        else:
            self._stop("tap_unverified", "点击后手牌数量未验证，停止自动点击", base)
        return self._event(event_data)

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
        ledger_applied = self._hand_ledger.forget_tile(expected_tile)
        self._single_bright.reset()
        marker["outcome"] = "verified"
        event_data = {
            **base, "event": "action", "tap": outcome.get("tap"),
            "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed") and
                             outcome.get("after_count") ==
                             (outcome.get("before_count") or 0) - 1),
            "hand_ledger_discard": {
                "tile_id": expected_tile, "applied": ledger_applied},
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }
        if event_data["verified"]:
            event_data = self._complete_post_claim_after_discard(event_data)
        return self._event(event_data)

    def _timeout_dissolve_event(self, image: np.ndarray, frame_path: Path,
                                started: float, *, state=None) -> dict | None:
        """精确超时解散弹窗 → 单击唯一“确定”，随后终止本场。

        这不是通用“确定”通道。只有 :mod:`timeout_dissolve` 的文案指纹、
        面板、暗遮罩、唯一青色按钮与双字形同帧通过才可能点击。处理后本场
        已解散，N1/N2/N3 必须从新场重算，因此成功后也立即停掉本次循环。
        """
        try:
            if state is None:
                state = read_timeout_dissolve(image)
        except Exception as exc:  # noqa: BLE001 - 无法排除遮挡时必须停手
            return self._stop(
                "timeout_dissolve_detect_failed",
                f"超时解散弹窗检测异常，无法安全继续：{exc}",
                {"frame": str(frame_path)},
            )
        if not state.present:
            return None
        base = {
            "ts": datetime.now().astimezone().isoformat(),
            "round_seq": self.round.seq,
            "frame": str(frame_path),
            "timeout_dissolve": state.ledger(),
        }
        if not state.complete or state.confirm_center is None:
            return self._event({
                **base, "event": "idle", "status": "abstain",
                "reason": "命中超时解散文案，专用确定按钮证据暂不完整；零点击继续观察",
            })
        try:
            outcome = self._tap.execute_timeout_dissolve_confirm(
                state.confirm_center,
                image=image,
                session_dir=self.session_dir,
            )
        except TapUnverified as exc:
            return self._stop(
                "timeout_dissolve_unverified",
                f"点超时解散确定后无法证明弹窗消失，停手不补点：{exc}",
                base,
            )
        except TapAborted as exc:
            return self._stop(
                "timeout_dissolve_aborted",
                f"超时解散确定被安全门拒绝：{exc}",
                base,
            )
        except Exception as exc:  # noqa: BLE001 - 结果未知一律停手
            return self._stop(
                "timeout_dissolve_failed",
                f"超时解散确定结果未知：{exc}",
                base,
            )
        if self.dry_run:
            return self._event({
                **base,
                "event": "dry_run_plan",
                "timeout_dissolve_confirm": outcome,
                "reason": "dry_run 只记录精确超时解散确定计划，不点击",
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })
        return self._stop(
            "match_dissolved",
            "已单击精确超时解散确定并验证弹窗消失；本场提前解散，不计16把总分，验收从新场重算",
            {**base, "timeout_dissolve_confirm": outcome},
        )

    def _promo_dismiss_event(self, image: np.ndarray, frame_path: Path,
                             base: dict, started: float, *, state=None) -> dict | None:
        """促销弹窗（「限时优惠」）→ 关掉右上角菱形 ❌。

        调用点刻意放在换局、牌河、行动行、识牌和**回合门禁求值之前**：
        弹窗一旦在屏，它下面的一切读数都不可信，先关再判。否则它会把 HUD
        持续读成 ``round_suspect`` 并在到达本通道前早退。只要弹窗在屏，
        本帧一律不进入牌局状态机；台账行里写
        ``turn_gate_skipped`` 留证"门禁没看过这一帧"。

        三条出口：

        * 未命中弹窗 → ``None``，完全不影响现有流程（含本帧计数清零）；
        * 命中但没有落点（模板分过线、结构校验不过）→ ``promo_popup_abstain``，
          **零点击**，消费本帧（弹窗在屏，门禁不许看这帧）；
        * 命中且有落点 → 走 ``execute_close_popup``（它自己要求点后连续两帧
          不再命中弹窗），成功即记 ``popup_dismiss`` 并消费本帧（下一帧重新
          判定）；关了 :data:`PROMO_CLOSE_BUDGET` 次弹窗仍在则停手留证。
        """
        try:
            if state is None:
                state = read_promo_popup(image)
        except Exception as exc:  # noqa: BLE001 - 识别异常计入异常预算
            return self._anomaly("promo_popup_detect_failed", str(exc),
                                 started, base)
        if not state.present:
            # 弹窗确实没了：同屏预算随之作废，下一张新弹窗重新起算。
            self._promo_close_count = 0
            return None
        base["promo_popup"] = state.ledger()
        # 弹窗在屏 ⇒ 本帧不进回合门禁：blocked 会清 turn_history 并直接停手，
        # 而这两种后果都是拿弹窗像素做的判定。留证写在每一行里。
        base["turn_gate_skipped"] = "promo_popup_on_screen"
        if state.close_center is None:
            # 有弹窗但没有可信落点：不猜、不点，记一条弃权台账并消费本帧。
            return self._event({
                **base,
                "event": "promo_popup_abstain", "status": "abstain",
                "suggestion": None, "suggestion_id": None,
                "reason": (f"促销弹窗命中但落点不可信（{state.reason}）："
                           "本帧零点击，且不进回合门禁"),
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })
        if self._promo_close_count >= PROMO_CLOSE_BUDGET:
            return self._stop(
                "promo_popup_stuck",
                (f"促销弹窗已连续关闭 {self._promo_close_count} 次仍在屏上："
                 "停手留证，不无限重试"),
                base,
            )
        if self.dry_run:
            try:
                plan = self._tap.execute_close_popup(
                    tuple(int(v) for v in state.close_center), image=image)
            except TapAborted as exc:
                return self._stop("promo_popup_aborted", str(exc), base)
            return self._event({
                **base, "event": "popup_dismiss", "dry_run": True,
                "tap": plan.get("tap"), "box": plan.get("box"),
                "verified": None,
                "reason": "dry_run 已识别促销弹窗；只记录关闭计划，不点击",
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })
        try:
            outcome = self._tap.execute_close_popup(
                tuple(int(v) for v in state.close_center),
                session_dir=self.session_dir)
        except TapUnverified as exc:
            return self._stop(
                "promo_popup_unverified",
                f"点关闭 ❌ 后无法证明弹窗消失，停手不补点：{exc}", base)
        except TapAborted as exc:
            return self._stop("promo_popup_aborted",
                              f"关闭促销弹窗被拒绝：{exc}", base)
        except Exception as exc:  # noqa: BLE001 - 结果未知一律停手
            return self._stop("promo_popup_failed",
                              f"关闭促销弹窗结果未知：{exc}", base)
        self._promo_close_count += 1
        return self._event({
            **base, "event": "popup_dismiss", "dry_run": False,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "verified": bool(outcome.get("verified")),
            "promo_close_count": self._promo_close_count,
            "promo_close_budget": PROMO_CLOSE_BUDGET,
            "reason": ("促销弹窗已点关闭 ❌ 并验证连续两帧不再命中；"
                       "本帧结束，下一帧重新判定"),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })

    def _match_end_event(self, image: np.ndarray, frame_path: Path,
                         base: dict, started: float) -> dict | None:
        """牌局结束 → 返回大厅（独立通道，与出牌/行动行互不借道）。

        实机（20261025 22:58，房间 604624 局数 16/16）：局数打满后是
        「牌局结束」+ 冠军横幅 + 底部三按钮。旧行为被 gate_turn 判 blocked
        直接停机，整场会话结束、必须人工点「返回大厅」。这里补上这条出口：
        只在固定版式三按钮同时成立时点最左那个，点后要求版式连续两帧消失。
        """
        if self._left_table:
            return None
        try:
            state = read_match_end(image)
        except Exception as exc:  # noqa: BLE001 - 识别异常计入异常预算
            return self._anomaly("match_end_detect_failed", str(exc), started, base)
        if not state.present:
            return None
        base["match_end"] = state.ledger()
        from match_end import finish_match_summary

        def persist_result(row):
            self._ensure_dirs()
            self._append_line(self.match_result_path,
                              json.dumps(row, ensure_ascii=False))

        result = finish_match_summary(
            image, player_name=self.player_name, serial=self.serial,
            frame_path=frame_path, score_reader=self._match_score,
            persist_result=persist_result,
            execute_leave=lambda current: self._tap.execute_leave_table(
                image=current, session_dir=self.session_dir),
            authorized=lambda: (self.allow_return_lobby
                                and not self.stopped
                                and self._stop_requested_reason is None),
            dry_run=self.dry_run,
            already_recorded=self._match_result_recorded)
        self._match_result_recorded = bool(result["recorded"])
        base.update(result)
        base["allow_return_lobby"] = self.allow_return_lobby
        base["match_result_path"] = str(self.match_result_path)
        if result["status"] == "blocked":
            return self._stop(result["gate"], result["reason"], base)
        if result["status"] in {"observe", "dry_run_plan"}:
            return self._event({
                **base,
                "event": "idle" if result["status"] == "observe" else "dry_run_plan",
                "reason": result["reason"]})
        outcome = result["leave_table"]
        player_score = result["player_score"]
        self._left_table = True
        # 已离桌：本局所有跨帧状态全部作废，绝不允许带进下一场。
        self._promo_close_count = 0
        # 离桌 = 进入「房间」这一屏，开新场通道的预算从头起算。
        self._reset_lobby_open_budget()
        self.turn_history.clear()
        self._forget_discards()
        self._single_bright.reset()
        self._bright_subset.reset()
        self._bright_subset_advice = None
        self._dim_gain_snapshot = None
        self._dim_gain_evidence = None
        self._hand_ledger.reset()
        panel_recovery.clear_state(self._panel_recovery_path)
        return self._event({
            **base, "event": "left_table", "leave_table": outcome,
            "reason": (f"16/16 整场积分已按昵称落账：{self.player_name} "
                       f"{player_score:+d}；已点「返回大厅」并验证版式连续"
                       "两帧消失，本局状态已全部作废"),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })

    # ------------------------------------------------------------------
    # 开新场（比赛场房间 → 点空座入座）
    # ------------------------------------------------------------------
    def _reset_lobby_open_budget(self) -> None:
        """开新场通道的整屏状态清零：换局 / 离桌 / 关闭开关时都要调一次。

        ``_lobby_open_taps`` 是「同一屏最多点几次」的预算，换了一屏（换局、
        重新离桌）就必须重来；已经报过的台账键也要忘掉，否则房间画面从
        「需新建桌」变成「有空桌可并」时会被旧的记录挡住。
        """
        self._lobby_open_taps = 0
        self._lobby_open_last_tick = None
        self._lobby_open_blank_frames = 0
        self._lobby_open_seen.clear()

    def _lobby_open_once(self, key: str) -> bool:
        """同一屏只报一次：房间画面一动不动停几十帧，台账不能每帧刷。"""
        if key in self._lobby_open_seen:
            return False
        self._lobby_open_seen.add(key)
        return True

    def _lobby_open_blocked(self, reason: str, detail: str,
                            base: dict) -> None:
        """``lobby_open_blocked``：写一条弃权台账，**本帧零点击**。

        ``reason`` 取值固定四选一（接线说明 §3）：
        ``cards_unreadable`` / ``low_room_cards`` / ``budget_exhausted`` /
        ``unrecognised``。同一屏同一个 reason 只记一次。
        """
        if not self._lobby_open_once(f"blocked:{reason}"):
            return
        self._write_event({
            **base,
            "ts": datetime.now().astimezone().isoformat(),
            "event": "lobby_open_blocked",
            "round_seq": self.round.seq,
            "reason": reason,
            "detail": detail,
            "lobby_open_enabled": self.lobby_open_enabled,
            "lobby_open_taps": self._lobby_open_taps,
            "lobby_open_budget": LOBBY_OPEN_BUDGET,
        })

    def _lobby_open_event(self, image: np.ndarray, frame_path: Path,
                          base: dict, started: float) -> dict | None:
        """离桌后停在比赛场房间 → 算「下一步点哪个空座」（阶段一只记不点）。

        插点刻意放在 ``_match_end_event`` 之后、``verdict.state == "blocked"``
        之前：房间画面没有手牌，``gate_turn`` 对它的归类不定（可能 blocked 也
        可能别的 code），挂进 blocked 分支会因为归类变化整条通道失联。

        门禁（缺一条就不点；完整定义见 ``_takeover/lobby_autostart_wiring.md``）：

        * **G1** ``self._left_table`` —— 没离过桌绝不碰开新场（局内误判全挡死）；
        * **G2** ``read_lobby_room(image).present`` —— 红橙横幅 / 右下绿色功能
          圆盘 / 桌位菱形栅格三锚点必须同时成立；
        * **G3** 落点：``unit==0`` + 在自己空座卡框内 + 框在桌位带内 +
          不在五个禁区（功能圆盘 / 右侧竖排 / 充值 / 闭赛分享 / 大海钓场）；
          ``lobby_autostart`` 内建一遍，``TapExecutor.execute_open_table`` 再校一遍；
        * **G4** 点后要求**连续两帧**不再是「房间且有空座」，否则
          ``TapUnverified`` 停手，**绝不补点第二下**；
        * **G5** 同屏最多 ``LOBBY_OPEN_BUDGET`` 次，两次至少隔
          ``LOBBY_OPEN_MIN_GAP_FRAMES`` 帧；
        * **G6** 房卡门：房卡余额**一个数都读不出来**
          （``session_watch`` → ``digit_templates_incomplete``），所以只在
          ``join_existing=True``（并入已有空座、不扣卡）时放行；需要新建桌
          一律记 ``lobby_open_blocked: cards_unreadable`` 并弃权，**绝不点**；
        * **G7** 授权：``lobby_open_enabled`` 默认关，关着时只记计划不点；
        * **G8** 通道：走 ``TapExecutor.execute_open_table`` 这条独立白名单，
          与出牌 / 行动行 / 离桌 / 促销弹窗 / 吃面板五条通道互不借道。
        """
        if not self._left_table:            # G1
            return None
        try:
            state = read_lobby_room(image)
            plan = plan_start(image) if state.present else None
        except Exception as exc:  # noqa: BLE001 - 识别异常计入异常预算
            return self._anomaly("lobby_open_detect_failed", str(exc),
                                 started, base)
        if not state.present or plan is None or plan.action != "join_table":
            # 不是房间（G2 不成立）/ 房间但没空座 / 等候室（交给 lobby_start）。
            # 静默等，攒够帧数才报一次 unrecognised，别无限静等也别每帧刷。
            self._lobby_open_blank_frames += 1
            if self._lobby_open_blank_frames < LOBBY_OPEN_UNRECOGNISED_FRAMES:
                return None
            self._lobby_open_blocked(
                "unrecognised",
                (f"离桌后连续 {self._lobby_open_blank_frames} 帧没能算出入座计划"
                 f"（{state.reason}）：停机等人工，不乱点"),
                base,
            )
            return None
        self._lobby_open_blank_frames = 0
        base["lobby_room"] = state.ledger()
        base["lobby_plan"] = plan.ledger()
        join_existing = bool(plan.evidence.get("join_existing", False))
        # ---- G6 房卡门：读不出余额就只并桌、绝不建桌 -------------------------
        # 建桌要扣房卡，而 session_watch.classify_watch() 对房间帧返回
        # room_cards=None / status="digit_templates_incomplete"——一个数都没有。
        # 在看不见余额时点「+」建桌，等于把房卡悄悄打光。这条写死，不设开关。
        if not join_existing:
            self._lobby_open_blocked(
                "cards_unreadable",
                ("这一下是新建桌（会扣房卡），而房卡余额读不出来"
                 "(session_watch: digit_templates_incomplete)；阶段一只并入已有"
                 "空座，本帧弃权、零点击"),
                base,
            )
            return None
        # ---- G5 预算门 -----------------------------------------------------
        if self._lobby_open_taps >= LOBBY_OPEN_BUDGET:
            self._lobby_open_blocked(
                "budget_exhausted",
                (f"同一屏已点过 {self._lobby_open_taps} 次入座（预算 "
                 f"{LOBBY_OPEN_BUDGET}），画面仍有空座：停手留证，绝不补点"),
                base,
            )
            return None
        if (self._lobby_open_last_tick is not None
                and self.tick_index - self._lobby_open_last_tick
                < LOBBY_OPEN_MIN_GAP_FRAMES):
            return None     # 间隔未到，静默等下一帧
        # ---- G7 授权门 -----------------------------------------------------
        if self.dry_run:
            try:
                plan_out = self._tap.execute_open_table(
                    tuple(int(v) for v in plan.point), image=image)
            except TapAborted as exc:
                self._lobby_open_blocked(
                    "unrecognised",
                    f"执行器复核落点未通过：{exc}", base)
                return None
            return self._event({
                **base, "event": "lobby_open_plan", "dry_run": True,
                "tap": plan_out.get("tap"), "box": plan_out.get("box"),
                "verified": None,
                "reason": "dry_run 已识别比赛场房间并入座落点；只记录计划，不点击",
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })
        if not self.lobby_open_enabled:
            if not self._lobby_open_once(
                    f"plan:{plan.action}@{tuple(plan.point)}"):
                return None
            return self._event({
                **base, "event": "lobby_open_plan", "dry_run": False,
                "authorized": False, "tap": list(plan.point),
                "box": list(plan.box) if plan.box else None, "verified": None,
                "reason": ("开新场开关未授权（lobby_open_enabled=False）："
                           "已算出该点哪个空座，只记台账，零点击"),
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })
        # ---- G8 独立通道：房间入座专用白名单，不借道任何既有通道 ------------
        try:
            outcome = self._tap.execute_open_table(
                tuple(int(v) for v in plan.point),
                session_dir=self.session_dir)
        except TapUnverified as exc:
            return self._stop(
                "lobby_open_unverified",
                f"点房间空座后无法证明已离开房间，停手不补点：{exc}", base)
        except TapAborted as exc:
            return self._stop("lobby_open_aborted",
                              f"入座点击被拒绝：{exc}", base)
        except Exception as exc:  # noqa: BLE001 - 结果未知一律停手
            return self._stop("lobby_open_failed",
                              f"入座点击结果未知：{exc}", base)
        self._lobby_open_taps += 1
        self._lobby_open_last_tick = self.tick_index
        return self._event({
            **base, "event": "lobby_open", "dry_run": False,
            "authorized": True,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "verified": bool(outcome.get("verified")),
            "lobby_open_taps": self._lobby_open_taps,
            "lobby_open_budget": LOBBY_OPEN_BUDGET,
            "reason": ("已点房间空座并入座，且点后连续两帧不再是"
                       "「房间且有空座」"),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        })

    def _ledger_reject_reason(self) -> str:
        """账本给不出读数的首要原因，进台账便于区分"没账本"和"账本不可信"。"""
        rejects = (self._hand_ledger.ledger().get("rejects") or {})
        if not rejects:
            return "无读数"
        top = sorted(rejects.items(), key=lambda item: -item[1])[:2]
        return "、".join(f"{code}×{count}" for code, count in top)

    def _dim_gain_whitelist_advice(self, image: np.ndarray, subset,
                                   result: dict, *, frame: str) -> dict | None:
        """在已确认的茶 2 多亮压暗态，从原生帧补读全手后限定白名单算牌。

        不改变通用识牌、回合亮度门禁或原生点击图；任何一项证据不足就
        清掉跨帧读数，继续走既有账本/弃权路径。
        """
        self._dim_gain_evidence = None

        def reject(reason: str, **detail):
            self._dim_gain_snapshot = None
            self._dim_gain_evidence = {
                "attempted": True,
                "outcome": "no_advice",
                "reason": reason,
                "frame": frame,
                **detail,
            }
            return None

        if (not frame or self.mode != "tea_2p"
                or result.get("mode") != "tea_2p"
                or result.get("status") != "abstain"
                or result.get("reason") != "识牌不完整，停止决策"
                or result.get("caishen_source") != "image"
                or not getattr(subset, "hit", False)
                or not getattr(subset, "multi_candidate", False)
                or getattr(subset, "unresolved", True)):
            # 非 dim-gain 适用场景是正常分流，不在每帧刷失败证据。
            self._dim_gain_snapshot = None
            return None
        evidence = getattr(subset, "evidence", None) or {}
        whitelist = [int(candidate.tile_id) for candidate in subset.candidates
                     if candidate.tile_id is not None]
        if len(whitelist) != len(subset.candidates) or len(whitelist) < 2:
            return reject("whitelist_identity_incomplete")
        caishen = result.get("caishen")
        if not caishen or (self.round.caishen and self.round.caishen != caishen):
            return reject("caishen_missing_or_conflicting")
        try:
            from assist_tea import _complete_visible_meld_groups
            from brain_v0.engine.tea.shanten import (
                best_discard_by_efficiency, expected_concealed_count,
                validate_tiles,
            )
            from tea_bot import _TEMPLATE_DIR, engine_to_vision, vision_to_engine
            from tile_vision import TILE_IDS, TileVision

            vision = TileVision(template_dir=_TEMPLATE_DIR)
            vision.dim_gain_all = True  # 仅此帧的局部实例，不设置进程环境变量
            hand = [int(tile[0]) for tile in
                    vision.recognize_hand(image, caishen_label=caishen)]
            melds = (_complete_visible_meld_groups(vision)
                     if vision.last_meld_boxes else [])
            expected = int(evidence["expected_concealed"])
            if (not vision.last_complete or len(hand) != expected
                    or len(vision.diagnostics) != expected
                    or (vision.last_meld_boxes and not melds)
                    or expected != expected_concealed_count(
                        melds=melds, drawn=True)):
                return reject(
                    "enhanced_read_incomplete",
                    expected=expected,
                    hand_count=len(hand),
                    diagnostic_count=len(vision.diagnostics),
                    last_complete=bool(vision.last_complete),
                    meld_boxes=len(vision.last_meld_boxes),
                    meld_groups=len(melds),
                )
            counts = Counter(hand)
            if any(counts[tile] < count for tile, count
                   in Counter(whitelist).items()):
                return reject("whitelist_not_in_enhanced_hand",
                              whitelist=sorted(whitelist))
            caishen_id = TILE_IDS[caishen]
            engine_hand = [vision_to_engine(tile) for tile in hand]
            engine_melds = [[vision_to_engine(tile) for tile in group]
                            for group in melds]
            engine_caishen = vision_to_engine(caishen_id)
            validate_tiles(engine_hand, caishen_id=engine_caishen,
                           melds=engine_melds)
            candidates = best_discard_by_efficiency(
                engine_hand, caishen_id=engine_caishen, melds=engine_melds)
            allowed = set(whitelist)
            choice = next(((float(score), int(engine_to_vision(int(tile))))
                           for score, tile in candidates
                           if int(engine_to_vision(int(tile))) in allowed), None)
            if choice is None:
                return reject("engine_has_no_whitelisted_candidate",
                              whitelist=sorted(allowed))
        except (KeyError, TypeError, ValueError, RuntimeError,
                OSError, cv2.error) as exc:
            # 增益补读是可选的只读证据；文件/图像读取异常不能把
            # 整个代打循环变成 anomaly，更不能降级为猜牌。
            return reject("enhanced_read_exception",
                          error_type=type(exc).__name__)

        signature = (self.round.seq, caishen, tuple(hand),
                     tuple(tuple(group) for group in melds),
                     tuple(sorted(whitelist)), choice[1])
        previous = self._dim_gain_snapshot
        streak = (previous[1] + 1 if previous and previous[0] == signature
                  and previous[2] != frame
                  else 1)
        self._dim_gain_snapshot = (signature, streak, frame)
        self._dim_gain_evidence = {
            "attempted": True,
            "outcome": ("advice" if streak >= 2 else
                        "independent_frame_pending"),
            "reason": ("independent_frames_confirmed" if streak >= 2 else
                       "need_another_independent_frame"),
            "frame": frame,
            "streak": streak,
            "whitelist": sorted(allowed),
            "choice": choice[1],
        }
        if streak < 2:
            return None
        return {"tile_id": choice[1], "score": choice[0], "hand": hand,
                "melds": melds, "whitelist": sorted(allowed),
                "candidate_count": len(candidates), "streak": streak,
                "source": "dim_gain_independent_frames"}

    def _ledger_advice(self, subset, result: dict, *,
                       frame: str = "") -> dict | None:
        """压暗帧兜底建议：用账本手牌 + 本帧亮牌白名单算出该打哪一张。

        只在本帧 ``analyze`` 已经读残（没有可用建议）时介入。白名单只是
        **可打范围**，这里也不越界：先让算牌器给出完整候选序列，再取
        「第一个落在白名单内」的牌——顺序沿用引擎自己的优劣排序，不另设
        评分口径。

        ``frame`` 是本帧的帧标识（与 ``HandLedger.remember`` 写入用的同一个
        ``str(frame_path)``）。账本只在**另一帧**写入的读数上生效：本帧刚
        ``remember`` 进去的读数不能反过来证明本帧读对了（循环论证，
        2026-10-26 Codex P0）。漏传 frame 时账本按 ``unknown_frame`` 弃权，
        本函数也返回 ``None``——绝不为了兜底率放宽这条判据。
        """
        if not getattr(subset, "hit", False):
            return None
        if getattr(subset, "unresolved", False):
            return None
        evidence = getattr(subset, "evidence", None) or {}
        try:
            expected = int(evidence.get("expected_concealed"))
        except (TypeError, ValueError):
            return None
        whitelist = [int(c.tile_id) for c in subset.candidates
                     if c.tile_id is not None]
        if not whitelist or len(whitelist) != len(subset.candidates):
            return None
        # 本帧建议本身就在白名单内：交给正常出牌通道，兜底不介入。
        # 反过来，建议落在白名单外（典型：听牌压暗后引擎只看到部分手牌，
        # 建议了一张游戏不允许打的牌）时，白名单才是真正的可打范围，必须
        # 继续往下走、由引擎自己的候选序列里挑第一张合法牌——否则整桌会
        # 一直卡在这一家，直到超时。
        if (result.get("status") == "advise"
                and result.get("suggestion_id") is not None
                and int(result["suggestion_id"]) in set(whitelist)):
            return None
        memory = self._hand_ledger.recall(
            round_seq=self.round.seq,
            caishen=self.round.caishen or result.get("caishen"),
            count=expected, whitelist=whitelist, current_frame=str(frame))
        if memory is None or memory.caishen_id is None:
            return None
        try:
            from brain_v0.engine.tea.shanten import best_discard_by_efficiency
            from tea_bot import engine_to_vision, vision_to_engine

            candidates = best_discard_by_efficiency(
                [vision_to_engine(tile) for tile in memory.tiles],
                caishen_id=vision_to_engine(int(memory.caishen_id)),
                melds=[[vision_to_engine(tile) for tile in group]
                       for group in memory.melds],
            )
        except Exception as exc:  # noqa: BLE001 - 兜底建议算不出来就弃权
            return {"error": f"{type(exc).__name__}: {exc}"}
        allowed = set(whitelist)
        for score, tile in candidates:
            vision_id = int(engine_to_vision(int(tile)))
            if vision_id in allowed:
                return {
                    "tile_id": vision_id,
                    "score": float(score),
                    "hand": list(memory.tiles),
                    "whitelist": sorted(allowed),
                    "candidate_count": len(candidates),
                    "memory_frame": memory.frame,
                }
        return None

    def _track_bright_subset_advice(self, subset, result: dict, *,
                                    advice_id: int | None = None,
                                    override: bool = False) -> dict:
        """维护多亮白名单的「建议牌连续帧」计数（返回值直接进台账）。

        规格要求「建议牌落在白名单内 **且连续 2 帧一致**」才点，所以这里按
        ``(局序号, 建议牌 id)`` 记账：白名单命中且本帧有建议才 +1；换局、
        白名单不命中、建议缺失或建议变了都清零。

        ``override=True`` 时用 ``advice_id``（压暗帧由手牌账本算出的建议）
        取代 ``result["suggestion_id"]``：两种来源走同一把"连续 2 帧"尺子，
        不因为来源不同而放宽要求。
        """
        advice = advice_id if override else result.get("suggestion_id")
        if not getattr(subset, "hit", False) or advice is None:
            self._bright_subset_advice = None
            return {"advice_id": None, "streak": 0,
                    "required": BRIGHT_ADVICE_MIN_TICKS,
                    "source": "ledger" if override else "analyze"}
        key = (self.round.seq, advice)
        previous = self._bright_subset_advice
        streak = (previous[2] + 1
                  if previous is not None and (previous[0], previous[1]) == key
                  else 1)
        self._bright_subset_advice = (key[0], key[1], streak)
        return {"advice_id": advice, "streak": streak,
                "required": BRIGHT_ADVICE_MIN_TICKS,
                "source": "ledger" if override else "analyze"}

    def _bright_subset_abstain(self, base: dict, reason: str,
                               marker: dict | None = None,
                               outcome: str | None = None) -> dict:
        """多亮白名单未确认 → 本帧弃权（零点击），台账记 gate 名与证据。"""
        if marker is not None and outcome is not None:
            marker["outcome"] = outcome
        return self._event({
            **base,
            "event": "idle",
            "status": "abstain",
            "suggestion": None,
            "suggestion_id": None,
            "gate": BRIGHT_SUBSET_GATE,
            "reason": f"多亮白名单未确认（{BRIGHT_SUBSET_GATE}）：{reason}",
        })

    def _bright_subset_fallback(self, result: dict, subset, base: dict,
                                started: float) -> dict | None:
        """多亮白名单摸切：只打「白名单 ∩ 算牌建议」那一张，否则弃权。

        白名单（亮簇剔除财神位）只是**可打范围**，从不自己决定打哪张：

        * 任一亮牌牌面读不出 → 弃权（不猜牌）；
        * 本帧没有可执行建议（``status != advise``）→ 弃权；
        * 建议牌没连续 :data:`BRIGHT_ADVICE_MIN_TICKS` 帧一致 → 弃权；
        * 建议牌不落在白名单内 → 弃权；
        * 白名单已连续 2 帧一致（由 :class:`BrightSubsetCutTracker` 保证）。

        执行沿用 ``TapExecutor`` 四道闸 + fresh 帧独立复核（白名单仍一致、
        目标牌位置未漂移），点后按「手牌相对变化」验证，不用固定绝对张数。
        """
        marker = base.setdefault("bright_subset_cut", subset.ledger())
        whitelist = [c for c in subset.candidates if c.tile_id is not None]
        if subset.unresolved or len(whitelist) != len(subset.candidates):
            return self._bright_subset_abstain(
                base, "亮牌白名单里有牌面读不出", marker,
                "tile_identity_unknown")
        if not whitelist:
            return self._bright_subset_abstain(
                base, "亮牌白名单为空", marker, "whitelist_empty")
        suggestion_id = result.get("suggestion_id")
        if result.get("status") != "advise" or suggestion_id is None:
            # 压暗态下 analyze 通常直接 abstain（"识牌不完整"）：单看白名单
            # 不足以决定打哪张。这里先让手牌账本接手——同局同张数 + 白名单
            # 是手牌子集时，账本里的完整手牌就是本帧手牌（实测 22:13 那次
            # 卡死 6 分钟正是缺这一层）。账本也给不出读数才维持弃权。
            scoped_advice = (base.get("dim_gain_advice")
                             or base.get("ledger_advice") or {})
            scoped_tile = scoped_advice.get("tile_id")
            if scoped_tile is None:
                return self._bright_subset_abstain(
                    base,
                    f"本帧没有可核对的算牌建议（status={result.get('status')!r}，"
                    f"reason={result.get('reason')!r}），压暗补读及手牌账本无可信读数"
                    f"（{self._ledger_reject_reason()}）；白名单不自行选牌",
                    marker, "no_advice")
            suggestion_id = int(scoped_tile)
            marker["advice_source"] = ("dim_gain" if base.get("dim_gain_advice")
                                       else "ledger")
            marker["scoped_advice"] = scoped_advice
            hand_tiles = [int(v) for v in (scoped_advice.get("hand") or [])]
            if hand_tiles:
                # 后续 pending_context / 台账沿用账本手牌，而不是本帧的残读。
                result = {**result, "hand_ids": hand_tiles,
                          "hand_ledger_source": True}
        else:
            marker["advice_source"] = "analyze"
        try:
            suggested = int(suggestion_id)
        except (TypeError, ValueError):
            return self._bright_subset_abstain(
                base, f"建议牌 id {suggestion_id!r} 不可解析", marker,
                "advice_unreadable")
        advice_streak = (self._bright_subset_advice[2]
                         if self._bright_subset_advice is not None else 0)
        if advice_streak < BRIGHT_ADVICE_MIN_TICKS:
            return self._bright_subset_abstain(
                base,
                f"建议牌 {suggested} 只连续 {advice_streak}/"
                f"{BRIGHT_ADVICE_MIN_TICKS} 帧，还需再等一帧确认",
                marker, "advice_unstable")
        matching = [c for c in whitelist if int(c.tile_id) == suggested]
        if not matching:
            # 建议落在白名单外：游戏根本不允许打那张。此时白名单才是真正的
            # 可打范围，用账本/引擎候选里第一张合法牌接管（_bright_subset_advice
            # 已用连续帧确认）。否则这一家会永远打不出去、整桌卡到超时。
            fallback_id = (self._bright_subset_advice[1]
                           if self._bright_subset_advice is not None else None)
            matching = [c for c in whitelist
                        if fallback_id is not None
                        and int(c.tile_id) == int(fallback_id)]
            if matching:
                marker["advice_overridden"] = suggested
                marker["advice_source"] = "whitelist_fallback"
                suggested = int(matching[0].tile_id)
        if not matching:
            marker["advice_outside_whitelist"] = suggested
            return self._bright_subset_abstain(
                base,
                f"算牌建议 {suggested} 不在亮牌白名单 "
                f"{list(subset.whitelist_ids())} 内", marker,
                "advice_outside_whitelist")
        chosen = matching[0]
        marker["chosen"] = {"tile_id": chosen.tile_id,
                            "tile_label": chosen.tile_label,
                            "box": list(chosen.box),
                            "tap_point": list(chosen.tap_point)}
        synthetic = self._synthetic_discard(
            result, int(chosen.tile_id), chosen.box,
            before_count=(subset.evidence or {}).get("expected_concealed"))
        if self.actions_done >= self.max_actions:
            return self._stop("max_actions",
                              f"达到 --max-actions={self.max_actions}", base)
        if self.dry_run:
            try:
                plan = self._tap.plan(synthetic)
            except TapAborted as exc:
                marker["outcome"] = "plan_aborted"
                marker["reason"] = str(exc)
                return self._bright_subset_abstain(
                    base, f"白名单目标牌计划被拒绝（{exc}）", marker,
                    "plan_aborted")
            self.actions_done += 1
            marker["outcome"] = "dry_run_plan"
            return self._event({
                **base, "event": "dry_run_plan", "tap": [plan.x, plan.y],
                "box": list(plan.box), "verified": None,
                "elapsed_ms": round((self._clock() - started) * 1000, 1),
            })

        expected_tile = int(chosen.tile_id)
        expected_point = (int(chosen.tap_point[0]), int(chosen.tap_point[1]))
        expected_ids = sorted(int(v) for v in subset.whitelist_ids())

        def validate_fresh(fresh_image: np.ndarray, _original: dict) -> dict:
            fresh_result = self._analyze_fresh_image(fresh_image,
                                                     "preclick_bright_subset")
            fresh = detect_bright_subset_cut(
                fresh_image, fresh_result,
                caishen_label=self.round.caishen or result.get("caishen"),
                mode=self.mode)
            if not fresh.hit or not fresh.candidates:
                raise TapAborted("fresh 帧不再命中多亮白名单")
            if sorted(int(v) for v in fresh.whitelist_ids()) != expected_ids:
                raise TapAborted("fresh 帧亮牌白名单已变化，拒绝点击")
            targets = [c for c in fresh.candidates
                       if c.tile_id is not None and int(c.tile_id) == expected_tile]
            if not targets:
                raise TapAborted("fresh 帧白名单不再包含目标牌")
            target = targets[0]
            if (abs(target.tap_point[0] - expected_point[0]) > 20
                    or abs(target.tap_point[1] - expected_point[1]) > 20):
                raise TapAborted("fresh 帧目标牌位置漂移，拒绝点击")
            return self._synthetic_discard(
                fresh_result, expected_tile, target.box,
                before_count=(fresh.evidence or {}).get("expected_concealed"))

        self._tap.pending_context = self._pending_context(
            result.get("hand_ids"), result)
        marker["outcome"] = "attempt"
        try:
            outcome = self._tap.execute(synthetic, fresh_validator=validate_fresh)
        except TapPrecheckRetryExhausted as exc:
            marker["outcome"] = "precheck_retry_exhausted"
            return self._anomaly("bright_subset_precheck", str(exc), started, base)
        except TapUnverified as exc:
            return self._stop("tap_aborted", f"多亮白名单点击后无法验证：{exc}", base)
        except TapAborted as exc:
            return self._stop("tap_aborted", f"多亮白名单被执行器拒绝：{exc}", base)
        except Exception as exc:  # noqa: BLE001
            return self._stop("tap_failed", f"多亮白名单点击结果未知：{exc}", base)
        self.actions_done += 1
        ledger_applied = self._hand_ledger.forget_tile(expected_tile)
        self._bright_subset.reset()
        self._bright_subset_advice = None
        self._dim_gain_snapshot = None
        self._dim_gain_evidence = None
        marker["outcome"] = "verified"
        event_data = {
            **base, "event": "action", "tap": outcome.get("tap"),
            "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed") and
                             outcome.get("after_count") ==
                             (outcome.get("before_count") or 0) - 1),
            "hand_ledger_discard": {
                "tile_id": expected_tile, "applied": ledger_applied},
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }
        if event_data["verified"]:
            event_data = self._complete_post_claim_after_discard(event_data)
        return self._event(event_data)

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
        event_data = {
            **base, "event": "action", "after_frame": after_path,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed")
                             and outcome.get("after_count")
                             == (outcome.get("before_count") or 0) - 1),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }
        if event_data["verified"]:
            event_data["hand_ledger_discard"] = {
                "tile_id": expected_tile,
                "applied": self._hand_ledger.forget_tile(expected_tile),
            }
            event_data = self._complete_post_claim_after_discard(event_data)
        else:
            self._stop("tap_unverified",
                       "恢复第二击后手牌数量未验证，停止自动点击", base)
        return self._event(event_data)

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
        event_data = {
            **base, "event": "action", "after_frame": after_path,
            "tap": outcome.get("tap"), "box": outcome.get("box"),
            "before_count": outcome.get("before_count"),
            "after_count": outcome.get("after_count"),
            "verified": bool(outcome.get("executed") and outcome.get("after_count")
                             == (outcome.get("before_count") or 0) - 1),
            "elapsed_ms": round((self._clock() - started) * 1000, 1),
        }
        self._last_advise_read = None
        self._hint_panel_streak = 0
        if event_data["verified"]:
            event_data["hand_ledger_discard"] = {
                "tile_id": expected_tile,
                "applied": self._hand_ledger.forget_tile(expected_tile),
            }
            event_data = self._complete_post_claim_after_discard(event_data)
        else:
            self._stop("tap_unverified", "点击后手牌数量未验证，停止自动点击", base)
        return self._event(event_data)

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
        observer = getattr(self, "_incoming_shadow_observer", None)
        if observer is not None:
            observer.call("break_chain", "runtime_discard_reset")
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

    @staticmethod
    def _verify_pass_after_overlay_underread(
        before: BoardState,
        after: BoardState,
    ) -> tuple[bool, str]:
        """只救回“过后立即摸牌、点击前被行动栏少读”的实机状态。

        行动圆盘覆盖手牌时，点击前识牌可能从合法等待张数少读成 14；
        点“过”后圆盘消失且立即轮到我方摸牌，完整手牌恢复为合法摸牌
        张数 17。这个恢复不能放进通用 L3 的 ``+0/+1`` 白名单，否则会
        连带放宽碰/吃/杠。因此这里只接受一组封闭的正证据：副露逐组
        完全不变、点击前确实少于合法等待张数、点击后恰为合法摸牌张
        数、行动圆盘实测为 0、吃候选面板不在、且没有进入结算。
        """
        if before.melds is None or after.melds is None:
            return False, "过后副露不可测，不能启用行动栏遮挡恢复"
        if before.melds != after.melds:
            return False, "过后副露发生变化，不能按行动栏遮挡恢复"
        meld_groups = len(after.melds)
        legal_wait = 16 - 3 * meld_groups
        legal_draw = legal_wait + 1
        if before.concealed_count is None or before.concealed_count >= legal_wait:
            return False, (
                f"过前暗手 {before.concealed_count} 不符合行动栏遮挡少读"
                f"（应小于 {legal_wait}）")
        if after.concealed_count != legal_draw:
            return False, (
                f"过后暗手 {after.concealed_count} 不是合法摸牌张数 "
                f"{legal_draw}")
        if after.claim_buttons != 0:
            return False, "过后行动圆盘未被实测为 0"
        if after.selection_panel is True:
            return False, "过后吃候选面板仍在，不能按已完成恢复"
        if after.settled is True:
            return False, "过后已进入结算，不能按立即摸牌恢复"
        if after.turn_owner not in (None, "me"):
            return False, "过后倒计时不属于我方，不能按立即摸牌恢复"
        return True, (
            f"过状态已验证（行动栏遮挡少读 {before.concealed_count}→"
            f"合法摸牌 {legal_draw}，副露不变、圆盘消失）")

    @staticmethod
    def _verify_pass_dimmed_underread(
        before: BoardState,
        after: BoardState,
        image: np.ndarray,
    ) -> tuple[bool, str]:
        """只救回「过」之后客户端把暗手整体压暗造成的少读。

        实机 2026-10-25 21:56（房间 604624 第 7 局）：点「过」后暗手
        16→6，16 是本副露下的合法等待张数、6 是压暗帧坏读。既有
        :meth:`_verify_pass_after_overlay_underread` 只覆盖「点击前被行动
        栏遮挡少读」这个方向；压暗是「点击后少读」，必须单独一条。

        只接受一组封闭正证据：副露逐组完全不变、暗手读数确实偏低、
        固定 ROI 压暗证据成立（与吃后共用同一把尺 ``CHI_DIMMED_*``，
        不改任何全局识牌阈值）、行动圆盘实测为 0、候选面板不在、
        没有进入结算。任一条不成立都不启用。
        """
        if before.melds is None or after.melds is None:
            return False, "过后副露不可测，不能启用压暗少读豁免"
        if before.melds != after.melds:
            return False, "过后副露发生变化，不能按压暗少读豁免"
        if before.concealed_count is None or after.concealed_count is None:
            return False, "过后暗手张数不可测，不能按压暗少读豁免"
        if after.concealed_count >= before.concealed_count:
            return False, "过后暗手未少读，不按压暗少读豁免"
        dim = AutoPlayLoop._chi_dimmed_hand_evidence(image)
        if not dim["dimmed"]:
            return False, "过后手牌不是已验证的客户端压暗态"
        if after.claim_buttons != 0:
            return False, "过后行动圆盘未被实测为 0"
        if after.selection_panel is True:
            return False, "过后吃候选面板仍在，不能按压暗少读豁免"
        if after.settled is True:
            return False, "过后已进入结算，不能按压暗少读豁免"
        return True, (
            f"过状态已验证（过后副露逐组不变、行动圆盘消失；暗手读数 "
            f"{before.concealed_count}→{after.concealed_count} 处于客户端"
            f"压暗态 p70={dim['p70']}, bright_ratio={dim['bright_ratio']}，"
            "按已知压暗坏读处理，不用该张数否决真实过牌）")

    def _verify_pass_dimmed_geometry_repair(
        self,
        before: BoardState,
        after: BoardState,
        raw: bytes,
        path: Path,
        image: np.ndarray,
    ) -> tuple[bool, str]:
        """只救回「过」之后整手压暗把手牌分割框打没造成的少读。

        实机 2026-10-27 21:00（会话 203044 第 6 局）：过点成功、圆盘
        消失，但客户端随即把整手压暗，普通读只剩 4/10。离线复测定性：
        不是分类被拒，而是分割阶段就丢了中段 6 个框（几何丢框）——
        既有「压暗少读豁免」共用的 CHI 压暗尺 ROI 含大量非牌面像素，
        对本帧 bright_ratio 实测 0.2126 > 上限 0.15 而漏检，豁免与落定
        轮询的压暗抑制因此双双未触发。

        只接受一组封闭正证据（GPT 交叉复核收紧版）：
        * 失败必须是纯张数少读：副露逐组不变、圆盘实测 0、无候选
          面板、未进结算，且标准读确实少于点击前；
        * 压暗证据只量牌面带（``PASS_DIM_FACE_ROI``）：p70 与亮像素
          比双双崩塌（实机压暗 100/0.036，正常 214/0.794）；
        * 同一张 after 帧用亮度归一化识牌实例（``dim_gain_all``）
          整手重读——仍走原 min_score/min_margin，候选只看本框
          像素，绝不参考点击前手牌挑答案（恢复与验证单向）；
        * 重读框数必须恰为点击前张数或 +1，且牌面多重集与点击前
          完全一致，或严格 +1 超集且多出的一张位于最右摸牌位；
        * 补全后的盘面重跑完整 L3「过」验证通过。
        任一条不成立都维持未证实；全局识牌阈值一个字节不改。
        """
        if before.concealed is None or before.melds is None or after.melds is None:
            return False, "过后压暗几何恢复：点击前手牌/副露不可测，不启用"
        if after.melds != before.melds:
            return False, "过后压暗几何恢复：副露发生变化，不启用"
        if after.settled is True:
            return False, "过后压暗几何恢复：已进入结算，不启用"
        if after.selection_panel is True:
            return False, "过后压暗几何恢复：候选面板仍在，不启用"
        if after.claim_buttons != 0:
            return False, "过后压暗几何恢复：行动圆盘未被实测为 0，不启用"
        if (before.concealed_count is None or after.concealed_count is None
                or after.concealed_count >= before.concealed_count):
            return False, "过后压暗几何恢复：标准读不是少读，不启用"
        x0, y0, x1, y1 = PASS_DIM_FACE_ROI
        face = cv2.cvtColor(
            image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)[:, :, 2]
        if face.size == 0:
            return False, "过后压暗几何恢复：牌面带不可测，不启用"
        p70 = float(np.percentile(face, 70))
        bright_ratio = float((face >= CHI_DIMMED_BRIGHT_V).mean())
        if p70 > PASS_DIM_P70_MAX or bright_ratio > PASS_DIM_BRIGHT_RATIO_MAX:
            return False, (
                f"过后压暗几何恢复：牌面带压暗证据不成立"
                f"（p70={p70:.1f}，亮像素 {bright_ratio:.3f}），不启用")
        result = self._analyze(raw, path)
        caishen_label = (
            result.get("caishen") if isinstance(result, dict) else None)
        from tea_bot import _TEMPLATE_DIR, vision_to_engine
        from tile_vision import TileVision

        vision = TileVision(template_dir=_TEMPLATE_DIR)
        vision.dim_gain_all = True
        tiles = vision.recognize_hand(image, caishen_label=caishen_label)
        n_before = len(before.concealed)
        if len(tiles) not in (n_before, n_before + 1):
            return False, (
                f"过后压暗几何恢复：归一化重读框数 {len(tiles)} "
                f"≠ {n_before}/+1，不启用")
        from collections import Counter

        engine_tiles = [vision_to_engine(int(t[0])) for t in tiles]
        want = Counter(int(t) for t in before.concealed)
        got = Counter(engine_tiles)
        added = got - want
        removed = want - got
        if removed:
            return False, (
                f"过后压暗几何恢复：重读多重集缺 "
                f"{sorted(removed.elements())}，不启用")
        if added:
            if sum(added.values()) != 1:
                return False, "过后压暗几何恢复：重读多出不止一张，不启用"
            extra_id = next(iter(added))
            rightmost = max(tiles, key=lambda t: t[1])
            if vision_to_engine(int(rightmost[0])) != extra_id:
                return False, (
                    "过后压暗几何恢复：多出的一张不在最右摸牌位，不启用")
        completed = tuple(sorted(engine_tiles))
        repaired = BoardState(
            concealed_count=len(completed),
            melds=after.melds,
            claim_buttons=after.claim_buttons,
            settled=after.settled,
            turn_owner=after.turn_owner,
            selection_panel=after.selection_panel,
            note="过后压暗几何恢复重读",
            concealed=completed,
        )
        ok, why = verify_after_click(before, repaired, ACTION_PASS)
        if not ok:
            return False, f"过后压暗几何恢复后完整验证仍不通过：{why}"
        return True, (
            f"过状态已验证（path=pass_dim_geometry_repair，激活：牌面带 "
            f"p70={p70:.1f}、亮像素 {bright_ratio:.3f}；归一化重读 "
            f"{len(tiles)} 框，多重集与点击前"
            f"{'完全一致' if not added else '严格 +1 超集'}，"
            f"完整 L3 复核通过）")

    @staticmethod
    def _verify_pass_new_window(
        before: BoardState,
        after: BoardState,
    ) -> tuple[bool, str]:
        """执行器已证明「同槽位新窗口」时，「过」的 L3 追加判据。

        2 人局连续行动时，下一个行动窗口的圆盘落在**完全相同**的槽位
        （实测 2026-10-25 21:54：点击前 吃+过 → 点击后 杠+过）。执行器
        已经用「行动行以外画面推进」证明这一击送达（
        ``tap_executor.CLAIM_OUTSIDE_ACTION_BANDS``），L3 就**不能**再要求
        「圆盘消失」——新窗口的圆盘本来就在同一槽位。

        这里只补一组封闭正证据：副露逐组不变、暗手相对变化 0/+1、未结算、
        且确实实测到新窗口的圆盘（``claim_buttons > 0``；若为 0 则走原有
        的「圆盘已消失」路径，不经过本条）。
        """
        if before.melds is None or after.melds is None:
            return False, "同槽位新窗口：副露不可测，不能按新窗口放行"
        if before.melds != after.melds:
            return False, "同槽位新窗口：副露发生变化，不能按新窗口放行"
        if before.concealed_count is None or after.concealed_count is None:
            return False, "同槽位新窗口：暗手张数不可测，不能按新窗口放行"
        gap = after.concealed_count - before.concealed_count
        if gap not in (0, 1):
            return False, (f"同槽位新窗口：暗手 {before.concealed_count}→"
                           f"{after.concealed_count} 不在 0/+1 内")
        if after.settled is True:
            return False, "同槽位新窗口：已进入结算，不走豁免"
        if not after.claim_buttons:
            return False, "同槽位新窗口：未实测到新窗口圆盘，不走豁免"
        return True, (
            f"过状态已验证（同槽位新窗口）：执行器已证明点击送达且牌局推进，"
            f"新行动窗口圆盘数 {after.claim_buttons}、副露逐组不变、暗手 "
            f"{before.concealed_count}→{after.concealed_count}（0/+1 内），"
            "下一步交给新窗口自己的决策")

    def _verify_kong_dimmed_box_repair(
        self,
        before: BoardState,
        after: BoardState,
        raw: bytes,
        path: Path,
        image: np.ndarray,
        *,
        target_tile: int | None,
        kong_kind: str | None,
    ) -> tuple[bool, str]:
        """只救回「杠后复核帧中段局部压暗」造成的暗手少读。

        实机 2026-10-27 17:45（房间 405891 第 1 局，明杠北）：杠后帧
        副露 [[41,42,43],[64,64,64,64]] 读对，暗手中段 5 框
        （x 1021–1480）几何对齐正常、被局部压暗打坏字形分数
        （0.366–0.690），精确多重集缺 {29:2, 43:1, 47:1, 49:1}
        停手。这与「过」后整手压暗（16→6）不同：本帧其余暗手明亮
        可读，既有整手压暗豁免的 ROI 尺（bright_ratio）量不出中段
        局部压暗，因此单独一条。

        只接受一组封闭正证据，任一不成立都维持未证实：
        * 标准读的副露/暗手可测，且画面不在结算/候选面板态；
        * 压暗方向独立成立（激活门，不看牌面内容）：本帧诊断里
          被拒暗手框的亮度（V 中位数）比同帧已接受暗手框低至少
          2.0——实机帧实测被拒框 87.6–91.8、已接受框 92.9–94.0；
        * 只对被拒框，用同一 classify_crop 在亮度归一化
          （dim_gain_all 的逐框口径）下重读——每框仍须独立过原
          min_score/min_margin，已被接受的牌一律不重读；候选
          选择只看本框像素，绝不参考验证所缺的多重集（恢复与
          验证单向：先固定恢复结果，再跑状态迁移验证）；
        * 每个被拒框都必须恢复（不接受部分修复）；
        * 补全后的暗手重跑完整杠验证（精确多重集、副露迁移、
          张数差）通过。杠验证本身已区分补摸前/后两种合法相位
          （extras 0/1），本路径不放宽任何一条。全局识牌阈值
          一个字节不改。
        """
        if after.concealed is None or after.melds is None:
            return False, "杠后压暗框修复：标准读副露/暗手不可测，不启用"
        if after.settled is True or after.selection_panel is True:
            return False, "杠后压暗框修复：画面处于结算/候选面板态，不启用"
        result = self._analyze(raw, path)
        if not isinstance(result, dict):
            return False, "杠后压暗框修复：复核帧分析无结构化结果，不启用"
        diags = result.get("vision_diagnostics") or []
        failed = [
            d for d in diags
            if d.get("region") == "concealed"
            and not d.get("accepted") and d.get("box")
        ]
        if not failed:
            return False, "杠后压暗框修复：本帧无被拒暗手框，不启用"
        accepted = [
            d for d in diags
            if d.get("region") == "concealed"
            and d.get("accepted") and d.get("box")
        ]
        if not accepted:
            return False, "杠后压暗框修复：本帧无已接受暗手框作亮度参照，不启用"

        def _median_v(diag_list: list) -> float:
            values = []
            for diag in diag_list:
                x0, y0, x1, y1 = (int(v) for v in diag["box"])
                crop = image[y0:y1, x0:x1]
                if crop.size:
                    values.append(float(
                        cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 2].mean()))
            return float(np.median(values)) if values else float("nan")

        med_failed = _median_v(failed)
        med_accepted = _median_v(accepted)
        if not (med_failed + 2.0 <= med_accepted):
            return False, (
                f"杠后压暗框修复：压暗方向证据不成立（被拒框 V 中位数 "
                f"{med_failed:.1f}，已接受框 {med_accepted:.1f}），不启用")
        from tea_bot import _TEMPLATE_DIR
        from tile_vision import TileVision

        vision = TileVision(template_dir=_TEMPLATE_DIR)
        vision.dim_gain_all = True
        caishen_label = result.get("caishen") or None
        recovered: list[int] = []
        recovered_scores: list[float] = []
        for diag in failed:
            x0, y0, x1, y1 = (int(v) for v in diag["box"])
            retry = vision.classify_crop(
                image[y0:y1, x0:x1], caishen_label=caishen_label)
            if not retry.get("accepted") or retry.get("tile_id") is None:
                return False, (
                    f"杠后压暗框修复：框 {diag['box']} 亮度补偿重读仍未过"
                    f"原门槛（{retry.get('label')}/"
                    f"{float(retry.get('score') or 0.0):.3f}），维持未证实")
            recovered.append(int(retry["tile_id"]))
            recovered_scores.append(float(retry.get("score") or 0.0))
        completed = tuple(sorted(
            [int(t) for t in after.concealed] + recovered))
        repaired = BoardState(
            concealed_count=len(completed),
            melds=after.melds,
            claim_buttons=after.claim_buttons,
            settled=after.settled,
            turn_owner=after.turn_owner,
            selection_panel=after.selection_panel,
            note="杠后压暗框修复重读",
            concealed=completed,
        )
        ok, why = verify_after_click(
            before, repaired, ACTION_KONG,
            target_tile=target_tile, kong_kind=kong_kind)
        if not ok:
            return False, f"杠后压暗框修复后精确验证仍不通过：{why}"
        return True, (
            f"杠状态已验证（path=kong_dim_box_repair，激活：被拒框 "
            f"{len(failed)} 个、V 中位数 {med_failed:.1f} < 已接受 "
            f"{med_accepted:.1f}；仅对被拒框做亮度补偿重读、全部过原"
            f"门槛恢复 {sorted(recovered)}（最低分 "
            f"{min(recovered_scores):.3f}），补全后精确多重集验证通过）")

    def _verify_claim_settled(
        self,
        before: BoardState,
        action: str,
        *,
        tag: str,
        note: str,
        target_tile: int | None = None,
        kong_kind: str | None = None,
        extra_check=None,
        accept_new_window: bool = False,
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
        白名单口径保留）。``extra_check(board, verified, reason, image)``
        可在每帧验证后追加判据（吃链用它核对新增副露组 == 所选组，
        并读取吃后专用压暗证据）。

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
                from match_end import precise_settlement_evidence
                settlement_proven, settlement_proof = (False, {})
                if action == ACTION_PASS:
                    settlement_proven, settlement_proof = precise_settlement_evidence(
                        image, round_probe=self._round_transition.probe)
                if action == ACTION_PASS and settlement_proven:
                    verified, reason = True, (
                        "过后已通过明确结算正证据（下一局三重门或同帧16/16），"
                        "按已验证终局迁移完成")
                elif action == ACTION_PASS and board.settled is True:
                    # Generic settled includes renew/invite/dissolve modals.
                    # Those masks cannot prove that the requested pass happened.
                    verified, reason = False, "点后仅见模态/压暗，未证明明确结算"
                else:
                    verified, reason = verify_after_click(
                        before, board, action,
                        target_tile=target_tile, kong_kind=kong_kind)
                if action == ACTION_PASS and not verified:
                    recovered, recovery_reason = (
                        self._verify_pass_after_overlay_underread(before, board))
                    if not recovered:
                        # 反方向：客户端在「过」之后把暗手整体压暗，
                        # 分类器少读（实机 16→6）。同样只认封闭正证据。
                        recovered, recovery_reason = (
                            self._verify_pass_dimmed_underread(
                                before, board, image))
                    if not recovered:
                        # 几何恢复：整手压暗把分割框打没（分类之前
                        # 就丢框，既有两条豁免都不覆盖），同帧用亮度
                        # 归一化实例整手重读、多重集证明迁移（实机
                        # 2026-10-27 21:00，10→4）。
                        recovered, recovery_reason = (
                            self._verify_pass_dimmed_geometry_repair(
                                before, board, raw, path, image))
                    if not recovered and accept_new_window:
                        # 执行器已证明"旧窗口关闭 + 新窗口同槽位"：牌局推进了，
                        # 新窗口的圆盘本来就在同一槽位，不能再要求圆盘消失。
                        recovered, recovery_reason = (
                            self._verify_pass_new_window(before, board))
                    if recovered:
                        verified, reason = True, recovery_reason
                if action == ACTION_KONG and not verified:
                    recovered, recovery_reason = (
                        self._verify_kong_dimmed_box_repair(
                            before, board, raw, path, image,
                            target_tile=target_tile, kong_kind=kong_kind))
                    if recovered:
                        verified, reason = True, recovery_reason
                if extra_check is not None:
                    verified, reason = extra_check(
                        board, verified, reason, image)
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
                "settlement_proof": settlement_proof,
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
                # 例外（state-aware，GPT 交叉复核裁决）：「过」的复核
                # 帧处于整手压暗态时，坏读本身是确定性的、签名必然
                # 稳定，但行动圆盘精灵可能只是残留未消（实机
                # 2026-10-27 18:31 残留 ≥2.7s）——稳定 ≠ 终局，不许
                # 提前判负，继续有界轮询等真实状态迁移或轮询耗尽。
                if not (action == ACTION_PASS
                        and self._chi_dimmed_hand_evidence(image)["dimmed"]):
                    return False, reason, board, samples
                samples[-1]["settle_early_exit_suppressed"] = "pass_dimmed"
            previous_signature = signature
        return False, (f"点后状态 {CLAIM_SETTLE_ATTEMPTS} 帧内未落定，"
                       f"fail-closed：{reason}"), board, samples

    # ------------------------------------------------------------------
    # 行动（吃/碰/杠/胡/过）决策闸门
    # ------------------------------------------------------------------
    def _new_action_id(self, kind: str) -> str:
        return (
            f"{self.serial}:r{self.round.seq}:t{self.tick_index}:"
            f"{kind}:{uuid.uuid4().hex[:12]}"
        )

    def _post_claim_ledger(self) -> dict | None:
        pending = self._post_claim_pending
        return None if pending is None else pending.ledger(self._clock())

    def _post_claim_watchdog(
        self,
        frame_path: Path,
        started: float,
    ) -> dict | None:
        """鸣牌已成但尚未出牌：有界等待，超时必须显式停手。"""
        pending = self._post_claim_pending
        if pending is None:
            return None
        pending.observed_ticks += 1
        now = self._clock()
        evidence = pending.ledger(now)
        evidence["frame"] = str(frame_path)
        if pending.round_seq != self.round.seq:
            return self._stop(
                "post_claim_round_mismatch",
                "鸣牌后续出牌事务跨局，拒绝沿用",
                {"post_claim_continuation": evidence},
            )
        if (pending.observed_ticks > POST_CLAIM_DISCARD_MAX_TICKS
                or now - pending.started_at > POST_CLAIM_DISCARD_TIMEOUT_S):
            return self._stop(
                "post_claim_discard_timeout",
                (f"{pending.action} 已验证但未在有界时间内完成后续出牌；"
                 "停止自动点击，避免静默卡到被踢"),
                {"post_claim_continuation": evidence,
                 "elapsed_ms": round((now - started) * 1000, 1)},
            )
        return None

    def _begin_post_claim_continuation(
        self,
        action: str,
        action_id: str,
        target_tile: int | None,
    ) -> PostClaimContinuation:
        """鸣牌 L3 通过后清掉旧回合证据，只保留待 fresh 重算的事务。"""
        pending = PostClaimContinuation(
            action=action,
            action_id=action_id,
            target_tile=target_tile,
            round_seq=self.round.seq,
            started_tick=self.tick_index,
            started_at=self._clock(),
            supplement_required=(action == ACTION_KONG),
        )
        self._post_claim_pending = pending
        self.turn_history.clear()
        self._last_result = None
        self._last_advise_read = None
        self._hint_panel_streak = 0
        self._hint_panel_round = None
        self._single_bright.reset()
        self._bright_subset.reset()
        self._bright_subset_advice = None
        self._dim_gain_snapshot = None
        self._dim_gain_evidence = None
        self._hand_ledger.reset()
        self._forget_discards()
        self._chi_abstain_key = None
        panel_recovery.clear_state(self._panel_recovery_path)
        return pending

    def _verified_claim_action_event(
        self,
        payload: dict,
        *,
        action: str,
        target_tile: int | None,
    ) -> dict:
        """给已验证的行动点击落统一 action_id，并启动/闭合后续事务。"""
        action_id = self._new_action_id(f"claim-{action}")
        event: dict[str, Any] = {
            "event": "action",
            "action_id": action_id,
            "action_source": "bot",
            "action_kind": "claim",
            "claim": payload,
        }
        self.actions_done += 1
        if action in POST_CLAIM_DISCARD_ACTIONS:
            pending = self._begin_post_claim_continuation(
                action, action_id, target_tile)
            event["post_claim_continuation"] = pending.ledger(self._clock())
        elif action == ACTION_HU and self._post_claim_pending is not None:
            pending = self._post_claim_pending
            event["continuation_of"] = pending.action_id
            event["post_claim_completed_by"] = "verified_hu"
            event["post_claim_continuation"] = {
                **pending.ledger(self._clock()), "status": "completed_by_hu"}
            self._post_claim_pending = None
        return event

    def _complete_post_claim_after_discard(self, event: dict) -> dict:
        """把已验证的出牌与先前鸣牌关联；只有这一步后整链才算完成。"""
        pending = self._post_claim_pending
        if pending is None or event.get("verified") is not True:
            return event
        event["continuation_of"] = pending.action_id
        event["post_claim_continuation"] = {
            **pending.ledger(self._clock()),
            "status": "completed_by_verified_discard",
        }
        self._post_claim_pending = None
        return event

    def _claim_payload_from_ledger(
        self,
        analysis: dict | None,
        frame_path: Path,
    ) -> tuple[dict, dict]:
        """行动动画少读暗手时，按严格同局证据恢复上一帧完整手牌。

        账本只补 ``hand_ids``，绝不补来牌；吃碰杠胡仍必须使用当前帧的
        圆盘模板和当前牌河/闪光证据。允许恢复必须同时满足：茶2、当前帧
        确有非空残读、少于等待态张数、账本来自另一帧、同局、同财神、
        账本张数恰为等待态、当前残读是账本的多重集子集、副露完全一致。
        """
        payload = dict(analysis) if isinstance(analysis, dict) else {}
        evidence: dict[str, Any] = {
            "applied": False,
            "reason": "analysis_missing",
            "current_frame": str(frame_path),
        }
        if not payload:
            return payload, evidence
        current = HandState.from_analysis(payload)
        if current.player_count != 2:
            evidence["reason"] = "not_tea2"
            return payload, evidence
        raw_hand = payload.get("hand_ids") or []
        if (not isinstance(raw_hand, (list, tuple))
                or not raw_hand
                or any(isinstance(v, bool) or not isinstance(v, int)
                       for v in raw_hand)):
            evidence["reason"] = "current_hand_unreadable"
            return payload, evidence
        current_ids = tuple(int(v) for v in raw_hand)
        expected = int(current.concealed_expected)
        evidence.update({
            "current_count": len(current_ids),
            "expected_count": expected,
        })
        if len(current_ids) >= expected:
            evidence["reason"] = "current_hand_not_underread"
            return payload, evidence

        raw_melds = payload.get("melds") or []
        if not isinstance(raw_melds, (list, tuple)):
            evidence["reason"] = "current_melds_unreadable"
            return payload, evidence
        try:
            current_melds = tuple(
                tuple(sorted(int(v) for v in group))
                for group in raw_melds
            )
        except (TypeError, ValueError):
            evidence["reason"] = "current_melds_unreadable"
            return payload, evidence
        caishen = payload.get("caishen") or self.round.caishen
        if not isinstance(caishen, str) or not caishen:
            evidence["reason"] = "caishen_unknown"
            return payload, evidence
        memory = self._hand_ledger.recall(
            round_seq=self.round.seq,
            caishen=caishen,
            count=expected,
            whitelist=current_ids,
            current_frame=str(frame_path),
        )
        if memory is None:
            evidence["reason"] = "ledger_recall_rejected"
            return payload, evidence
        # HandLedger 的 whitelist 契约按集合判定；行动恢复再收紧为多重集，
        # 防止本帧把同一牌多读一张而集合仍然“看似是子集”。
        current_counts = Counter(current_ids)
        memory_counts = Counter(memory.tiles)
        if any(current_counts[tile] > memory_counts[tile]
               for tile in current_counts):
            evidence["reason"] = "current_hand_not_multiset_subset"
            evidence["memory_frame"] = memory.frame
            return payload, evidence
        if current_melds != memory.melds:
            evidence["reason"] = "meld_mismatch"
            evidence["memory_frame"] = memory.frame
            evidence["current_melds"] = [list(group) for group in current_melds]
            evidence["memory_melds"] = [list(group) for group in memory.melds]
            return payload, evidence
        payload["hand_ids"] = list(memory.tiles)
        payload["melds"] = [list(group) for group in memory.melds]
        evidence.update({
            "applied": True,
            "reason": "independent_complete_hand",
            "memory_frame": memory.frame,
            "memory_count": memory.count,
            "caishen": caishen,
        })
        return payload, evidence

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
        payload, hand_recovery = self._claim_payload_from_ledger(
            analysis, frame_path)
        if self._discard_state is not None and self._discard_trusted:
            payload["discards"] = self._discard_state
        hand = HandState.from_analysis(payload)
        # decide_action 内部会做一次来牌/归属仲裁，但只把结果封装进
        # ActionDecision，不会把“已恢复 incoming_tile 的 HandState”返回给
        # 吃候选面板。这里先显式 resolve 一次并沿整条 claim 链复用，避免
        # L2 已知来牌、进入选组后又退回 incoming_tile=None。
        resolved_hand, incoming_resolution = resolve_incoming(
            image, hand, self._claim_policy)
        decision = decide_action(
            claim_state, resolved_hand, self._claim_policy, image=image)
        if (decision is not None and decision.action == ACTION_CHI
                and self._chi_abstain_key is not None
                and self._chi_abstain_key == self._chi_window_key(resolved_hand)):
            # 同一行动窗里吃链已弃权过一次（选组弃权/点组被拒/面板读
            # 不全）：不再重复点吃，改用关吃配置重新决策（通常落到
            # 「过」），避免 点吃→取消→点吃 的死循环。
            decision = decide_action(
                claim_state, resolved_hand,
                replace(self._claim_policy, chi_enabled=False), image=image)
        if self._claim_policy.incoming_shadow_enabled:
            try:
                from incoming_shadow import ShadowConfig, observe_incoming_shadow
                observer = self._ensure_incoming_shadow_observer()
                producer = (observer.call("prepare_claim", hand_recovery=hand_recovery)
                            if observer is not None else None)
                # Shadow must see THIS frame's raw hand, never the ledger repair.
                shadow_hand = HandState.from_analysis(analysis)
                shadow = observe_incoming_shadow(
                    image, shadow_hand, incoming_resolution, config=ShadowConfig(enabled=True),
                    context=producer.get("context") if isinstance(producer, dict) else None,
                    actual_runtime_decision=decision.action if decision is not None else None)
                if isinstance(shadow, dict):
                    shadow["producer_evidence"] = producer or {
                        "producer_blockers": ["context_producer_unavailable"],
                        "strict_four_gates": False, "execution_eligible": False}
                incoming_resolution["shadow"] = shadow
            except Exception as exc:
                incoming_resolution["shadow"] = {
                    "shadow_only": True, "would_accept": False, "identity_confirmed": False,
                    "execution_eligible": False, "strict_four_gates": False,
                    "blockers": ["shadow_context_error:" + type(exc).__name__],
                    "actual_runtime_decision": decision.action if decision is not None else None}
        if decision is None:
            return {"claim": {
                "status": "abstain",
                "frame": str(frame_path),
                "incoming_resolution": incoming_resolution,
                "known": bool(claim_state.known),
                "buttons": [button.kind for button in claim_state.buttons],
                "hand_recovery": hand_recovery,
                "reason": claim_state.reason,
            }}
        payload = decision.ledger()
        payload["hand_recovery"] = hand_recovery
        payload["incoming_resolution"] = incoming_resolution
        payload["frame"] = str(frame_path)
        payload["status"] = ("blocked" if decision.blocked_reason else
                             "ready" if decision.executable else
                             "awaiting_human_confirm")
        payload["claim_reason"] = claim_state.reason
        pending = self._post_claim_pending
        if pending is not None:
            payload["post_claim_continuation"] = pending.ledger(self._clock())
            # 吃/碰之后只能等 fresh 手重算并出牌；刚完成的行动圆盘若还残留，
            # 不能重复点击。杠后唯一例外是补牌直接成胡，且仍须走胡自己的
            # 模板、引擎与 L3 门禁。
            allow_kong_hu = (
                pending.supplement_required and decision.action == ACTION_HU)
            if not allow_kong_hu:
                payload["status"] = "awaiting_post_claim_discard"
                payload["blocked_reason"] = (
                    f"{pending.action} 已验证，等待 fresh 重算并完成后续出牌；"
                    "本帧不重复点击行动圆盘")
                return {"claim": payload}
        if not (decision.executable and self.claim_auto_click):
            return {"claim": payload}
        payload["auto_click"] = {
            "requested": True, "executed": False,
            "channel_ready": CLAIM_TAP_CHANNEL_READY,
        }
        if not CLAIM_TAP_CHANNEL_READY:
            payload["auto_click"]["reason"] = "行动圆盘点击通道未启用"
            return {"claim": payload}
        if self.actions_done >= self.max_actions:
            payload["status"] = "blocked"
            payload["blocked_reason"] = (
                f"达到 --max-actions={self.max_actions}；本帧零点击")
            payload["auto_click"]["reason"] = payload["blocked_reason"]
            return {"claim": payload}
        if (decision.action in POST_CLAIM_DISCARD_ACTIONS
                and self.actions_done + 2 > self.max_actions):
            payload["status"] = "blocked"
            payload["blocked_reason"] = (
                "动作预算不足以原子完成鸣牌及后续必出牌；本帧零点击")
            payload["auto_click"]["reason"] = payload["blocked_reason"]
            return {"claim": payload}

        if decision.action == ACTION_CHI:
            return self._chi_chain_event(
                decision, resolved_hand, claim_state, image, frame_path, payload)

        before = BoardState.from_hand_state(
            resolved_hand, claim_buttons=len(claim_state.buttons), settled=False,
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
        action_engine = (
            decision.engine.get(decision.action, {})
            if isinstance(decision.engine, dict) else {})
        kong_kind = (
            action_engine.get("kong_kind")
            if decision.action == ACTION_KONG
            and isinstance(action_engine, dict) else None)
        verified, verify_reason, _after_board, settle_samples = (
            self._verify_claim_settled(
                before, decision.action, tag="after_claim",
                note="行动点击后 fresh 状态",
                target_tile=decision.target_tile,
                kong_kind=kong_kind,
                accept_new_window=bool(
                    outcome.get("same_slot_new_window"))))
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
        return self._verified_claim_action_event(
            payload, action=decision.action,
            target_tile=decision.target_tile)

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
        # Every BOT-originated gameplay action gets an immutable provenance
        # marker before either ledger sees it.  Review code must never infer a
        # human click merely because an older row omitted these fields.
        if event.get("event") == "action":
            serial = str(getattr(self, "serial", "unknown") or "unknown")
            round_seq = getattr(getattr(self, "round", None), "seq", "unknown")
            tick = getattr(self, "tick_index", "unknown")
            event.setdefault("action_source", "bot")
            event.setdefault(
                "action_id",
                f"{serial}:r{round_seq}:t{tick}:{uuid.uuid4().hex[:12]}",
            )
            # A verified in-hand action after an accepted ``下一局`` proves
            # that the old settlement is over.  The visual round detector can
            # miss the first dealt frame and leave ``round.seq`` unchanged;
            # without releasing that *old* local-key lock the next settlement
            # is falsely rejected forever as a duplicate.
            if event.get("verified") is True:
                note_gameplay = getattr(
                    getattr(self, "_round_transition", None),
                    "note_verified_gameplay", None)
                if callable(note_gameplay):
                    event["round_transition_rearmed"] = bool(
                        note_gameplay(getattr(self.round, "seq", None)))
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
        dim_ok, dim_reason = self._chi_dimmed_completion_verdict(
            before, board, image, group=new_groups[0])
        return (board, dim_reason) if dim_ok else None

    def _chi_dimmed_completion_verdict(
        self,
        before: BoardState,
        after: BoardState,
        image: np.ndarray,
        *,
        group=None,
    ) -> tuple[bool, str]:
        """吃后压暗态专用 L3：只认强结构证据，不改全局识牌阈值。"""
        dim = self._chi_dimmed_hand_evidence(image)
        if not dim["dimmed"]:
            return False, "吃后手牌不是已验证的客户端压暗态"
        if after.claim_buttons != 0:
            return False, "吃后行动圆盘仍在"
        if after.selection_panel is not False:
            return False, "吃后候选面板仍在或不可确认"
        new_groups = self._new_meld_groups(before, after)
        if len(new_groups) != 1 or not self._is_engine_sequence(new_groups[0]):
            return False, "吃后未确认唯一新增合法顺子副露"
        if (group is not None
                and tuple(new_groups[0]) != tuple(
                    sorted(int(v) for v in group))):
            return False, (
                f"吃后新增副露组 {list(new_groups[0])} 与所选组 "
                f"{sorted(int(v) for v in group)} 不一致")
        if before.meld_group_count is None or after.meld_group_count is None:
            return False, "吃后副露组数不可测"
        if after.meld_group_count - before.meld_group_count != 1:
            return False, "吃后副露组数未增加 1"
        if before.meld_tile_total is None or after.meld_tile_total is None:
            return False, "吃后副露张数不可测"
        if after.meld_tile_total - before.meld_tile_total != 3:
            return False, "吃后副露张数未增加 3"
        expected = (before.concealed_count - 2
                    if before.concealed_count is not None else None)
        if (expected is None or after.concealed_count is None
                or after.concealed_count >= expected):
            return False, "暗手读数未呈现吃后压暗少读特征"
        reason = (
            "吃已由结构证据确认：新增合法顺子副露 +1组/+3张、行动圆盘消失、"
            "候选面板消失；暗手处于客户端压暗态 "
            f"(p70={dim['p70']}, bright_ratio={dim['bright_ratio']})，"
            f"当前暗手读数 {after.concealed_count} 低于应有 {expected}，"
            "按已知压暗坏读处理，不用该张数否决真实成吃")
        return True, reason

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
        return self._verified_claim_action_event(
            payload, action=ACTION_CHI,
            target_tile=payload.get("target_tile"))

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
        def _group_check(board, ok, why, current_image):
            checked, checked_reason = self._chi_group_verdict(
                before, board, choice.group, ok, why)
            if checked:
                return checked, checked_reason
            dim_ok, dim_reason = self._chi_dimmed_completion_verdict(
                before, board, current_image, group=choice.group)
            if dim_ok:
                return True, dim_reason
            return False, checked_reason

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
        return self._verified_claim_action_event(
            payload, action=ACTION_PASS, target_tile=None)

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
