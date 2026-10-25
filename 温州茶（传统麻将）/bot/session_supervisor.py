# -*- coding: utf-8 -*-
"""代打会话常驻监督程序（session supervisor）——只分类、只出计划，不执行。

为什么需要它（2026-10-25 实证）：游戏对闲置零容忍，代打循环停手约 4 分钟
无人出牌，整桌被踢回比赛场大厅。循环本身 fail-closed 停手是对的（点后验证
误判、结算面板、胡提示面板冻结、财神未知都应该停），缺的是一个常驻监督者：
发现停手 → 判读画面状态 → 产出恢复计划（重拉循环 / 过结算 / 重进桌），
把「停手」从终点变成可恢复事件。

本模块的边界（硬约束）：
- **只读**：只读会话台账（actions.jsonl）与注入的帧，不碰 ADB、不启停进程。
- **只产计划**：:func:`build_plan` 返回 JSON 可序列化的计划 dict；执行层
  （executor）由主线集成时注入，本模块内没有任何执行实现。
- ``--execute`` 缺省 False；即便打开，没有注入 executor 也拒绝执行。
- 计划里的一切坐标都是「名义坐标」（今晚实测值），执行层启用时必须以
  执行时帧复核后才许点击（不许盲点坐标）。

分类口径（全部有今晚/昨日存帧实测标定，见 tests/test_session_supervisor.py）：
- SETTLEMENT：复用 turn_gate.detect_settled_screen 的**双信号**口径
  （central_panel + bottom_blackout 同时成立）。实测结算帧 panel 14.2~14.3%、
  bottomV 23~30。
- HINT_PANEL：胡提示面板——central_panel **单**信号 + 手牌带可读
  （bottomV ≥ 100，实测 172~175）+ 桌面绿毡仍占主导（green ≥ 0.35）。
  实测 panel 6.3%~18.3%（单行/双行两版式），与结算的根本区别是**没有
  bottom_blackout**：面板是非交互提示条，手牌带完整可见。
- LOBBY：比赛场大厅——暖色占比 ≥ 0.45（实测 0.72）且绿毡 ≤ 0.15
  （实测 0.02）；对局帧 warm ≤ 0.19、green ≥ 0.62，两边不重叠。
- CLAIM_WINDOW：claim_vision.read_claims 实测到行动圆盘（known=True）。
- IN_GAME / LOOP_STOPPED：画面是对局（green ≥ 0.35）时由台账健康度区分：
  台账尾部有 stop 事件、或距最后事件静默 > 60s → LOOP_STOPPED；
  台账缺失/无可解析时间 → UNKNOWN（证据不足，fail-closed 只告警）。
- UNKNOWN：帧缺失/尺寸未验证/任何证据不足，一律 fail-closed。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import cv2
import numpy as np

from claim_vision import read_claims
from session_watch import detect_waiting_room
from turn_gate import detect_settled_screen

__all__ = [
    "SessionState",
    "Classification",
    "classify",
    "build_plan",
    "load_ledger_tail",
    "load_frame",
    "infer_serial",
    "Supervisor",
]

# --- 标定阈值（依据见模块 docstring；均为存帧实测分隔值，不是拍脑袋） ---
SILENCE_STOP_S = 60.0        # 台账静默超过此秒数 = 循环已停（游戏约 240s 踢人，留足余量）
LOBBY_WARM_MIN = 0.45        # 大厅暖色占比下限（实测 0.72；对局帧最高 0.19）
LOBBY_GREEN_MAX = 0.15       # 大厅绿毡占比上限（实测 0.02；对局帧最低 0.62）
TABLE_GREEN_MIN = 0.35       # 「仍在牌桌」绿毡占比下限（实测对局 0.62~0.73、结算 0.04）
HAND_BAND_MIN_V = 100.0      # 手牌带可读的 bottom_mean_v 下限（实测面板帧 172~175；压黑 <45）
EXPECTED_HW = (900, 1600)    # 已验证帧尺寸 (height, width)


class SessionState(str, Enum):
    """监督分类状态。str 枚举，台账里直接落字符串值。"""

    IN_GAME = "in_game"
    LOOP_STOPPED = "loop_stopped"
    SETTLEMENT = "settlement"
    HINT_PANEL = "hint_panel"
    LOBBY = "lobby"
    WAITING_ROOM = "waiting_room"
    CLAIM_WINDOW = "claim_window"
    UNKNOWN = "unknown"


@dataclass
class Classification:
    """一次分类的完整结论：状态 + 可进台账的证据 + 计划要用的上下文。"""

    state: SessionState
    evidence: dict = field(default_factory=dict)
    loop_stopped: bool = False
    caishen: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "loop_stopped": self.loop_stopped,
            "caishen": self.caishen,
            "evidence": self.evidence,
        }


def load_frame(path: str | Path) -> Optional[np.ndarray]:
    """读一帧（中文路径安全：np.fromfile + imdecode，cv2.imread 读不了中文路径）。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None


def _color_stats(image: np.ndarray) -> dict:
    """绿毡/暖色占比（HSV 口径与探针标定一致）。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(np.int16)
    s = hsv[:, :, 1].astype(np.int16)
    v = hsv[:, :, 2].astype(np.int16)
    green = float(((h >= 35) & (h <= 95) & (s >= 60) & (v >= 30) & (v <= 170)).mean())
    warm = float(((h <= 25) & (s >= 40) & (v >= 120)).mean())
    return {"green_ratio": round(green, 3), "warm_ratio": round(warm, 3)}


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def load_ledger_tail(session_dir: str | Path, max_events: int = 120) -> list[dict]:
    """读会话目录下 actions.jsonl 的尾部事件（跳过会话头与坏行）。文件缺失返回 []。"""
    path = Path(session_dir) / "actions.jsonl"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict] = []
    for line in text.splitlines()[-max_events:]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _ledger_health(events: Sequence[Mapping[str, Any]], now: datetime) -> dict:
    """台账健康度：最后事件时间/静默秒数/stop 事件/最后状态/最后财神。"""
    health: dict[str, Any] = {
        "events_seen": len(events),
        "last_event_age_s": None,
        "stop_event": False,
        "stop_reason": None,
        "last_status": None,
        "last_caishen": None,
    }
    timed: list[tuple[datetime, Mapping[str, Any]]] = []
    for item in events:
        ts = _parse_ts(item.get("ts"))
        if ts is not None:
            timed.append((ts, item))
        caishen = item.get("caishen")
        if isinstance(caishen, str) and caishen:
            health["last_caishen"] = caishen
    if not timed:
        return health
    timed.sort(key=lambda pair: pair[0])
    last_ts, last_item = timed[-1]
    reference = now
    if reference.tzinfo is None and last_ts.tzinfo is not None:
        reference = reference.replace(tzinfo=last_ts.tzinfo)
    age = (reference - last_ts).total_seconds()
    health["last_event_age_s"] = round(max(age, 0.0), 1)
    health["last_status"] = last_item.get("status") or last_item.get("event")
    # stop 判定：存在 stop 事件且其后没有带 status 的 tick 事件（新会话不会复用旧目录，
    # 但同目录续写时以「stop 之后无新 tick」为准，避免把历史 stop 当现行状态）。
    last_stop_idx = -1
    last_tick_idx = -1
    for idx, (_ts, item) in enumerate(timed):
        if item.get("event") == "stop":
            last_stop_idx = idx
        if "status" in item:
            last_tick_idx = idx
    if last_stop_idx >= 0 and last_stop_idx > last_tick_idx:
        stop_item = timed[last_stop_idx][1]
        health["stop_event"] = True
        health["stop_reason"] = stop_item.get("reason") or stop_item.get("gate")
    return health


def infer_serial(session_dir: str | Path) -> Optional[str]:
    """从会话目录名推断 serial：play6_5556 -> emulator-5556（今晚命名约定）。"""
    tail = Path(session_dir).name.rsplit("_", 1)[-1]
    if tail.isdigit() and len(tail) == 4:
        return f"emulator-{tail}"
    return None


def classify(
    image: Optional[np.ndarray],
    events: Optional[Sequence[Mapping[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Classification:
    """把一帧 + 台账尾部判成一个监督状态。纯读、无副作用。

    Args:
        image: 当前帧 BGR 图（1600x900 已验证尺寸）；None 或不可用 → UNKNOWN。
        events: 会话台账尾部事件（load_ledger_tail 的输出）；None 表示台账不可得。
        now: 「现在」的参照时刻（监督主循环传当前时刻；测试传固定值以便
            确定性地测静默）。缺省取当前 UTC 时刻。

    判定顺序（先硬场景、后软推断）：结算（双信号）→ 大厅（暖色场景）→
    行动窗口（圆盘实测）→ 胡提示面板（单信号+手牌带可读）→
    对局画面再由台账健康度分 IN_GAME / LOOP_STOPPED。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    evidence: dict[str, Any] = {}
    health: Optional[dict] = None
    if events is not None:
        health = _ledger_health(events, now)
        evidence["ledger"] = health
    loop_stopped = bool(
        health
        and (
            health["stop_event"]
            or (
                health["last_event_age_s"] is not None
                and health["last_event_age_s"] > SILENCE_STOP_S
            )
        )
    )
    caishen = health["last_caishen"] if health else None

    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        evidence["frame_error"] = "frame_missing_or_invalid"
        return Classification(SessionState.UNKNOWN, evidence, loop_stopped, caishen)
    if tuple(image.shape[:2]) != EXPECTED_HW:
        evidence["frame_error"] = "frame_size_unverified"
        evidence["frame_size"] = [int(image.shape[1]), int(image.shape[0])]
        return Classification(SessionState.UNKNOWN, evidence, loop_stopped, caishen)

    stats = _color_stats(image)
    evidence.update(stats)
    settled_hit, settled_ev = detect_settled_screen(image)
    evidence["settled_signals"] = settled_ev.get("settled_signals")
    evidence["panel_area_pct"] = settled_ev.get("panel_area_pct")
    evidence["bottom_mean_v"] = settled_ev.get("bottom_mean_v")

    # 1) 结算/牌局结束：双信号口径（与 turn_gate 完全一致，不自创阈值）。
    if settled_hit:
        evidence["screen"] = "settled_dual_signal"
        return Classification(SessionState.SETTLEMENT, evidence, loop_stopped, caishen)

    # 2) 比赛场大厅：暖色场景 + 几乎无绿毡。
    if stats["warm_ratio"] >= LOBBY_WARM_MIN and stats["green_ratio"] <= LOBBY_GREEN_MAX:
        evidence["screen"] = "venue_lobby"
        return Classification(SessionState.LOBBY, evidence, loop_stopped, caishen)

    # 3) 茶 2 人等候室：正面规则文字 + 中央“开始游戏”按钮。必须先于
    # 通用绿毡分支，否则会误判为对局并错误重拉循环；这里只分类，不点击。
    waiting = detect_waiting_room(image)
    evidence["waiting_room"] = waiting
    if waiting["matched"]:
        evidence["screen"] = "tea_2p_waiting_room"
        return Classification(SessionState.WAITING_ROOM, evidence,
                              loop_stopped, caishen)

    # 4) 行动窗口：圆盘被 claim_vision 实测到才算（known 语义即「测到了」）。
    claims = read_claims(image)
    evidence["claim_kinds"] = [button.kind for button in claims.buttons]
    if claims.known:
        evidence["screen"] = "claim_discs_visible"
        return Classification(SessionState.CLAIM_WINDOW, evidence, loop_stopped, caishen)

    # 5) 胡提示面板：central_panel 单信号 + 手牌带可读 + 仍在牌桌。
    #    与结算的硬区别：没有 bottom_blackout（detect_settled_screen 未命中即保证）。
    signals = settled_ev.get("settled_signals") or []
    bottom_v = settled_ev.get("bottom_mean_v")
    if (
        signals == ["central_panel"]
        and bottom_v is not None
        and bottom_v >= HAND_BAND_MIN_V
        and stats["green_ratio"] >= TABLE_GREEN_MIN
    ):
        evidence["screen"] = "hint_panel_single_signal"
        return Classification(SessionState.HINT_PANEL, evidence, loop_stopped, caishen)

    # 6) 对局画面：由台账健康度分流。
    if stats["green_ratio"] >= TABLE_GREEN_MIN:
        evidence["screen"] = "table_in_game"
        if health is None or health["last_event_age_s"] is None:
            evidence["frame_error"] = "ledger_missing"
            return Classification(SessionState.UNKNOWN, evidence, loop_stopped, caishen)
        if loop_stopped:
            return Classification(SessionState.LOOP_STOPPED, evidence, True, caishen)
        return Classification(SessionState.IN_GAME, evidence, False, caishen)

    evidence["screen"] = "unrecognized"
    return Classification(SessionState.UNKNOWN, evidence, loop_stopped, caishen)


def _relaunch_plan(reason: str, serial: Optional[str], session_dir: Optional[str],
                   caishen: Optional[str]) -> dict:
    # 台账里的 caishen 只作审计证据，绝不作为重拉参数。新进程必须从当前
    # 画面重新识别；读不到时 AutoPlayLoop 自己 fail-closed。
    plan = {
        "action": "relaunch_loop",
        "reason": reason,
        "serial": serial,
        "session_dir": session_dir,
        "last_ledger_caishen": caishen,
        "caishen": "auto",
        "command_hint": (
            "python bot/run_auto_play_dev.py --serial {serial} --mode tea_2p "
            "--caishen auto --enable --no-dry-run --claim-auto-click --max-actions 60 "
            "--duration 3600 --session <new_session_dir>"
        ).format(serial=serial or "<serial>"),
        "caishen_note": (
            "重拉后只认当前画面自动财神；读不到继续 fail-closed，绝不沿用台账旧值。"
        ),
    }
    return plan


def build_plan(
    classification: Classification,
    *,
    serial: Optional[str] = None,
    session_dir: Optional[str] = None,
) -> dict:
    """按分类结论产出恢复计划（只产出、不执行；JSON 可序列化）。

    计划里的坐标均为今晚实测的「名义坐标」，带 nominal=True 与复核说明：
    执行层启用时必须先以执行时帧复核目标位置，复核不过不许点。
    """
    state = classification.state
    base: dict[str, Any] = {
        "state": state.value,
        "loop_stopped": classification.loop_stopped,
        "serial": serial,
        "session_dir": session_dir,
    }
    if state is SessionState.IN_GAME:
        base.update({"action": "none", "reason": "对局进行中且循环正常产出事件，监督不介入"})
    elif state is SessionState.LOOP_STOPPED:
        base.update(_relaunch_plan(
            "画面仍是对局但循环已停（stop 事件或台账静默 >60s）；游戏约 4 分钟闲置踢人，必须重拉",
            serial, session_dir, classification.caishen))
    elif state is SessionState.SETTLEMENT:
        base.update({
            "action": "alert_only",
            "reason": ("结算/牌局结束画面（双信号确认）：保存截图并告警；"
                       "确定、返回大厅、开始游戏等离桌/开局按钮永久禁止自动点击"),
        })
    elif state is SessionState.HINT_PANEL:
        if classification.loop_stopped:
            base.update(_relaunch_plan(
                "胡提示面板在屏且循环已停：面板是非交互提示条，重拉循环后由循环的面板处理路径出牌",
                serial, session_dir, classification.caishen))
        else:
            base.update({
                "action": "alert_only",
                "reason": ("胡提示面板在屏、循环仍在产出事件：面板非交互，监督不抢循环的出牌权；"
                           "若循环随后停手或静默，本监督将以 loop_stopped 升级为重拉计划"),
            })
    elif state is SessionState.LOBBY:
        base.update({
            "action": "alert_only",
            "reason": ("画面已在大厅：保存截图并告警；桌子、规则窗、开始游戏、"
                       "充值及任何重进桌流程永久禁止自动点击"),
        })
    elif state is SessionState.WAITING_ROOM:
        base.update({
            "action": "wait_for_manual_start",
            "reason": ("茶 2 人等候室：只确认当前实例已坐下；开始游戏仍由用户手动点击，"
                       "监督不启动/重拉局内循环，也不发布点击计划"),
        })
    elif state is SessionState.CLAIM_WINDOW:
        if classification.loop_stopped:
            plan = _relaunch_plan(
                "行动窗口在屏且循环已停：窗口有倒计时，先重拉循环；若窗口已超时自动过，告警留痕",
                serial, session_dir, classification.caishen)
            plan["alert"] = True
            base.update(plan)
        else:
            base.update({
                "action": "alert_only",
                "reason": ("行动窗口在屏、循环在跑：claim 通道由 AutoPlayLoop 的"
                           "模板/L2/L3 门禁独占，监督只告警留痕，不代循环抢点"),
                "claim_kinds": classification.evidence.get("claim_kinds"),
            })
    else:  # UNKNOWN
        base.update({
            "action": "alert_only",
            "reason": "证据不足（fail-closed）：只告警留痕，不产出任何点击/重拉计划",
        })
    return base

# ---------------------------------------------------------------------------
# 常驻监督主循环：轮询会话 → 分类 → 出计划 → 状态变化写监督台账。
# 帧与执行都靠注入：frame_provider(session_cfg) -> image|None；
# executor.execute(plan) -> Any。本模块自身不实现任何帧来源与任何执行动作。
# ---------------------------------------------------------------------------

FrameProvider = Callable[[Mapping[str, Any]], Optional[np.ndarray]]


class Supervisor:
    """监督器。execute=False（缺省）时 executor 永远不会被调用。"""

    def __init__(
        self,
        *,
        ledger_path: str | Path | None = None,
        frame_provider: Optional[FrameProvider] = None,
        executor: Any = None,
        execute: bool = False,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.frame_provider = frame_provider
        self.executor = executor
        self.execute = bool(execute)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._prev_state: dict[str, str] = {}

    def _append_ledger(self, entry: dict) -> None:
        if self.ledger_path is None:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def poll_once(self, sessions: Sequence[Mapping[str, Any]]) -> list[dict]:
        """轮询一轮。sessions: [{"session_dir": ..., "serial": 可选}]。

        返回每会话一条 {"session_dir", "serial", "classification", "plan"}；
        状态相对上一轮发生变化时才写监督台账（外加执行结果条目）。
        """
        now = self._now_fn()
        results: list[dict] = []
        for cfg in sessions:
            session_dir = str(cfg["session_dir"])
            serial = cfg.get("serial") or infer_serial(session_dir)
            events = load_ledger_tail(session_dir)
            frame = self.frame_provider(cfg) if self.frame_provider else None
            result_cls = classify(frame, events, now=now)
            plan = build_plan(result_cls, serial=serial, session_dir=session_dir)
            results.append({
                "session_dir": session_dir,
                "serial": serial,
                "classification": result_cls.to_dict(),
                "plan": plan,
            })
            prev = self._prev_state.get(session_dir)
            if prev != result_cls.state.value:
                self._prev_state[session_dir] = result_cls.state.value
                self._append_ledger({
                    "ts": now.isoformat(),
                    "session_dir": session_dir,
                    "serial": serial,
                    "state": result_cls.state.value,
                    "prev_state": prev,
                    "plan": plan,
                    "evidence": result_cls.evidence,
                })
            if (
                self.execute
                and self.executor is not None
                and plan.get("action") not in ("none", "alert_only")
            ):
                try:
                    outcome = self.executor.execute(plan)
                    self._append_ledger({
                        "ts": self._now_fn().isoformat(),
                        "session_dir": session_dir,
                        "serial": serial,
                        "executed": plan.get("action"),
                        "result": str(outcome)[:500],
                    })
                except Exception as exc:  # 执行层异常不许打死监督循环
                    self._append_ledger({
                        "ts": self._now_fn().isoformat(),
                        "session_dir": session_dir,
                        "serial": serial,
                        "executed": plan.get("action"),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    })
        return results

    def run(
        self,
        sessions: Sequence[Mapping[str, Any]],
        *,
        interval: float = 10.0,
        max_cycles: Optional[int] = None,
    ) -> None:
        """常驻轮询。max_cycles=None 表示无限循环（常驻）；测试传小整数。"""
        cycles = 0
        while True:
            self.poll_once(sessions)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            time.sleep(interval)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="代打会话常驻监督（只分类+出计划+写台账；执行层由主线注入，本 CLI 不带执行实现）")
    parser.add_argument("--sessions", nargs="+", required=True,
                        help="会话目录列表，如 D:\\温州麻将\\_takeover\\play6_5556")
    parser.add_argument("--interval", type=float, default=10.0, help="轮询间隔秒（缺省 10）")
    parser.add_argument("--ledger", default="supervisor_ledger.jsonl",
                        help="监督台账 jsonl 输出路径（缺省 ./supervisor_ledger.jsonl）")
    parser.add_argument("--cycles", type=int, default=0, help="轮询轮数，0=常驻无限循环")
    parser.add_argument("--execute", action="store_true",
                        help="（预留）执行计划；CLI 无注入执行层时拒绝启动")
    args = parser.parse_args(argv)
    if args.execute:
        print("拒绝启动：--execute 需要由主线集成时注入 executor，CLI 不带执行实现（fail-closed）。")
        return 2
    supervisor = Supervisor(ledger_path=args.ledger)
    sessions = [{"session_dir": item} for item in args.sessions]
    print(f"监督启动（dry-plan 模式，绝不执行）：{len(sessions)} 个会话，间隔 {args.interval}s，台账 {args.ledger}")
    try:
        supervisor.run(sessions, interval=args.interval,
                       max_cycles=(args.cycles or None))
    except KeyboardInterrupt:
        print("监督退出（Ctrl-C）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
