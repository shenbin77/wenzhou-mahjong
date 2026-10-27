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

import itertools
import json
import string
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import auto_play_loop  # noqa: E402
import action_policy as action_policy_module  # noqa: E402
from auto_play_loop import (  # noqa: E402
    MAX_CONSECUTIVE_ERRORS,
    AutoPlayLoop,
    GateBlocked,
    RoundState,
    _same_hud,
    hud_fingerprint,
)
from action_guard import ActionGuardResult  # noqa: E402
from action_policy import (  # noqa: E402
    ACTION_CHI, ACTION_HU, ACTION_KONG, ACTION_PASS, ACTION_PENG,
    ActionDecision, ActionPolicyConfig, BoardState, HandState, decide_action,
    recommended_runner_config,
)
from claim_vision import ClaimButton, ClaimState, read_claims  # noqa: E402
from harvest_tea import EXPECTED_PACKAGE  # noqa: E402
from invite_dialog import InvitationState  # noqa: E402
from match_score import MatchScoreState, read_match_score  # noqa: E402
from promo_popup import PromoPopupState  # noqa: E402
from round_transition import TransitionResult  # noqa: E402
from tap_executor import TapPlan, TapRejected  # noqa: E402
from timeout_dissolve import TimeoutDissolveState  # noqa: E402

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


def test_direct_discard_redraw_proof_requires_exact_multiset_transition():
    before = {
        "mode": "tea_2p", "caishen_id": 8,
        "hand_ids": [3, 4, 9, 43, 44, 45, 46, 47, 49, 22, 23, 25, 25, 29],
    }
    after = {
        "mode": "tea_2p", "caishen_id": 8,
        "hand_ids": [3, 4, 43, 44, 45, 46, 47, 49, 22, 23, 25, 25, 29, 6],
    }
    proof = auto_play_loop.direct_discard_redraw_proof(before, after, 9)
    assert proof["discarded"] is True
    assert proof["removed"] == [9]
    assert proof["added"] == [6]
    assert proof["after_count"] == 14

    unchanged = auto_play_loop.direct_discard_redraw_proof(before, before, 9)
    assert unchanged["discarded"] is False
    wrong_target = auto_play_loop.direct_discard_redraw_proof(before, after, 4)
    assert wrong_target["discarded"] is False
    wrong_caishen = dict(after, caishen_id=9)
    assert auto_play_loop.direct_discard_redraw_proof(
        before, wrong_caishen, 9)["discarded"] is False


def test_direct_discard_proof_accepts_verified_n_to_n_minus_one_with_melds():
    """实机 214616：裸框误把暗手前三张切成副露，完整分析仍读对 14→13。"""
    before = {
        "mode": "tea_2p", "caishen_id": 8,
        "melds": [[3, 4, 5]],
        "hand_ids": [7, 67, 9, 6, 67, 46, 48, 48, 26, 27, 27, 28, 29, 3],
    }
    after = {
        "mode": "tea_2p", "caishen_id": 8,
        "melds": [[3, 4, 5]],
        "hand_ids": [7, 67, 9, 6, 67, 46, 48, 48, 26, 27, 27, 28, 29],
    }

    proof = auto_play_loop.direct_discard_redraw_proof(before, after, 3)

    assert proof["discarded"] is True
    assert proof["after_count"] == 13
    assert proof["removed"] == [3]
    assert proof["added"] == []
    assert "禁止第二击" in proof["reason"]

    changed_meld = dict(after, melds=[[3, 3, 3]])
    assert auto_play_loop.direct_discard_redraw_proof(
        before, changed_meld, 3)["discarded"] is False


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
        self.execute_claim_calls: list[dict] = []
        self.execute_invitation_calls: list[dict] = []
        self.execute_leave_calls: list[dict] = []
        self.execute_close_popup_calls: list[dict] = []
        self.execute_timeout_dissolve_calls: list[dict] = []

    def plan(self, analysis):
        self.plan_calls.append(analysis)
        if self.plan_error is not None:
            raise self.plan_error
        return self.plan_result

    def execute(self, analysis, **_kwargs):
        self.execute_calls.append(analysis)
        if self.execute_error is not None:
            raise self.execute_error
        return dict(self.execute_result)

    def execute_claim(self, point, **kwargs):
        self.execute_claim_calls.append({"point": point, **kwargs})
        return {"executed": True, "dry_run": False, "stage": "clicked",
                "tap": list(point), "ui_responded": True}

    def execute_leave_table(self, point=None, **kwargs):
        self.execute_leave_calls.append({"point": point, **kwargs})
        return {"executed": True, "dry_run": False, "stage": "verified",
                "action": "leave_table", "tap": [400, 824],
                "box": [246, 777, 554, 872], "verified": True,
                "reason": "牌局结束版式双帧确认，点击后连续两帧消失"}

    def execute_invitation_cancel(self, point, **kwargs):
        self.execute_invitation_calls.append({"point": point, **kwargs})
        return {"executed": True, "dry_run": False, "stage": "verified",
                "tap": list(point), "verified": True}

    def execute_close_popup(self, point=None, **kwargs):
        self.execute_close_popup_calls.append({"point": point, **kwargs})
        return {"executed": True, "dry_run": False, "stage": "verified",
                "action": "close_promo_popup",
                "tap": list(point or (1466, 190)),
                "box": [1438, 160, 1495, 221], "verified": True,
                "reason": "促销弹窗版式双帧确认，点击后连续两帧不再命中"}

    def execute_timeout_dissolve_confirm(self, point=None, **kwargs):
        self.execute_timeout_dissolve_calls.append({"point": point, **kwargs})
        return {
            "executed": True, "dry_run": False, "stage": "verified",
            "action": "confirm_timeout_dissolve",
            "tap": list(point or (800, 585)),
            "box": [692, 541, 908, 630], "verified": True,
            "reason": "精确超时解散弹窗双帧确认，单击后连续两帧消失",
        }


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
    # adb 现场枚举默认是**离线替身**：本文件全程不接触真实模拟器，所以这里
    # 绝不能落到真实的 adb_runtime.adb_inventory()（会真起 adb devices 子进程）。
    # 替身把测试 serial 报成在线，保证 check_gates() 的设备门禁走得通。
    inventory = kwargs.pop("adb_inventory_fn", None) or (
        lambda: {"chosen": sys.executable, "version": "adb-fake-1.0.41",
                 "servers": ["18368"], "devices": ["emulator-test"],
                 "found": [sys.executable], "notes": ["offline test double"]})
    score_reader = kwargs.pop("match_score_fn", None) or (
        lambda _image, **_kwargs: MatchScoreState(
            True, left_player="玉姐", right_player="拾则",
            left_score=-59, right_score=59, confidence=.99,
            reason="离线假整场积分"))
    loop = AutoPlayLoop(
        enabled=enabled,
        adb_path=sys.executable,          # 只做 is_file() 检查，不会被执行
        serial="emulator-test",
        adb_inventory_fn=inventory,
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
        player_name=kwargs.pop("player_name", "玉姐"),
        match_score_fn=score_reader,
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


def test_invitation_is_declined_before_any_game_analysis(monkeypatch, tmp_path):
    state = InvitationState(
        present=True, complete=True, phrase_score=.99, cancel_score=.99,
        phrase_box=(765, 360, 935, 399),
        cancel_box=(508, 540, 753, 640), cancel_center=(630, 590),
        reason="邀请证据完整",
    )
    monkeypatch.setattr(auto_play_loop, "read_invitation", lambda _image: state)
    loop, _shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "invitation_declined"
    assert event["invitation_cancel"]["verified"] is True
    assert analyzer.calls == 0
    assert tap.execute_calls == [] and tap.execute_claim_calls == []
    assert len(tap.execute_invitation_calls) == 1
    assert tap.execute_invitation_calls[0]["point"] == (630, 590)
    assert loop.actions_done == 0 and loop.stopped is False


def test_incomplete_invitation_stops_without_clicking_anywhere(monkeypatch,
                                                                tmp_path):
    state = InvitationState(
        present=True, complete=False, phrase_score=.99, cancel_score=.4,
        phrase_box=(765, 360, 935, 399),
        reason="命中邀请文案，但矩形取消按钮证据不足",
    )
    monkeypatch.setattr(auto_play_loop, "read_invitation", lambda _image: state)
    loop, _shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_invitation_unresolved"
    assert analyzer.calls == 0
    assert tap.execute_invitation_calls == []
    assert tap.execute_calls == [] and tap.execute_claim_calls == []


def _timeout_dissolve_state(*, complete: bool) -> TimeoutDissolveState:
    return TimeoutDissolveState(
        present=True,
        complete=complete,
        confirm_box=(692, 541, 908, 630) if complete else None,
        confirm_center=(800, 585) if complete else None,
        panel_box=(476, 222, 1151, 654),
        reason=("精确证据完整" if complete else "按钮证据不完整"),
        evidence={"checks": {"message": True, "panel": True,
                             "overlay": True, "button": complete}},
    )


def test_timeout_dissolve_confirms_before_analysis_then_stops_match(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        auto_play_loop, "read_timeout_dissolve",
        lambda _image: _timeout_dissolve_state(complete=True))
    loop, _shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "match_dissolved"
    assert event["timeout_dissolve_confirm"]["verified"] is True
    assert analyzer.calls == 0
    assert len(tap.execute_timeout_dissolve_calls) == 1
    assert tap.execute_timeout_dissolve_calls[0]["point"] == (800, 585)
    assert tap.execute_calls == [] and tap.execute_claim_calls == []
    assert loop.stopped is True


def test_timeout_dissolve_incomplete_evidence_stops_without_click(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        auto_play_loop, "read_timeout_dissolve",
        lambda _image: _timeout_dissolve_state(complete=False))
    loop, _shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "gate_timeout_dissolve_unresolved"
    assert analyzer.calls == 0
    assert tap.execute_timeout_dissolve_calls == []
    assert tap.execute_calls == [] and tap.execute_claim_calls == []


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
    assert event["hand_ledger_discard"] == {"tile_id": 4, "applied": True}
    assert loop._hand_ledger.memory is not None
    assert len(loop._hand_ledger.memory.tiles) == 16
    assert loop._hand_ledger.memory.tiles.count(4) == 1


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


def test_run_ledgers_unhandled_exception_before_reraising(tmp_path, monkeypatch):
    loop, *_ = make_loop(tmp_path, enabled=False)

    def explode():
        raise RuntimeError("boom-for-ledger")

    monkeypatch.setattr(loop, "tick", explode)
    with pytest.raises(RuntimeError, match="boom-for-ledger"):
        loop.run()
    stop = events_of(loop, "stop")[-1]
    assert stop["gate"] == "unhandled_exception"
    assert "RuntimeError: boom-for-ledger" in stop["traceback"]
    assert stop["device_serial"] == "emulator-test"


# ----------------------------------------------------------------------
# 6. 换局重置
# ----------------------------------------------------------------------
def test_hud_change_resets_round_state(tmp_path):
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=False,
        analyses=[advise(status="wait", suggested_boxes=[])],
        hud=[(HUD_A, None), (HUD_B, None), (HUD_B, None)])

    assert loop.tick()["event"] == "idle"
    assert loop.round.hud_key == HUD_A
    assert loop.round.seq == 1                 # 会话第一局从 1 起

    suspect = loop.tick()
    assert suspect["event"] == "idle"
    assert suspect["round_signal"]["state"] == "round_suspect"
    assert loop.round.hud_key == HUD_A
    assert loop.tick()["event"] == "idle"
    assert loop.round.hud_key == HUD_B
    assert loop.round.seq == 2
    resets = events_of(loop, "round_reset")
    assert len(resets) == 1 and resets[0]["round_seq"] == 2


def test_hud_change_with_advice_skips_the_change_frame(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        hud=[(HUD_A, None), (HUD_A, None), (HUD_B, None), (HUD_B, None)])
    settle(loop)
    assert loop.tick()["event"] == "action"
    assert loop.tick()["event"] == "idle"
    assert loop.tick()["event"] == "idle"
    assert loop.round.seq == 2
    assert len(tap.execute_calls) == 1
    assert loop.turn_history == []             # 换局后稳定帧必须重新攒


def test_manual_caishen_round_change_requires_new_confirmation(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, caishen_label="w1",
        hud=[(HUD_A, None), (HUD_A, None), (HUD_B, None), (HUD_B, None)])
    settle(loop)
    assert loop.tick()["event"] == "action"
    assert loop.tick()["event"] == "idle"
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_manual_caishen"
    assert len(tap.execute_calls) == 1


def test_unknown_round_identity_refuses_real_tap(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, hud=[(None, None), (None, None), (None, None)])
    assert loop.tick()["event"] == "idle"
    assert loop.tick()["event"] == "idle"
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_round_identity"
    assert tap.execute_calls == []


def test_manual_caishen_conflict_requires_two_frames_before_stop(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, caishen_label="s1",
        hud=[(None, "t1"), (None, "t1")])
    assert loop.tick()["event"] == "idle"
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
        analyses=[advise(caishen="w1"), advise(caishen="t9"),
                  advise(caishen="t9")],
        hud=[(HUD_A, None)])

    loop.tick()
    assert loop.round.caishen == "w1"
    assert loop.round.seq == 1                   # 首个 advise 周期即 1

    assert loop.tick()["round_signal"]["state"] == "round_suspect"
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




def test_wall_cards_at_16_stops_without_tapping(tmp_path):
    """P0-5: 牌墙/房卡 ≤ 16 → card_exhausted 停机，不点。"""
    loop, shot, analyzer, tap, _ = make_loop(tmp_path, enabled=True, cards=[16])
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "card_exhausted"
    assert loop.stopped is True
    assert loop.card_count == 16
    assert analyzer.calls == 0
    assert tap.execute_calls == []


def test_wall_cards_above_16_does_not_trip_wall_stop(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True, cards=[17])
    settle(loop)
    event = loop.tick()
    assert event.get("gate") != "card_exhausted"
    assert loop.stopped is False or event.get("gate") != "card_exhausted"
    assert tap.execute_calls or event["event"] in {"action", "dry_run_plan", "idle", "stop"}


def test_unknown_caishen_stops_when_enabled(tmp_path):
    """P0-5: 单帧坏读先重拍；连续三帧未知才 gate_unknown_caishen。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(caishen=None)],
        hud=[(b"hud-ok", None)],
    )
    first = loop.tick()
    second = loop.tick()
    event = loop.tick()

    assert first["event"] == "idle"
    assert first["status"] == "abstain"
    assert first["unknown_caishen_ticks"] == 1
    assert second["event"] == "idle"
    assert second["unknown_caishen_ticks"] == 2
    assert event["event"] == "stop"
    assert event["gate"] == "gate_unknown_caishen"
    assert event["unknown_caishen_ticks"] == 3
    assert tap.execute_calls == []
    assert "table_state" in event
    assert event["table_state"]["caishen_known"] is False


def test_unknown_caishen_budget_recovers_when_next_frame_is_known(tmp_path):
    """两帧漏读后恢复：清空预算，仍需正常回合门禁，不能补点。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(caishen=None), advise(caishen=None),
                  advise(caishen="w1")],
        hud=[(b"hud-ok", None)],
    )

    assert loop.tick()["unknown_caishen_ticks"] == 1
    assert loop.tick()["unknown_caishen_ticks"] == 2
    recovered = loop.tick()

    assert recovered.get("gate") != "gate_unknown_caishen"
    assert loop._unknown_caishen_ticks == 0
    assert loop.stopped is False
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


def test_preclick_fresh_frame_exhaustion_is_bounded_recovery_not_terminal(
        tmp_path):
    error = auto_play_loop.TapPrecheckRetryExhausted(
        "点击前连续 3 帧复核失败，未发送点击：副露瞬时误读")
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, tap=FakeTap(execute_error=error))
    settle(loop)
    event = loop.tick()
    assert event["event"] == "error"
    assert event["error"] == "discard_precheck_retry_exhausted"
    assert event["consecutive_errors"] == 1
    assert loop.stopped is False
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


def test_event_callback_is_observer_only_and_request_stop_is_logged(tmp_path):
    seen = []
    loop, *_ = make_loop(
        tmp_path, enabled=False,
        event_callback=lambda event, analysis: seen.append((event, analysis)))
    loop.request_stop("UI stop")
    summary = loop.run()

    assert summary["stopped"] is True
    assert summary["stop_reason"] == "UI stop"
    assert seen and seen[-1][0]["event"] == "stop"
    assert seen[-1][0]["gate"] == "user_stop"


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


def selection_cancel_frame() -> bytes:
    """1600x900 候选面板：只有一个高位蓝色圆形“取消”。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = (30, 120, 40)
    blue = cv2.cvtColor(np.uint8([[[95, 200, 160]]]), cv2.COLOR_HSV2BGR)[0][0]
    cv2.circle(frame, (1300, 380), 50, tuple(int(c) for c in blue), thickness=-1)
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


def test_claim_auto_click_abstains_when_template_gate_has_no_glyph(tmp_path):
    """通道已接线，但合成纯色圆盘没有字形模板证据，仍须零点击。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[action_row_frame()],
        action_guard=CLAIM_GUARD, claim_auto_click=True)
    loop._last_result = dict(CLAIM_HAND_ANALYSIS)

    event = loop.tick()

    assert event["claim"]["status"] in ("blocked", "awaiting_human_confirm", "abstain")
    assert event["claim"].get("auto_click") is None
    assert tap.execute_calls == [] and tap.plan_calls == []


def test_claim_auto_click_executes_and_l3_verifies_pass(tmp_path, monkeypatch):
    """循环接线回归：可执行决策 → execute_claim → fresh L3 语义验证。"""
    import auto_play_loop as apl

    def decide_pass(state, _hand, _config, image=None):
        button = next(button for button in state.buttons if button.kind == "pass")
        return ActionDecision(
            action=ACTION_PASS, confidence=1.0,
            l1_evidence={"template_status": "matched"},
            l2_reason="测试：安全过", needs_human_confirm=False,
            button=button, tap_point=button.center,
        )

    monkeypatch.setattr(apl, "decide_action", decide_pass)
    tap = FakeTap()
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        frames=[action_row_frame(), make_frame()],
        analyses=[CLAIM_HAND_ANALYSIS, CLAIM_HAND_ANALYSIS],
        action_guard=CLAIM_GUARD, claim_auto_click=True, tap=tap)

    event = loop.tick()

    assert event["event"] == "action"
    assert event["claim"]["auto_click"]["executed"] is True
    assert event["claim"]["auto_click"]["semantic_verified"] is True
    assert len(tap.execute_claim_calls) == 1
    assert tap.execute_claim_calls[0]["action"] == ACTION_PASS
    assert loop.actions_done == 1


def test_self_draw_hu_with_unknown_caishen_never_reaches_tap(
        tmp_path, monkeypatch):
    """17 张看似成胡也必须先确认财神；行动循环不得调用点击器。"""
    from tea_bot import engine_to_vision

    hand = [21, 2, 2, 3, 3, 5, 5, 41, 41, 42, 42, 46, 73, 73, 25, 25, 41]
    analysis = {
        "hand_ids": [engine_to_vision(tile) for tile in hand],
        "melds": [], "caishen": None, "mode": "tea_2p",
    }
    hu = ClaimButton("hu", (1370, 540, 1460, 630), (1415, 585))
    passed = ClaimButton("pass", (1480, 540, 1570, 630), (1525, 585))
    monkeypatch.setattr(
        auto_play_loop, "read_claims",
        lambda _image: ClaimState(
            (hu, passed), "hu", "测试胡/过", {}, True))
    monkeypatch.setattr(
        action_policy_module, "_l1_evidence",
        lambda _state, _image, _config: (
            {"geometry_ok": True,
             "template_status": action_policy_module.TEMPLATE_STATUS_MATCHED},
            {0: {"status": action_policy_module.TEMPLATE_STATUS_MATCHED},
             1: {"status": action_policy_module.TEMPLATE_STATUS_MATCHED}},
        ))
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, claim_auto_click=True)
    loop._claim_policy = ActionPolicyConfig(
        auto_click_enabled=True,
        confirmed_actions=frozenset({ACTION_HU, ACTION_PASS}),
        trust_server_hu_button=True,
        require_template_match=False,
    )

    event = loop._claim_policy_event(
        np.zeros((900, 1600, 3), np.uint8), tmp_path / "before.png", analysis)

    assert event["claim"]["status"] == "blocked"
    assert "禁止胡也禁止自动过" in event["claim"]["blocked_reason"]
    assert tap.execute_claim_calls == []


def test_unique_selection_cancel_is_auto_clicked_without_hand_decision(tmp_path):
    """候选面板只剩取消时直接关闭，不要求来牌/手牌分析，也不算一次出牌。"""
    tap = FakeTap()
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[selection_cancel_frame()],
        action_guard=CLAIM_GUARD, claim_auto_click=True, tap=tap,
        analyses=[ValueError("候选面板遮住手牌")])

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["claim"]["action"] == "cancel_selection"
    assert event["claim"]["auto_click"]["executed"] is True
    assert event["claim"]["auto_click"]["semantic_verified"] is True
    assert tap.execute_claim_calls[0]["action"] == "cancel_selection"
    assert tap.execute_claim_calls[0]["kind"] == "cancel"
    assert loop.actions_done == 0


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

def test_follow_session_jsonl_dual_writes_with_claim_channel_ready(tmp_path):
    """Structured follow_session.jsonl mirrors actions.jsonl and records channel state."""
    import action_policy as ap
    assert ap.CLAIM_TAP_CHANNEL_READY is True
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path,
        analyses=[advise(caishen="w1"), advise(caishen="w1")],
        hud=[(HUD_A, "w1"), (HUD_A, "w1")],
    )
    first = loop.tick()
    second = loop.tick()
    assert tap.execute_calls == []
    assert loop.follow_log_path.name == "follow_session.jsonl"
    assert loop.follow_log_path.is_file()
    rows = [
        json.loads(line)
        for line in loop.follow_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0].get("kind") == "session_header"
    assert rows[0]["claim_tap_channel_ready"] is True
    body = [
        row for row in rows
        if row.get("schema") == "follow_session/v1" and row.get("kind") != "session_header"
    ]
    assert body, (first, second, rows)
    for row in body:
        assert row["claim_tap_channel_ready"] is True
        assert "vision" in row and "gates" in row and "caishen" in row and "decision" in row



# ----------------------------------------------------------------------
# 2026-10-25 实机 Bug 1：P0-5 财神门在等候室/局间过渡误杀
# ----------------------------------------------------------------------
def no_hand_analysis(**overrides) -> dict:
    """未发牌的一帧（等候室/局间过渡）：无手牌、财神不可读。"""
    result = advise()
    result.update({
        "status": "abstain",
        "suggestion": None,
        "suggestion_id": None,
        "suggested_boxes": [],
        "caishen": None,
        "hand_ids": [],
        "vision_diagnostics": [],
    })
    result.update(overrides)
    return result


def test_unknown_caishen_frozen_in_waiting_room_without_dealt_hand(tmp_path):
    """等候室启动：连读多帧财神未知也不计数、永不停手（实机 3 次误杀）。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[no_hand_analysis()],
        hud=[(HUD_A, None)],
    )
    for _ in range(6):
        event = loop.tick()
        assert event["event"] == "idle"
        assert event["status"] == "abstain"
        assert "计数冻结" in event["reason"]
        assert event["unknown_caishen_ticks"] == 0
    assert loop.stopped is False
    assert loop._unknown_caishen_ticks == 0
    assert tap.execute_calls == [] and tap.execute_claim_calls == []


def test_unknown_caishen_counts_only_after_hand_is_dealt(tmp_path):
    """等候室冻结 → 发牌后局中财神未知，照旧 3 帧 fail-closed 停手。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[no_hand_analysis(), no_hand_analysis(),
                  no_hand_analysis(), advise(caishen=None)],
        hud=[(HUD_A, None)],
    )
    for _ in range(3):
        event = loop.tick()
        assert event["event"] == "idle"
        assert loop._unknown_caishen_ticks == 0
    assert loop.tick()["unknown_caishen_ticks"] == 1
    assert loop.tick()["unknown_caishen_ticks"] == 2
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_unknown_caishen"
    assert event["unknown_caishen_ticks"] == 3
    assert tap.execute_calls == []


def test_unknown_caishen_frozen_frame_does_not_reset_budget(tmp_path):
    """局中偶发一帧读不到手牌只冻结不清零：预算跨冻结帧保留。"""
    loop, _shot, _analyzer, _tap, _ = make_loop(
        tmp_path, enabled=True,
        analyses=[advise(caishen=None), no_hand_analysis(),
                  advise(caishen=None)],
        hud=[(HUD_A, None)],
    )
    assert loop.tick()["unknown_caishen_ticks"] == 1
    frozen = loop.tick()
    assert frozen["event"] == "idle"
    assert frozen["unknown_caishen_ticks"] == 1
    assert loop.tick()["unknown_caishen_ticks"] == 2
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "gate_unknown_caishen"


# ----------------------------------------------------------------------
# 2026-10-25 实机 Bug 2：claim「过」点后单帧验证误杀 → 落定轮询
# ----------------------------------------------------------------------
def _hand_analysis(hand_ids, melds=None) -> dict:
    return {"hand_ids": list(hand_ids), "melds": melds or [],
            "caishen": None, "mode": "tea_2p"}


HAND16_IDS = [1, 1, 2, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43]
HAND14_IDS = HAND16_IDS[:14]
HAND17_IDS = HAND16_IDS + [44]
HAND10_IDS = [1, 1, 2, 4, 5, 6, 7, 21, 22, 23]
HAND12_IDS = [1, 1, 2, 4, 5, 6, 7, 8, 21, 22, 23, 24]


def _pass_loop(tmp_path, monkeypatch, analyses):
    """构造一个会点「过」的循环（决策层替身，执行/验证走真路）。"""
    import auto_play_loop as apl

    def decide_pass(state, _hand, _config, image=None):
        button = next(b for b in state.buttons if b.kind == "pass")
        return ActionDecision(
            action=ACTION_PASS, confidence=1.0,
            l1_evidence={"template_status": "matched"},
            l2_reason="测试：安全过", needs_human_confirm=False,
            button=button, tap_point=button.center,
        )

    monkeypatch.setattr(apl, "decide_action", decide_pass)
    tap = FakeTap()
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True,
        frames=[action_row_frame(), make_frame()],
        analyses=analyses,
        action_guard=CLAIM_GUARD, claim_auto_click=True, tap=tap)
    return loop, tap


def test_pass_overlay_underread_recovers_only_exact_legal_draw_state():
    before = BoardState(
        concealed_count=14, melds=(), claim_buttons=2,
        settled=False, turn_owner="opponent")
    after = BoardState(
        concealed_count=17, melds=(), claim_buttons=0,
        settled=False, turn_owner="me", selection_panel=False)
    ok, reason = AutoPlayLoop._verify_pass_after_overlay_underread(before, after)
    assert ok, reason
    assert "14→合法摸牌 17" in reason


@pytest.mark.parametrize("after", [
    BoardState(concealed_count=16, melds=(), claim_buttons=0,
               settled=False, turn_owner="me", selection_panel=False),
    BoardState(concealed_count=17, melds=((1, 1, 1),), claim_buttons=0,
               settled=False, turn_owner="me", selection_panel=False),
    BoardState(concealed_count=17, melds=(), claim_buttons=1,
               settled=False, turn_owner="me", selection_panel=False),
    BoardState(concealed_count=17, melds=(), claim_buttons=0,
               settled=False, turn_owner="me", selection_panel=True),
])
def test_pass_overlay_underread_rejects_incomplete_or_changed_evidence(after):
    before = BoardState(
        concealed_count=14, melds=(), claim_buttons=2,
        settled=False, turn_owner="opponent")
    ok, _reason = AutoPlayLoop._verify_pass_after_overlay_underread(before, after)
    assert not ok


def test_pass_verify_accepts_real_14_to_17_overlay_recovery(tmp_path, monkeypatch):
    """5558 实机回归：行动栏前帧少读 14，过后立即摸牌恢复 17。"""
    loop, tap = _pass_loop(tmp_path, monkeypatch, [
        _hand_analysis(HAND14_IDS),
        _hand_analysis(HAND17_IDS),
    ])
    event = loop.tick()
    assert event["event"] == "action"
    auto = event["claim"]["auto_click"]
    assert auto["executed"] is True
    assert auto["semantic_verified"] is True
    assert "行动栏遮挡少读 14→合法摸牌 17" in auto["semantic_reason"]
    assert len(tap.execute_claim_calls) == 1
    assert loop.stopped is False


def test_pass_verify_recovers_from_dimmed_first_frame(tmp_path, monkeypatch):
    """实机 5558 两例：过后首帧压暗读 16→10，下一帧落定 16 → 通过。

    旧实现单帧即判 claim_unverified 停机；落定轮询必须救回来，且
    只点一次「过」、不重复点击。
    """
    loop, tap = _pass_loop(tmp_path, monkeypatch, [
        _hand_analysis(HAND16_IDS),
        _hand_analysis(HAND10_IDS),          # 点后首帧：压暗塌读
        _hand_analysis(HAND16_IDS),          # 落定帧：真实手牌
    ])
    event = loop.tick()
    assert event["event"] == "action"
    auto = event["claim"]["auto_click"]
    assert auto["executed"] is True
    assert auto["semantic_verified"] is True
    samples = auto["settle_samples"]
    assert [s["concealed"] for s in samples] == [10, 16]
    assert samples[-1]["verified"] is True
    assert len(tap.execute_claim_calls) == 1
    assert loop.actions_done == 1
    assert loop.stopped is False


def test_pass_verify_recovers_from_meld_mixed_first_frame(
        tmp_path, monkeypatch):
    """实机 5556 一例：基线暗手 14+副露 1 组，点后首帧副露混入暗手
    读成 17（14→17），落定帧恢复 14+1 组 → 通过。"""
    before = _hand_analysis(
        [1, 1, 2, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41],
        melds=[[42, 42, 42]])
    mixed = _hand_analysis(HAND16_IDS + [42])     # 17 张全读成暗手
    loop, tap = _pass_loop(tmp_path, monkeypatch, [before, mixed, before])
    event = loop.tick()
    auto = event["claim"]["auto_click"]
    assert auto["semantic_verified"] is True
    samples = auto["settle_samples"]
    assert [s["concealed"] for s in samples] == [17, 14]
    assert samples[-1]["meld_groups"] == 1
    assert len(tap.execute_claim_calls) == 1
    assert loop.actions_done == 1
    assert loop.stopped is False


def test_pass_verify_settled_mismatch_still_stops(tmp_path, monkeypatch):
    """落定后仍是 16→10（连续两帧同签名）= 真未证实，照旧停机。"""
    loop, tap = _pass_loop(tmp_path, monkeypatch, [
        _hand_analysis(HAND16_IDS),
        _hand_analysis(HAND10_IDS),
        _hand_analysis(HAND10_IDS),
    ])
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "claim_unverified"
    assert "16→10" in event["reason"]
    samples = event["claim"]["auto_click"]["settle_samples"]
    assert len(samples) == 2          # 落定即判，不耗满轮询预算
    assert len(tap.execute_claim_calls) == 1
    assert loop.actions_done == 0


def test_pass_verify_unsettled_frames_fail_closed(tmp_path, monkeypatch):
    """计数在 10/12 之间反复跳变、永不落定 → 耗尽轮询后 fail-closed。"""
    analyses = [_hand_analysis(HAND16_IDS)]
    for _ in range(4):
        analyses.append(_hand_analysis(HAND10_IDS))
        analyses.append(_hand_analysis(HAND12_IDS))
    loop, tap = _pass_loop(tmp_path, monkeypatch, analyses)
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "claim_unverified"
    assert "未落定" in event["reason"]
    samples = event["claim"]["auto_click"]["settle_samples"]
    assert len(samples) == 8
    assert len(tap.execute_claim_calls) == 1
    assert loop.actions_done == 0


# ----------------------------------------------------------------------
# 9. 多亮白名单（亮簇 1..N 张）：亮牌集合只是可打范围，绝不自行选牌
# ----------------------------------------------------------------------
# 实机反馈（2026-10-25，房间 604624）：听牌/摸切时游戏压暗整手，亮起的牌
# 可能是 1 张、2 张甚至更多。单亮路径表达不了 >=2 张，循环因此卡在「识牌
# 不完整/等待摸牌」。这里锁死新的接线：
#   * 亮簇 >=2 张 → 白名单；只有「算牌建议落在白名单内 + 白名单连续 2 帧
#     一致」才点，其余一律弃权并记 gate=bright_subset_unconfirmed；
#   * 单亮帧仍旧走原来那条路径（白名单不接管），行为不变。
ID_W1, ID_DONG, ID_NAN, ID_BEI = 1, 61, 62, 64
BRIGHT_LABELS = {ID_W1: "w1", ID_DONG: "dong", ID_NAN: "nan", ID_BEI: "bei"}


def bright_frame(levels: list[float], band: int = 100) -> bytes:
    """手牌带压暗到 ``band``，再按 ``hand_boxes`` 逐框涂指定亮度。"""
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, 70, 40)
    image[760:, :] = band
    for box, level in zip(hand_boxes(len(levels)), levels):
        x0, y0, x1, y1 = box
        image[y0:y1, x0:x1] = int(level)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def bright_result(ids, *, status="abstain", suggestion_index=None,
                  caishen="w1", reason="识牌不完整，停止决策"):
    """与真实 analyze 返回体同形：逐框给 id，可选带一条建议。"""
    boxes = hand_boxes(len(ids))
    diagnostics = [{
        "tile_id": tile_id,
        "label": BRIGHT_LABELS.get(tile_id),
        "box": list(box),
        "accepted": tile_id is not None,
        "region": "concealed",
        "score": .99 if tile_id is not None else .1,
        "margin": .2,
        "caishen_marked": False,
    } for tile_id, box in zip(ids, boxes)]
    suggestion_id = (ids[suggestion_index]
                     if suggestion_index is not None else None)
    return {
        "status": status, "mode": "tea_2p", "caishen": caishen,
        "hand_ids": [int(t) for t in ids if t is not None],
        "melds": [], "meld_count": 0, "reason": reason,
        "suggestion": BRIGHT_LABELS.get(suggestion_id),
        "suggestion_id": suggestion_id,
        "suggested_boxes": ([list(boxes[suggestion_index])]
                            if suggestion_index is not None else []),
        "vision_diagnostics": diagnostics,
    }


TWO_BRIGHT_LEVELS = [100.0] * 15 + [214.0, 214.0]
TWO_BRIGHT_IDS = [None] * 15 + [ID_NAN, ID_BEI]


def test_multi_bright_without_advice_abstains_with_gate(tmp_path):
    """真实多亮帧的样子：analyze abstain（识牌不完整）→ 弃权，零点击。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame],
        analyses=[bright_result(TWO_BRIGHT_IDS)])
    settle(loop)                                   # 第 1 帧：白名单待确认
    event = loop.tick()                            # 第 2 帧：白名单已确认

    assert event["event"] == "idle"
    assert event["gate"] == "bright_subset_unconfirmed"
    assert event["status"] == "abstain"
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert loop.actions_done == 0
    marker = event["bright_subset_cut"]
    assert marker["multi_candidate"] is True
    assert marker["bright_count"] == 2
    assert marker["outcome"] == "no_advice"
    assert [item["tile_id"] for item in marker["whitelist"]] == [ID_NAN, ID_BEI]


def test_multi_bright_advice_outside_whitelist_abstains(tmp_path):
    """算牌建议不在白名单内 → 弃权（绝不点白名单外的牌）。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    ids = TWO_BRIGHT_IDS
    result = bright_result(ids, status="advise", suggestion_index=0)
    # 建议牌改成白名单外的东（模拟"算牌器想打暗牌"）
    result["suggestion_id"] = ID_DONG
    result["suggestion"] = BRIGHT_LABELS[ID_DONG]
    result["suggested_boxes"] = [list(hand_boxes(17)[0])]
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[result])
    settle(loop)
    event = loop.tick()

    assert event["event"] == "idle"
    assert event["gate"] == "bright_subset_unconfirmed"
    assert "不在亮牌白名单" in event["reason"]
    assert event["bright_subset_cut"]["outcome"] == "advice_outside_whitelist"
    assert tap.execute_calls == [] and tap.plan_calls == []
    assert loop.actions_done == 0


def test_multi_bright_advice_inside_whitelist_taps_that_tile(tmp_path):
    """建议牌落在白名单内 + 连续 2 帧一致 → 点它，并按手牌 −1 验证。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    result = bright_result(TWO_BRIGHT_IDS, status="advise",
                           suggestion_index=16)
    assert result["suggestion_id"] == ID_BEI
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[result])
    settle(loop)
    event = loop.tick()

    assert event["event"] == "action"
    assert event["verified"] is True
    assert len(tap.execute_calls) == 1
    synthetic = tap.execute_calls[0]
    assert synthetic["suggestion_id"] == ID_BEI
    assert synthetic["suggested_boxes"] == [list(hand_boxes(17)[16])]
    assert synthetic["_locked_before_count"] == 17
    assert event["bright_subset_cut"]["outcome"] == "verified"
    assert event["bright_subset_cut"]["chosen"]["tile_id"] == ID_BEI
    assert loop.actions_done == 1


def test_multi_bright_first_frame_never_taps(tmp_path):
    """白名单还必须连续 2 帧一致：第 1 帧只记证据，不点。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    result = bright_result(TWO_BRIGHT_IDS, status="advise",
                           suggestion_index=16)
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[result])

    event = loop.tick()

    assert event["event"] == "idle"
    assert event["bright_subset_cut"]["streak"] == 1
    assert event["bright_subset_cut"]["confirmed"] is False
    assert tap.execute_calls == []


def test_multi_bright_unreadable_tile_abstains(tmp_path):
    """亮牌里有一张牌面读不出 → 弃权（不猜牌）。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    ids = [None] * 15 + [ID_NAN, None]
    result = bright_result(ids, status="advise", suggestion_index=15)
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[result])
    settle(loop)
    event = loop.tick()

    assert event["event"] == "idle"
    assert event["gate"] == "bright_subset_unconfirmed"
    assert event["bright_subset_cut"]["outcome"] == "tile_identity_unknown"
    assert tap.execute_calls == []


def test_multi_bright_dry_run_plans_without_tapping(tmp_path):
    """dry_run 下多亮白名单同样只算坐标、零点击。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    result = bright_result(TWO_BRIGHT_IDS, status="advise",
                           suggestion_index=16)
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=False, frames=[frame], analyses=[result])
    settle(loop)
    event = loop.tick()

    assert event["event"] == "dry_run_plan"
    assert tap.plan_calls and tap.execute_calls == []
    assert tap.plan_calls[0]["suggestion_id"] == ID_BEI


def test_single_bright_frame_still_uses_single_path(tmp_path):
    """1 亮帧不被白名单接管：仍走原有单亮摸切路径（行为不变）。"""
    levels = [100.0] * 16 + [214.0]
    ids = [None] * 16 + [ID_NAN]
    frame = bright_frame(levels)
    result = bright_result(ids, status="abstain")
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[result])
    settle(loop)
    event = loop.tick()

    assert event["event"] == "action"
    assert len(tap.execute_calls) == 1
    assert tap.execute_calls[0]["suggestion_id"] == ID_NAN
    assert event["single_bright_cut"]["outcome"] == "verified"
    # 白名单检测同样命中，但张数只有 1 → 不接管
    assert event["bright_subset_cut"]["multi_candidate"] is False
    assert "outcome" not in event["bright_subset_cut"]


def test_multi_bright_changed_advice_waits_for_stable_frame(tmp_path):
    """建议牌两帧不一致 → 先弃权；第三帧建议稳定后才点（防单帧抖动）。"""
    frame = bright_frame(TWO_BRIGHT_LEVELS)
    first = bright_result(TWO_BRIGHT_IDS, status="advise",
                          suggestion_index=15)   # 建议南
    second = bright_result(TWO_BRIGHT_IDS, status="advise",
                           suggestion_index=16)  # 建议北
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[frame], analyses=[first, second])
    settle(loop)                                     # 第 1 帧：建议南
    unstable = loop.tick()                           # 第 2 帧：建议北（变了）

    assert unstable["event"] == "idle"
    assert unstable["gate"] == "bright_subset_unconfirmed"
    assert unstable["bright_subset_cut"]["outcome"] == "advice_unstable"
    assert unstable["bright_subset_advice"]["streak"] == 1
    assert tap.execute_calls == []

    stable = loop.tick()                             # 第 3 帧：建议仍是北
    assert stable["event"] == "action"
    assert stable["bright_subset_advice"]["streak"] == 2
    assert tap.execute_calls[0]["suggestion_id"] == ID_BEI


# ---------------------------------------------------------------------------
# 手牌账本兜底（2026-10-25 22:13 实机：整手压暗 + 亮起 2 张时弃权 6 分钟）
#
# 真帧证据：17 个手牌框里 15 框面亮度 100（压暗）、2 框 214（亮）；
# analyze 只读出 5 张 → abstain；亮牌白名单本身读得对（南/西）。
# 旧行为是 bright_subset_unconfirmed 死等"算牌建议"，直到游戏高亮自己
# 超时才恢复。这里锁住新行为：同局同张数时用账本手牌算出该打哪张。
# ---------------------------------------------------------------------------
LEDGER_HAND17 = [1, 1, 2, 2, 3, 3, 4, 4, 21, 21, 22, 22, 23, 23, 24, 24, 62]
DIM_GAIN_REAL_FRAMES = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261026_172351_emulator5554_玉姐" / "frames"
    / "20261026_173700_841310_before_3fe653.png",
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261026_172351_emulator5554_玉姐" / "frames"
    / "20261026_173703_517647_before_65fe30.png",
)
DIM_GAIN_EDGE_DIR = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261027_095253_emulator5556_烘熊瞎" / "frames"
)
DIM_GAIN_EDGE_FRAMES = tuple(sorted(
    path for path in DIM_GAIN_EDGE_DIR.glob("20261027_100*_before_*.png")
    if "100032" <= path.name[9:15] <= "100125"
))


def _fake_subset(tile_ids, *, expected=17, hit=True, unresolved=False):
    from types import SimpleNamespace

    candidates = [
        SimpleNamespace(tile_id=tile_id, tile_label=f"t{tile_id}",
                        box=hand_boxes(len(tile_ids))[index],
                        tap_point=[0, 0])
        for index, tile_id in enumerate(tile_ids)
    ]
    return SimpleNamespace(
        hit=hit, multi_candidate=len(candidates) >= 2,
        unresolved=unresolved, candidates=candidates,
        evidence={"expected_concealed": expected},
        whitelist_ids=lambda: tuple(c.tile_id for c in candidates),
    )


def _dim_gain_result(caishen="s2"):
    return {
        "mode": "tea_2p",
        "status": "abstain",
        "reason": "识牌不完整，停止决策",
        "caishen": caishen,
        "caishen_source": "image",
    }


def test_dim_gain_2057_real_frames_restore_whitelisted_advice(tmp_path):
    """用户20:57暗牌停手：两张原生帧均补齐才可给西/北白名单建议。"""
    paths = [BOT_DIR.parent / "采集" / "dev_auto_play" /
             "ui_20261027_205027_emulator5556_烘熊瞎" / "frames" / name
             for name in ("20261027_205753_417490_before_c2bce7.png",
                          "20261027_205755_910170_before_c9499a.png")]
    assert all(path.is_file() for path in paths)
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop.round.caishen = "t5"
    subset = _fake_subset([63, 64])
    images = [cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
              for path in paths]
    first = loop._dim_gain_whitelist_advice(
        images[0], subset, _dim_gain_result("t5"), frame=str(paths[0]))
    second = loop._dim_gain_whitelist_advice(
        images[1], subset, _dim_gain_result("t5"), frame=str(paths[1]))
    assert first is None
    assert second is not None
    assert second["hand"] == [25, 4, 8, 8, 8, 41, 41, 42, 44, 47, 21, 21, 23, 24, 29, 63, 64]
    assert second["tile_id"] in (63, 64)
    assert second["source"] == "dim_gain_independent_frames"
    assert tap.execute_calls == [] and tap.plan_calls == []


@pytest.mark.skipif(
    not all(path.is_file() for path in DIM_GAIN_REAL_FRAMES),
    reason="17:37 dim-gain real frames missing",
)
def test_dim_gain_real_frames_only_fill_hand_then_choose_inside_whitelist(
        tmp_path):
    """17:37 实机压暗手：增益只补齐17张，不扩大可点范围。"""
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop.round.caishen = "s2"
    subset = _fake_subset([ID_NAN, 63])
    images = [cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
              for path in DIM_GAIN_REAL_FRAMES]

    first = loop._dim_gain_whitelist_advice(
        images[0], subset, _dim_gain_result(), frame=str(DIM_GAIN_REAL_FRAMES[0]))
    second = loop._dim_gain_whitelist_advice(
        images[1], subset, _dim_gain_result(), frame=str(DIM_GAIN_REAL_FRAMES[1]))

    assert first is None
    assert second is not None
    assert len(second["hand"]) == 17
    assert second["tile_id"] in (ID_NAN, 63)
    assert second["whitelist"] == [ID_NAN, 63]
    assert second["source"] == "dim_gain_independent_frames"
    assert loop._dim_gain_evidence["outcome"] == "advice"
    # 补读本身只产出受限建议；点击仍必须另走 fresh-frame/TapExecutor。
    assert tap.execute_calls == [] and tap.plan_calls == []


@pytest.mark.skipif(
    len(DIM_GAIN_EDGE_FRAMES) != 23,
    reason="10:00:32-10:01:25 dim-edge real frames missing",
)
def test_dim_gain_right_edge_retry_recovers_all_23_real_frames_without_new_thresholds():
    """实机暗牌：只补裁框右边 1px；0.82/0.055 与模板保持原样。"""
    from tea_bot import _TEMPLATE_DIR
    from tile_vision import TileVision

    for path in DIM_GAIN_EDGE_FRAMES:
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        vision = TileVision(template_dir=_TEMPLATE_DIR)
        vision.dim_gain_all = True
        hand = vision.recognize_hand(image, caishen_label="t5")

        assert vision.min_score == pytest.approx(.82)
        assert vision.min_margin == pytest.approx(.055)
        assert vision.last_complete is True, path.name
        assert len(hand) == 17, path.name
        repaired = [d for d in vision.diagnostics
                    if d.get("reason") == "accepted_dim_right_edge_retry"]
        assert len(repaired) == 1, path.name
        assert repaired[0]["label"] == "t9"
        assert repaired[0]["original_box"] == [1206, 768, 1289, 891]
        assert repaired[0]["box"] == [1206, 768, 1290, 891]


@pytest.mark.skipif(
    not DIM_GAIN_REAL_FRAMES[0].is_file(),
    reason="17:37 dim-gain real frame missing",
)
def test_dim_gain_same_frame_cannot_self_confirm(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop.round.caishen = "s2"
    subset = _fake_subset([ID_NAN, 63])
    image = cv2.imdecode(
        np.fromfile(str(DIM_GAIN_REAL_FRAMES[0]), np.uint8), cv2.IMREAD_COLOR)
    frame = str(DIM_GAIN_REAL_FRAMES[0])

    assert loop._dim_gain_whitelist_advice(
        image, subset, _dim_gain_result(), frame=frame) is None
    assert loop._dim_gain_whitelist_advice(
        image, subset, _dim_gain_result(), frame=frame) is None
    assert loop._dim_gain_evidence["outcome"] == "independent_frame_pending"
    assert loop._dim_gain_evidence["streak"] == 1


@pytest.mark.parametrize("read_error", [
    OSError("frame read failed"),
    cv2.error("opencv decode failed"),
])
def test_dim_gain_read_errors_fail_closed_with_evidence(
        tmp_path, monkeypatch, read_error):
    """OSError/cv2.error 是无建议证据，不得穿透成整环 anomaly。"""
    import tile_vision

    class BrokenVision:
        def __init__(self, **_kwargs):
            self.dim_gain_all = False

        def recognize_hand(self, _image, **_kwargs):
            raise read_error

    monkeypatch.setattr(tile_vision, "TileVision", BrokenVision)
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop.round.caishen = "s2"

    advice = loop._dim_gain_whitelist_advice(
        np.zeros((900, 1600, 3), np.uint8),
        _fake_subset([ID_NAN, 63]), _dim_gain_result(), frame="fresh-b.png")

    assert advice is None
    assert loop._dim_gain_snapshot is None
    assert loop._dim_gain_evidence == {
        "attempted": True,
        "outcome": "no_advice",
        "reason": "enhanced_read_exception",
        "frame": "fresh-b.png",
        "error_type": type(read_error).__name__,
    }
    assert tap.execute_calls == [] and loop.stopped is False


def test_dim_gain_never_runs_outside_exact_bright_subset_gate(
        tmp_path, monkeypatch):
    """模式/原始状态/画面财神任一不对，连增益识牌器都不实例化。"""
    import tile_vision

    def forbidden(**_kwargs):
        raise AssertionError("门禁外不得实例化 TileVision")

    monkeypatch.setattr(tile_vision, "TileVision", forbidden)
    loop, *_ = make_loop(tmp_path, enabled=True)
    subset = _fake_subset([ID_NAN, 63])
    result = _dim_gain_result()
    result["caishen_source"] = "manual"

    assert loop._dim_gain_whitelist_advice(
        np.zeros((900, 1600, 3), np.uint8), subset, result,
        frame="fresh-c.png") is None
    assert loop._dim_gain_evidence is None


def test_dim_gain_read_error_is_logged_by_tick_without_loop_anomaly(
        tmp_path, monkeypatch):
    """整环也必须把补读异常写进当帧证据，不进异常预算。"""
    import tile_vision

    class BrokenVision:
        def __init__(self, **_kwargs):
            self.dim_gain_all = False

        def recognize_hand(self, _image, **_kwargs):
            raise OSError("offline real-frame read failed")

    monkeypatch.setattr(tile_vision, "TileVision", BrokenVision)
    result = bright_result(TWO_BRIGHT_IDS, caishen="s2")
    result["caishen_source"] = "image"
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, frames=[bright_frame(TWO_BRIGHT_LEVELS)],
        analyses=[result])

    settle(loop, frames=2)  # 财神换局门需两帧；换局帧不许补读
    event = loop.tick()

    assert event["event"] == "idle"
    assert event["gate"] == "bright_subset_unconfirmed"
    assert event["dim_gain_read"]["reason"] == "enhanced_read_exception"
    assert event["dim_gain_read"]["error_type"] == "OSError"
    assert event["consecutive_errors"] == 0
    assert loop.stopped is False
    assert tap.execute_calls == [] and tap.plan_calls == []


#: 账本读数写于上一帧、取回发生在当前帧：账本只在**另一帧**上生效。
LEDGER_PREV_FRAME = "frames/20261026_001122_000001_before_ab12cd.png"
LEDGER_THIS_FRAME = "frames/20261026_001123_000002_before_34ef56.png"


def _prime_ledger(loop, frame: str = LEDGER_PREV_FRAME):
    loop._hand_ledger.remember(
        {"status": "advise", "hand_ids": list(LEDGER_HAND17), "melds": [],
         "caishen": "w1", "caishen_id": 1},
        round_seq=loop.round.seq, caishen="w1", frame=frame)


def test_ledger_advice_replaces_missing_advice(tmp_path):
    """压暗帧没有算牌建议时，账本给出「白名单内最优」那张，且不越界。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)
    subset = _fake_subset([24, 62])

    advice = loop._ledger_advice(subset, {"status": "abstain",
                                          "reason": "识牌不完整，停止决策",
                                          "caishen": "w1"},
                                 frame=LEDGER_THIS_FRAME)

    assert advice is not None
    assert advice["tile_id"] in (24, 62)
    assert advice["whitelist"] == [24, 62]
    assert sorted(advice["hand"]) == sorted(LEDGER_HAND17)
    assert advice["candidate_count"] >= 1
    assert advice["memory_frame"] == LEDGER_PREV_FRAME


def test_ledger_advice_never_overrides_real_advice(tmp_path):
    """本帧真建议本身就在白名单内时，兜底不许接管（避免两套口径打架）。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    advice = loop._ledger_advice(
        _fake_subset([24, 62]),
        {"status": "advise", "suggestion_id": 24, "caishen": "w1"},
        frame=LEDGER_THIS_FRAME)

    assert advice is None


def test_ledger_advice_takes_over_when_real_advice_is_illegal(tmp_path):
    """真建议落在白名单外 → 游戏根本不允许打那张，必须由兜底改挑白名单内候选。

    实机场景（2026-10-25 房间 915995，emulator-5558）：听牌后整手压暗，识牌只
    读到部分手牌，引擎建议 3条(id=43)，而游戏高亮的可打范围只有 东/西(id=61/63)。
    若继续"一成不变地尊重真建议"，这一家会永远打不出去，整桌卡到超时。
    """
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    advice = loop._ledger_advice(
        _fake_subset([24, 62]),
        {"status": "advise", "suggestion_id": 1, "caishen": "w1"},
        frame=LEDGER_THIS_FRAME)

    assert advice is not None
    assert advice["tile_id"] in (24, 62)


def test_ledger_advice_rejects_whitelist_outside_memory(tmp_path):
    """白名单有一张不在账本手牌上 → 账本与本帧不是同一手牌，弃权。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    assert loop._ledger_advice(
        _fake_subset([24, 63]),
        {"status": "abstain", "caishen": "w1"},
        frame=LEDGER_THIS_FRAME) is None


def test_ledger_advice_rejects_count_mismatch(tmp_path):
    """张数不等（摸牌/吃碰后）→ 账本过期，弃权。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    assert loop._ledger_advice(
        _fake_subset([24, 62], expected=16),
        {"status": "abstain", "caishen": "w1"},
        frame=LEDGER_THIS_FRAME) is None


def test_ledger_advice_rejects_without_memory(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)

    assert loop._ledger_advice(
        _fake_subset([24, 62]),
        {"status": "abstain", "caishen": "w1"},
        frame=LEDGER_THIS_FRAME) is None


def test_ledger_advice_requires_whitelist_hit(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    from types import SimpleNamespace
    empty = SimpleNamespace(hit=False, unresolved=False, candidates=[],
                            evidence={}, whitelist_ids=lambda: ())
    assert loop._ledger_advice(empty, {"status": "abstain"},
                               frame=LEDGER_THIS_FRAME) is None


# 2026-10-27 02:04:45 真帧：行动圆盘动画把 16 张等待手少读成 14 张。
# 上一独立帧已稳定读全；恢复只能补暗手，来牌仍由当前 discard_vision 提供。
CLAIM_LEDGER_HAND16 = (
    6, 6, 8, 9, 41, 42, 42, 43, 44, 67, 49, 23, 24, 24, 25, 26,
)
CLAIM_LEDGER_UNDERREAD14 = CLAIM_LEDGER_HAND16[:-2]


def _prime_claim_ledger(loop, *, hand=CLAIM_LEDGER_HAND16,
                        melds=(), caishen="s6",
                        frame=LEDGER_PREV_FRAME):
    loop._hand_ledger.remember(
        {"status": "advise", "hand_ids": list(hand),
         "melds": [list(group) for group in melds],
         "caishen": caishen, "caishen_id": 46},
        round_seq=loop.round.seq, caishen=caishen, frame=frame)


def _claim_underread(*, hand=CLAIM_LEDGER_UNDERREAD14,
                     melds=(), caishen="s6") -> dict:
    return {
        "status": "abstain", "mode": "tea_2p",
        "hand_ids": list(hand),
        "melds": [list(group) for group in melds],
        "caishen": caishen, "caishen_id": 46,
    }


def test_claim_payload_recovers_independent_complete_hand(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop)

    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(), Path(LEDGER_THIS_FRAME))

    assert payload["hand_ids"] == list(CLAIM_LEDGER_HAND16)
    assert evidence["applied"] is True
    assert evidence["memory_frame"] == LEDGER_PREV_FRAME
    assert evidence["current_count"] == 14
    assert evidence["expected_count"] == 16


@pytest.mark.parametrize(
    ("analysis", "frame", "reason"),
    [
        (_claim_underread(), Path(LEDGER_PREV_FRAME),
         "ledger_recall_rejected"),
        (_claim_underread(caishen="s5"), Path(LEDGER_THIS_FRAME),
         "ledger_recall_rejected"),
        (_claim_underread(melds=((41, 42, 43),)), Path(LEDGER_THIS_FRAME),
         "current_hand_not_underread"),
        (_claim_underread(hand=CLAIM_LEDGER_HAND16), Path(LEDGER_THIS_FRAME),
         "current_hand_not_underread"),
    ],
)
def test_claim_payload_recovery_rejects_unproven_state(
        tmp_path, analysis, frame, reason):
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop)

    payload, evidence = loop._claim_payload_from_ledger(analysis, frame)

    assert evidence["applied"] is False
    assert evidence["reason"] == reason
    assert payload["hand_ids"] == analysis["hand_ids"]


def test_claim_payload_recovery_requires_multiset_subset(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    memory = (6, 8, 9, 41, 42, 42, 43, 44, 67, 49, 23, 24, 24, 25, 26, 27)
    _prime_claim_ledger(loop, hand=memory)
    # 集合仍是子集，但本帧把 6 万重复读成两张；不得被集合判据蒙混过关。
    current = (6, 6, 8, 9, 41, 42, 43, 44, 67, 49, 23, 24, 25, 26)

    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(hand=current), Path(LEDGER_THIS_FRAME))

    assert evidence["applied"] is False
    assert evidence["reason"] == "current_hand_not_multiset_subset"
    assert payload["hand_ids"] == list(current)


def test_live_missed_peng_is_recovered_from_prior_complete_hand(tmp_path):
    """02:04:45 原生帧：只恢复手牌后，现有模板/策略应从过改为碰。"""
    frame = (BOT_DIR.parent / "采集" / "dev_auto_play"
             / "ui_20261027_015814_emulator5556_烘熊瞎" / "frames"
             / "20261027_020445_274090_before_749282.png")
    if not frame.is_file():
        pytest.skip("02:04:45 碰真帧缺失")
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop)
    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(), frame)
    payload["incoming_tile"] = 6
    payload["incoming_source"] = "opponent"

    decision = decide_action(
        read_claims(image), HandState.from_analysis(payload),
        recommended_runner_config(auto_click_enabled=True), image=image)

    assert evidence["applied"] is True
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.executable


# ---------------------------------------------------------------------------
# 2026-10-26 Codex P0：账本禁止同帧自证
#
# 实机（_takeover/wb_live/w9）台账里 **8 条**「账本辅助动作」的 memory_frame
# 与当前帧相同：那一帧自己写进账本、同一个 tick 再取出来当"本帧手牌"。这是
# 循环论证——账本不是独立证据，只是本帧残读的复读机。这里锁死两条判据。
#
# 条数（software-architect 2026-10-26 按 ledger_advice.memory_frame 与同行
# frame 逐条全量比对实测，不是估的）：
#   emulator-5556_20261025_235242_g1 → 2 条
#   emulator-5558_20261025_235242_g1 → 6 条
#   emulator-5558_20261026_003543_g2 → 0 条
#   合计 8 条。早期注释写的"4 次"偏小，别再拿它当基线估算修好了多少。
# ---------------------------------------------------------------------------
def test_ledger_advice_rejects_same_frame_memory(tmp_path):
    """读数写于本帧 → 绝不当作本帧手牌使用（same_frame 弃权）。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop, frame=LEDGER_THIS_FRAME)

    advice = loop._ledger_advice(
        _fake_subset([24, 62]),
        {"status": "abstain", "caishen": "w1"},
        frame=LEDGER_THIS_FRAME)

    assert advice is None
    assert loop._hand_ledger.ledger()["rejects"]["same_frame"] == 1
    assert loop._hand_ledger.ledger()["recalls"] == 0


def test_saved_frame_names_are_unique_per_frame(tmp_path, monkeypatch):
    """帧名每帧唯一 —— 账本「禁同帧自证」判据全靠它，必须是硬断言。

    三条一起锁死，任何一条被改都会立刻红，不留软断言：

    1. 连续两次 ``_save_frame`` 得到的文件名**必须不同**；
    2. 把 ``datetime.now()`` 冻成常量后仍**必须不同** —— 证明唯一性不是靠
       系统时钟撑的；
    3. 文件名结构必须真的含 ``%f`` 微秒段（6 位数字）与 uuid 段（6 位 hex）。

    为什么第 2 条要冻时钟而不是冻 uuid：本机实测 ``datetime.now()`` 分辨率
    只有 ~15.6ms（20000 组连续采样里 19853 组微秒段完全相同），所以**不能**
    指望两次调用拿到不同微秒——反过来靠"两次调用必须不同"根本测不出有没有
    ``%f``。冻时钟才能把时钟的贡献摘干净，剩下的差异必须来自 uuid。
    谁把帧名改成按秒（去掉 ``%f``）→ 第 3 条立刻红；谁把 uuid 段去掉→
    第 2 条立刻红。
    """
    loop, *_ = make_loop(tmp_path, enabled=False)
    raw = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    first = loop._save_frame(raw, "before")
    second = loop._save_frame(raw, "before")
    assert str(first) != str(second), "同一会话连续两帧拿到了同一个帧名"

    parts = first.stem.split("_")
    assert len(parts) == 5, f"帧名结构变了（{first.name}），不再是 时间戳_微秒_tag_uuid"
    date_part, time_part, micro_part, tag_part, uuid_part = parts
    assert len(date_part) == 8 and date_part.isdigit()
    assert len(time_part) == 6 and time_part.isdigit()
    assert len(micro_part) == 6 and micro_part.isdigit(), "帧名缺少 %f 微秒段"
    assert tag_part == "before"
    assert len(uuid_part) == 6 and set(uuid_part) <= set(string.hexdigits), \
        "帧名缺少 uuid 段（唯一性不能只靠时钟）"

    class _FrozenDatetime(datetime):
        """``now()`` 恒返回同一时刻：把时钟对帧名的贡献彻底摘掉。"""

        @classmethod
        def now(cls, tz=None):  # noqa: D102 - 测试替身
            return datetime(2026, 10, 26, 12, 0, 0, 0)

    monkeypatch.setattr(auto_play_loop, "datetime", _FrozenDatetime)
    frozen_a = loop._save_frame(raw, "before")
    frozen_b = loop._save_frame(raw, "before")
    assert str(frozen_a) != str(frozen_b), \
        "时钟冻结后两帧重名：帧名唯一性只剩时钟撑着，uuid 段被去掉了"


def test_ledger_advice_rejects_when_frame_is_missing(tmp_path):
    """本帧标识缺失 → fail-closed 弃权，绝不为了兜底率放宽判据。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_ledger(loop)

    assert loop._ledger_advice(
        _fake_subset([24, 62]),
        {"status": "abstain", "caishen": "w1"}) is None
    assert loop._hand_ledger.ledger()["rejects"]["unknown_frame"] == 1


def test_tick_feeds_current_frame_into_ledger_recall(tmp_path):
    """整环回归：tick 把本帧 frame_path 传进 recall，账本不由本帧写入。

    同一 tick 里先 remember(本帧) 再 recall(本帧) 必须弃权；下一帧的
    frame_path 变了，账本才允许命中。
    """
    loop, _shot, _analyzer, _tap, _ = make_loop(tmp_path, enabled=True)
    loop._hand_ledger.remember(
        {"status": "advise", "hand_ids": list(LEDGER_HAND17), "melds": [],
         "caishen": "w1", "caishen_id": 1},
        round_seq=loop.round.seq, caishen="w1", frame="frame-of-this-tick")

    same_tick = loop._ledger_advice(
        _fake_subset([24, 62]), {"status": "abstain", "caishen": "w1"},
        frame="frame-of-this-tick")
    next_tick = loop._ledger_advice(
        _fake_subset([24, 62]), {"status": "abstain", "caishen": "w1"},
        frame="frame-of-next-tick")

    assert same_tick is None
    assert next_tick is not None


# ---------------------------------------------------------------------------
# 局间「下一局」：瞬时 fresh 帧不匹配不是末局；点后未知必须显式停手
# ---------------------------------------------------------------------------
class FakeRoundTransition:
    def __init__(self, outcome):
        self.outcome = outcome
        self.attempt_kwargs = []

    def probe(self, _image):
        return True, {
            "button": {"ok": True, "score": 0.9755,
                       "reason": "button_ok"},
            "scene": {"ok": True, "reason": "round_summary_scene"},
        }

    def attempt(self, **kwargs):
        self.attempt_kwargs.append(kwargs)
        return self.outcome


class RearmRoundTransition(FakeRoundTransition):
    def __init__(self, outcome):
        super().__init__(outcome)
        self.rearmed = []

    def note_verified_gameplay(self, round_no):
        self.rearmed.append(round_no)
        return True


def test_verified_action_rearms_previous_round_transition_lock(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    transition = RearmRoundTransition(TransitionResult("no_click", "unused"))
    loop._round_transition = transition

    event = loop._event({"event": "action", "verified": True})

    assert transition.rearmed == [loop.round.seq]
    assert event["round_transition_rearmed"] is True


def test_unverified_action_never_rearms_round_transition_lock(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    transition = RearmRoundTransition(TransitionResult("no_click", "unused"))
    loop._round_transition = transition

    event = loop._event({"event": "action", "verified": False})

    assert transition.rearmed == []
    assert "round_transition_rearmed" not in event


def test_round_transition_transient_fresh_miss_keeps_loop_alive(tmp_path):
    """外层0.9755后 fresh 帧0.683只记 no_click，不得 match_complete。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._round_transition = FakeRoundTransition(TransitionResult(
        "no_click",
        "fresh 帧未再次确认下一局按钮：template_score_low:0.683"))

    event = loop._round_transition_event(
        np.zeros((900, 1600, 3), dtype=np.uint8),
        Path(tmp_path) / "before.png")

    assert event["event"] == "round_transition"
    assert event["round_transition"]["status"] == "no_click"
    assert loop.stopped is False


def test_round_transition_does_not_treat_local_seq_as_actual_16_of_16(tmp_path):
    """中途接管时 local round.seq 与画面 13/16 不对齐，不得用于末局。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop.round.seq = 16
    transition = FakeRoundTransition(TransitionResult(
        "no_click", "fresh 帧等待重拍"))
    loop._round_transition = transition

    event = loop._round_transition_event(
        np.zeros((900, 1600, 3), dtype=np.uint8),
        Path(tmp_path) / "before.png")

    assert event["event"] == "round_transition"
    assert loop.stopped is False
    assert transition.attempt_kwargs == [{"round_no": 16, "round_total": 0}]


@pytest.mark.parametrize("status", ["post_verify_uncertain", "halted"])
def test_round_transition_uncertain_or_halted_stops_explicitly(tmp_path, status):
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._round_transition = FakeRoundTransition(TransitionResult(
        status, "点下一局后未观察到新局证据"))

    event = loop._round_transition_event(
        np.zeros((900, 1600, 3), dtype=np.uint8),
        Path(tmp_path) / "before.png")

    assert event["event"] == "stop"
    assert event["gate"] == "round_transition_uncertain"
    assert loop.stopped is True


def test_pending_post_claim_blocks_round_transition_before_attempt(tmp_path):
    """鸣牌后尚未出牌时，下一局探针只能停手，不能先点再发现跨局。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    transition = FakeRoundTransition(TransitionResult(
        "clicked", "不应到达 attempt"))
    loop._round_transition = transition
    loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)

    event = loop._round_transition_event(
        np.zeros((900, 1600, 3), dtype=np.uint8),
        Path(tmp_path) / "pending-claim.png")

    assert event["event"] == "stop"
    assert event["gate"] == "post_claim_round_transition"
    assert transition.attempt_kwargs == []
    assert loop.stopped is True


# ---------------------------------------------------------------------------
# 牌局结束（局数打满 16/16）→ 返回大厅
#
# 实机 20261025 22:58：局数 16/16，画面为「牌局结束」+ 冠军横幅 + 底部三按钮。
# 旧行为被 gate_turn 判 blocked 直接停机，整场会话结束、必须人工点返回大厅。
#
# 2026-10-26 Codex P0：返回大厅必须**显式授权**。默认（allow_return_lobby=
# False）时这条通道零点击、落 stop/gate=match_complete，把用户留在牌桌上，
# 由人工决定要不要回大厅。
# ---------------------------------------------------------------------------
MATCH_END_BOXES = ((246, 777, 554, 872), (646, 777, 954, 872),
                   (1046, 764, 1413, 873))


def match_end_frame() -> np.ndarray:
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (48, 96, 60)
    for x0, y0, x1, y1 in MATCH_END_BOXES:
        cv2.rectangle(image, (x0, y0), (x1, y1), (95, 205, 170), -1)
    return image


def test_match_end_clicks_return_lobby_once(tmp_path):
    """已授权（allow_return_lobby=True）→ 点返回大厅（一次），状态全部作废。

    逐行沿用授权前的原行为，只是把开关显式打开。
    """
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True)
    loop._bright_subset_advice = (loop.round.seq, 24, 2)

    event = loop._match_end_event(
        match_end_frame(), Path(tmp_path) / "f.png", {}, 0.0)

    assert event is not None and event["event"] == "left_table"
    assert len(tap.execute_leave_calls) == 1
    assert event["match_end"]["present"] is True
    assert event["player_name"] == "玉姐"
    assert event["player_score"] == -59
    assert event["match_result_record"]["round_no"] == 16
    rows = [json.loads(line) for line in loop.match_result_path.read_text(
        encoding="utf-8").splitlines()]
    assert rows == [event["match_result_record"]]
    assert rows[0]["left_player"] == "玉姐"
    assert rows[0]["right_player"] == "拾则"
    assert loop._left_table is True
    assert loop._bright_subset_advice is None
    # 再调一次不许重复点
    assert loop._match_end_event(match_end_frame(), Path(tmp_path) / "f2.png",
                                 {}, 0.0) is None
    assert len(tap.execute_leave_calls) == 1


def test_match_end_unknown_configured_player_does_not_persist_or_leave(
        tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True,
        player_name="未登记昵称")

    event = loop._match_end_event(
        match_end_frame(), Path(tmp_path) / "f.png", {}, 0.0)

    assert event["event"] == "stop"
    assert event["gate"] == "match_score_player_unverified"
    assert tap.execute_leave_calls == []
    assert not loop.match_result_path.exists()


def test_match_end_requires_verified_score_before_persist_or_leave(tmp_path):
    rejected = MatchScoreState(False, reason="左右积分不守恒")
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True,
        match_score_fn=lambda _image, **_kwargs: rejected)

    event = loop._match_end_event(
        match_end_frame(), Path(tmp_path) / "f.png", {}, 0.0)

    assert event["event"] == "stop"
    assert event["gate"] == "match_score_unverified"
    assert tap.execute_leave_calls == []
    assert not loop.match_result_path.exists()


# ---------------------------------------------------------------------------
# 返回大厅授权门（2026-10-26 Codex P0）：默认不许把用户带离牌桌
# ---------------------------------------------------------------------------
def test_match_end_without_authorization_never_clicks_return_lobby(tmp_path):
    """默认（未授权）→ 零点击，落 stop/gate=match_complete，按钮证据留台账。"""
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop._bright_subset_advice = (loop.round.seq, 24, 2)

    event = loop._match_end_event(
        match_end_frame(), Path(tmp_path) / "f.png", {}, 0.0)

    assert event is not None
    assert event["event"] == "stop"
    assert event["gate"] == "match_complete"
    assert "返回大厅需要显式授权（allow_return_lobby=False）" in event["reason"]
    # 按钮证据必须留下来（人工据此决定要不要回大厅）
    assert event["match_end"]["present"] is True
    assert event["allow_return_lobby"] is False
    # 零点击：离桌通道一次都没走
    assert tap.execute_leave_calls == []
    assert loop._left_table is False
    # 停手留证，不是"打完了继续跑"
    assert loop.stopped is True
    # 跨帧状态不许因为"牌局结束"被清掉（人还没离开牌桌）
    assert loop._bright_subset_advice == (loop.round.seq, 24, 2)


def test_match_end_gate_ledger_row_carries_button_evidence(tmp_path):
    """未授权的 stop 行必须写进 actions.jsonl，且带 match_end 证据。"""
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    loop._match_end_event(
        match_end_frame(), Path(tmp_path) / "f.png", {}, 0.0)

    rows = events_of(loop, "stop")
    assert len(rows) == 1
    row = rows[0]
    assert row["gate"] == "match_complete"
    assert row["match_end"]["present"] is True
    assert tap.execute_leave_calls == []


@pytest.mark.parametrize("bad", ["True", "yes", 1, None, "1"])
def test_allow_return_lobby_only_accepts_real_bool(tmp_path, bad):
    """只认显式 bool：字符串 / 1 / None 一律按未授权处理。"""
    loop, *_ = make_loop(tmp_path, enabled=True, allow_return_lobby=bad)
    assert loop.allow_return_lobby is False

    allowed, *_ = make_loop(tmp_path / "ok", enabled=True,
                            allow_return_lobby=True)
    assert allowed.allow_return_lobby is True
    assert make_loop(tmp_path / "def", enabled=True)[0].allow_return_lobby is False


def test_match_end_ignores_normal_table(tmp_path):
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    plain = np.zeros((900, 1600, 3), dtype=np.uint8)
    plain[:, :] = (48, 96, 60)

    assert loop._match_end_event(plain, Path(tmp_path) / "f.png", {}, 0.0) is None
    assert tap.execute_leave_calls == []


def test_match_end_ignores_normal_table_when_authorized(tmp_path):
    """授权开着也不会在普通牌桌上误触发离桌。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True)
    plain = np.zeros((900, 1600, 3), dtype=np.uint8)
    plain[:, :] = (48, 96, 60)

    assert loop._match_end_event(plain, Path(tmp_path) / "f.png", {}, 0.0) is None
    assert tap.execute_leave_calls == []


# ---------------------------------------------------------------------------
# adb 单点治理（2026-10-26）：adb_runtime 是 adb 路径的唯一真源
#
# 本机实测有 3 个 adb.exe（含 采集/scrcpy/adb.exe）。真源确认不了 adb 时
# **不许**静默回退硬编码候选——那正是"两个 server 互踢"的成因，宁可停手。
# ---------------------------------------------------------------------------
def test_default_adb_follows_the_single_source():
    """真源就绪时 DEFAULT_ADB 必须就是它给的路径；它给不出就留空，不回退。"""
    if not getattr(auto_play_loop, "_ADB_SOURCE_READY", False):
        pytest.skip("adb_runtime 未就绪：本次验收的是回退分支之外的语义")
    if auto_play_loop._RESOLVED_ADB:
        assert auto_play_loop.DEFAULT_ADB == auto_play_loop._RESOLVED_ADB
    else:
        assert auto_play_loop.DEFAULT_ADB == ""
        assert auto_play_loop.DEFAULT_ADB not in auto_play_loop.ADB_CANDIDATES


def test_after_leaving_blocked_frame_is_idle_not_stop(tmp_path):
    """已返回大厅后，即使画面被判 blocked 也不许硬停机。"""
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True)
    loop._left_table = True

    event = loop._match_end_event(
        np.zeros((900, 1600, 3), dtype=np.uint8), Path(tmp_path) / "f.png",
        {}, 0.0)

    assert event is None
    assert tap.execute_leave_calls == []


# ---------------------------------------------------------------------------
# 促销弹窗（「限时优惠」）→ 关掉右上角菱形 ❌
#
# 弹窗盖住整屏：它必须比牌局结束、回合门禁更早判，否则下面所有读数都是
# 弹窗上的像素。关闭通道与出牌通道完全隔离，且关了预算次数还在就停手。
# 落点 (1466,190) 与 tests/test_promo_popup.py 的实机探针帧一致。
# ---------------------------------------------------------------------------
PROMO_CLOSE_CENTER = (1466, 190)
PROMO_CLOSE_BOX = (1438, 160, 1495, 221)


def promo_present(center=PROMO_CLOSE_CENTER) -> PromoPopupState:
    """一帧「弹窗在屏且落点可信」的读数。"""
    return PromoPopupState(
        present=True, close_center=center, close_box=PROMO_CLOSE_BOX,
        score=.983, scale=1.45, margin=.0, struct_ok=True,
        reason="促销弹窗关闭按钮命中",
    )


def promo_absent() -> PromoPopupState:
    return PromoPopupState(
        present=False, score=.603, reason="模板分不足：0.603 < 0.9")


def promo_unresolved() -> PromoPopupState:
    """弹窗在屏但结构校验不过：只记弃权，绝不给落点。"""
    return PromoPopupState(
        present=True, close_center=None, close_box=None,
        score=.94, scale=1.45, margin=.0, struct_ok=False,
        reason="结构特征与模板不符，拒绝给出落点",
    )


def test_promo_popup_is_closed_once_and_logged(tmp_path, monkeypatch):
    """弹窗帧 → 触发一次 execute_close_popup，台账记 popup_dismiss。"""
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_present())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "popup_dismiss"
    assert len(tap.execute_close_popup_calls) == 1
    assert tap.execute_close_popup_calls[0]["point"] == PROMO_CLOSE_CENTER
    assert event["promo_popup"]["present"] is True
    assert event["promo_close_count"] == 1
    logged = events_of(loop, "popup_dismiss")
    assert len(logged) == 1
    assert logged[0]["promo_popup"]["close_center"] == list(PROMO_CLOSE_CENTER)
    assert loop.stopped is False


def test_promo_popup_close_consumes_the_frame_without_discarding(
        tmp_path, monkeypatch):
    """关闭成功后本帧直接 return：绝不在弹窗还盖着的时候出牌。"""
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_present())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "popup_dismiss"
    assert tap.execute_calls == []               # 出牌通道零调用
    assert tap.execute_claim_calls == []         # 行动行零调用
    assert tap.execute_leave_calls == []         # 离桌通道零调用
    assert tap.plan_calls == []
    assert loop.actions_done == 0                # 关弹窗不算一次出牌


def test_promo_popup_stuck_after_budget_stops_the_loop(tmp_path, monkeypatch):
    """连续关闭 PROMO_CLOSE_BUDGET 次弹窗仍在 → 停手留证，不无限重试。"""
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_present())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    for _ in range(auto_play_loop.PROMO_CLOSE_BUDGET):
        assert loop.tick()["event"] == "popup_dismiss"
        assert loop.stopped is False

    stuck = loop.tick()

    assert stuck["event"] == "stop"
    assert stuck["gate"] == "promo_popup_stuck"
    assert "promo_popup_stuck" in loop.stop_reason or "促销弹窗" in loop.stop_reason
    # 预算用尽后一次都不许多点。
    assert len(tap.execute_close_popup_calls) == auto_play_loop.PROMO_CLOSE_BUDGET
    assert loop.tick()["event"] == "skip"
    assert len(tap.execute_close_popup_calls) == auto_play_loop.PROMO_CLOSE_BUDGET


def test_promo_popup_unresolved_abstains_without_clicking(
        tmp_path, monkeypatch):
    """弹窗在屏但落点不可信 → 弃权、零点击，且本帧不进回合门禁。

    门禁在这类帧上求值大概率判 ``blocked``，那会既清 ``turn_history`` 又
    直接 ``_stop("gate_turn")`` 把整场打停——所以弹窗在屏的帧必须被拦在
    门禁之前，宁可空转等弹窗变可读，也不能用弹窗像素去做停机判定。
    """
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_unresolved())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    event = loop.tick()

    assert event["event"] == "promo_popup_abstain"
    assert event["status"] == "abstain"
    assert tap.execute_close_popup_calls == []
    assert tap.execute_calls == []
    # 门禁没看过这一帧：台账里既没有 turn_gate，也留了跳过的留证。
    assert "turn_gate" not in event
    assert event["turn_gate_skipped"] == "promo_popup_on_screen"
    assert len(events_of(loop, "promo_popup_abstain")) == 1
    assert events_of(loop, "promo_popup_abstain")[0]["promo_popup"]["present"] is True
    assert loop.stopped is False


def test_pending_post_claim_watchdog_precedes_unresolved_promo(
        tmp_path, monkeypatch):
    """不可读促销遮罩不得靠早退让鸣牌后的必出牌事务无限挂起。"""
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_unresolved())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)
    loop._post_claim_pending.observed_ticks = (
        auto_play_loop.POST_CLAIM_DISCARD_MAX_TICKS)

    event = loop.tick()

    assert event["event"] == "stop"
    assert event["gate"] == "post_claim_discard_timeout"
    assert tap.execute_close_popup_calls == []
    assert tap.execute_calls == []
    assert loop.stopped is True


def test_promo_popup_frame_never_reaches_the_turn_gate(tmp_path, monkeypatch):
    """弹窗帧不进回合门禁求值：turn_history 不被清，也不会被 blocked 打停。"""
    popup_on = {"on": False}
    monkeypatch.setattr(
        auto_play_loop, "read_promo_popup",
        lambda _image: promo_present() if popup_on["on"] else promo_absent())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    settle(loop)                                  # 无弹窗：先攒出连续帧历史
    history_before = list(loop.turn_history)
    assert history_before

    popup_on["on"] = True
    event = loop.tick()

    assert event["event"] == "popup_dismiss"
    assert "turn_gate" not in event               # 门禁确实没求值
    assert event["turn_gate_skipped"] == "promo_popup_on_screen"
    assert loop.turn_history == history_before    # 帧历史原样保住
    assert loop.stopped is False
    assert len(tap.execute_close_popup_calls) == 1


def test_no_promo_popup_leaves_the_original_path_untouched(tmp_path,
                                                            monkeypatch):
    """无弹窗 → 完全走原链路；弹窗通道零调用，计数保持清零。"""
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_absent())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    settle(loop)

    event = loop.tick()

    assert event["event"] == "action"
    assert tap.execute_close_popup_calls == []
    assert len(tap.execute_calls) == 1
    assert loop._promo_close_count == 0
    assert events_of(loop, "popup_dismiss") == []
    assert events_of(loop, "promo_popup_abstain") == []


def test_promo_popup_preempts_round_probe_and_suspect_early_return(
        tmp_path, monkeypatch):
    """促销层先于换局层；真帧不能再被 round_suspect 永久截走。

    2026-10-26 23:46 的 5554 真帧连续写了数十条 round_suspect，虽然
    promo 识别器能以 0.9646 命中关闭 X，旧顺序却永远到不了 promo 通道。
    本测试把换局探针设为“调用即失败”，钉住覆盖层的结构优先级。
    """
    monkeypatch.setattr(auto_play_loop, "read_promo_popup",
                        lambda _image: promo_present())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    monkeypatch.setattr(
        loop, "_probe_round_key",
        lambda *_args, **_kwargs: pytest.fail(
            "promo frame reached round identity probe"),
    )

    event = loop.tick()

    assert event["event"] == "popup_dismiss"
    assert event["turn_gate_skipped"] == "promo_popup_on_screen"
    assert loop.round.seq == 1
    assert loop._promo_close_count == 1
    assert len(tap.execute_close_popup_calls) == 1
    assert loop.stopped is False


def test_promo_close_budget_resets_after_a_clear_frame(tmp_path, monkeypatch):
    """同一弹窗有界重试；弹窗确实消失后下一次重新起算。"""
    popup_on = {"value": True}
    monkeypatch.setattr(
        auto_play_loop, "read_promo_popup",
        lambda _image: promo_present() if popup_on["value"] else promo_absent())
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)

    assert loop.tick()["event"] == "popup_dismiss"
    assert loop._promo_close_count == 1

    popup_on["value"] = False
    loop.tick()
    assert loop._promo_close_count == 0

    popup_on["value"] = True
    assert loop.tick()["event"] == "popup_dismiss"
    assert loop._promo_close_count == 1
    assert len(tap.execute_close_popup_calls) == 2


# ---------------------------------------------------------------------------
# adb 单点治理现场（2026-10-26）：check_gates() 留证据 + 靶机在线门禁
#
# 只读枚举（adb devices / 进程列表），不发 tap/keyevent。**绝不**调
# ensure_single_server(allow_restart=True)——kill-server 只能是用户显式动作。
# 离线测试一律走 make_loop 注入的替身，本文件全程不接触真实模拟器。
# ---------------------------------------------------------------------------
def test_check_gates_records_adb_inventory_evidence(tmp_path):
    """放行时也要把 devices/servers/version/chosen 原样带出来，供故障定性。"""
    loop, *_ = make_loop(tmp_path, enabled=False)
    loop.adb_inventory_fn = lambda: {
        "chosen": "D:/leidian/LDPlayer14/adb.exe", "version": "1.0.41",
        "servers": ["18368"], "devices": ["emulator-test", "emulator-5558"],
        "found": ["D:/leidian/LDPlayer14/adb.exe"], "notes": []}
    loop._adb_inventory = loop.adb_inventory_fn

    report = loop.check_gates()

    inv = report["adb_inventory"]
    assert inv is not None
    assert inv["devices"] == ["emulator-test", "emulator-5558"]
    assert inv["servers"] == ["18368"]
    assert inv["version"] == "1.0.41"
    assert inv["chosen"] == "D:/leidian/LDPlayer14/adb.exe"
    assert report["adb_inventory_error"] == ""
    assert loop._last_gate_report == report


def test_check_gates_blocks_when_serial_is_not_online(tmp_path):
    """靶机不在 adb devices 里 → fail closed 停手，不重试、不自愈。"""
    loop, *_ = make_loop(tmp_path, enabled=False)
    loop._adb_inventory = lambda: {
        "chosen": "adb", "version": "1.0.41", "servers": ["18368"],
        "devices": ["emulator-5556", "emulator-5558"],
        "found": [], "notes": []}

    with pytest.raises(GateBlocked, match="靶机不在线"):
        loop.check_gates()

    report = loop._last_gate_report
    assert report["adb_inventory"]["devices"] == ["emulator-5556", "emulator-5558"]


def test_check_gates_fails_open_when_inventory_is_unreadable(tmp_path):
    """枚举读不到 → 不因此拦人（读不到 ≠ 靶机不在线），但错误要留痕。"""
    loop, *_ = make_loop(tmp_path, enabled=False)
    loop._adb_inventory = lambda: {
        "available": False, "error": "adb 调用超时", "chosen": None,
        "version": None, "servers": [], "devices": [], "found": [], "notes": []}

    report = loop.check_gates()             # 不许抛

    assert report["adb_inventory"] is None
    assert "adb 调用超时" in report["adb_inventory_error"]


def test_gate_blocked_writes_the_adb_evidence_to_the_ledger(tmp_path):
    """门禁失败那一刻必须有现场：今晨 3221225773 就是没留下任何现场才没定性。

    这里先把会话目录建起来（= 循环已经在跑的情形），所以证据要真的落盘。
    """
    loop, *_ = make_loop(tmp_path, foreground=["com.android.launcher3"])
    loop._adb_inventory = lambda: {
        "chosen": "adb", "version": "1.0.41", "servers": ["18368", "20001"],
        "devices": ["emulator-test"], "found": [], "notes": ["two servers?"]}
    loop._ensure_dirs()                     # 循环已建目录 → 留证走落盘

    with pytest.raises(GateBlocked, match="前台包不是游戏"):
        loop.check_gates()

    rows = [row for row in read_ledger(loop) if row.get("event") == "gate_blocked"]
    assert len(rows) == 1
    row = rows[0]
    assert row["adb_inventory"]["servers"] == ["18368", "20001"]
    assert row["adb_inventory"]["devices"] == ["emulator-test"]
    assert "前台包不是游戏" in row["reason"]


def test_cold_start_gate_failure_never_creates_a_session_dir(tmp_path):
    """冷启动自检失败：现场进异常消息，但**绝不**为此新建会话目录。

    既有用例钉死「门禁不放行时不留空会话目录」——留证不能把这条打破，所以
    冷启动路径只把 servers/devices 挂进 GateBlocked 的消息里（stdout 可见）。
    """
    loop, *_ = make_loop(tmp_path, foreground=["com.android.launcher3"])
    # devices 里带上测试 serial，让门禁走到"前台包"这一关再失败。
    loop._adb_inventory = lambda: {
        "chosen": "adb", "version": "1.0.41", "servers": ["18368"],
        "devices": ["emulator-test", "emulator-5556"], "found": [], "notes": []}

    with pytest.raises(GateBlocked) as excinfo:
        loop.check_gates()

    assert not loop.session_dir.exists(), "不该为了留证新建会话目录"
    message = str(excinfo.value)
    assert "前台包不是游戏" in message
    assert "servers=['18368']" in message
    assert "devices=['emulator-test', 'emulator-5556']" in message   # 现场仍可见


# ---------------------------------------------------------------------------
# check_gates() 前台读取失败必须留证（2026-10-26 取证报告登记的第 1 号证据黑洞）
#
# 旧代码 check_gates() 里是裸调用 ``package = self._foreground()``：
# dumpsys 超时/崩溃抛出的异常会**绕过 _gate_blocked()**，于是前面刚采到的
# adb_inventory 现场一行都落不了盘，异常最后只变成 Tk 上一句状态字符串，
# 窗口一关证据归零。以下三条用例把修复后的口径钉死：
#   ① fail closed（绝不停在"读取失败但继续跑"）
#   ② 消息里带【翻译过的】退出码
#   ③ 现场真的写进台账（读盘断言，不是只看有没有调用）
# ---------------------------------------------------------------------------
def _broken_foreground(text: str):
    """返回一个抛指定文本 RuntimeError 的假 _foreground()。"""
    def boom():
        raise RuntimeError(text)

    return boom


def test_check_gates_foreground_crash_persists_the_scene_and_translates_code(tmp_path):
    """带 3221225794 的崩溃：Fail closed + 现场落盘 + 退出码翻译进消息。"""
    loop, *_ = make_loop(tmp_path, enabled=False)
    loop._adb_inventory = lambda: {
        "chosen": "D:/leidian/LDPlayer14/adb.exe", "version": "1.0.41",
        "servers": ["18368"], "devices": ["emulator-test"],
        "found": [], "notes": []}
    # 房卡读数是前台之后的下一道门禁：被调到就说明没有 fail closed。
    card_calls = {"n": 0}

    def counting_card_reader(_image):
        card_calls["n"] += 1
        return 3

    loop._card_reader = counting_card_reader
    loop._foreground = _broken_foreground(
        "ADB 调用失败: Command '['C:\\leidian\\LDPlayer14\\adb.exe', '-s', "
        "'emulator-5556', 'shell', 'dumpsys', 'window', 'windows']' "
        "returned non-zero exit status 3221225794.")
    loop._ensure_dirs()                 # 会话目录已存在 → 这一条必须真落盘

    with pytest.raises(GateBlocked) as excinfo:
        loop.check_gates()

    # ① 不许继续往下跑
    assert card_calls["n"] == 0
    assert "foreground" not in loop._last_gate_report
    # ② 异常原文 + 翻译后的退出码都要在消息里（stdout 也看得见）
    message = str(excinfo.value)
    assert "无法确认前台应用" in message
    assert "RuntimeError" in message
    assert "0xC0000142 STATUS_DLL_INIT_FAILED" in message
    assert "进程起不来" in message
    # ③ 现场真的写进了台账
    rows = [row for row in read_ledger(loop) if row.get("event") == "gate_blocked"]
    assert len(rows) == 1
    row = rows[0]
    assert row["adb_inventory"]["servers"] == ["18368"]
    assert row["adb_inventory"]["devices"] == ["emulator-test"]
    assert row["adb_inventory"]["chosen"] == "D:/leidian/LDPlayer14/adb.exe"
    assert row["foreground_error"]["type"] == "RuntimeError"
    assert "3221225794" in row["foreground_error"]["message"]
    assert row["foreground_error"]["exit_code"] == 3221225794
    assert "STATUS_DLL_INIT_FAILED" in row["foreground_error"]["diagnosis"]


def test_check_gates_foreground_failure_without_exit_code_says_unretrieved(tmp_path):
    """WinError 1455 这条台账原文里没有退出码 → 必须写"未取到退出码"，不许猜。"""
    loop, *_ = make_loop(tmp_path, enabled=False)
    loop._adb_inventory = lambda: {
        "chosen": "adb", "version": "1.0.41", "servers": ["18368"],
        "devices": ["emulator-test"], "found": [], "notes": []}
    loop._foreground = _broken_foreground(
        "ADB 调用失败: [WinError 1455] 页面文件太小，无法完成操作。")
    loop._ensure_dirs()

    with pytest.raises(GateBlocked) as excinfo:
        loop.check_gates()

    message = str(excinfo.value)
    assert "未取到退出码" in message
    assert "WinError 1455" in message
    for known in ("STATUS_ACCESS_VIOLATION", "STATUS_DLL_INIT_FAILED",
                  "STATUS_COMMITMENT_LIMIT"):
        assert known not in message, "抠不到退出码时不许套任何已知码"

    rows = [row for row in read_ledger(loop) if row.get("event") == "gate_blocked"]
    assert len(rows) == 1
    assert rows[0]["foreground_error"]["exit_code"] is None
    assert rows[0]["foreground_error"]["diagnosis"] == "未取到退出码"
    assert rows[0]["adb_inventory"]["servers"] == ["18368"]


def test_cold_start_foreground_failure_keeps_report_but_creates_no_dir(tmp_path):
    """冷启动路径：report 里留住异常，但 818 行的"不建目录"不变量不许被打破。

    这条同时是任务 C 的回归护栏——改 try/except 时最容易顺手把那段条件也改掉。
    """
    loop, *_ = make_loop(tmp_path, enabled=False)

    def timeout_boom():
        raise TimeoutError("Command '[...]' timed out after 15 seconds")

    loop._foreground = timeout_boom

    with pytest.raises(GateBlocked) as excinfo:
        loop.check_gates()

    report = loop._last_gate_report
    assert report["foreground_error"]["type"] == "TimeoutError"
    assert "timed out after 15 seconds" in report["foreground_error"]["message"]
    assert report["foreground_error"]["diagnosis"] == "未取到退出码"
    # 已经采到的现场仍在 report 里，且照样挂在异常消息上
    assert report["adb_inventory"]["servers"] == ["18368"]
    assert "adb 现场: servers=['18368']" in str(excinfo.value)
    # 冷启动不变量完好
    assert not loop.session_dir.exists()
    assert not loop.ledger_path.exists()


# ---------------------------------------------------------------------------
# 内存看门狗（2026-10-26）：观测优先，未标定绝不停手
#
# limit_mb 的 1500 是未标定的初值。WZMJ_RSS_LIMIT_MB 没设 → 只记遥测；
# 设了 → 连续 consecutive=2 次超限才 stop(gate="resource_guard")。
# 只停手留证：不降采样、不关预览、不重启子进程、不自愈重试。
# ---------------------------------------------------------------------------
def _read_resource_log(loop: AutoPlayLoop) -> list[dict]:
    """读 resource_guard.jsonl（内存遥测，与审计台账 actions.jsonl 分开）。"""
    path = loop._resource_log_path
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8"
                                                        ).splitlines() if line.strip()]


def _over_sampler(rss: float = 999.0, commit: float = 1200.0):
    return lambda: {"rss_mb": rss, "commit_mb": commit,
                    "available": True, "reason": ""}


def _ticking_guard(loop, *, limit_mb, sampler, consecutive=2):
    """装一个"每次调用都真采样"的看门狗（interval_s=0、时钟自增）。

    迟滞与节流本身是 mem_guard 的职责、由 test_mem_guard.py 覆盖；这里只验
    auto_play_loop 侧的接线口径（未标定不停手 / 标定后连续超限才停手）。
    """
    try:
        from mem_guard import ResourceGuard
    except Exception as exc:                # noqa: BLE001 - 别人的模块可能正在改
        pytest.skip(f"mem_guard 不可用（{type(exc).__name__}: {exc}），"
                    "跳过看门狗接线口径的验收")
    ticks = itertools.count(0.0, 1.0)
    loop._guard = ResourceGuard(
        limit_mb=limit_mb, interval_s=0.0, consecutive=consecutive,
        sampler=sampler, clock=lambda: next(ticks))
    return loop


def test_resource_guard_only_observes_without_calibration(tmp_path):
    """没设 WZMJ_RSS_LIMIT_MB → 哪怕爆表也只记遥测，绝不停手。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    assert loop._rss_limit_mb is None, "默认必须是未标定（只观测）"
    _ticking_guard(loop, limit_mb=1.0, sampler=_over_sampler(99999.0))

    for _ in range(5):
        assert loop._resource_guard_tick() is None

    assert loop.stopped is False
    assert loop.stop_reason == ""
    rows = _read_resource_log(loop)
    assert rows, "观测模式下必须留下遥测，否则拿不到标定依据"
    assert rows[0]["abort_enabled"] is False
    assert rows[0]["limit_mb"] is None
    assert rows[0]["peak_mb"] == 99999.0
    assert rows[0]["rss_mb"] == 99999.0
    # 只观测：审计台账 actions.jsonl 不许被遥测污染（只观测时压根不该建）
    assert not loop.ledger_path.exists(), "观测模式不该往审计台账写任何东西"


def test_resource_guard_stops_after_consecutive_overruns(tmp_path):
    """设了阈值 → 连续 2 次超限才停手（迟滞：单次尖峰不杀）。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _ticking_guard(loop, limit_mb=10.0, sampler=_over_sampler(999.0))
    loop._rss_limit_mb = 10.0

    assert loop._resource_guard_tick() is None     # 第 1 次超限：不杀
    assert loop.stopped is False
    event = loop._resource_guard_tick()            # 第 2 次连续超限 → 停手

    assert event["event"] == "stop"
    assert event["gate"] == "resource_guard"
    assert "rss=999.0MB" in event["reason"]
    assert "commit=1200.0MB" in event["reason"]
    assert "limit=10.0MB" in event["reason"]
    assert event["limit_mb"] == 10.0
    assert event["abort_enabled"] is True
    assert loop.stopped is True


def test_resource_guard_never_aborts_on_unavailable_reading(tmp_path):
    """读数不可用 → 只记遥测，绝不停手（读不到 ≠ 超限）。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    _ticking_guard(loop, limit_mb=1.0,
                   sampler=lambda: {"rss_mb": None, "commit_mb": None,
                                    "available": False, "reason": "unavailable"})
    loop._rss_limit_mb = 1.0

    for _ in range(4):
        assert loop._resource_guard_tick() is None

    assert loop.stopped is False


def test_rss_limit_env_var_parsing(monkeypatch):
    """WZMJ_RSS_LIMIT_MB：合法正数才生效；未设/非法/非正数一律 = 未标定。"""
    cases = [("900", 900.0), ("1500.5", 1500.5), ("abc", None),
             ("0", None), ("-5", None), ("", None)]
    for raw, expected in cases:
        monkeypatch.setenv("WZMJ_RSS_LIMIT_MB", raw)
        assert auto_play_loop._read_rss_limit_from_env() == expected, raw
    monkeypatch.delenv("WZMJ_RSS_LIMIT_MB", raising=False)
    assert auto_play_loop._read_rss_limit_from_env() is None


def test_uncalibrated_guard_never_signals_abort(tmp_path):
    """未标定（_rss_limit_mb=None）→ 构造出的看门狗**不许**导致停手。

    ``_build_resource_guard(None)`` 必须把 None **原样透传**（mem_guard 的纯观测
    模式），不能塞一个假的 1500 冒充已标定——假阈值写进遥测会看起来像已标定，
    那正是要避免的事。这里刻意不做"新旧 mem_guard 版本"的断言：无论走哪条实现，
    只要 ``_rss_limit_mb is None``，``_resource_guard_tick()`` 都必须在
    ``should_abort()`` 之前短路返回。
    """
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._guard, loop._rss_limit_fell_back = auto_play_loop._build_resource_guard(
        None, sampler=_over_sampler(99999.0))
    if loop._guard is None:
        pytest.skip("mem_guard 不可用")
    loop._rss_limit_mb = None

    for _ in range(3):
        assert loop._resource_guard_tick() is None

    assert loop.stopped is False
    rows = _read_resource_log(loop)
    assert rows and rows[0]["limit_mb"] is None      # 遥测里也必须是"未标定"
    assert rows[0]["abort_enabled"] is False
    assert rows[0]["limit_fallback"] is False        # 新版：没走回退


def test_limit_fallback_is_never_silent(tmp_path, monkeypatch):
    """回退到假阈值时**必须**在遥测里留痕，绝不能静默。

    回退一旦静默生效，就等于把"未标定时塞假 1500"那个错误原样放回：遥测会
    重新出现 ``over: true``，看起来像"已标定且超限"，而其实根本没有阈值。
    所以这里强制要求每一条遥测都带 ``limit_fallback`` 标记。
    """
    try:
        import mem_guard
    except Exception as exc:                # noqa: BLE001 - 别人的模块可能正在改
        pytest.skip(f"mem_guard 不可用（{type(exc).__name__}: {exc}）")
    real = mem_guard.ResourceGuard

    class _OldGuard(real):
        """模拟旧版 mem_guard：不接受 limit_mb=None。"""

        def __init__(self, limit_mb=1500, **kwargs):
            if limit_mb is None:
                raise TypeError("旧版 mem_guard 不支持 limit_mb=None")
            super().__init__(limit_mb=limit_mb, **kwargs)

    monkeypatch.setattr(mem_guard, "ResourceGuard", _OldGuard)
    guard, fell_back = auto_play_loop._build_resource_guard(
        None, sampler=_over_sampler(99999.0))
    assert guard is not None and fell_back is True   # 回退确实发生了

    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._guard, loop._rss_limit_fell_back = guard, fell_back
    loop._rss_limit_mb = None

    assert loop._resource_guard_tick() is None       # 仍然不许停手
    assert loop.stopped is False

    rows = _read_resource_log(loop)
    assert rows and rows[0]["limit_fallback"] is True    # 留痕：不许静默
    assert rows[0]["limit_mb"] is None                   # 同时仍是"未标定"


def test_bot_action_event_gets_unique_provenance():
    loop = object.__new__(AutoPlayLoop)
    loop.consecutive_errors = 9
    loop.card_count = 88
    loop.serial = "emulator-5554"
    loop.tick_index = 42
    loop.round = type("Round", (), {"seq": 7})()
    written = []
    loop._write_event = lambda event: written.append(dict(event))

    first = loop._event({"event": "action", "verified": True})
    second = loop._event({"event": "action", "verified": True})

    assert first["action_source"] == "bot"
    assert first["action_id"].startswith("emulator-5554:r7:t42:")
    assert first["action_id"] != second["action_id"]
    assert written == [first, second]


def test_non_action_event_does_not_claim_bot_click_provenance():
    loop = object.__new__(AutoPlayLoop)
    loop.consecutive_errors = 0
    loop.card_count = None
    loop._write_event = lambda _event: None

    event = loop._event({"event": "idle", "reason": "waiting"})

    assert "action_id" not in event
    assert "action_source" not in event


# ----------------------------------------------------------------------
# 2026-10-27 硬验收：吃／碰／杠不是终点，必须续接一次 verified 出牌
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("action", "supplement_required"),
    [
        (ACTION_CHI, False),
        (ACTION_PENG, False),
        (ACTION_KONG, True),
    ],
)
def test_verified_claim_l3_starts_post_claim_continuation(
        tmp_path, action, supplement_required):
    """L3 已确认的吃/碰/杠要落 action_id，并建立必出牌事务。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    payload = {"action": action, "target_tile": 24,
               "auto_click": {"semantic_verified": True}}

    event = loop._verified_claim_action_event(
        payload, action=action, target_tile=24)

    assert event["event"] == "action"
    assert event["action_kind"] == "claim"
    assert event["action_source"] == "bot"
    assert event["action_id"].startswith(
        f"emulator-test:r{loop.round.seq}:t{loop.tick_index}:claim-{action}:")
    assert loop.actions_done == 1
    assert loop._post_claim_pending is not None
    assert loop._post_claim_pending.action_id == event["action_id"]
    assert loop._post_claim_pending.action == action
    assert loop._post_claim_pending.supplement_required is supplement_required
    assert event["post_claim_continuation"]["status"] == (
        "awaiting_recomputed_discard")


def test_verified_chi_tail_starts_post_claim_continuation(tmp_path):
    """吃的二段链只有 L3=true 才能进入后续出牌事务。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    payload = {"action": ACTION_CHI, "target_tile": 24,
               "auto_click": {}}
    chain = {"stage": "group_clicked"}

    event = loop._chi_finish_tail(payload, chain, True, "吃后副露已落定")

    assert event["event"] == "action"
    assert event["claim"]["auto_click"]["semantic_verified"] is True
    assert event["claim"]["auto_click"]["semantic_reason"] == "吃后副露已落定"
    assert chain["stage"] == "verified"
    assert loop._post_claim_pending is not None
    assert loop._post_claim_pending.action == ACTION_CHI


def test_unverified_chi_tail_never_starts_continuation(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    payload = {"action": ACTION_CHI, "target_tile": 24,
               "auto_click": {}}

    event = loop._chi_finish_tail(
        payload, {"stage": "group_clicked"}, False, "副露尚未落定")

    assert event["_terminal"]["gate"] == "claim_unverified"
    assert loop.actions_done == 0
    assert loop._post_claim_pending is None


def test_verified_discard_closes_post_claim_continuation(tmp_path):
    """后续牌只有 verified 离手后才把 claim action_id 串起来并闭合。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    claim = loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)

    discard = loop._complete_post_claim_after_discard({
        "event": "action", "verified": True, "tap": [346, 836],
    })

    assert discard["continuation_of"] == claim["action_id"]
    assert discard["post_claim_continuation"]["status"] == (
        "completed_by_verified_discard")
    assert loop._post_claim_pending is None


def test_unverified_discard_never_closes_post_claim_continuation(tmp_path):
    """调用侧即使误传未验证事件，事务本身也必须 fail-closed。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    claim = loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)

    discard = loop._complete_post_claim_after_discard({
        "event": "action", "verified": False, "tap": [346, 836],
    })

    assert "continuation_of" not in discard
    assert loop._post_claim_pending is not None
    assert loop._post_claim_pending.action_id == claim["action_id"]


def test_tick_verified_discard_closes_post_claim_continuation(tmp_path):
    """整环接线：鸣牌后要等 fresh 稳定帧重算，下一次 verified 出牌才闭合。"""
    loop, _shot, _analyzer, tap, _ = make_loop(tmp_path, enabled=True)
    claim = loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)

    settle(loop)
    event = loop.tick()

    assert event["event"] == "action"
    assert event["verified"] is True
    assert event["continuation_of"] == claim["action_id"]
    assert event["post_claim_continuation"]["status"] == (
        "completed_by_verified_discard")
    assert loop.actions_done == 2
    assert loop._post_claim_pending is None
    assert len(tap.execute_calls) == 1


def test_post_claim_continuation_timeout_stops_instead_of_silent_wait(
        tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._verified_claim_action_event(
        {"action": ACTION_PENG}, action=ACTION_PENG, target_tile=24)
    loop._post_claim_pending.observed_ticks = (
        auto_play_loop.POST_CLAIM_DISCARD_MAX_TICKS)

    event = loop._post_claim_watchdog(tmp_path / "waiting.png", 0.0)

    assert event["event"] == "stop"
    assert event["gate"] == "post_claim_discard_timeout"
    assert event["post_claim_continuation"]["action"] == ACTION_PENG
    assert loop.stopped is True


def test_post_claim_continuation_never_crosses_round(tmp_path):
    loop, *_ = make_loop(tmp_path, enabled=True)
    claim = loop._verified_claim_action_event(
        {"action": ACTION_CHI}, action=ACTION_CHI, target_tile=24)
    loop.round.seq += 1

    event = loop._post_claim_watchdog(tmp_path / "new-round.png", 0.0)

    assert event["event"] == "stop"
    assert event["gate"] == "post_claim_round_mismatch"
    assert event["post_claim_continuation"]["action_id"] == claim["action_id"]
    assert loop.stopped is True


@pytest.mark.parametrize("action", [ACTION_CHI, ACTION_PENG, ACTION_KONG])
def test_claim_budget_is_atomic_for_required_followup_discard(
        tmp_path, monkeypatch, action):
    """只剩一个动作额度时不得先鸣牌再因 max_actions 留在半条链。"""
    button = ClaimButton("gold", (970, 534, 1097, 666), (1033, 597))
    monkeypatch.setattr(
        auto_play_loop, "read_claims",
        lambda _image: ClaimState(
            (button,), action, f"测试 {action}", {}, True),
    )
    monkeypatch.setattr(
        auto_play_loop, "decide_action",
        lambda *_args, **_kwargs: ActionDecision(
            action=action, target_tile=24, confidence=1.0,
            l1_evidence={"template_status": "matched"},
            l2_reason="测试：鸣牌可执行", needs_human_confirm=False,
            button=button, tap_point=button.center,
        ),
    )
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, claim_auto_click=True, max_actions=1)

    event = loop._claim_policy_event(
        np.zeros((900, 1600, 3), np.uint8),
        tmp_path / "claim.png", CLAIM_HAND_ANALYSIS)

    assert event["claim"]["status"] == "blocked"
    assert "原子完成" in event["claim"]["blocked_reason"]
    assert loop.actions_done == 0
    assert loop._post_claim_pending is None
    assert tap.execute_claim_calls == []


def test_verified_hu_closes_pending_kong_after_supplement(tmp_path):
    """杠后补牌若形成胡，verified Hu 是合法终点，不应硬等一次 discard。"""
    loop, *_ = make_loop(tmp_path, enabled=True)
    kong = loop._verified_claim_action_event(
        {"action": ACTION_KONG}, action=ACTION_KONG, target_tile=24)
    assert loop._post_claim_pending.supplement_required is True

    hu = loop._verified_claim_action_event(
        {"action": ACTION_HU}, action=ACTION_HU, target_tile=None)

    assert hu["continuation_of"] == kong["action_id"]
    assert hu["post_claim_completed_by"] == "verified_hu"
    assert hu["post_claim_continuation"]["status"] == "completed_by_hu"
    assert loop.actions_done == 2
    assert loop._post_claim_pending is None


@pytest.mark.parametrize(
    ("pending_action", "expected_status", "should_tap"),
    [
        (ACTION_PENG, "awaiting_post_claim_discard", False),
        (ACTION_KONG, "ready", True),
    ],
)
def test_only_pending_kong_may_finish_through_verified_hu(
        tmp_path, monkeypatch, pending_action, expected_status, should_tap):
    """吃/碰残留胡盘也不能点；只有杠后补牌成胡可走胡自己的 L3。"""
    button = ClaimButton("gold", (1179, 533, 1305, 666), (1241, 597))
    monkeypatch.setattr(
        auto_play_loop, "read_claims",
        lambda _image: ClaimState((button,), ACTION_HU, "测试胡", {}, True),
    )
    monkeypatch.setattr(
        auto_play_loop, "decide_action",
        lambda *_args, **_kwargs: ActionDecision(
            action=ACTION_HU, confidence=1.0,
            l1_evidence={"template_status": "matched"},
            l2_reason="测试：胡可执行", needs_human_confirm=False,
            button=button, tap_point=button.center,
        ),
    )
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, claim_auto_click=True)
    claim = loop._verified_claim_action_event(
        {"action": pending_action}, action=pending_action, target_tile=24)
    monkeypatch.setattr(
        loop, "_verify_claim_settled",
        lambda *_args, **_kwargs: (
            True, "胡后结算已落定", BoardState(0), [{"frame": "after-hu.png"}]),
    )

    event = loop._claim_policy_event(
        np.zeros((900, 1600, 3), np.uint8),
        tmp_path / "hu.png", CLAIM_HAND_ANALYSIS)

    assert event["claim"]["status"] == expected_status
    assert bool(tap.execute_claim_calls) is should_tap
    if should_tap:
        assert event["event"] == "action"
        assert event["continuation_of"] == claim["action_id"]
        assert event["post_claim_completed_by"] == "verified_hu"
        assert event["claim"]["auto_click"]["semantic_verified"] is True
        assert loop._post_claim_pending is None
    else:
        assert "不重复点击行动圆盘" in event["claim"]["blocked_reason"]
        assert loop._post_claim_pending is not None


# ----------------------------------------------------------------------
# 2026-10-27 01:59--02:05 真帧：账本恢复只补暗手，不替来牌/副露作证
# ----------------------------------------------------------------------
LIVE_015814_ROOT = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
)


def _read_native_frame_or_skip(path: Path, label: str) -> np.ndarray:
    if not path.is_file():
        pytest.skip(f"{label} 真帧缺失")
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


def test_live_015954_one_meld_underread_recovers_peng(tmp_path):
    """01:59:54：一组副露等待手 13→11 少读，独立账本恢复后应碰。"""
    frame = (LIVE_015814_ROOT
             / "ui_20261027_015814_emulator5556_烘熊瞎" / "frames"
             / "20261027_015954_408173_before_587b85.png")
    image = _read_native_frame_or_skip(frame, "01:59:54 一组副露碰")
    complete13 = (28, 6, 7, 8, 43, 43, 47, 48, 24, 24, 25, 25, 26)
    current11 = (28, 6, 7, 8, 43, 43, 47, 48, 24, 24, 26)
    melds = ((3, 4, 5),)
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop, hand=complete13, melds=melds, caishen="t8")

    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(hand=current11, melds=melds, caishen="t8"), frame)
    payload.update({"incoming_tile": 24, "incoming_source": "opponent"})
    decision = decide_action(
        read_claims(image), HandState.from_analysis(payload),
        recommended_runner_config(auto_click_enabled=True), image=image)

    assert evidence["applied"] is True
    assert evidence["expected_count"] == 13
    assert evidence["memory_count"] == 13
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.executable


@pytest.mark.parametrize(
    ("frame_name", "current14", "incoming"),
    [
        ("20261027_020034_166079_before_f6d17d.png",
         (41, 48, 49, 49, 23, 23, 67, 67, 29, 29, 61, 61, 63, 63), 44),
        ("20261027_020110_035872_before_935104.png",
         (41, 44, 44, 48, 49, 49, 23, 23, 29, 29, 61, 61, 63, 63), 67),
    ],
)
def test_live_peng_offer_still_passes_when_peng_worsens_shanten(
        tmp_path, frame_name, current14, incoming):
    """02:00:34/02:01:10：恢复完整手不等于强碰，向听 1→3 必须过。"""
    frame = (LIVE_015814_ROOT
             / "ui_20261027_015814_emulator5554_玉姐" / "frames"
             / frame_name)
    image = _read_native_frame_or_skip(frame, frame_name)
    complete16 = (
        41, 44, 44, 48, 49, 49, 23, 23,
        67, 67, 29, 29, 61, 61, 63, 63,
    )
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop, hand=complete16, caishen="t8")
    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(hand=current14, caishen="t8"), frame)
    payload.update({"incoming_tile": incoming,
                    "incoming_source": "opponent"})

    decision = decide_action(
        read_claims(image), HandState.from_analysis(payload),
        recommended_runner_config(auto_click_enabled=True), image=image)

    assert evidence["applied"] is True
    assert decision is not None
    assert decision.action == ACTION_PASS
    assert "1→3" in decision.l2_reason
    assert decision.executable


def test_live_020523_low_confidence_incoming_stays_blocked(tmp_path):
    """02:05:23：暗手可恢复，但来牌只有 0.849，须弃权而不是猜碰/吃。"""
    frame = (LIVE_015814_ROOT
             / "ui_20261027_015814_emulator5556_烘熊瞎" / "frames"
             / "20261027_020523_725939_before_f3b05e.png")
    image = _read_native_frame_or_skip(frame, "02:05:23 低置信来牌")
    complete16 = (
        6, 6, 7, 8, 9, 41, 42, 42,
        43, 44, 67, 23, 24, 24, 25, 26,
    )
    current14 = (6, 6, 7, 8, 9, 41, 42, 42, 43, 44, 67, 23, 25, 26)
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop, hand=complete16, caishen="s6")

    event = loop._claim_policy_event(
        image, frame,
        _claim_underread(hand=current14, caishen="s6"))

    assert event["claim"]["hand_recovery"]["applied"] is True
    assert event["claim"]["action"] == ""
    assert event["claim"]["status"] == "blocked"
    assert event["claim"]["tap_point"] is None
    sparkle = event["claim"]["incoming_resolution"]["sparkle_tile"]
    assert sparkle["accepted"] is False
    assert sparkle["score"] == pytest.approx(0.8488225, abs=1e-5)
    assert "来牌未知" in event["claim"]["l2_reason"]


def test_claim_payload_recovery_rejects_true_meld_mismatch(tmp_path):
    """张数和多重集即使都能对上，只要副露组变了也绝不恢复旧手。"""
    memory13 = (28, 6, 7, 8, 43, 43, 47, 48, 24, 24, 25, 25, 26)
    current11 = memory13[:-2]
    loop, *_ = make_loop(tmp_path, enabled=True)
    _prime_claim_ledger(loop, hand=memory13, melds=(), caishen="t8")

    payload, evidence = loop._claim_payload_from_ledger(
        _claim_underread(
            hand=current11, melds=((3, 4, 5),), caishen="t8"),
        Path(LEDGER_THIS_FRAME),
    )

    assert evidence["applied"] is False
    assert evidence["reason"] == "meld_mismatch"
    assert evidence["current_melds"] == [[3, 4, 5]]
    assert evidence["memory_melds"] == []
    assert payload["hand_ids"] == list(current11)


# ----------------------------------------------------------------------
# 2026-10-27 17:45 真帧：明杠北后中段局部压暗，杠复核被拒框修复
# ----------------------------------------------------------------------
KONG_DIMMED_ROOT = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261027_174407_emulator5554_玉姐" / "frames"
)
KONG_DIMMED_BEFORE = BoardState(
    concealed_count=13, melds=((41, 42, 43),), claim_buttons=2,
    settled=False,
    concealed=(2, 3, 5, 6, 7, 43, 47, 49, 29, 29, 64, 64, 64))


def _kong_dimmed_frame(tmp_path):
    from live_capture import analyze_bytes

    frame = KONG_DIMMED_ROOT / "20261027_174501_892238_after_claim_8d0c30.png"
    image = _read_native_frame_or_skip(frame, "杠后压暗复核帧")
    result = analyze_bytes(
        frame.read_bytes(), mode="tea_2p", image_path=frame,
        log_dir=Path(tmp_path) / "logs")
    board = BoardState.from_hand_state(
        HandState.from_analysis(result), claim_buttons=0, settled=False,
        note="杠后压暗测试")
    return frame, image, result, board


def test_kong_dimmed_standard_verify_fails_and_box_repair_recovers(tmp_path):
    """标准杠验证因中段压暗少读失败；只修被拒框后完整验证通过。"""
    frame, image, result, board = _kong_dimmed_frame(tmp_path)
    ok, why = auto_play_loop.verify_after_click(
        KONG_DIMMED_BEFORE, board, ACTION_KONG,
        target_tile=64, kong_kind="ming")
    assert not ok
    assert "29: 2" in why and "43: 1" in why

    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[result])
    ok2, why2 = loop._verify_kong_dimmed_box_repair(
        KONG_DIMMED_BEFORE, board, frame.read_bytes(), frame, image,
        target_tile=64, kong_kind="ming")
    assert ok2, why2
    assert "kong_dim_box_repair" in why2


def test_kong_dimmed_box_repair_still_rejects_wrong_before(tmp_path):
    """补全的牌没错也不许过：杠前盘面不对，完整验证照样否决。"""
    frame, image, result, board = _kong_dimmed_frame(tmp_path)
    wrong_before = BoardState(
        concealed_count=13, melds=((41, 42, 43),), claim_buttons=2,
        settled=False,
        concealed=(22, 3, 5, 6, 7, 43, 47, 49, 29, 29, 64, 64, 64))
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[result])
    ok, why = loop._verify_kong_dimmed_box_repair(
        wrong_before, board, frame.read_bytes(), frame, image,
        target_tile=64, kong_kind="ming")
    assert not ok
    assert "仍不通过" in why


def test_kong_dimmed_box_repair_not_engaged_without_rejected_boxes(tmp_path):
    """诊断里没有被拒暗手框时不启用修复（不凭空补牌）。"""
    frame, image, result, board = _kong_dimmed_frame(tmp_path)
    clean = dict(result)
    clean["vision_diagnostics"] = [
        d for d in result["vision_diagnostics"] if d.get("accepted")]
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[clean])
    ok, why = loop._verify_kong_dimmed_box_repair(
        KONG_DIMMED_BEFORE, board, frame.read_bytes(), frame, image,
        target_tile=64, kong_kind="ming")
    assert not ok
    assert "无被拒暗手框" in why


def test_kong_dimmed_box_repair_requires_dim_direction(tmp_path):
    """激活门：把诊断的接受/被拒整体对调后，被拒框并不更暗 → 不启用。"""
    frame, image, result, board = _kong_dimmed_frame(tmp_path)
    tampered = dict(result)
    diags = []
    for d in result["vision_diagnostics"]:
        flipped = dict(d)
        flipped["accepted"] = not d.get("accepted")
        diags.append(flipped)
    tampered["vision_diagnostics"] = diags
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[tampered])
    ok, why = loop._verify_kong_dimmed_box_repair(
        KONG_DIMMED_BEFORE, board, frame.read_bytes(), frame, image,
        target_tile=64, kong_kind="ming")
    assert not ok
    assert "压暗方向证据不成立" in why


# ----------------------------------------------------------------------
# 2026-10-27 18:31 真帧：「过」后整手压暗 + 圆盘残留，落定轮询不得
# 凭签名稳定提前判负（state-aware settle，GPT 交叉复核裁决 v1）
# ----------------------------------------------------------------------
PASS_DIMMED_ROOT = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261027_180652_emulator5556_烘熊瞎" / "frames"
)


def _pass_settle_loop(tmp_path, raw, boards):
    loop, *_ = make_loop(tmp_path, enabled=True)
    loop._shot = lambda: raw
    loop._save_frame = lambda _raw, _tag: Path(tmp_path) / "settle.png"
    loop._sleep = lambda _seconds: None
    sequence = iter(boards)
    loop._board_from_frame = lambda *_a, **_k: next(sequence)
    return loop


def test_pass_dimmed_settle_does_not_early_exit_on_stable_signature(tmp_path):
    """压暗态下前三帧签名全同也不提前判负；第四帧圆盘消失即验证通过。"""
    frames = sorted(PASS_DIMMED_ROOT.glob(
        "20261027_183142_*_after_claim_*.png"))
    if not frames:
        pytest.skip("过后压暗真帧缺失")
    raw = frames[0].read_bytes()
    before = BoardState(
        concealed_count=14, melds=((25, 26, 27),), claim_buttons=2,
        settled=False, selection_panel=False, note="过前")
    dimmed_disc = BoardState(
        concealed_count=6, melds=((25, 26, 27),), claim_buttons=2,
        settled=False, selection_panel=False, note="过后压暗+圆盘残留")
    dimmed_gone = BoardState(
        concealed_count=6, melds=((25, 26, 27),), claim_buttons=0,
        settled=False, selection_panel=False, note="过后压暗+圆盘已消")
    loop = _pass_settle_loop(
        tmp_path, raw, [dimmed_disc, dimmed_disc, dimmed_disc, dimmed_gone])
    verified, reason, _board, samples = loop._verify_claim_settled(
        before, ACTION_PASS, tag="test", note="过后落定测试")
    assert verified, reason
    assert len(samples) == 4
    assert samples[1].get("settle_early_exit_suppressed") == "pass_dimmed"


def test_pass_bright_settle_still_early_exits_on_stable_signature(tmp_path):
    """非压暗态不放宽：两帧签名一致照旧判已落定、立即未证实。"""
    ok, encoded = cv2.imencode(
        ".png", np.full((900, 1600, 3), 214, dtype=np.uint8))
    assert ok
    raw = encoded.tobytes()
    before = BoardState(
        concealed_count=14, melds=((25, 26, 27),), claim_buttons=2,
        settled=False, selection_panel=False, note="过前")
    stuck = BoardState(
        concealed_count=6, melds=((25, 26, 27),), claim_buttons=2,
        settled=False, selection_panel=False, note="过后未迁移")
    loop = _pass_settle_loop(tmp_path, raw, [stuck, stuck, stuck, stuck])
    verified, _reason, _board, samples = loop._verify_claim_settled(
        before, ACTION_PASS, tag="test", note="过后落定测试")
    assert not verified
    assert len(samples) == 2


# ----------------------------------------------------------------------
# 2026-10-27 21:00 真帧：「过」后整手压暗丢分割框，几何恢复 + 结算优先
# ----------------------------------------------------------------------
PASS_DIM_ROOT = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261027_203044_emulator5554_玉姐" / "frames"
)
PASS_DIM_MELDS = ((23, 24, 25), (31, 32, 33))
PASS_DIM_BEFORE = BoardState(
    concealed_count=10, melds=PASS_DIM_MELDS, claim_buttons=2,
    settled=False,
    concealed=(25, 7, 9, 42, 42, 43, 43, 48, 48, 49))
PASS_DIM_AFTER = BoardState(
    concealed_count=4, melds=PASS_DIM_MELDS, claim_buttons=0,
    settled=False, selection_panel=False, concealed=(25, 7, 9, 64))


def _pass_dim_frames():
    after = PASS_DIM_ROOT / "20261027_210051_432031_after_claim_259a6e.png"
    before = PASS_DIM_ROOT / "20261027_210047_756147_before_2d55f6.png"
    img_after = _read_native_frame_or_skip(after, "过后压暗复核帧")
    img_before = _read_native_frame_or_skip(before, "过前亮帧")
    return after, img_after, img_before


def test_pass_dimmed_geometry_repair_recovers_real_frame(tmp_path):
    """标准读 10→4（分割丢框）；同帧归一化重读补全多重集后验证通过。"""
    frame, image, _bright = _pass_dim_frames()
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[{"caishen": "t5"}])
    ok, why = loop._verify_pass_dimmed_geometry_repair(
        PASS_DIM_BEFORE, PASS_DIM_AFTER, frame.read_bytes(), frame, image)
    assert ok, why
    assert "pass_dim_geometry_repair" in why


def test_pass_dimmed_geometry_repair_requires_dim_evidence(tmp_path):
    """亮帧不许启用恢复：牌面带压暗证据不成立即拒绝。"""
    frame, _image, bright = _pass_dim_frames()
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[{"caishen": "t5"}])
    ok, why = loop._verify_pass_dimmed_geometry_repair(
        PASS_DIM_BEFORE, PASS_DIM_AFTER, frame.read_bytes(), frame, bright)
    assert not ok
    assert "压暗证据不成立" in why


def test_pass_dimmed_geometry_repair_rejects_wrong_before(tmp_path):
    """重读牌面没错也不许过：与点击前多重集对不上照样否决。"""
    frame, image, _bright = _pass_dim_frames()
    wrong_before = BoardState(
        concealed_count=10, melds=PASS_DIM_MELDS, claim_buttons=2,
        settled=False,
        concealed=(25, 7, 9, 42, 42, 43, 43, 48, 48, 29))
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[{"caishen": "t5"}])
    ok, why = loop._verify_pass_dimmed_geometry_repair(
        wrong_before, PASS_DIM_AFTER, frame.read_bytes(), frame, image)
    assert not ok
    assert "多重集缺" in why


def test_pass_dimmed_geometry_repair_requires_discs_gone(tmp_path):
    """圆盘还在就不能按压暗恢复放行。"""
    frame, image, _bright = _pass_dim_frames()
    after = BoardState(
        concealed_count=4, melds=PASS_DIM_MELDS, claim_buttons=1,
        settled=False, selection_panel=False, concealed=(25, 7, 9, 64))
    loop, *_ = make_loop(tmp_path, enabled=True, analyses=[{"caishen": "t5"}])
    ok, why = loop._verify_pass_dimmed_geometry_repair(
        PASS_DIM_BEFORE, after, frame.read_bytes(), frame, image)
    assert not ok
    assert "圆盘未被实测为 0" in why


def test_pass_settle_settlement_screen_takes_priority(tmp_path):
    """过后进入结算画面 = 终局迁移：不再用盘面读数否决「过」。

    GPT 交叉复核裁决：强结算证据优先于普通 pass 点后验证。
    """
    frame = PASS_DIM_ROOT / "20261027_210047_756147_before_2d55f6.png"
    _read_native_frame_or_skip(frame, "结算优先测试帧")
    raw = frame.read_bytes()
    settled_board = BoardState(
        concealed_count=2, melds=PASS_DIM_MELDS, claim_buttons=0,
        settled=True, selection_panel=False)
    loop = _pass_settle_loop(tmp_path, raw, [settled_board])
    verified, reason, _board, samples = loop._verify_claim_settled(
        PASS_DIM_BEFORE, ACTION_PASS, tag="after_claim", note="结算优先")
    assert verified, reason
    assert "结算优先" in reason
    assert samples[0]["verified"] is True


# ---------------------------------------------------------------------------
# 2026-10-27 21:46 实机：整场终局总结屏无房号/无财神/无手牌，tick 先落
# 「不在牌桌」分支直接 idle，排在回合门禁之前的返回大厅通道永远走不到
# （两端 16/16 打完只记 idle，卡在总结屏等倒计时消散）。tick 必须在
# 该分支内先给返回大厅通道一次判定机会；不是总结屏时它返回 None，
# 分支行为逐字不变（下方空白帧负例）。
# ---------------------------------------------------------------------------
FINAL_FRAMES_5556 = (
    BOT_DIR.parent / "采集" / "dev_auto_play"
    / "ui_20261027_211056_emulator5556_烘熊瞎" / "frames"
)
NO_HAND_ANALYSIS = {"caishen": None, "hand_ids": [], "mode": "tea_2p"}


def _final_frame_bytes() -> bytes:
    hits = sorted(FINAL_FRAMES_5556.glob("20261027_214600*.png"))
    if not hits:
        pytest.skip("实机终局帧不在本机（采集目录未入 Git）")
    return hits[0].read_bytes()


def test_tick_routes_final_summary_to_return_lobby(tmp_path):
    """整场终局帧过 tick：落账 + 点返回大厅一次，不再记不在牌桌 idle。"""
    from match_score import read_match_score

    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True,
        analyses=[NO_HAND_ANALYSIS], frames=[_final_frame_bytes()],
        hud=[(None, None)], player_name="烘熊瞎",
        match_score_fn=read_match_score)
    event = loop.tick()
    assert event["event"] == "left_table", event
    assert event["match_end"]["present"] is True
    assert event["player_score"] == 4
    assert len(tap.execute_leave_calls) == 1
    assert loop._left_table is True


def test_tick_plain_frame_still_idles_without_leave(tmp_path):
    """普通不在牌桌帧：照旧 idle，返回大厅零调用。"""
    ok, raw = cv2.imencode(".png", np.zeros((900, 1600, 3), dtype=np.uint8))
    assert ok
    loop, _shot, _analyzer, tap, _ = make_loop(
        tmp_path, enabled=True, allow_return_lobby=True,
        analyses=[NO_HAND_ANALYSIS], frames=[raw.tobytes()],
        hud=[(None, None)], player_name="烘熊瞎")
    event = loop.tick()
    assert event["event"] == "idle"
    assert "不在牌桌" in event["reason"]
    assert tap.execute_leave_calls == []
