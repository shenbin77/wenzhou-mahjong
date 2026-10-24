"""auto_play_loop 的离线单元测试：假 adb / 假截图 / 假分析器。

**全程不接触真实模拟器**：所有外部依赖（截图、分析、前台包、房卡、HUD、
时钟）都通过 ``AutoPlayLoop`` 的注入点替换成假实现，并在每个测试里把
``subprocess.run`` 换成“一调用就失败”的哨兵——任何真实 adb 调用都会
让测试直接炸掉，而不是静默点一下。

覆盖 team-lead 指定的 8 类场景，外加台账与构造期安全约束：
1. 正常一轮（enabled）：点击 + 手牌 −1 验证；
2. dry_run：只算坐标，零点击；
3. 无建议 / 弃权：只等待，零点击；
4. 连续 3 次截图/识牌异常停机（成功一次即清零）；点击失败立即停机；
5. 前台包不符：截屏前就停手，不计异常预算；
6. 换局重置（HUD 指纹变化、财神 label 变化）；
7. 房卡 0 停机（读不到记 unknown 不阻断）；
8. 几何越界拒绝（TapRejected 计入异常，单帧只尝试一次，不重试）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from auto_play_loop import (  # noqa: E402
    MAX_CONSECUTIVE_ERRORS,
    AutoPlayLoop,
    GateBlocked,
    RoundState,
    _same_hud,
    hud_fingerprint,
)
from action_guard import ActionGuardResult  # noqa: E402
from harvest_tea import EXPECTED_PACKAGE  # noqa: E402
from tap_executor import TapPlan, TapRejected  # noqa: E402

HUD_A = b"hud-" + bytes(range(32))
HUD_B = b"hud-" + bytes(range(1, 33))     # 同长度、全字节不同 → 必判换局


# ----------------------------------------------------------------------
# 离线护栏：任何真实 adb 调用都让测试失败
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch):
    """离线红线：测试期间禁止任何 subprocess.run。"""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("离线测试禁止任何真实 subprocess/adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)


# ----------------------------------------------------------------------
# 假件
# ----------------------------------------------------------------------
def make_frame(fill: int = 235) -> bytes:
    """Return a valid 1600x900 PNG shaped like a real game frame.

    The top 760 rows are the saturated green table; the bottom 140 rows are the
    hand band at brightness ``fill``.  ``fill=235`` matches the measured normal
    hand-tile brightness (203.6–230.0 → the turnover gate measures p70(V) 235
    here), while ``fill=100`` reproduces the measured "请出牌" dimmed band.

    A flat bright full-frame would be (correctly) rejected by the settlement /
    modal detector — a full-screen light rectangle is exactly what a modal or
    result screen looks like — so the fake frame must keep a dark table.
    """
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, 70, 40)                 # BGR 桌面绿：V=70、饱和，不算浅色面板
    image[760:, :] = fill
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


FRAME = make_frame()


def hand_boxes(count: int = 17) -> list[list[int]]:
    """手牌区的互不重叠牌框（y≥720，与 TapExecutor 的 ROI 白名单一致）。"""
    return [[90 + index * 88, 780, 170 + index * 88, 888] for index in range(count)]


def advise(**overrides) -> dict:
    """One advise-shaped analyze_snapshot result.

    形状必须与真实 ``assist_tea.analyze_snapshot`` 对齐（17 张满手、带牌框、
    财神可解析），否则回合所有权门禁会（正确地）不放行。
    """
    boxes = overrides.pop("hand_boxes", None) or hand_boxes(17)
    hand = overrides.pop("hand_ids", None) or [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8]
    diagnostics = overrides.pop("vision_diagnostics", None) or [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(hand, boxes)
    ]
    base = {
        "status": "advise",
        "suggestion": "4筒",
        "suggestion_id": 4,
        "suggested_boxes": [list(box) for box in boxes[:1]],
        "caishen": "w1",
        "caishen_source": "image",
        "hand_ids": list(hand),
        "melds": [],
        "meld_count": 0,
        "mode": "tea_2p",
        "reason": "",
        "vision_diagnostics": diagnostics,
    }
    base.update(overrides)
    return base


class Feeder:
    """A callable returning queued values (last value sticks)."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        if not self.values:
            raise AssertionError("假件被调用次数超出预期")
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class Shot:
    """Fake screencap: hands out queued PNG bytes, counts calls."""

    def __init__(self, frames=None):
        self.frames = list(frames) if frames else [FRAME]
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        if not self.frames:
            raise RuntimeError("假截图已用尽")
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class Analyzer:
    """Fake analyze_snapshot: queued dicts, or queued exceptions."""

    def __init__(self, results=None):
        self.results = list(results) if results else [advise()]
        self.calls = 0

    def __call__(self, _raw, _path):
        self.calls += 1
        if not self.results:
            raise AssertionError("假分析器被调用次数超出预期")
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, BaseException):
            raise item
        return dict(item)


class FakeTap:
    """Stand-in for TapExecutor: records plan/execute, can raise on demand."""

    def __init__(self, *, plan_result=None, execute_result=None,
                 plan_error=None, execute_error=None):
        self.plan_result = plan_result or TapPlan(
            label="t4", tile_id=4, box=(300, 780, 392, 892), x=346, y=836)
        self.execute_result = execute_result or {
            "executed": True, "dry_run": False, "stage": "verified",
            "tile": "t4", "tile_id": 4, "tap": [346, 836],
            "box": [300, 780, 392, 892],
            "before_count": 14, "after_count": 13,
            "reason": "点击后手牌恰好少一张，验证通过",
        }
        self.plan_error = plan_error
        self.execute_error = execute_error
        self.plan_calls: list[dict] = []
        self.execute_calls: list[dict] = []

    def plan(self, analysis):
        self.plan_calls.append(analysis)
        if self.plan_error is not None:
            raise self.plan_error
        return self.plan_result

    def execute(self, analysis):
        self.execute_calls.append(analysis)
        if self.execute_error is not None:
            raise self.execute_error
        return dict(self.execute_result)


def increasing_clock(step: float = 0.01):
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def make_loop(tmp_path, *, enabled: bool = False, analyses=None, frames=None,
              foreground=None, cards=None, hud=None, tap=None,
              action_guard=None, **kwargs):
    """Build a fully injected AutoPlayLoop plus its fakes."""
    shot = Shot(frames)
    analyzer = Analyzer(analyses)
    foreman = Feeder(foreground if foreground is not None else [EXPECTED_PACKAGE])
    hud_feeder = Feeder(hud if hud is not None else [(HUD_A, None)])
    card_reader = None
    if cards is not None:
        card_reader = cards if callable(cards) else Feeder(cards)
    fake_tap = tap if tap is not None else FakeTap()
    loop = AutoPlayLoop(
        enabled=enabled,
        adb_path=sys.executable,          # 只做 is_file() 检查，不会被执行
        serial="emulator-test",
        session_dir=Path(tmp_path) / "session",
        max_actions=kwargs.pop("max_actions", 20),
        duration_s=kwargs.pop("duration_s", 600.0),
        interval_s=kwargs.pop("interval_s", 0.0),
        tap_executor=fake_tap,
        shot_fn=shot,
        analyze_fn=analyzer,
        foreground_fn=foreman,
        card_reader=card_reader,
        action_guard_fn=(action_guard or
                         (lambda _image: ActionGuardResult("clear"))),
        round_key_fn=hud_feeder,
        sleep_fn=lambda _seconds: None,
        clock_fn=increasing_clock(),
        **kwargs,
    )
    return loop, shot, analyzer, fake_tap, foreman


def settle(loop: AutoPlayLoop, frames: int = 1) -> list[dict]:
    """Warm up the turn gate: it needs two consecutive identical frames.

    The first frame only establishes the "stable hand" baseline, so every test
    that expects a tap must run this warm-up tick first.  Returns the events so
    a caller can assert the warm-up really stayed idle.
    """
    warmed = []
    for _ in range(frames):
        event = loop.tick()
        assert event["event"] == "idle", event
        warmed.append(event)
    return warmed


def test_action_ui_blocks_before_analysis_and_tap(tmp_path):
    guard = lambda _image: ActionGuardResult(  # noqa: E731
        "action_visible", ((1383, 552, 1483, 652),), "检测到行动界面")
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, action_guard=guard)

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["guard"] == "action_visible"
    # P0-5: claim path refreshes hand from THIS frame before decide_action.
    assert analyzer.calls == 1
    assert tap.execute_calls == []
    assert loop.stopped is False


def test_uncertain_action_guard_fails_closed(tmp_path):
    guard = lambda _image: ActionGuardResult(  # noqa: E731
        "uncertain", reason="按钮几何不完整")
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, action_guard=guard)

    event = loop.tick()

    assert event["event"] == "error"
    assert event["error"] == "action_guard_uncertain"
    # Uncertain guard still fail-closes; may analyze for claim ledger, never tap.
    assert analyzer.calls <= 1
    assert tap.execute_calls == []


def read_ledger(loop: AutoPlayLoop) -> list[dict]:
    lines = loop.ledger_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def events(loop: AutoPlayLoop) -> list[dict]:
    return [item for item in read_ledger(loop) if "event" in item]


def events_of(loop: AutoPlayLoop, name: str) -> list[dict]:
    return [item for item in events(loop) if item["event"] == name]


# ----------------------------------------------------------------------
# 1. 正常一轮：点击 + 手牌 −1 验证
# ----------------------------------------------------------------------
def test_enabled_tick_taps_once_and_verifies_hand_minus_one(tmp_path):
    loop, shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    settle(loop)                                # 门禁需要连续两帧一致
    event = loop.tick()

    assert event["event"] == "action"
    assert event["verified"] is True
    assert (event["before_count"], event["after_count"]) == (14, 13)
    assert event["tap"] == [346, 836]
    assert event["turn_gate"]["state"] == "discard_ready"
    assert len(tap.execute_calls) == 1          # 一帧只点一次
    assert tap.plan_calls == []                 # 真点路径不走 dry_run 的 plan
    assert shot.calls == 3                      # 预热 1 张 + 点击前后各 1 张
    assert analyzer.calls == 2
    assert loop.actions_done == 1
    assert loop.stopped is False
    assert event["after_frame"] and Path(event["after_frame"]).is_file()


def test_enabled_tick_marks_unverified_when_hand_does_not_shrink(tmp_path):
    """点击后手牌没少：台账如实记 verified=False，立即停手。"""
    bad = {
        "executed": True, "dry_run": False, "stage": "verified",
        "tile": "t4", "tile_id": 4, "tap": [346, 836],
        "box": [300, 780, 392, 892],
        "before_count": 14, "after_count": 14,
    }
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, tap=FakeTap(execute_result=bad))
    settle(loop)
    event = loop.tick()

    assert event["event"] == "action"
    assert event["verified"] is False
    assert len(tap.execute_calls) == 1
    assert loop.stopped is True
    assert events_of(loop, "stop")[-1]["gate"] == "tap_unverified"
    assert loop.tick()["event"] == "skip"
    assert len(tap.execute_calls) == 1


# ----------------------------------------------------------------------
# 2. dry_run：只算坐标，零点击
# ----------------------------------------------------------------------
def test_dry_run_tick_records_plan_without_executing(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=False)
    settle(loop)                                # 门禁在 dry_run 下同样生效
    event = loop.tick()

    assert event["event"] == "dry_run_plan"
    assert event["tap"] == [346, 836]
    assert event["verified"] is None
    assert event["turn_gate"]["state"] == "discard_ready"
    assert len(tap.plan_calls) == 1
    assert tap.execute_calls == []               # 关键：零点击
    assert loop.dry_run is True and loop.enabled is False
    assert loop.actions_done == 1


def test_enabled_and_dry_run_flags_follow_constructor(tmp_path):
    dry, *_ = make_loop(tmp_path, enabled=False)
    live, *_ = make_loop(tmp_path, enabled=True)
    assert (dry.enabled, dry.dry_run) == (False, True)
    assert (live.enabled, live.dry_run) == (True, False)


# ----------------------------------------------------------------------
# 3. 无建议 / 弃权：只等待，零点击
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "result",
    [
        advise(status="abstain", suggested_boxes=[], reason="识牌不完整"),
        advise(status="wait", suggested_boxes=[], reason="等待上家"),
        advise(suggested_boxes=[]),
        advise(status="hu", suggested_boxes=[[300, 780, 392, 892]]),
    ],
)
def test_no_actionable_advice_never_taps(tmp_path, result):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, analyses=[result])
    event = loop.tick()

    assert event["event"] == "idle"
    assert tap.execute_calls == []
    assert tap.plan_calls == []
    assert loop.actions_done == 0
    assert loop.stopped is False


# ----------------------------------------------------------------------
# 4. 连续异常停机（成功一次即清零）
# ----------------------------------------------------------------------
def test_three_consecutive_errors_stop_the_loop(tmp_path):
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[RuntimeError("识牌炸了")])

    for _ in range(MAX_CONSECUTIVE_ERRORS):
        loop.tick()

    assert loop.stopped is True
    assert "连续 3 次异常" in loop.stop_reason
    assert len(events_of(loop, "error")) == 3
    assert len(events_of(loop, "stop")) == 1
    assert tap.execute_calls == []
    assert analyzer.calls == 3

    # 停机后不再截屏/分析（skip，不再消耗假件）。
    skipped = loop.tick()
    assert skipped["event"] == "skip"
    assert analyzer.calls == 3


def test_success_resets_the_error_streak(tmp_path):
    """两次失败之间夹一次正常周期 → 计数清零，不误停机。"""
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[RuntimeError("第一次"), advise(), advise(),
                  RuntimeError("第二次")])

    assert loop.tick()["event"] == "error"
    assert loop.consecutive_errors == 1
    assert settle(loop)[0]["event"] == "idle"      # 门禁预热：不算异常
    assert loop.consecutive_errors == 0
    assert loop.tick()["event"] == "action"
    assert loop.consecutive_errors == 0
    assert loop.tick()["event"] == "error"
    assert loop.consecutive_errors == 1
    assert loop.stopped is False


def test_capture_failure_is_an_anomaly_not_a_silent_skip(tmp_path):
    class BrokenShot:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            raise RuntimeError("screencap 超时")

    loop, _shot, _analyzer, _tap, _ = make_loop(tmp_path, enabled=True)
    loop._shot = BrokenShot()
    event = loop.tick()

    assert event["event"] == "error"
    assert event["error"] == "capture_failed"
    assert loop.consecutive_errors == 1
    assert loop.stopped is False


# ----------------------------------------------------------------------
# 5. 前台包不符：截屏前停手
# ----------------------------------------------------------------------
def test_foreground_gate_stops_before_capture_and_costs_no_error_budget(tmp_path):
    loop, shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, foreground=["com.android.launcher3"])
    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_foreground"
    assert loop.stopped is True
    assert shot.calls == 0                       # 一张截图都没抓
    assert analyzer.calls == 0
    assert tap.execute_calls == []
    assert loop.consecutive_errors == 0          # 门禁不计异常预算


def test_foreground_read_failure_stops_fail_closed(tmp_path):
    """连前台包都读不到：fail closed，停手，不猜、不抓屏、不点。"""
    def broken_foreground():
        raise RuntimeError("adb dumpsys 超时")

    loop, shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop._foreground = broken_foreground
    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_foreground"
    assert "无法确认前台应用" in event["reason"]
    assert shot.calls == 0 and analyzer.calls == 0
    assert tap.execute_calls == []


def test_blocked_gate_records_stop_event_but_saves_no_frames(tmp_path):
    """门禁不放行：只落一条 stop 台账，不建 frames、不写截图。"""
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=True, foreground=["com.android.launcher3"])
    loop.tick()
    assert loop.stopped is True
    assert not loop.frame_dir.exists()
    rows = read_ledger(loop)
    assert rows[0]["dev"] is True
    assert [row.get("event") for row in rows[1:]] == ["stop"]


def test_check_gates_rejects_launcher_and_missing_adb(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, foreground=["com.android.launcher3"])
    with pytest.raises(GateBlocked, match="前台包不是游戏"):
        loop.check_gates()

    bad_adb, *_ = make_loop(tmp_path / "b", enabled=False)
    bad_adb.adb_path = str(tmp_path / "nope" / "adb.exe")
    with pytest.raises(GateBlocked, match="ADB 不存在"):
        bad_adb.check_gates()

    ok, *_ = make_loop(tmp_path / "c", enabled=False)
    report = ok.check_gates()
    assert report["foreground"] == EXPECTED_PACKAGE
    assert report["dry_run"] is True
    assert report["adb_exists"] is True


# ----------------------------------------------------------------------
# 6. 换局重置
# ----------------------------------------------------------------------
def test_hud_change_resets_round_state(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=False,
        analyses=[advise(status="wait", suggested_boxes=[])],
        hud=[(HUD_A, None), (HUD_B, None)])

    assert loop.tick()["event"] == "idle"
    assert loop.round.hud_key == HUD_A
    assert loop.round.seq == 1                 # 会话第一局从 1 起

    assert loop.tick()["event"] == "idle"
    assert loop.round.hud_key == HUD_B
    assert loop.round.seq == 2
    resets = events_of(loop, "round_reset")
    assert len(resets) == 1 and resets[0]["round_seq"] == 2


def test_hud_change_with_advice_skips_the_change_frame(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, hud=[(HUD_A, None), (HUD_A, None), (HUD_B, None)])
    settle(loop)
    assert loop.tick()["event"] == "action"
    assert loop.tick()["event"] == "idle"
    assert loop.round.seq == 2
    assert len(tap.execute_calls) == 1
    assert loop.turn_history == []             # 换局后稳定帧必须重新攒


def test_manual_caishen_round_change_requires_new_confirmation(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, caishen_label="w1",
        hud=[(HUD_A, None), (HUD_A, None), (HUD_B, None)])
    settle(loop)
    assert loop.tick()["event"] == "action"
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_manual_caishen"
    assert len(tap.execute_calls) == 1


def test_unknown_round_identity_refuses_real_tap(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, hud=[(None, None)])
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_round_identity"
    assert tap.execute_calls == []


def test_manual_caishen_conflict_on_first_frame_stops_before_tap(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, caishen_label="s1", hud=[(None, "t1")])
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_manual_caishen"
    assert tap.execute_calls == []


def test_occluded_hud_does_not_fall_back_to_raw_pixels(tmp_path, monkeypatch):
    import assist_window

    frame = tmp_path / "occluded.png"
    frame.write_bytes(b"data")
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda _path: None)
    assert hud_fingerprint(frame) is None


def test_default_round_key_uses_visible_caishen_with_occluded_hud(tmp_path):
    frame = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_002903"
             / "live" / "tea_2p" / "20261024_004902_334879_4f692d.png")
    if not frame.is_file():
        pytest.skip("t1 实战帧缺失")
    loop, *_ = make_loop(tmp_path, enabled=False)
    hud, caishen = loop._default_round_key(frame)
    assert hud is None
    assert caishen == "t1"


def test_caishen_change_resets_round_state(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=False,
        analyses=[advise(caishen="w1"), advise(caishen="t9")],
        hud=[(HUD_A, None)])

    loop.tick()
    assert loop.round.caishen == "w1"
    assert loop.round.seq == 1                   # 首个 advise 周期即 1

    loop.tick()
    assert loop.round.caishen == "t9"
    assert loop.round.seq == 2
    assert len(events_of(loop, "round_reset")) == 1


def test_same_round_stays_stable_with_tolerant_hud(tmp_path):
    nudged = bytearray(HUD_A)
    nudged[3] = (nudged[3] + 1) % 256            # 单字节抖动：视为同局
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=False,
        analyses=[advise(status="wait", suggested_boxes=[])],
        hud=[(HUD_A, None), (bytes(nudged), None)])

    loop.tick()
    loop.tick()
    assert loop.round.seq == 1                 # 单字节抖动不换局
    assert events_of(loop, "round_reset") == []


def test_same_hud_tolerance_and_unknown_fingerprint():
    assert _same_hud(HUD_A, HUD_A) is True
    assert _same_hud(HUD_A, HUD_B) is False
    assert _same_hud(HUD_A, HUD_A[:-1]) is False       # 长度不同直接判不同局
    nudged = bytearray(HUD_A)
    nudged[0] = (nudged[0] + 1) % 256
    assert _same_hud(HUD_A, bytes(nudged)) is True     # ≤8 字节差异容忍
    assert hud_fingerprint(None) is None
    assert hud_fingerprint("C:/definitely/not/here.png") is None

    state = RoundState()
    assert state.same_round(None, None) is True        # 全未知：保守按同局


# ----------------------------------------------------------------------
# 7. 房卡 0 停机 / 读不到不阻断
# ----------------------------------------------------------------------
def test_card_zero_stops_the_loop_without_tapping(tmp_path):
    loop, shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True, cards=[0])
    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "card_exhausted"
    assert loop.stopped is True
    assert loop.card_count == 0
    assert shot.calls == 1                   # 只抓了一张用于读数
    assert analyzer.calls == 0
    assert tap.execute_calls == []


def test_card_unknown_does_not_block_and_never_clicks_recharge(tmp_path):
    def broken_reader(_image):
        raise RuntimeError("房卡区域读不到")

    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, cards=broken_reader)
    settle(loop)
    event = loop.tick()

    assert event["event"] == "action"
    assert loop.card_count is None            # 记 unknown
    assert loop.stopped is False
    assert event["card_count"] is None
    assert len(tap.execute_calls) == 1
    # 唯一一次外部动作就是 TapExecutor.execute，不存在“充值”兜底。
    assert not any(call.get("status") == "recharge" for call in tap.execute_calls)


# ----------------------------------------------------------------------
# 8. 几何越界拒绝：立即停手，不重试
# ----------------------------------------------------------------------
def test_geometry_rejection_stops_without_retry(tmp_path):
    rejected = TapRejected("目标 y=430 不在手牌 ROI（y≥720）——充值/开始游戏区域，拒绝点击")
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, tap=FakeTap(execute_error=rejected))
    settle(loop)

    first = loop.tick()
    assert first["event"] == "stop"
    assert first["gate"] == "tap_aborted"
    assert "手牌 ROI" in first["reason"]
    assert len(tap.execute_calls) == 1        # 本帧只尝试一次
    assert loop.stopped is True
    assert loop.tick()["event"] == "skip"
    assert len(tap.execute_calls) == 1


def test_dry_run_plan_rejection_is_an_error_not_a_tap(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=False,
        tap=FakeTap(plan_error=TapRejected("建议牌像素框无效")))
    settle(loop)
    event = loop.tick()

    assert event["event"] == "error"
    assert event["error"] == "plan_rejected"
    assert tap.execute_calls == []


# ----------------------------------------------------------------------
# 台账 / 构造期安全约束
# ----------------------------------------------------------------------
def test_ledger_header_is_dev_tagged_and_events_are_jsonl(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(tmp_path, enabled=False)
    settle(loop)
    loop.tick()

    rows = read_ledger(loop)
    header = rows[0]
    assert header["dev"] is True
    assert "不进训练语料" in header["note"]
    assert header["dry_run"] is True
    assert header["mode"] == "tea_2p"
    # 第一条是门禁预热（wait），第二条才是计划。每条事件都带 turn_gate 证据。
    assert [row.get("event") for row in rows[1:]] == ["idle", "dry_run_plan"]
    assert all("turn_gate" in row for row in rows[1:])
    plan_row = rows[-1]
    assert plan_row["frame"].endswith(".png")
    assert Path(plan_row["frame"]).is_file()
    assert loop.ledger_path.name == "actions.jsonl"
    assert loop.session_dir.name == "session"


def test_enabled_requires_explicit_bool_and_tea_2p_mode(tmp_path):
    with pytest.raises(TypeError, match="enabled 必须是显式 bool"):
        AutoPlayLoop(enabled="yes", session_dir=tmp_path / "a")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tea_2p"):
        AutoPlayLoop(mode="tea_4p", session_dir=tmp_path / "b")
    # 非法构造不应留下任何会话目录
    assert not (tmp_path / "a").exists()
    assert not (tmp_path / "b").exists()


def test_run_stops_at_max_actions_and_reports_summary(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, max_actions=2)
    summary = loop.run()

    assert summary["actions"] == 2
    assert summary["stopped"] is True
    assert "max-actions=2" in summary["stop_reason"]
    assert summary["dry_run"] is False
    assert len(tap.execute_calls) == 2
    assert Path(summary["ledger"]).is_file()


def test_runner_defaults_to_dry_run_and_needs_both_switches(tmp_path):
    """运行器：默认 dry_run；--enable 与 --no-dry-run 缺一个都退回 dry_run。"""
    import run_auto_play_dev as runner

    parser = runner.build_parser()
    default_args = parser.parse_args([])
    assert default_args.mode == "tea_2p"
    assert default_args.max_actions == 20
    assert runner.resolve_enabled(default_args) is False

    only_enable = parser.parse_args(["--enable"])
    assert runner.resolve_enabled(only_enable) is False      # 缺 --no-dry-run
    only_live = parser.parse_args(["--no-dry-run"])
    assert runner.resolve_enabled(only_live) is False        # 缺 --enable
    both = parser.parse_args(["--enable", "--no-dry-run"])
    assert runner.resolve_enabled(both) is True

    assert runner.resolve_caishen("auto") == ""
    assert runner.resolve_caishen("") == ""
    assert runner.resolve_caishen("w1") == "w1"


def test_runner_exits_nonzero_when_adb_missing(tmp_path, capsys):
    """自检不过：非零退出码，且一帧都不抓（离线，不需要设备）。"""
    import run_auto_play_dev as runner

    session = tmp_path / "cli_session"
    code = runner.main(["--check-only", "--adb",
                        str(tmp_path / "nope" / "adb.exe"),
                        "--session", str(session)])
    assert code == runner.EXIT_GATE_BLOCKED
    assert "门禁拦截" in capsys.readouterr().out
    assert not session.exists()          # 自检失败不留空目录


def test_run_stops_on_duration(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, duration_s=0.02, max_actions=99)
    summary = loop.run()

    assert summary["stopped"] is True
    assert "运行时长" in summary["stop_reason"]
    assert summary["actions"] <= 1
    assert len(tap.execute_calls) <= 1


# ----------------------------------------------------------------------
# 9. 回合所有权门禁（turn_gate）接进 tick()
# ----------------------------------------------------------------------
def test_turn_gate_wait_on_sixteen_tiles_is_idle_and_costs_no_budget(tmp_path):
    """16 张（未摸牌）：只等待，零点击，不计连续异常，不停机。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(hand_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7],
                         hand_boxes=hand_boxes(16))])

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["turn_gate"]["state"] == "wait"
    assert "未摸牌" in event["reason"]
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert loop.stopped is False
    assert loop.consecutive_errors == 0
    assert loop.actions_done == 0


def test_turn_gate_blocked_on_contradictory_count_taps_nothing(tmp_path):
    """18 张：证据自相矛盾 → gate_turn 停机，零点击、无补点。"""
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(hand_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                         hand_boxes=hand_boxes(18))])

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_turn"
    assert "自相矛盾" in event["reason"]
    assert event["turn_gate"]["state"] == "blocked"
    assert event["turn_gate"]["evidence"]["physical_boxes"] == 18   # 守恒按物理框数
    assert event["turn_gate"]["evidence"]["accepted_tiles"] == 18
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert analyzer.calls == 1                 # 本帧只分析一次，不重试
    assert loop.stopped is True
    assert loop.tick()["event"] == "skip"
    assert tap.execute_calls == []


def test_turn_gate_requires_two_stable_frames_before_tapping(tmp_path):
    """第一帧只建立稳定基线；第二帧手牌一致才允许点。"""
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    first = loop.tick()
    assert first["event"] == "idle"
    assert first["turn_gate"]["state"] == "wait"
    assert tap.execute_calls == []
    assert len(loop.turn_history) == 1

    second = loop.tick()
    assert second["event"] == "action"
    assert second["turn_gate"]["state"] == "discard_ready"
    assert second["turn_gate"]["evidence"]["stable_frames"] == 2
    assert len(tap.execute_calls) == 1


def test_turn_gate_wait_when_hand_changes_between_frames(tmp_path):
    """相邻两帧手牌不同（摸牌/发牌动画）：仍然只等待。"""
    changed = advise(hand_ids=[9, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8])
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, analyses=[advise(), changed, changed])

    assert loop.tick()["event"] == "idle"
    second = loop.tick()
    assert second["event"] == "idle"
    assert second["turn_gate"]["evidence"]["stable_frames"] == 1
    assert tap.execute_calls == []

    third = loop.tick()
    assert third["event"] == "action"
    assert len(tap.execute_calls) == 1


def test_turn_gate_blocks_dimmed_hand_in_real_loop(tmp_path):
    """整手压暗（「请出牌」前的过渡）：dry_run 下也只等待、不生成计划。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=False, frames=[make_frame(100)])

    settle_events = [loop.tick() for _ in range(2)]

    assert [item["event"] for item in settle_events] == ["idle", "idle"]
    assert all(item["turn_gate"]["state"] == "wait" for item in settle_events)
    assert "压暗" in settle_events[-1]["reason"]
    assert tap.plan_calls == [] and tap.execute_calls == []


def test_turn_gate_evidence_is_written_to_every_ledger_event(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(tmp_path, enabled=False)
    settle(loop)
    loop.tick()

    rows = [row for row in read_ledger(loop) if "event" in row]
    for row in rows:
        gate = row["turn_gate"]
        assert set(gate) == {"state", "reason", "code", "evidence"}
        assert gate["state"] in {"discard_ready", "wait", "blocked"}
        assert "physical_boxes" in gate["evidence"]
        assert "accepted_tiles" in gate["evidence"]
        assert "concealed_count" in gate["evidence"]
        assert "meld_total" in gate["evidence"]
        assert "hand_face_mean_p70" in gate["evidence"]
        assert "settled_signals" in gate["evidence"]
    assert rows[-1]["turn_gate"]["state"] == "discard_ready"


# ----------------------------------------------------------------------
# 10. 结算 / 牌局结束 / 模态弹窗：必须停机且零点击
# ----------------------------------------------------------------------
SETTLED_FRAME = (BOT_DIR.parent / "采集" / "dev_auto_play" / "takeover_live" /
                 "wb_probe_043707.png")


def test_settled_frame_stops_with_gate_turn_and_zero_taps(tmp_path):
    """实机帧「牌局结束 + 多数玩家同意解散比赛！」：绝不点「确定」。"""
    if not SETTLED_FRAME.is_file():
        pytest.skip("结算证据帧缺失")
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[SETTLED_FRAME.read_bytes()])

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_turn"
    assert "牌局已结束" in event["reason"]
    assert event["turn_gate"]["state"] == "blocked"
    assert event["turn_gate"]["code"] == "gate_settled"   # 具体归类在同一行
    assert set(event["turn_gate"]["evidence"]["settled_signals"]) == {
        "central_panel", "bottom_blackout"}
    assert tap.execute_calls == [] and tap.plan_calls == []   # 零点击
    assert analyzer.calls == 1                               # 只分析一次
    assert loop.stopped is True
    assert loop.tick()["event"] == "skip"
    assert tap.execute_calls == []


def test_settled_frame_never_even_plans_in_dry_run(tmp_path):
    """dry_run 下同样零计划：结算画面连坐标都不该算。"""
    if not SETTLED_FRAME.is_file():
        pytest.skip("结算证据帧缺失")
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=False, frames=[SETTLED_FRAME.read_bytes()])

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_turn"
    assert event["turn_gate"]["code"] == "gate_settled"
    assert tap.plan_calls == [] and tap.execute_calls == []
    assert loop.actions_done == 0


# ----------------------------------------------------------------------
# 11. 有牌没读出：只等待、零点击（旧实现会 blocked 硬停机）
# ----------------------------------------------------------------------
def test_unread_tiles_wait_without_tapping(tmp_path):
    """17 个物理牌框只读出 15 张 → wait + 零点击 + 不停机。"""
    all_boxes = hand_boxes(17)
    accepted = [1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6]
    diagnostics = [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(accepted, all_boxes[:len(accepted)])
    ]
    diagnostics += [
        {"tile_id": None, "label": None, "box": list(box), "accepted": False,
         "region": "concealed", "score": .5, "margin": .0, "reason": "low_score"}
        for box in all_boxes[len(accepted):]
    ]
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(hand_ids=accepted, vision_diagnostics=diagnostics)])

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["turn_gate"]["state"] == "wait"
    assert "有牌未读出" in event["reason"]
    assert (event["turn_gate"]["evidence"]["physical_boxes"],
            event["turn_gate"]["evidence"]["accepted_tiles"]) == (17, 15)
    assert tap.execute_calls == [] and tap.plan_calls == []   # 零点击
    assert loop.stopped is False                              # 不停机
    assert loop.consecutive_errors == 0


def test_dimmed_and_unread_wait_on_dimming_not_tile_count(tmp_path):
    """压暗 + 读不全：reason 必须是"压暗"（顺序：亮度先于张数）。"""
    all_boxes = hand_boxes(17)
    accepted = [1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2]
    diagnostics = [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(accepted, all_boxes[:len(accepted)])
    ]
    diagnostics += [
        {"tile_id": None, "label": None, "box": list(box), "accepted": False,
         "region": "concealed", "score": .5, "margin": .0, "reason": "low_score"}
        for box in all_boxes[len(accepted):]
    ]
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[make_frame(106)],
        analyses=[advise(hand_ids=accepted, vision_diagnostics=diagnostics)])

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["turn_gate"]["state"] == "wait"
    assert "压暗" in event["reason"]
    assert "张数" not in event["reason"]
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert loop.stopped is False


# ----------------------------------------------------------------------
# 行动行（吃/碰/杠/胡/过）：决策只落台账，默认不点击
# ----------------------------------------------------------------------
def action_row_frame() -> bytes:
    """1600x900 帧：动作行右侧画一个金盘（吃碰杠）加一个蓝盘（过）。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = (30, 120, 40)
    gold = cv2.cvtColor(np.uint8([[[25, 200, 220]]]), cv2.COLOR_HSV2BGR)[0][0]
    blue = cv2.cvtColor(np.uint8([[[95, 200, 160]]]), cv2.COLOR_HSV2BGR)[0][0]
    cv2.circle(frame, (1033, 600), 66, tuple(int(c) for c in gold), thickness=-1)
    cv2.circle(frame, (1431, 600), 50, tuple(int(c) for c in blue), thickness=-1)
    ok, encoded = cv2.imencode(".png", frame)
    assert ok
    return encoded.tobytes()


CLAIM_GUARD = lambda _image: ActionGuardResult(  # noqa: E731
    "action_visible", ((1383, 552, 1483, 652),), "检测到行动界面")

#: 上一帧识牌结果（视觉编码）：1,1,2,4,5,6,7,8,9 万 + 1234 筒 + 123 条 = 16 张。
CLAIM_HAND_ANALYSIS = {
    "hand_ids": [1, 1, 2, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43],
    "melds": [], "caishen": None, "mode": "tea_2p",
}


def test_claim_row_decision_is_logged_and_never_tapped(tmp_path):
    """行动行可见 → 跑一次决策闸门并落台账，但零点击。"""
    loop, _shot, analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD)

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["guard"] == "action_visible"
    assert "claim" in event, "行动行必须留下决策证据"
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert loop.stopped is False


def test_claim_row_without_incoming_tile_stays_blocked(tmp_path):
    """来牌链路未接线 → 金盘在也只能拦下，绝不点。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD, analyses=[CLAIM_HAND_ANALYSIS])

    event = loop.tick()

    assert event["claim"]["status"] == "blocked"
    assert "来牌未知" in json.dumps(event["claim"], ensure_ascii=False)
    assert event["claim"]["tap_point"] is None
    assert tap.execute_calls == []


def test_claim_auto_click_switch_does_not_tap_before_channel_is_wired(tmp_path):
    """``claim_auto_click=True`` 也不许发点击：通道还没接线。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD, claim_auto_click=True)
    loop._last_result = dict(CLAIM_HAND_ANALYSIS)

    event = loop.tick()

    assert event["claim"]["status"] in ("blocked", "awaiting_human_confirm", "abstain")
    assert event["claim"].get("auto_click") is None
    assert tap.execute_calls == [] and tap.plan_calls == []


def test_claim_row_uses_discard_vision_incoming_tile(tmp_path):
    """来牌接进来后，L2 能验证"碰"——但仍然不点（未过人工确认闸门）。"""
    from discard_vision import DiscardState

    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD,
        discard_fn=lambda _img: DiscardState(incoming_tile=1,
                                             incoming_source="opponent"),
        analyses=[CLAIM_HAND_ANALYSIS])

    event = loop.tick()

    assert event["claim"]["status"] == "awaiting_human_confirm"
    assert event["claim"]["action"] == "peng"
    assert event["claim"]["needs_human_confirm"] is True
    assert tap.execute_calls == []


def test_claim_row_rejects_self_discard(tmp_path):
    """自家刚打出的牌不许碰——行动行常在自家出牌后刷出来。"""
    from discard_vision import DiscardState

    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD,
        discard_fn=lambda _img: DiscardState(incoming_tile=1,
                                             incoming_source="self"))
    loop._last_result = dict(CLAIM_HAND_ANALYSIS)

    event = loop.tick()

    assert event["claim"]["status"] == "blocked"
    assert "self" in json.dumps(event["claim"], ensure_ascii=False)
    assert event["claim"]["tap_point"] is None
    assert tap.execute_calls == []


def test_discard_reading_can_be_switched_off(tmp_path):
    """``read_discards=False`` 时一次都不读（排障/降负载用）。"""
    calls = []

    def spy(_img):
        calls.append(1)
        return None

    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, frames=[action_row_frame()], action_guard=CLAIM_GUARD,
        read_discards=False, discard_fn=spy)

    loop.tick()

    assert calls == []


def test_round_change_forgets_river_baseline_before_reading(tmp_path):
    """换局帧：必须先作废牌河基线再读本帧（否则拿上一局的牌河做差分）。

    guard 早退分支在正式换局判定之前就会用 ``_discard_state``，所以顺序
    只能是 forget → read。
    """
    order: list[str] = []

    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, frames=[action_row_frame()], action_guard=CLAIM_GUARD,
        discard_fn=lambda _img: None, hud=[(HUD_B, None)])
    # 模拟"本局身份是上一帧的正常帧登记的"，本帧 HUD 变成另一局。
    loop.round.hud_key = HUD_A
    loop.round.caishen = None

    real_forget = loop._forget_discards

    def spy_forget() -> None:
        order.append("forget")
        real_forget()

    loop._forget_discards = spy_forget
    loop._discard = lambda _img: order.append("read")

    loop.tick()

    assert "read" in order, "牌河必须被读"
    assert "forget" in order, "换局帧必须先作废基线"
    assert order.index("forget") < order.index("read")


def test_discard_read_failure_forgets_baseline_too(tmp_path):
    """读牌河抛异常：来牌置空**且**基线作废，异常不许外泄。"""
    order: list[str] = []

    def boom(_img):
        raise RuntimeError("牌河读取炸了")

    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, frames=[action_row_frame()], action_guard=CLAIM_GUARD,
        discard_fn=boom)

    real_forget = loop._forget_discards

    def spy_forget() -> None:
        order.append("forget")
        real_forget()

    loop._forget_discards = spy_forget

    event = loop.tick()

    assert loop._discard_state is None
    assert "forget" in order, "异常帧的基线也要作废"
    assert loop.stopped is False, "读牌河失败不许停机"
    assert event["claim"]["status"] == "blocked"


def test_round_change_frame_never_feeds_discards_to_l2(tmp_path):
    """换局未确认的帧：本帧来牌不许喂给 L2（哪怕它读出了牌）。

    早判只看画面读到的 HUD/财神，正式判定还会看 analyze 的财神；这一帧两者
    之间有缝，所以宁可不给来牌——L2 因此走"来牌未知"弃权。
    """
    from discard_vision import DiscardState

    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, frames=[action_row_frame()], action_guard=CLAIM_GUARD,
        discard_fn=lambda _img: DiscardState(incoming_tile=1,
                                             incoming_source="opponent"),
        hud=[(HUD_B, None)], analyses=[CLAIM_HAND_ANALYSIS])
    loop.round.hud_key = HUD_A          # 本帧 HUD 变成本帧之外的另一局

    event = loop.tick()

    assert event["claim"]["status"] == "blocked", event["claim"]
    assert "来牌未知" in json.dumps(event["claim"], ensure_ascii=False)
    assert event["claim"]["tap_point"] is None
    assert tap.execute_calls == []
