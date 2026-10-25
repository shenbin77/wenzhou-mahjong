# -*- coding: utf-8 -*-
"""吃完整链的离线单元测试：面板读者 / 执行器点组 / 循环编排 / runner。

链路（auto_play_loop._chi_chain_event）：点吃圆盘 → 有界轮询候选面板
→ chi_panel_vision.read_chi_panel 读组 → choose_chi_panel_option 选组
→ TapExecutor.execute_chi_panel_group 点组 → L3 复核（暗手 −2、副露
新增组恰为所选组）。任一步证据不足 fail closed：取消关面板退回过，
或停机落证据。

全程离线：真实归档帧只读回归（文件缺失即 skip）；其余全部假件——
假 vision、假 adb、假截图/分析器、面板读数队列。禁止真实 subprocess。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

TESTS_DIR = Path(__file__).resolve().parent
BOT_DIR = TESTS_DIR.parent
for _p in (str(BOT_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auto_play_loop as apl  # noqa: E402
import tap_executor as te  # noqa: E402
import test_auto_play_loop as tal  # noqa: E402
import test_tap_executor as tte  # noqa: E402
from action_policy import (  # noqa: E402
    ACTION_CHI,
    ACTION_PASS,
    ActionDecision,
    recommended_runner_config,
    validate_runner_config,
)
from chi_panel_vision import ChiPanelGroup, ChiPanelState, read_chi_panel  # noqa: E402
from claim_vision import ClaimButton  # noqa: E402
from tap_executor import (  # noqa: E402
    TapExecutor,
    TapRejected,
    TapUnverified,
)

ARCHIVE_ROOT = BOT_DIR.parent / "采集" / "assist_sessions"


@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch):
    """离线红线：任何真实 subprocess 调用都让测试失败。

    执行器测试在函数体内再 setattr 成 FakeAdb（后装者优先，teardown
    按相反顺序恢复），其余测试一律走注入通道、零 subprocess。
    """
    def forbidden(*_args, **_kwargs):
        raise AssertionError("离线测试禁止任何真实 subprocess/adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)


# ---------------------------------------------------------------------------
# 合成面板状态（循环/执行器测试共用）
# ---------------------------------------------------------------------------

def _group(tiles, x0: int) -> ChiPanelGroup:
    boxes = []
    x = x0
    for _ in tiles:
        boxes.append((x, 550, x + 76, 655))
        x += 79
    union = (boxes[0][0], 550, boxes[-1][2], 655)
    return ChiPanelGroup(tiles=tuple(tiles), box=union,
                         tile_boxes=tuple(boxes))


G123 = _group((1, 2, 3), 605)    # 并集 (605,550,838,655)，中心 (721,602)
G234 = _group((2, 3, 4), 858)    # 并集 (858,550,1091,655)
CANCEL_BTN = ClaimButton(kind="cancel", box=(1401, 554, 1498, 646),
                         center=(1450, 600))
PANEL_OK = ChiPanelState(present=True, complete=True, groups=(G123, G234),
                         cancel=CANCEL_BTN, reason="完整读出")
PANEL_ONLY_WORSENING = ChiPanelState(
    present=True, complete=True, groups=(G234,), cancel=CANCEL_BTN,
    reason="完整读出")
PANEL_INCOMPLETE = ChiPanelState(
    present=True, complete=False, groups=(G123,), cancel=CANCEL_BTN,
    reason="有牌框未认出")
PANEL_ABSENT = ChiPanelState(present=False, complete=False, groups=(),
                             cancel=None, reason="无候选面板证据")


class PanelQueue:
    """apl.read_chi_panel 的假实现：按队列返回，最后一个重复。"""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self, _image):
        self.calls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


# ---------------------------------------------------------------------------
# 面板读者：假 vision 单元测试
# ---------------------------------------------------------------------------

class FakeVision:
    """count_tiles 返回预置框；classify_crop 按裁剪区灰度反推牌 ID。

    牌面用灰度 tile_id*5 涂满牌框；均匀性检查让纵向合并的高框首读
    必然失败，从而 exercise 底部重裁修复路径。
    """

    def __init__(self, boxes, *, unresolved=()):
        self._boxes = list(boxes)
        self.last_meld_boxes: list = []
        self.detection_diagnostics = {
            "unresolved_regions": list(unresolved)}

    def count_tiles(self, _image, roi=None):
        return len(self._boxes), list(self._boxes)

    def classify_crop(self, crop, caishen_label=None):
        if crop is None or crop.size == 0:
            return {"tile_id": None, "label": None, "accepted": False,
                    "score": 0.0}
        mean = float(crop.mean())
        if float(crop.std()) >= 3.0 or mean <= 0:
            return {"tile_id": None, "label": None, "accepted": False,
                    "score": 0.0}
        tile_id = int(round(mean / 5.0))
        if 1 <= tile_id <= 49 and abs(mean - tile_id * 5) < 2.0:
            return {"tile_id": tile_id, "label": f"x{tile_id}",
                    "accepted": True, "score": 0.99}
        return {"tile_id": None, "label": None, "accepted": False,
                "score": 0.0}


def _panel_image(paint: dict) -> np.ndarray:
    """黑底帧 + 取消圆盘（位置照 selection_cancel_frame 实测）+ 涂牌。"""
    image = np.zeros((900, 1600, 3), np.uint8)
    cv2.circle(image, (1300, 380), 50, (255, 170, 0), thickness=-1)
    for box, tile_id in paint.items():
        x0, y0, x1, y1 = box
        image[y0:y1, x0:x1] = tile_id * 5
    return image


def _tile_boxes(x0: int, count: int = 3, y0: int = 550, y1: int = 655):
    boxes = []
    x = x0
    for _ in range(count):
        boxes.append((x, y0, x + 76, y1))
        x += 79
    return boxes


def test_panel_reader_groups_by_gap_and_reads_tiles():
    boxes = _tile_boxes(605) + _tile_boxes(858)
    tiles = (25, 26, 27, 27, 28, 29)
    paint = {box: tile for box, tile in zip(boxes, tiles)}
    state = read_chi_panel(_panel_image(paint), vision=FakeVision(boxes))
    assert state.present and state.complete
    assert [list(g.tiles) for g in state.groups] == [[25, 26, 27],
                                                      [27, 28, 29]]
    assert state.cancel is not None


def test_panel_reader_unread_tile_marks_incomplete():
    boxes = _tile_boxes(605)
    paint = {boxes[0]: 25, boxes[1]: 26}  # 第三张不涂 → 认不出
    state = read_chi_panel(_panel_image(paint), vision=FakeVision(boxes))
    assert state.present and not state.complete
    assert "未认出" in state.reason or state.groups == ()


def test_panel_reader_repairs_tall_merged_box_from_bottom():
    """被弃牌行纵向合并的高框（y0=500）：底部 105px 重裁后认出。"""
    tall = (698, 500, 774, 655)
    normal = _tile_boxes(779, count=2)
    boxes = [tall] + normal
    image = _panel_image({})
    image[550:655, 698:774] = 43 * 5       # 只涂底部面板行段（条3）
    paint = {normal[0]: 44, normal[1]: 45}
    for box, tile in paint.items():
        image[box[1]:box[3], box[0]:box[2]] = tile * 5
    state = read_chi_panel(image, vision=FakeVision(boxes))
    assert state.present and state.complete
    assert [list(g.tiles) for g in state.groups] == [[43, 44, 45]]


def test_panel_reader_absent_without_evidence():
    image = np.zeros((900, 1600, 3), np.uint8)
    state = read_chi_panel(image, vision=FakeVision([]))
    assert not state.present and not state.complete and state.groups == ()


# ---------------------------------------------------------------------------
# 面板读者：真实归档帧回归（只读；缺失即 skip）
# ---------------------------------------------------------------------------

REAL_FRAMES = [
    ("20261024_122607/live/tea_2p/20261024_130737_886389_d18f75.png",
     True, [[26, 27, 28], [27, 28, 29]]),
    ("20261023_235121/live/tea_2p/20261024_000443_510724_da9d53.png",
     True, [[5, 6, 7], [6, 7, 8]]),
    ("20261024_002903/live/tea_2p/20261024_004048_063741_333e3c.png",
     True, [[2, 3, 4], [3, 4, 5], [4, 5, 6]]),
    ("20261023_235121/live/tea_2p/20261024_001052_355565_28411b.png",
     False, [[43, 44, 45], [44, 45, 46]]),
]


@pytest.mark.parametrize("rel,complete,groups", REAL_FRAMES)
def test_panel_reader_real_archive_frames(rel, complete, groups):
    path = ARCHIVE_ROOT / rel
    if not path.is_file():
        pytest.skip(f"归档帧缺失：{rel}")
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    assert image is not None
    state = read_chi_panel(image)
    assert state.present
    assert state.complete is complete
    assert [list(g.tiles) for g in state.groups] == groups


# ---------------------------------------------------------------------------
# 执行器：execute_chi_panel_group
# ---------------------------------------------------------------------------

def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _panel_png() -> bytes:
    image = np.zeros((900, 1600, 3), np.uint8)
    image[550:655, 605:838] = (200, 200, 200)
    return _png(image)


def _blank_png() -> bytes:
    return _png(np.zeros((900, 1600, 3), np.uint8))


def _executor(monkeypatch, fake, session_dir) -> TapExecutor:
    monkeypatch.setattr(subprocess, "run", fake)
    return TapExecutor(enabled=True, adb_path="fake-adb",
                       serial="emulator-test", select_delay=0,
                       verify_delay=0, session_dir=session_dir)


def test_chi_group_tap_clicks_measured_group_center(monkeypatch, tmp_path):
    fake = tte.FakeAdb([_panel_png(), _blank_png()])
    executor = _executor(monkeypatch, fake, tmp_path)
    monkeypatch.setattr(te, "read_chi_panel",
                        PanelQueue([PANEL_OK, PANEL_ABSENT]))
    outcome = executor.execute_chi_panel_group(
        G123.center, group=(1, 2, 3), session_dir=tmp_path)
    assert outcome["executed"] is True and outcome["stage"] == "clicked"
    assert outcome["tap"] == list(G123.center)
    assert outcome["semantic_verified"] is False
    taps = fake.tap_commands()
    assert len(taps) == 1
    assert taps[0][-2:] == [str(G123.center[0]), str(G123.center[1])]


def test_chi_group_tap_rejects_illegal_group_without_adb(monkeypatch,
                                                         tmp_path):
    fake = tte.FakeAdb([])
    executor = _executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapRejected):
        executor.execute_chi_panel_group((721, 602), group=(1, 2, 4),
                                         session_dir=tmp_path)
    with pytest.raises(TapRejected):
        executor.execute_chi_panel_group((721, 602), group=(1, 2, 73),
                                         session_dir=tmp_path)
    assert fake.commands == []


def test_chi_group_tap_rejects_point_outside_panel_roi(monkeypatch,
                                                       tmp_path):
    fake = tte.FakeAdb([])
    executor = _executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapRejected):
        executor.execute_chi_panel_group((100, 100), group=(1, 2, 3),
                                         session_dir=tmp_path)
    assert fake.commands == []


def test_chi_group_tap_rejects_when_group_missing_on_fresh_frame(
        monkeypatch, tmp_path):
    fake = tte.FakeAdb([_panel_png()])
    executor = _executor(monkeypatch, fake, tmp_path)
    only_other = ChiPanelState(present=True, complete=True, groups=(G234,),
                               cancel=CANCEL_BTN, reason="完整")
    monkeypatch.setattr(te, "read_chi_panel", PanelQueue([only_other]))
    with pytest.raises(TapRejected):
        executor.execute_chi_panel_group(
            G123.center, group=(1, 2, 3), session_dir=tmp_path)
    assert fake.tap_commands() == []


def test_chi_group_tap_rejects_point_outside_group_box(monkeypatch,
                                                       tmp_path):
    fake = tte.FakeAdb([_panel_png()])
    executor = _executor(monkeypatch, fake, tmp_path)
    monkeypatch.setattr(te, "read_chi_panel", PanelQueue([PANEL_OK]))
    with pytest.raises(TapRejected):
        # (1200,600) 在面板 ROI 内，但不在 (1,2,3) 组的实测框内。
        executor.execute_chi_panel_group(
            (1200, 600), group=(1, 2, 3), session_dir=tmp_path)
    assert fake.tap_commands() == []


def test_chi_group_tap_unverified_when_frame_unchanged(monkeypatch,
                                                       tmp_path):
    same = _panel_png()
    fake = tte.FakeAdb([same, same])
    executor = _executor(monkeypatch, fake, tmp_path)
    monkeypatch.setattr(te, "read_chi_panel",
                        PanelQueue([PANEL_OK, PANEL_ABSENT]))
    with pytest.raises(TapUnverified):
        executor.execute_chi_panel_group(
            G123.center, group=(1, 2, 3), session_dir=tmp_path)
    assert len(fake.tap_commands()) == 1


def test_chi_group_dry_run_plans_without_adb(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry_run 不允许任何 adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(te, "read_chi_panel", PanelQueue([PANEL_OK]))
    executor = TapExecutor(enabled=False, session_dir=tmp_path)
    image = cv2.imdecode(np.frombuffer(_panel_png(), np.uint8),
                         cv2.IMREAD_COLOR)
    outcome = executor.execute_chi_panel_group(
        G123.center, group=(1, 2, 3), image=image, session_dir=tmp_path)
    assert outcome["executed"] is False and outcome["dry_run"] is True
    assert outcome["stage"] == "plan"


# ---------------------------------------------------------------------------
# 循环编排：吃链
# ---------------------------------------------------------------------------

BEFORE_CHI = {
    "hand_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43],
    "melds": [], "caishen": None, "mode": "tea_2p",
    "incoming_tile": 2, "incoming_source": "opponent",
}
AFTER_CHI = {
    "hand_ids": [2, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43],
    "melds": [[1, 2, 3]], "caishen": None, "mode": "tea_2p",
}
AFTER_CHI_WRONG_GROUP = {
    "hand_ids": [1, 3, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43],
    "melds": [[2, 3, 4]], "caishen": None, "mode": "tea_2p",
}
CHI_POLICY = recommended_runner_config(auto_click_enabled=True,
                                       include_chi=True)


def decide_chi_or_pass(state, _hand, config, image=None):
    """主决策给吃；关吃配置（弃权记忆/取消后重决策）给可执行的过。"""
    if not config.chi_enabled:
        button = next(b for b in state.buttons if b.kind == "pass")
        return ActionDecision(
            action=ACTION_PASS, confidence=1.0,
            l1_evidence={"template_status": "matched"},
            l2_reason="测试：关吃后退回过", needs_human_confirm=False,
            button=button, tap_point=button.center)
    button = next((b for b in state.buttons if b.kind == "gold"), None)
    return ActionDecision(
        action=ACTION_CHI, confidence=1.0,
        l1_evidence={"template_status": "matched"},
        l2_reason="测试：吃", needs_human_confirm=False,
        button=button,
        tap_point=button.center if button is not None else (1033, 600))


class ChiFakeTap(tal.FakeTap):
    """FakeTap + 吃面板点组记录（可注入异常）。"""

    def __init__(self, *args, group_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.group_error = group_error
        self.group_calls: list[dict] = []

    def execute_chi_panel_group(self, point=None, **kwargs):
        self.group_calls.append({"point": point, **kwargs})
        if self.group_error is not None:
            raise self.group_error
        return {"executed": True, "dry_run": False, "stage": "clicked",
                "tap": list(point), "ui_responded": True}


def make_chi_loop(tmp_path, monkeypatch, *, tap, frames, analyses,
                  panel_states, enabled=True):
    monkeypatch.setattr(apl, "decide_action", decide_chi_or_pass)
    monkeypatch.setattr(apl, "read_chi_panel", PanelQueue(panel_states))
    loop, _shot, _analyzer, tap, _ = tal.make_loop(
        tmp_path, enabled=enabled, frames=frames, analyses=analyses,
        action_guard=tal.CLAIM_GUARD, claim_auto_click=True,
        claim_policy=CHI_POLICY, tap=tap)
    return loop, tap


def _claim_actions(tap) -> list:
    return [call["action"] for call in tap.execute_claim_calls]


def test_chi_chain_full_success(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI, AFTER_CHI],
        panel_states=[PANEL_OK, PANEL_ABSENT])
    event = loop.tick()
    claim = event["claim"]
    assert claim["chi_chain"]["choice"]["group"] == [1, 2, 3]
    assert len(tap.group_calls) == 1
    assert list(tap.group_calls[0]["group"]) == [1, 2, 3]
    assert _claim_actions(tap) == [ACTION_CHI]
    assert claim["auto_click"]["semantic_verified"] is True
    assert loop.actions_done == 1
    assert loop.stopped is False


def test_chi_chain_choice_abstain_cancels_then_passes(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.action_row_frame(),
                tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI],
        panel_states=[PANEL_ONLY_WORSENING, PANEL_ABSENT])
    event = loop.tick()
    claim = event["claim"]
    assert claim["chi_chain"]["stage"] == "cancelled_then_passed"
    assert tap.group_calls == []
    assert _claim_actions(tap) == [ACTION_CHI, "cancel_selection",
                                   ACTION_PASS]
    assert loop.actions_done == 1
    assert loop.stopped is False
    assert loop._chi_abstain_key is not None


def test_chi_chain_abstain_memory_blocks_second_chi(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    frames = [tal.action_row_frame(), tal.action_row_frame(),
              tal.action_row_frame(), tal.make_frame(),
              tal.action_row_frame(), tal.make_frame()]
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap, frames=frames,
        analyses=[BEFORE_CHI],
        panel_states=[PANEL_ONLY_WORSENING, PANEL_ABSENT])
    loop.tick()
    event2 = loop.tick()
    claim2 = event2["claim"]
    assert claim2["action"] == ACTION_PASS
    assert "chi_chain" not in claim2
    assert tap.group_calls == []
    assert _claim_actions(tap) == [ACTION_CHI, "cancel_selection",
                                   ACTION_PASS, ACTION_PASS]
    assert loop.stopped is False


def test_chi_chain_panel_missing_stops(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI], panel_states=[PANEL_ABSENT])
    event = loop.tick()
    assert loop.stopped is True
    assert "没读到完整候选面板" in loop.stop_reason
    assert tap.group_calls == []
    assert loop.actions_done == 0
    assert event["claim"]["chi_chain"]["panel"]["present"] is False


def test_chi_chain_panel_incomplete_cancels_then_passes(tmp_path,
                                                        monkeypatch):
    tap = ChiFakeTap()
    frames = ([tal.action_row_frame()] + [tal.make_frame()] * 6
              + [tal.action_row_frame(), tal.make_frame()])
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap, frames=frames,
        analyses=[BEFORE_CHI], panel_states=[PANEL_INCOMPLETE])
    event = loop.tick()
    claim = event["claim"]
    assert claim["chi_chain"]["stage"] == "cancelled_then_passed"
    assert tap.group_calls == []
    assert _claim_actions(tap) == [ACTION_CHI, "cancel_selection",
                                   ACTION_PASS]
    assert loop.stopped is False


def test_chi_chain_group_rejected_falls_back_to_cancel_pass(
        tmp_path, monkeypatch):
    tap = ChiFakeTap(group_error=TapRejected("fresh 帧组不存在"))
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.action_row_frame(),
                tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI],
        panel_states=[PANEL_OK, PANEL_ABSENT])
    event = loop.tick()
    claim = event["claim"]
    assert claim["chi_chain"]["stage"] == "cancelled_then_passed"
    assert len(tap.group_calls) == 1
    assert _claim_actions(tap) == [ACTION_CHI, "cancel_selection",
                                   ACTION_PASS]
    assert loop.stopped is False


def test_chi_chain_group_unverified_stops(tmp_path, monkeypatch):
    tap = ChiFakeTap(group_error=TapUnverified("点后未变"))
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI],
        panel_states=[PANEL_OK, PANEL_ABSENT])
    loop.tick()
    assert loop.stopped is True
    assert "吃选组点击未通过" in loop.stop_reason
    assert loop.actions_done == 0


def test_chi_chain_l3_wrong_meld_group_stops(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI, AFTER_CHI_WRONG_GROUP],
        panel_states=[PANEL_OK, PANEL_ABSENT])
    event = loop.tick()
    assert loop.stopped is True
    assert event["claim"]["auto_click"]["semantic_verified"] is False
    assert "与所选组" in event["claim"]["auto_click"]["semantic_reason"]
    assert loop.actions_done == 0


def test_chi_chain_single_candidate_auto_completes(tmp_path, monkeypatch):
    """只有一组候选时客户端可能不弹面板直接成吃：轮询帧迁移成立即认。"""
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap,
        frames=[tal.action_row_frame(), tal.make_frame()],
        analyses=[BEFORE_CHI, AFTER_CHI],
        panel_states=[PANEL_ABSENT])
    event = loop.tick()
    assert event["claim"]["auto_click"]["semantic_verified"] is True
    assert tap.group_calls == []
    assert loop.actions_done == 1
    assert loop.stopped is False


def test_chi_chain_dry_run_plans_only(tmp_path, monkeypatch):
    tap = ChiFakeTap()
    loop, tap = make_chi_loop(
        tmp_path, monkeypatch, tap=tap, enabled=False,
        frames=[tal.action_row_frame()], analyses=[BEFORE_CHI],
        panel_states=[PANEL_OK])
    event = loop.tick()
    assert event["claim"]["chi_chain"]["stage"] == "planned_only"
    assert tap.group_calls == []
    assert loop.actions_done == 0
    assert loop.stopped is False


# ---------------------------------------------------------------------------
# runner：--claim-chi → claim_policy
# ---------------------------------------------------------------------------

def test_runner_claim_policy_requires_both_switches():
    import run_auto_play_dev as runner

    both = argparse.Namespace(claim_chi=True, claim_auto_click=True)
    cfg = runner.resolve_claim_policy(both)
    assert cfg is not None and cfg.chi_enabled
    assert ACTION_CHI in cfg.confirmed_actions
    assert validate_runner_config(cfg) == ()

    no_click = argparse.Namespace(claim_chi=True, claim_auto_click=False)
    assert runner.resolve_claim_policy(no_click) is None

    no_chi = argparse.Namespace(claim_chi=False, claim_auto_click=True)
    assert runner.resolve_claim_policy(no_chi) is None


def test_runner_parser_accepts_claim_chi_flag():
    import run_auto_play_dev as runner

    args = runner.build_parser().parse_args(
        ["--claim-auto-click", "--claim-chi"])
    assert args.claim_chi is True and args.claim_auto_click is True
    assert runner.resolve_claim_policy(args) is not None
