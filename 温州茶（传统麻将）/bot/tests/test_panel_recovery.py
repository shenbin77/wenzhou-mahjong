# -*- coding: utf-8 -*-
"""胡提示面板「待二击」重启恢复的离线测试：零真实设备操作。

覆盖：
1. panel_recovery 纯函数：状态文件写/读/清、格式校验；
   decide_recovery 的放行与各类弃权（唯一抬起牌恢复成功、抬起不唯一、
   抬起的是别的牌、局身份不一致、状态过期、手牌不完整、列漂移、
   移出 ROI 后唯一定位 / 定位不到）。
2. TapExecutor：execute() 第一击后落盘待二击状态、验证成功清除、
   无 pending_context 时不落盘；execute_recovery_second_tap 的
   成功（只点重定位中心一次）/ 点后未验证（状态保留）/ 点击前中止
   （零点击）。
3. AutoPlayLoop：新进程在面板帧上凭状态文件恢复第二击；局身份不符
   → 清除状态并回落原兜底；抬起不唯一 → 继续弃权等待且不让摸切
   兜底出手；无状态文件 → 行为与旧版一致；确认换局 → 状态清除。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import panel_recovery  # noqa: E402
from action_guard import ActionGuardResult  # noqa: E402
from assist_tea import analyze_snapshot  # noqa: E402
from auto_play_loop import AutoPlayLoop  # noqa: E402
from harvest_tea import EXPECTED_PACKAGE  # noqa: E402
from tap_executor import (  # noqa: E402
    TapAborted,
    TapExecutor,
    TapPlan,
    TapUnverified,
)

# ----------------------------------------------------------------------
# 1. panel_recovery 纯函数
# ----------------------------------------------------------------------

STATE_IDS = [6, 7, 46, 48, 48, 21, 22, 23, 26, 26, 26]
STATE_BOXES = [[90 + i * 92, 768, 178 + i * 92, 892] for i in range(11)]
PENDING_BOX = STATE_BOXES[10]                    # 最右一张 26（待二击牌）
RAISED_BOX = [PENDING_BOX[0], 728, PENDING_BOX[2], 852]
HUD_BYTES = b"hud-fixed-0123456789"


def make_diag_result(ids, boxes, *, extra_diagnostics=None) -> dict:
    diagnostics = [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(ids, boxes)
    ]
    diagnostics.extend(extra_diagnostics or [])
    return {"status": "abstain", "mode": "tea_2p", "caishen": "bai",
            "hand_ids": list(ids), "vision_diagnostics": diagnostics}


def make_state(**overrides) -> dict:
    state = panel_recovery.build_state(
        serial="emulator-5556", tile_id=26, tile_label="t6",
        tap=[(PENDING_BOX[0] + PENDING_BOX[2]) // 2,
             (PENDING_BOX[1] + PENDING_BOX[3]) // 2],
        box=PENDING_BOX, before_count=11, hand_ids=STATE_IDS,
        hud_key=HUD_BYTES.hex(), caishen="bai", caishen_id=67,
        round_seq=3, saved_at=1000.0)
    state.update(overrides)
    return state


def decide(state, result, **kwargs):
    kwargs.setdefault("hud_key", HUD_BYTES)
    kwargs.setdefault("caishen", "bai")
    kwargs.setdefault("now", 1000.0)
    return panel_recovery.decide_recovery(state, result, **kwargs)


def test_state_path_for_is_serial_stable(tmp_path):
    path = panel_recovery.state_path_for("emulator-5556", root=tmp_path)
    assert path == tmp_path / "emulator-5556.json"
    weird = panel_recovery.state_path_for("a/b:c", root=tmp_path)
    assert weird.name == "a_b_c.json"
    default = panel_recovery.state_path_for("emulator-5556")
    assert default.parent.name == "_pending_second_tap"


def test_state_write_read_clear_roundtrip(tmp_path):
    path = tmp_path / "sub" / "pending.json"
    state = make_state()
    assert panel_recovery.write_state(path, state) is True
    loaded, error = panel_recovery.read_state(path)
    assert error is None
    assert loaded == state
    assert panel_recovery.clear_state(path) is True
    assert panel_recovery.read_state(path) == (None, None)
    assert panel_recovery.clear_state(path) is True  # 不存在不算失败


def test_read_state_corrupt_reports_error(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("{not json", encoding="utf-8")
    state, error = panel_recovery.read_state(path)
    assert state is None and error is not None
    path.write_text('{"version": 99}', encoding="utf-8")
    state, error = panel_recovery.read_state(path)
    assert state is None and "格式无效" in error


def test_decide_case_a_unique_raised_recovers():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[10] = list(RAISED_BOX)
    decision = decide(make_state(), make_diag_result(STATE_IDS, boxes))
    assert decision.allowed is True
    assert decision.case == "in_roi_lifted"
    assert decision.tile_id == 26
    assert decision.box == RAISED_BOX
    assert decision.tap_point == ((RAISED_BOX[0] + RAISED_BOX[2]) // 2,
                                   (RAISED_BOX[1] + RAISED_BOX[3]) // 2)


def test_decide_abstains_when_two_raised():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[9] = [box[0], 728, box[2], 852] if (box := STATE_BOXES[9]) else None
    boxes[10] = list(RAISED_BOX)
    decision = decide(make_state(), make_diag_result(STATE_IDS, boxes))
    assert decision.allowed is False
    assert decision.reason.startswith("raised_not_unique")


def test_decide_abstains_when_raised_is_another_tile():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[0] = [STATE_BOXES[0][0], 728, STATE_BOXES[0][2], 852]  # 抬起的是 6
    decision = decide(make_state(), make_diag_result(STATE_IDS, boxes))
    assert decision.allowed is False
    assert decision.reason == "raised_tile_mismatch"


def test_decide_abstains_on_identity_mismatch():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[10] = list(RAISED_BOX)
    result = make_diag_result(STATE_IDS, boxes)
    by_caishen = decide(make_state(), result, caishen="dong")
    assert by_caishen.allowed is False
    assert by_caishen.reason.startswith("identity_mismatch")
    by_hud = decide(make_state(), result, hud_key=b"hud-other-9876543210")
    assert by_hud.allowed is False
    assert by_hud.reason.startswith("identity_mismatch")
    unverifiable = decide(make_state(), result, hud_key=None, caishen=None)
    assert unverifiable.allowed is False
    assert unverifiable.reason.startswith("identity_mismatch")


def test_decide_abstains_when_state_stale():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[10] = list(RAISED_BOX)
    decision = decide(make_state(), make_diag_result(STATE_IDS, boxes),
                      now=1000.0 + panel_recovery.MAX_STATE_AGE_S + 1)
    assert decision.allowed is False
    assert decision.reason.startswith("state_stale")


def test_decide_abstains_when_hand_incomplete():
    # 少了一张非待二击牌（46 被面板遮挡）：既非情形 A 也非情形 B。
    ids = [tile for tile in STATE_IDS if tile != 46]
    boxes = STATE_BOXES[:2] + STATE_BOXES[3:]
    decision = decide(make_state(), make_diag_result(ids, boxes))
    assert decision.allowed is False
    assert decision.reason == "hand_mismatch"


def test_decide_abstains_on_column_drift():
    # 抬起的 26 跑到最左列：与第一击牌框 x 完全不重叠。
    ids = [26] + STATE_IDS[1:10] + [6]
    boxes = [list(box) for box in STATE_BOXES]
    boxes[0] = [STATE_BOXES[0][0], 728, STATE_BOXES[0][2], 852]
    decision = decide(make_state(), make_diag_result(ids, boxes))
    assert decision.allowed is False
    assert decision.reason == "raised_column_drift"


def test_decide_case_b_out_of_roi_uniquely_located():
    ids = STATE_IDS[:-1]                       # 暗手恰少待二击的 26
    boxes = STATE_BOXES[:-1]
    extra = [{"tile_id": 26, "label": None, "box": list(RAISED_BOX),
              "accepted": False, "region": "concealed",
              "score": .8, "margin": .1}]
    result = make_diag_result(ids, boxes, extra_diagnostics=extra)
    decision = decide(make_state(), result)
    assert decision.allowed is True
    assert decision.case == "out_of_roi_located"
    assert decision.box == RAISED_BOX


def test_decide_case_b_not_located_abstains():
    result = make_diag_result(STATE_IDS[:-1], STATE_BOXES[:-1])
    decision = decide(make_state(), result)
    assert decision.allowed is False
    assert decision.reason.startswith("raised_tile_not_uniquely_located")


def test_decide_abstains_without_state_hand():
    boxes = [list(box) for box in STATE_BOXES]
    boxes[10] = list(RAISED_BOX)
    decision = decide(make_state(hand_ids=None),
                      make_diag_result(STATE_IDS, boxes))
    assert decision.allowed is False
    assert decision.reason == "state_hand_unknown"


# ----------------------------------------------------------------------
# 2. TapExecutor：落盘 / 清除 / 恢复第二击（真实帧 + FakeAdb，离线）
# ----------------------------------------------------------------------

FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
         / "live" / "tea_2p" / "20261023_205409_557426_31fea1.png")


@pytest.fixture(scope="module")
def analysis(tmp_path_factory) -> dict:
    logs = tmp_path_factory.mktemp("panel_recovery_logs")
    return analyze_snapshot(
        FRAME, caishen_label="", confirmed_two_player=True, log_dir=logs)


def frame_png() -> bytes:
    return FRAME.read_bytes()


def erase_tile(png_bytes: bytes, box) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, y1 = (int(value) for value in box)
    image[y0:y1, x0:x1] = (20, 60, 30)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def raise_tile(png_bytes: bytes, box) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, y1 = (int(value) for value in box)
    image[y0 - 25:y1 - 25, x0:x1] = image[y0:y1, x0:x1]
    image[y1 - 25:y1, x0:x1] = (20, 60, 30)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def confirmed_frame(selected_png: bytes, box) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(selected_png, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, _y1 = (int(value) for value in box)
    image[max(0, y0 - 70):max(0, y0 - 35), x0:x1] = (0, 0, 220)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def hint_panel_frame(png_bytes: bytes, box) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(erase_tile(png_bytes, box), dtype=np.uint8),
        cv2.IMREAD_COLOR).copy()
    image[470:680, 300:1300] = (200, 230, 240)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


class FakeAdb:
    """与 test_tap_executor 同口径：记录命令，离线应答 dumpsys/截图/点击。"""

    def __init__(self, png_frames: list[bytes]):
        self.frames = list(png_frames)
        self.commands: list[list[str]] = []

    def __call__(self, command, capture_output=False, timeout=None,
                 check=False, creationflags=0, **kwargs):
        parts = [str(part) for part in command]
        if not parts or parts[0] != "fake-adb":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        self.commands.append(parts)
        joined = " ".join(parts)
        if "dumpsys" in joined:
            stdout = (b"topResumedActivity=ActivityRecord{1 u0 "
                      b"com.xm.zjgamecenter/com.xm.main.MainActivity}\n")
            return subprocess.CompletedProcess(command, 0, stdout, b"")
        if "screencap" in joined:
            assert self.frames, "unexpected extra screencap"
            return subprocess.CompletedProcess(
                command, 0, self.frames.pop(0), b"")
        if "input" in joined and "tap" in joined:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(f"unexpected adb command: {joined}")

    def tap_commands(self) -> list[list[str]]:
        return [cmd for cmd in self.commands
                if "input" in cmd and "tap" in cmd]


def _executor(monkeypatch, fake, session_dir, **kwargs) -> TapExecutor:
    monkeypatch.setattr(subprocess, "run", fake)
    return TapExecutor(enabled=True, adb_path="fake-adb",
                       serial="emulator-test", select_delay=0,
                       verify_delay=0, session_dir=session_dir, **kwargs)


def _pending_path(tmp_path) -> Path:
    return tmp_path / "pending_second_tap.json"


def _context(analysis) -> dict:
    return {"hand_ids": sorted(int(t) for t in analysis["hand_ids"]),
            "hud_key": "abcd", "caishen": analysis.get("caishen"),
            "caishen_id": None, "round_seq": 3}


def test_execute_persists_pending_state_when_second_tap_unverified(
        analysis, monkeypatch, tmp_path):
    """第一击抬牌后落盘；第二击验证失败（进程可能重启）→ 状态必须留着。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    raised = raise_tile(png, box)
    fake = FakeAdb([png, raised, raised])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.pending_context = _context(analysis)
    executor.pending_state_path = _pending_path(tmp_path)
    with pytest.raises(TapUnverified):
        executor.execute(analysis)
    state, error = panel_recovery.read_state(_pending_path(tmp_path))
    assert error is None and state is not None
    assert state["tile_id"] == analysis["suggestion_id"]
    assert state["hand_ids"] == sorted(int(t) for t in analysis["hand_ids"])
    assert state["box"] == box
    assert state["tap"] == [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
    assert state["serial"] == "emulator-test"
    assert state["round_seq"] == 3


def test_execute_clears_pending_state_on_verified_discard(
        analysis, monkeypatch, tmp_path):
    """正常两击打完：状态文件清除，不给下个进程留假恢复入口。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    panel_recovery.write_state(
        _pending_path(tmp_path),
        panel_recovery.build_state(
            serial="emulator-test", tile_id=analysis["suggestion_id"],
            tile_label="x", tap=[0, 0], box=box, before_count=17,
            hand_ids=[1], saved_at=time.time()))
    fake = FakeAdb([png, raise_tile(png, box), erase_tile(png, box)])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.pending_context = _context(analysis)
    executor.pending_state_path = _pending_path(tmp_path)
    outcome = executor.execute(analysis)
    assert outcome["stage"] == "verified"
    assert not _pending_path(tmp_path).exists()


def test_execute_without_context_never_writes_state(
        analysis, monkeypatch, tmp_path):
    """没人给 pending_context（旧调用方）→ 行为与旧版完全一致，不落盘。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    raised = raise_tile(png, box)
    fake = FakeAdb([png, raised, raised])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.pending_state_path = _pending_path(tmp_path)
    with pytest.raises(TapUnverified):
        executor.execute(analysis)
    assert not _pending_path(tmp_path).exists()


def _recovery_state_and_geometry(executor, analysis):
    png = frame_png()
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    count, boxes = executor._make_vision().count_tiles(image)
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    state = panel_recovery.build_state(
        serial="emulator-test", tile_id=analysis["suggestion_id"],
        tile_label="x", tap=[(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
        box=box, before_count=count,
        hand_ids=sorted(int(t) for t in analysis["hand_ids"]),
        saved_at=time.time())
    concealed = [[int(v) for v in item[:4]] for item in boxes]
    return state, box, concealed


def test_recovery_second_tap_success_clears_state(
        analysis, monkeypatch, tmp_path):
    png = frame_png()
    box0 = [int(v) for v in analysis["suggested_boxes"][0]]
    panel = hint_panel_frame(png, box0)
    after = confirmed_frame(erase_tile(png, box0), box0)
    fake = FakeAdb([panel, after])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.pending_state_path = _pending_path(tmp_path)
    state, box, concealed = _recovery_state_and_geometry(executor, analysis)
    panel_recovery.write_state(_pending_path(tmp_path), state)

    def locator(_image):
        return {"suggested_boxes": [list(box)],
                "_locked_before_count": state["before_count"],
                "_recovery_concealed_boxes": concealed}

    outcome = executor.execute_recovery_second_tap(state, locator)
    assert outcome["executed"] is True
    assert outcome["stage"] == "recovery_second_tap"
    assert outcome["before_count"] - outcome["after_count"] == 1
    taps = fake.tap_commands()
    assert len(taps) == 1
    assert taps[0][-2:] == [str((box[0] + box[2]) // 2),
                            str((box[1] + box[3]) // 2)]
    assert not _pending_path(tmp_path).exists()


def test_recovery_second_tap_unverified_keeps_state(
        analysis, monkeypatch, tmp_path):
    png = frame_png()
    panel = hint_panel_frame(png, [int(v) for v in
                                   analysis["suggested_boxes"][0]])
    fake = FakeAdb([panel, png])  # 点后手牌没少 → 验证失败
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.pending_state_path = _pending_path(tmp_path)
    state, box, concealed = _recovery_state_and_geometry(executor, analysis)
    panel_recovery.write_state(_pending_path(tmp_path), state)

    def locator(_image):
        return {"suggested_boxes": [list(box)],
                "_locked_before_count": state["before_count"],
                "_recovery_concealed_boxes": concealed}

    with pytest.raises(TapUnverified):
        executor.execute_recovery_second_tap(state, locator)
    assert len(fake.tap_commands()) == 1
    assert _pending_path(tmp_path).exists()


def test_recovery_locator_abort_sends_zero_taps(
        analysis, monkeypatch, tmp_path):
    png = frame_png()
    fake = FakeAdb([hint_panel_frame(png, [int(v) for v in
                                           analysis["suggested_boxes"][0]])])
    executor = _executor(monkeypatch, fake, tmp_path)
    state, _box, _concealed = _recovery_state_and_geometry(executor, analysis)

    def locator(_image):
        raise TapAborted("fresh 帧不再是单信号胡提示面板")

    with pytest.raises(TapAborted):
        executor.execute_recovery_second_tap(state, locator)
    assert fake.tap_commands() == []


# ----------------------------------------------------------------------
# 3. AutoPlayLoop：跨进程恢复的接线（假截图/假分析/假执行器）
# ----------------------------------------------------------------------

GOOD_IDS = [6, 7, 46, 48, 48, 21, 22, 23, 26, 26, 26]
GOOD_MELDS = [[45, 46, 47], [3, 4, 5]]
GOOD_BOXES = [[90 + i * 92, 768, 178 + i * 92, 892] for i in range(11)]
PANEL_IDS = [6, 7, 48, 48, 21, 22, 23, 26, 26, 26]
PANEL_BOXES = [[560, 768, 648, 892], [652, 768, 741, 892],
               [837, 768, 925, 892], [929, 768, 1017, 892],
               [1021, 768, 1110, 892], [1113, 768, 1202, 892],
               [1206, 768, 1294, 892], [1298, 768, 1386, 892],
               [1390, 768, 1480, 892], [1491, 768, 1581, 892]]


def make_image(*, panel: bool = True, blackout: bool = False) -> np.ndarray:
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:, :] = (40, 70, 40)
    if panel:
        image[470:680, 300:1300] = (200, 230, 240)
    image[760:, :] = 235
    if blackout:
        image[765:, :] = 8
    return image


def encode(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


NORMAL_PNG = encode(make_image(panel=False))
PANEL_PNG = encode(make_image(panel=True))


def make_result(status: str, hand_ids, boxes, *, melds=None,
                suggestion_id=None) -> dict:
    diagnostics = [
        {"tile_id": tile, "label": None, "box": list(box), "accepted": True,
         "region": "concealed", "score": .99, "margin": .2}
        for tile, box in zip(hand_ids, boxes)
    ]
    result = {
        "status": status, "mode": "tea_2p", "caishen": "bai",
        "caishen_source": "manual", "hand_ids": list(hand_ids),
        "melds": [list(g) for g in (melds or [])],
        "meld_count": len(melds or []), "stacked_kongs": [], "reason": "",
        "vision_diagnostics": diagnostics, "suggested_boxes": [],
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


def panel_raised_result(second_raised: bool = False) -> dict:
    """面板帧 + 整手可读 + 最右 26 处于抬起位（重启恢复的目标画面）。"""
    boxes = [list(box) for box in GOOD_BOXES]
    boxes[10] = [GOOD_BOXES[10][0], 728, GOOD_BOXES[10][2], 852]
    if second_raised:
        boxes[9] = [GOOD_BOXES[9][0], 728, GOOD_BOXES[9][2], 852]
    return make_result("abstain", GOOD_IDS, boxes)


class Shot:
    def __init__(self, frames):
        self.frames = list(frames)

    def __call__(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class Analyzer:
    def __init__(self, results):
        self.results = list(results)

    def __call__(self, _raw, _path):
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


class RecoveryTap(FakeTap):
    """带恢复入口的假执行器：像真执行器一样先调 fresh_locator 再报成功。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recovery_calls: list[dict] = []

    def execute_recovery_second_tap(self, state, fresh_locator):
        self.recovery_calls.append(state)
        synthetic = fresh_locator(make_image(panel=True))
        assert synthetic["suggestion_id"] == state["tile_id"]
        return {"executed": True, "dry_run": False,
                "stage": "recovery_second_tap",
                "tile": "t6", "tile_id": state["tile_id"],
                "tap": list(synthetic["suggested_boxes"][0][:2]),
                "box": synthetic["suggested_boxes"][0],
                "before_count": state["before_count"],
                "after_count": state["before_count"] - 1,
                "reason": "恢复第二击完成"}


def increasing_clock(step: float = 0.01):
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def make_loop(tmp_path, *, enabled, analyses, frames, tap=None,
              round_key_fn=None):
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
        round_key_fn=round_key_fn or (lambda _path: (HUD_BYTES, "bai")),
        sleep_fn=lambda _seconds: None,
        clock_fn=increasing_clock(),
    )
    return loop, fake_tap


def write_loop_state(loop, tmp_path, **overrides) -> Path:
    path = Path(tmp_path) / "pending.json"
    loop._panel_recovery_path = path
    state = panel_recovery.build_state(
        serial="emulator-test", tile_id=26, tile_label="t6",
        tap=[1054, 830], box=GOOD_BOXES[10], before_count=11,
        hand_ids=GOOD_IDS, hud_key=HUD_BYTES.hex(), caishen="bai",
        caishen_id=67, round_seq=1, saved_at=time.time())
    state.update(overrides)
    assert panel_recovery.write_state(path, state)
    return path


def test_loop_recovers_second_tap_after_restart(tmp_path):
    tap = RecoveryTap()
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[good_advise(), panel_raised_result()],
        frames=[NORMAL_PNG, PANEL_PNG])
    path = write_loop_state(loop, tmp_path)

    first = loop.tick()          # 面板前 advise：只存快照，不出手
    assert first["event"] == "idle"
    second = loop.tick()         # 面板帧 + 状态文件 → 恢复第二击
    assert second["event"] == "action"
    assert second["verified"] is True
    assert second["panel_recovery"]["outcome"] == "recovered"
    assert second["panel_recovery"]["case"] == "in_roi_lifted"
    assert len(tap.recovery_calls) == 1
    assert tap.execute_calls == []          # 没走普通出牌/摸切兜底
    assert loop.actions_done == 1
    assert loop.stopped is False
    assert not path.exists()                # 恢复成功 → 状态清除


def test_loop_identity_mismatch_clears_state_and_falls_back(tmp_path):
    tap = RecoveryTap()
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[good_advise(), panel_abstain()],
        frames=[NORMAL_PNG, PANEL_PNG])
    path = write_loop_state(loop, tmp_path, caishen="dong")

    loop.tick()
    event = loop.tick()
    assert event["event"] == "idle"
    assert event["panel_recovery"]["outcome"] == "abstain"
    assert event["panel_recovery"]["state_cleared"] is True
    # 失效状态清除后回落原兜底（本帧 streak 不足，只等待）。
    assert event["hint_panel"]["outcome"] == "waiting_streak"
    assert tap.recovery_calls == []
    assert not path.exists()
    assert loop.stopped is False


def test_loop_not_unique_raised_waits_and_blocks_fallback(tmp_path):
    tap = RecoveryTap()
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[good_advise(), panel_raised_result(second_raised=True)],
        frames=[NORMAL_PNG, PANEL_PNG])
    path = write_loop_state(loop, tmp_path)

    loop.tick()
    event = loop.tick()
    assert event["event"] == "idle"
    assert event["panel_recovery"]["outcome"] == "abstain"
    assert event["panel_recovery"]["reason"].startswith("raised_not_unique")
    assert "hint_panel" not in event        # 摸切兜底没有被放行
    assert tap.recovery_calls == []
    assert tap.execute_calls == []
    assert path.exists()                    # 状态仍像本局待二击，保留
    assert loop.stopped is False


def test_loop_without_state_file_behaves_like_before(tmp_path):
    tap = RecoveryTap()
    loop, tap = make_loop(
        tmp_path, enabled=True, tap=tap,
        analyses=[panel_abstain(), panel_abstain(), panel_abstain()],
        frames=[PANEL_PNG, PANEL_PNG, PANEL_PNG])
    loop._panel_recovery_path = Path(tmp_path) / "pending.json"
    events = [loop.tick() for _ in range(3)]
    assert all(event["event"] == "idle" for event in events)
    assert "panel_recovery" not in events[-1]
    assert events[-1]["hint_panel"]["outcome"] == "no_prior_advise_read"
    assert tap.recovery_calls == []
    assert loop.stopped is False


def test_loop_round_change_clears_pending_state(tmp_path):
    keys = [(HUD_BYTES, "bai"), (b"hud-other-9876543210", "dong"),
            (b"hud-other-9876543210", "dong")]

    def round_keys(_path):
        return keys.pop(0) if len(keys) > 1 else keys[0]

    loop, tap = make_loop(
        tmp_path, enabled=True,
        analyses=[good_advise(), good_advise(), good_advise()],
        frames=[NORMAL_PNG, NORMAL_PNG, NORMAL_PNG],
        round_key_fn=round_keys)
    path = write_loop_state(loop, tmp_path)

    loop.tick()          # 局 A 身份确立
    assert path.exists()
    loop.tick()          # 换局嫌疑：等待确认，状态先留着
    assert path.exists()
    loop.tick()          # 换局确认：状态清除
    assert not path.exists()
    assert tap.execute_calls == []
