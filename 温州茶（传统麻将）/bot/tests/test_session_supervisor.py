# -*- coding: utf-8 -*-
"""session_supervisor 的离线测试：存帧分类夹具 + 台账健康度 + 计划形状 + 监督轮询。

全部离线：只读 _takeover / 采集 里的存帧与真台账，零 ADB、零进程操作、
不写任何生产目录（监督台账写 pytest tmp_path）。夹具帧缺失时 skip，
与 test_turn_gate 里真实帧测试的约定一致。

夹具来源（全部真实存帧，2026-10-24/25）：
- SETTLEMENT：play3_5558 解散弹窗帧（双信号 14.21%/29.59）+
  dev_auto_play 房间解散帧（双信号 14.3%/23.28）。
- LOBBY：双端被踢回比赛场大厅的截图 + play4_5556 大厅帧。
- HINT_PANEL：play3 玉姐面板帧（单信号 6.29%）、play6 玉姐面板帧（7.86%）、
  live_5558 双行面板帧（18.25%）——三种版式都必须是单信号+手牌带亮。
- IN_GAME：play6/play3 的正常对局帧（green 0.72、无 settled 信号、无圆盘）。
- CLAIM_WINDOW：assist_sessions 里 read_claims 实测到圆盘的帧
  （gold+pass / hu+gold+pass / hu+pass）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from session_supervisor import (  # noqa: E402
    SessionState,
    Supervisor,
    build_plan,
    classify,
    load_frame,
    load_ledger_tail,
)

TAKEOVER = Path(r"D:\温州麻将\_takeover")
CORPUS = Path(r"D:\温州麻将\温州茶（传统麻将）\采集")
ROOT_CAPTURE = Path(r"D:\温州麻将\采集")
TZ8 = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 25, 3, 0, 0, tzinfo=TZ8)

SETTLEMENT_FRAMES = [
    TAKEOVER / "play3_5558" / "frames" / "20261025_014935_739554_before_23754d.png",
    CORPUS / "dev_auto_play" / "20261024_051634_4286f3" / "frames" / "20261024_051634_773578_before_87fd97.png",
]
LOBBY_FRAMES = [
    TAKEOVER / "settle_emulator-5556.png",
    TAKEOVER / "settle_emulator-5558.png",
    TAKEOVER / "play4_5556" / "frames" / "20261025_015008_142139_before_3a1b50.png",
]
WAITING_FRAMES = [
    ROOT_CAPTURE / "dev_auto_play" / "waiting_room_evidence_20261025"
    / "room_877158_waiting_5556_native.png",
    ROOT_CAPTURE / "dev_auto_play" / "waiting_room_evidence_20261025"
    / "room_877158_waiting_5558_native.png",
]
HINT_FRAMES = [
    TAKEOVER / "play3_5556" / "frames" / "20261025_014201_407831_before_6c1e83.png",
    TAKEOVER / "play6_5556" / "frames" / "20261025_015901_471052_before_622288.png",
    TAKEOVER / "live_5558" / "frames" / "20261025_012922_320987_before_6db942.png",
]
INGAME_FRAMES = [
    TAKEOVER / "play6_5556" / "frames" / "20261025_015722_963068_before_853cde.png",
    TAKEOVER / "play3_5558" / "frames" / "20261025_014933_683681_before_24f4af.png",
]
CLAIM_FRAMES = [
    CORPUS / "assist_sessions" / "20261024_122607" / "live" / "tea_2p" / "20261024_125224_741278_0931f1.png",
    CORPUS / "assist_sessions" / "20261024_122607" / "live" / "tea_2p" / "20261024_125613_646151_299b31.png",
    CORPUS / "assist_sessions" / "20261024_002903" / "live" / "tea_2p" / "20261024_003731_386176_ca2d9c.png",
]


def _frame(path: Path) -> np.ndarray:
    if not path.exists():
        pytest.skip(f"夹具帧缺失：{path}")
    image = load_frame(path)
    assert image is not None, f"夹具帧读不出：{path}"
    return image


def _play6_events() -> list[dict]:
    ledger = TAKEOVER / "play6_5556" / "actions.jsonl"
    if not ledger.exists():
        pytest.skip("play6_5556 台账缺失")
    return load_ledger_tail(TAKEOVER / "play6_5556", max_events=100000)


def _ts(event: dict) -> datetime:
    return datetime.fromisoformat(event["ts"])


def _slice(events: list[dict], start: str, end: str) -> list[dict]:
    lo = datetime.fromisoformat(start)
    hi = datetime.fromisoformat(end)
    return [e for e in events if e.get("ts") and lo <= _ts(e) <= hi]


def _events_upto_last_stop(events: list[dict]) -> tuple[list[dict], datetime]:
    """把真台账截到最后一个 stop 事件为止（其后主线可能续写，截断保证确定性）。"""
    stop_idx = [i for i, e in enumerate(events) if e.get("event") == "stop"]
    if not stop_idx:
        pytest.skip("play6_5556 台账里没有 stop 事件")
    cut = stop_idx[-1]
    return events[: cut + 1], _ts(events[cut])


# ---------------------------------------------------------------------------
# 帧分类夹具（逐类至少 2 帧）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", SETTLEMENT_FRAMES, ids=[p.name for p in SETTLEMENT_FRAMES])
def test_settlement_frames(path: Path) -> None:
    result = classify(_frame(path), events=[], now=FIXED_NOW)
    assert result.state is SessionState.SETTLEMENT
    assert result.evidence["settled_signals"] == ["central_panel", "bottom_blackout"]
    plan = build_plan(result, serial="emulator-5558", session_dir="s")
    assert plan["action"] == "alert_only"
    assert "永久禁止自动点击" in plan["reason"]
    json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize("path", LOBBY_FRAMES, ids=[p.name for p in LOBBY_FRAMES])
def test_lobby_frames(path: Path) -> None:
    result = classify(_frame(path), events=[], now=FIXED_NOW)
    assert result.state is SessionState.LOBBY
    assert result.evidence["warm_ratio"] >= 0.45
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "alert_only"
    assert "永久禁止自动点击" in plan["reason"]
    json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize("path", WAITING_FRAMES, ids=[p.name for p in WAITING_FRAMES])
def test_waiting_room_frames_are_not_misclassified_as_in_game(path: Path) -> None:
    result = classify(_frame(path), events=[], now=FIXED_NOW)
    assert result.state is SessionState.WAITING_ROOM
    assert result.evidence["waiting_room"]["player_count"] == 2
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "wait_for_manual_start"
    assert "不发布点击计划" in plan["reason"]
    json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize("path", HINT_FRAMES, ids=[p.name for p in HINT_FRAMES])
def test_hint_panel_frames(path: Path) -> None:
    result = classify(_frame(path), events=None, now=FIXED_NOW)
    assert result.state is SessionState.HINT_PANEL
    # 与结算的硬区别：只有 central_panel 单信号，手牌带没被压黑。
    assert result.evidence["settled_signals"] == ["central_panel"]
    assert result.evidence["bottom_mean_v"] >= 100.0
    # 循环还在跑时监督只告警，绝不产出过结算/重拉计划。
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "alert_only"
    json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize("path", CLAIM_FRAMES, ids=[p.name for p in CLAIM_FRAMES])
def test_claim_window_frames(path: Path) -> None:
    result = classify(_frame(path), events=None, now=FIXED_NOW)
    assert result.state is SessionState.CLAIM_WINDOW
    assert result.evidence["claim_kinds"], "圆盘种类应进证据"
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "alert_only"  # 循环在跑时 claim 通道未接线，只告警
    json.dumps(plan, ensure_ascii=False)

# ---------------------------------------------------------------------------
# 台账驱动：IN_GAME / LOOP_STOPPED（真台账切片，now 固定保证确定性）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", INGAME_FRAMES, ids=[p.name for p in INGAME_FRAMES])
def test_ingame_frames_with_live_ledger(path: Path) -> None:
    events = _play6_events()
    live = _slice(events, "2026-10-25T01:57:20+08:00", "2026-10-25T01:58:40+08:00")
    if not live:
        pytest.skip("play6 台账早期切片为空")
    now = _ts(live[-1]) + timedelta(seconds=10)
    result = classify(_frame(path), events=live, now=now)
    assert result.state is SessionState.IN_GAME
    assert result.loop_stopped is False
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "none"


def test_loop_stopped_by_stop_event() -> None:
    events, stop_ts = _events_upto_last_stop(_play6_events())
    image = _frame(INGAME_FRAMES[0])
    result = classify(image, events=events, now=stop_ts + timedelta(seconds=30))
    assert result.state is SessionState.LOOP_STOPPED
    assert result.evidence["ledger"]["stop_event"] is True
    # 台账旧财神只留审计；重拉必须 auto 重新读当前画面。
    assert result.caishen == "bai"
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "relaunch_loop"
    assert plan["caishen"] == "auto"
    assert plan["last_ledger_caishen"] == "bai"
    assert "--caishen auto" in plan["command_hint"]
    json.dumps(plan, ensure_ascii=False)


def test_loop_stopped_by_silence() -> None:
    events = _play6_events()
    live = _slice(events, "2026-10-25T01:57:20+08:00", "2026-10-25T01:58:40+08:00")
    if not live:
        pytest.skip("play6 台账早期切片为空")
    image = _frame(INGAME_FRAMES[0])
    now = _ts(live[-1]) + timedelta(seconds=150)  # 静默 150s > 60s 门槛
    result = classify(image, events=live, now=now)
    assert result.state is SessionState.LOOP_STOPPED
    assert result.evidence["ledger"]["stop_event"] is False


def test_hint_panel_with_stopped_loop_plans_relaunch() -> None:
    events, stop_ts = _events_upto_last_stop(_play6_events())
    result = classify(_frame(HINT_FRAMES[0]), events=events,
                      now=stop_ts + timedelta(seconds=30))
    assert result.state is SessionState.HINT_PANEL
    assert result.loop_stopped is True
    plan = build_plan(result, serial="emulator-5556", session_dir="s")
    assert plan["action"] == "relaunch_loop"


# ---------------------------------------------------------------------------
# UNKNOWN：证据不足一律 fail-closed
# ---------------------------------------------------------------------------

def test_unknown_when_frame_missing() -> None:
    result = classify(None, events=[], now=FIXED_NOW)
    assert result.state is SessionState.UNKNOWN
    assert build_plan(result)["action"] == "alert_only"


def test_unknown_when_frame_unrecognized() -> None:
    black = np.zeros((900, 1600, 3), dtype=np.uint8)
    result = classify(black, events=[], now=FIXED_NOW)
    assert result.state is SessionState.UNKNOWN


def test_unknown_when_frame_size_unverified() -> None:
    small = np.zeros((100, 100, 3), dtype=np.uint8)
    result = classify(small, events=[], now=FIXED_NOW)
    assert result.state is SessionState.UNKNOWN
    assert result.evidence["frame_error"] == "frame_size_unverified"


def test_ingame_frame_without_ledger_is_unknown() -> None:
    # 画面是对局但台账不可得：分不清 IN_GAME 与 LOOP_STOPPED → UNKNOWN。
    result = classify(_frame(INGAME_FRAMES[0]), events=None, now=FIXED_NOW)
    assert result.state is SessionState.UNKNOWN
    assert result.evidence["frame_error"] == "ledger_missing"


# ---------------------------------------------------------------------------
# 监督轮询：状态变化才写台账；execute 缺省时 executor 永不被调用
# ---------------------------------------------------------------------------

class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, plan: dict) -> str:
        self.calls.append(plan)
        return "fake-ok"


def _write_synthetic_ledger(session_dir: Path, ts: datetime) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": (ts - timedelta(seconds=5)).isoformat(), "status": "advise",
         "caishen": "bai", "suggestion": "6万"},
        {"ts": ts.isoformat(), "status": "wait", "caishen": "bai"},
    ]
    (session_dir / "actions.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def test_supervisor_poll_ledger_and_execute_gate(tmp_path: Path) -> None:
    lobby_image = _frame(LOBBY_FRAMES[0])
    ingame_image = _frame(INGAME_FRAMES[0])
    session_dir = tmp_path / "play9_5556"
    _write_synthetic_ledger(session_dir, FIXED_NOW)
    ledger_path = tmp_path / "supervisor_ledger.jsonl"
    holder = {"image": lobby_image}
    executor = _FakeExecutor()
    supervisor = Supervisor(
        ledger_path=ledger_path,
        frame_provider=lambda cfg: holder["image"],
        executor=executor,
        execute=False,
        now_fn=lambda: FIXED_NOW,
    )
    sessions = [{"session_dir": str(session_dir)}]

    first = supervisor.poll_once(sessions)
    assert first[0]["classification"]["state"] == "lobby"
    assert first[0]["plan"]["action"] == "alert_only"
    assert first[0]["serial"] == "emulator-5556"  # 目录名推断
    assert executor.calls == [], "execute=False 时绝不许调用 executor"
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["state"] == "lobby"

    supervisor.poll_once(sessions)  # 状态未变 → 不重复写台账
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    holder["image"] = ingame_image  # 状态变化 → 再写一条
    third = supervisor.poll_once(sessions)
    assert third[0]["classification"]["state"] == "in_game"
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert executor.calls == []

    # execute=True 也不执行大厅计划：大厅/开局按钮永久黑名单。
    holder["image"] = lobby_image
    executing = Supervisor(
        ledger_path=tmp_path / "supervisor_ledger2.jsonl",
        frame_provider=lambda cfg: holder["image"],
        executor=executor,
        execute=True,
        now_fn=lambda: FIXED_NOW,
    )
    executing.poll_once(sessions)
    assert executor.calls == []
