# -*- coding: utf-8 -*-
"""胡提示面板（听牌提示条）路径的离线测试：零 ADB、零真实点击。

覆盖：
1. turn_gate 分类：面板帧（central_panel 单信号 + 手牌带可读 + analyze
   被干扰）→ wait + code="hint_panel"；advise 满手结果不进该分类；
   双信号结算仍 blocked/gate_settled；bottom_blackout 单信号不进该分类。
2. locate_drawn_tile 纯函数：实测几何（play3 事故帧的牌框）→ 最右分离位
   的六筒(26)；各种不一致 → None（fail-closed）。
3. 循环兜底：面板持续 ≥3 tick + 面板前 advise 好读数 → 恰一次摸切合成
   建议交给 TapExecutor；只闪 1-2 tick / 无 advise 快照 / 定位不到 →
   零出手；结算帧 → 停机；执行器点击前中止 → 继续等待不停机；
   点击后无法验证 → 停机。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_guard import ActionGuardResult  # noqa: E402
from auto_play_loop import AutoPlayLoop, locate_drawn_tile  # noqa: E402
from harvest_tea import EXPECTED_PACKAGE  # noqa: E402
from tap_executor import TapAborted, TapPlan, TapUnverified  # noqa: E402
from turn_gate import HINT_PANEL_CODE, evaluate_turn  # noqa: E402

TAKEOVER_FRAME = (BOT_DIR.parents[1] / "_takeover" / "play3_5556" / "frames"
                  / "20261025_014230_310914_before_d34171.png")

# play3 事故帧实测：面板前好读数（advise，11 张暗手 + 2 组副露 = 17）
GOOD_IDS = [6, 7, 46, 48, 48, 21, 22, 23, 26, 26, 26]
GOOD_MELDS = [[45, 46, 47], [3, 4, 5]]
GOOD_BOXES = [[90 + i * 92, 768, 178 + i * 92, 892] for i in range(11)]
# play3 事故帧实测：面板帧读数（10 张；x≈744 一框被面板遮挡，46 不见了；
# 最右 1491 与左邻间距 101 > 常规 92 = 刚摸的牌 26 六筒）
PANEL_IDS = [6, 7, 48, 48, 21, 22, 23, 26, 26, 26]
PANEL_BOXES = [[560, 768, 648, 892], [652, 768, 741, 892], [837, 768, 925, 892],
               [929, 768, 1017, 892], [1021, 768, 1110, 892],
               [1113, 768, 1202, 892], [1206, 768, 1294, 892],
               [1298, 768, 1386, 892], [1390, 768, 1480, 892],
               [1491, 768, 1581, 892]]


# ----------------------------------------------------------------------
# 合成帧：绿桌面 + 中央奶油面板 + 明亮手牌带（与 test_turn_gate 同口径）
# ----------------------------------------------------------------------
def make_image(*, panel: bool = True, blackout: bool = False) -> np.ndarray:
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, 70, 40)                       # 桌面绿：高饱和，不算奶油色
    if panel:
        image[470:680, 300:1300] = (200, 230, 240)   # 奶油色面板（V=240, S 低）
    image[760:, :] = 235                             # 手牌带明亮可读
    if blackout:
        image[765:, :] = 8                           # 结算遮罩：手牌带全黑
    return image


def encode(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


NORMAL_PNG = encode(make_image(panel=False))
PANEL_PNG = encode(make_image(panel=True))
SETTLED_PNG = encode(make_image(panel=True, blackout=True))


def make_result(status: str, hand_ids, boxes, *, melds=None,
                suggestion_id=None) -> dict:
    diagnostics = [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(hand_ids, boxes)
    ]
    result = {
        "status": status,
        "mode": "tea_2p",
        "caishen": "bai",
        "caishen_source": "manual",
        "hand_ids": list(hand_ids),
        "melds": [list(g) for g in (melds or [])],
        "meld_count": len(melds or []),
        "stacked_kongs": [],
        "reason": "",
        "vision_diagnostics": diagnostics,
        "suggested_boxes": [],
    }
    if status == "advise":
        result["suggestion_id"] = suggestion_id
        result["suggestion"] = "x"
        result["suggested_boxes"] = [list(boxes[0])]
    return result


def good_advise() -> dict:
    return make_result("advise", GOOD_IDS, GOOD_BOXES,
                       melds=GOOD_MELDS, suggestion_id=6)


def panel_abstain() -> dict:
    result = make_result("abstain", PANEL_IDS, PANEL_BOXES)
    result["reason"] = "明牌第1组仅1张，疑似漏检，停止决策"
    return result


# ----------------------------------------------------------------------
# 1. turn_gate 分类
# ----------------------------------------------------------------------
def test_gate_classifies_hint_panel():
    verdict = evaluate_turn(panel_abstain(), make_image(panel=True), [],
                            round_seq=1, tick=1)
    assert verdict.state == "wait"
    assert verdict.code == HINT_PANEL_CODE
    assert verdict.evidence["settled_signals"] == ["central_panel"]


def test_gate_classifies_hint_panel_for_wait_status():
    result = make_result("wait", PANEL_IDS, PANEL_BOXES)
    verdict = evaluate_turn(result, make_image(panel=True), [],
                            round_seq=1, tick=1)
    assert verdict.state == "wait"
    assert verdict.code == HINT_PANEL_CODE


def test_gate_panel_image_with_advise_result_stays_generic_wait():
    """面板图 + 满手 advise 结果（读数没被干扰）→ 旧单信号支，不进分类。"""
    verdict = evaluate_turn(good_advise(), make_image(panel=True), [],
                            round_seq=1, tick=1)
    assert verdict.state == "wait"
    assert verdict.code != HINT_PANEL_CODE
    assert "疑似结算" in verdict.reason


def test_gate_settlement_dual_signal_still_blocked():
    verdict = evaluate_turn(panel_abstain(),
                            make_image(panel=True, blackout=True), [],
                            round_seq=1, tick=1)
    assert verdict.state == "blocked"
    assert verdict.code == "gate_settled"


def test_gate_bottom_blackout_only_is_not_hint_panel():
    image = make_image(panel=False, blackout=True)
    verdict = evaluate_turn(panel_abstain(), image, [], round_seq=1, tick=1)
    assert verdict.state == "wait"
    assert verdict.code != HINT_PANEL_CODE
    assert verdict.evidence["settled_signals"] == ["bottom_blackout"]


def test_real_incident_frame_classifies_and_locates():
    """真实事故帧（play3 01:42:30）端到端：分类 + 定位六筒(26)。"""
    if not TAKEOVER_FRAME.is_file():
        pytest.skip(f"事故帧缺失: {TAKEOVER_FRAME}")
    from assist_tea import analyze_snapshot

    image = cv2.imdecode(np.fromfile(str(TAKEOVER_FRAME), np.uint8),
                         cv2.IMREAD_COLOR)
    result = analyze_snapshot(str(TAKEOVER_FRAME), caishen_label="bai",
                              confirmed_two_player=True,
                              require_visible_caishen=False)
    verdict = evaluate_turn(result, image, [], round_seq=7, tick=50)
    assert verdict.state == "wait"
    assert verdict.code == HINT_PANEL_CODE
    located = locate_drawn_tile(result, GOOD_IDS)
    assert located is not None
    assert located["tile_id"] == 26
    assert located["box"] == [1491, 768, 1581, 892]


# ----------------------------------------------------------------------
# 2. locate_drawn_tile
# ----------------------------------------------------------------------
def test_locator_finds_drawn_tile_on_incident_geometry():
    located = locate_drawn_tile(panel_abstain(), GOOD_IDS)
    assert located == {"tile_id": 26, "box": [1491, 768, 1581, 892]}


def test_locator_rejects_uniform_spacing():
    boxes = [[90 + i * 92, 768, 178 + i * 92, 892] for i in range(len(PANEL_IDS))]
    result = make_result("abstain", PANEL_IDS, boxes)
    assert locate_drawn_tile(result, PANEL_IDS) is None


def test_locator_rejects_added_tile_not_at_right_end():
    # 好读数比面板帧少一张中间的牌：新增牌(21)不在最右 → 不定位
    good = [6, 7, 48, 48, 22, 23, 26, 26, 26]
    assert locate_drawn_tile(panel_abstain(), good) is None


def test_locator_rejects_two_tile_difference():
    panel = make_result("abstain", [6, 7, 48, 48, 21, 22, 23, 26, 26, 24],
                        PANEL_BOXES)
    assert locate_drawn_tile(panel, GOOD_IDS) is None


def test_locator_rejects_diagnostics_hand_mismatch():
    result = panel_abstain()
    result["hand_ids"] = [6, 7, 48, 48, 21, 22, 23, 26, 26, 25]
    assert locate_drawn_tile(result, GOOD_IDS) is None


def test_locator_rejects_too_few_tiles():
    result = make_result("abstain", PANEL_IDS[:3], PANEL_BOXES[:3])
    assert locate_drawn_tile(result, GOOD_IDS) is None


# ----------------------------------------------------------------------
# 3. 循环兜底（假截图 / 假分析 / 假执行器，全程离线）
# ----------------------------------------------------------------------
class Shot:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class Analyzer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, _raw, _path):
        self.calls += 1
        item = (self.results.pop(0) if len(self.results) > 1
                else self.results[0])
        return dict(item)


class FakeTap:
    def __init__(self, *, execute_error=None):
        self.plan_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self.execute_error = execute_error

    def plan(self, analysis):
        self.plan_calls.append(analysis)
        return TapPlan(label="t6", tile_id=26, box=(1491, 768, 1581, 892),
                       x=1536, y=830)

    def execute(self, analysis, **_kwargs):
        self.execute_calls.append(analysis)
        if self.execute_error is not None:
            raise self.execute_error
        return {"executed": True, "dry_run": False, "stage": "verified",
                "tile": "t6", "tile_id": 26, "tap": [1536, 830],
                "box": [1491, 768, 1581, 892],
                "before_count": 11, "after_count": 10,
                "reason": "点击后手牌恰好少一张，验证通过"}


def increasing_clock(step: float = 0.01):
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def make_loop(tmp_path, *, enabled, analyses, frames, tap=None):
    fake_tap = tap if tap is not None else FakeTap()
    loop = AutoPlayLoop(
        enabled=enabled,
        adb_path=sys.executable,
        serial="emulator-test",
        session_dir=Path(tmp_path) / "session",
        max_actions=20,
        duration_s=600.0,
        interval_s=0.0,
        tap_executor=fake_tap,
        shot_fn=Shot(frames),
        analyze_fn=Analyzer(analyses),
        foreground_fn=lambda: EXPECTED_PACKAGE,
        action_guard_fn=lambda _image: ActionGuardResult("clear"),
        round_key_fn=lambda _path: (b"hud-fixed-0123456789", "bai"),
        sleep_fn=lambda _seconds: None,
        clock_fn=increasing_clock(),
    )
    return loop, fake_tap


def test_loop_fallback_draw_discards_after_three_panel_ticks(tmp_path):
    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[good_advise(), panel_abstain(), panel_abstain(),
                  panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])

    first = loop.tick()          # 好读数：门禁稳定性不足 → idle，但快照已存
    assert first["event"] == "idle"
    second = loop.tick()         # 面板第 1 帧
    assert second["event"] == "idle"
    assert second["turn_gate"]["code"] == HINT_PANEL_CODE
    assert second["hint_panel"]["outcome"] == "waiting_streak"
    third = loop.tick()          # 面板第 2 帧：仍不出手
    assert third["event"] == "idle"
    assert tap.execute_calls == []

    fourth = loop.tick()         # 面板第 3 帧：兜底摸切刚摸的牌
    assert fourth["event"] == "action"
    assert fourth["verified"] is True
    assert fourth["hint_panel"]["outcome"] == "attempt"
    assert fourth["hint_panel"]["drawn_tile_id"] == 26
    assert len(tap.execute_calls) == 1
    assert tap.execute_calls[0]["suggestion_id"] == 26
    assert tap.execute_calls[0]["suggested_boxes"] == [[1491, 768, 1581, 892]]
    assert loop.actions_done == 1
    assert loop.stopped is False


def test_loop_panel_flash_two_ticks_never_acts(tmp_path):
    wait_result = make_result("wait", GOOD_IDS, GOOD_BOXES, melds=GOOD_MELDS)
    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[good_advise(), panel_abstain(), panel_abstain(),
                  wait_result],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, NORMAL_PNG])
    for _ in range(4):
        event = loop.tick()
        assert event["event"] == "idle"
    assert tap.execute_calls == []
    assert tap.plan_calls == []
    assert loop.actions_done == 0
    assert loop.stopped is False


def test_loop_settlement_frame_stops_and_never_falls_back(tmp_path):
    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[good_advise(), panel_abstain()],
        frames=[NORMAL_PNG, SETTLED_PNG])
    loop.tick()
    event = loop.tick()
    assert loop.stopped is True
    assert event["turn_gate"]["code"] == "gate_settled"
    assert tap.execute_calls == []
    assert tap.plan_calls == []


def test_loop_no_fallback_without_prior_advise(tmp_path):
    wait_result = make_result("wait", GOOD_IDS, GOOD_BOXES, melds=GOOD_MELDS)
    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[wait_result, panel_abstain(), panel_abstain(),
                  panel_abstain(), panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])
    events = [loop.tick() for _ in range(5)]
    assert all(event["event"] == "idle" for event in events)
    assert events[3]["hint_panel"]["outcome"] == "no_prior_advise_read"
    assert tap.execute_calls == []
    assert loop.stopped is False


def test_loop_no_fallback_when_drawn_tile_unlocatable(tmp_path):
    shifted = make_result("abstain", [6, 7, 48, 48, 21, 22, 23, 26, 26, 24],
                          PANEL_BOXES)
    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[good_advise(), shifted, shifted, shifted],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])
    events = [loop.tick() for _ in range(4)]
    assert events[-1]["hint_panel"]["outcome"] == "drawn_tile_not_located"
    assert tap.execute_calls == []
    assert loop.stopped is False


def test_loop_dry_run_fallback_plans_draw_discard(tmp_path):
    loop, tap = make_loop(
        tmp_path, enabled=False,
        analyses=[good_advise(), panel_abstain(), panel_abstain(),
                  panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])
    events = [loop.tick() for _ in range(4)]
    assert events[-1]["event"] == "dry_run_plan"
    assert events[-1]["hint_panel"]["outcome"] == "dry_run_plan"
    assert len(tap.plan_calls) == 1
    assert tap.plan_calls[0]["suggestion_id"] == 26
    assert tap.execute_calls == []


def test_loop_fallback_pre_tap_abort_keeps_waiting(tmp_path):
    tap = FakeTap(execute_error=TapAborted("新截图无可执行建议"))
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[good_advise(), panel_abstain(), panel_abstain(),
                  panel_abstain(), panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])
    events = [loop.tick() for _ in range(5)]
    assert events[3]["event"] == "idle"
    assert events[3]["hint_panel"]["outcome"] == "executor_aborted_pre_tap"
    assert events[4]["event"] == "idle"
    assert loop.stopped is False
    assert loop.actions_done == 0


def test_loop_fallback_unverified_stops(tmp_path):
    tap = FakeTap(execute_error=TapUnverified("点击后手牌未减少"))
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[good_advise(), panel_abstain(), panel_abstain(),
                  panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG, PANEL_PNG, PANEL_PNG])
    for _ in range(4):
        loop.tick()
    assert loop.stopped is True
    assert len(tap.execute_calls) == 1
