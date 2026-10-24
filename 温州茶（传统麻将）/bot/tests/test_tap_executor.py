"""Offline tests for tap_executor: dry-run default, whitelist, re-screenshot,
post-tap verification. No real device is touched — adb is always faked."""

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

from assist_tea import analyze_snapshot  # noqa: E402
from tap_executor import (  # noqa: E402
    TapAborted,
    TapExecutor,
    TapRejected,
    TapUnverified,
)

FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
         / "live" / "tea_2p" / "20261023_205409_557426_31fea1.png")


@pytest.fixture(scope="module")
def analysis(tmp_path_factory) -> dict:
    """One real advise result, computed once for the whole module."""
    logs = tmp_path_factory.mktemp("tap_logs")
    return analyze_snapshot(
        FRAME, caishen_label="", confirmed_two_player=True, log_dir=logs,
    )


def frame_png() -> bytes:
    return FRAME.read_bytes()


def erase_tile(png_bytes: bytes, box) -> bytes:
    """Return a PNG where the given hand tile is erased (table background)."""
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, y1 = (int(value) for value in box)
    image[y0:y1, x0:x1] = (20, 60, 30)  # dark table, outside the tile HSV mask
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def hint_panel_after_select(png_bytes: bytes, box) -> bytes:
    """模拟实机：已选牌抬出常规 ROI，中央同时出现胡提示面板。"""
    image = cv2.imdecode(
        np.frombuffer(erase_tile(png_bytes, box), dtype=np.uint8),
        cv2.IMREAD_COLOR).copy()
    image[470:680, 300:1300] = (200, 230, 240)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


RAISE_LIFT_PX = 25


def raise_tile(png_bytes: bytes, box) -> bytes:
    """Return a PNG where the given hand tile is lifted up like the game does.

    已用真帧探针验证（2026-10-27）：上移 25px 后 count_tiles 仍数出 17 张、
    recognize_hand 仍完整、该框 y1=867 ≤ 中位数 892 - 12，"几何抬起"判据成立。
    """
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, y1 = (int(value) for value in box)
    image[y0 - RAISE_LIFT_PX:y1 - RAISE_LIFT_PX, x0:x1] = image[y0:y1, x0:x1]
    image[y1 - RAISE_LIFT_PX:y1, x0:x1] = (20, 60, 30)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def confirmed_frame(selected_png: bytes, box) -> bytes:
    """Keep the hand count reduced while making the raised target disappear."""
    image = cv2.imdecode(
        np.frombuffer(selected_png, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, _y1 = (int(value) for value in box)
    # Simulate the local table/discard animation above the selected column.
    image[max(0, y0 - 70):max(0, y0 - 35), x0:x1] = (0, 0, 220)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


class FakeAdb:
    """Record every command; answer dumpsys / screencap / tap offline."""

    def __init__(self, png_frames: list[bytes]):
        self.frames = list(png_frames)
        self.commands: list[list[str]] = []
        self.creationflags: list[int] = []

    def __call__(self, command, capture_output=False, timeout=None, check=False,
                 creationflags=0, **kwargs):
        # 环境 safe-delete/编码 shim 会带 text=True 等额外参数；一律忽略，
        # stdout 仍按字节返回（tap_executor 只解码字节）。
        parts = [str(part) for part in command]
        # 只严格校验本测试注入的 adb 路径；环境工具（safe-delete guard 等）
        # 的 subprocess 调用原样放行，避免与测试无关的清理流程互相干扰。
        if not parts or parts[0] != "fake-adb":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        self.creationflags.append(creationflags)
        self.commands.append(parts)
        joined = " ".join(parts)
        if "dumpsys" in joined:
            stdout = (b"topResumedActivity=ActivityRecord{1 u0 "
                      b"com.xm.zjgamecenter/com.xm.main.MainActivity}\n")
            return subprocess.CompletedProcess(command, 0, stdout, b"")
        if "screencap" in joined:
            assert self.frames, "unexpected extra screencap"
            return subprocess.CompletedProcess(command, 0, self.frames.pop(0), b"")
        if "input" in joined and "tap" in joined:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(f"unexpected adb command: {joined}")

    def tap_commands(self) -> list[list[str]]:
        return [cmd for cmd in self.commands
                if "input" in cmd and "tap" in cmd]


def test_dry_run_default_produces_plan_without_any_adb_call(analysis, monkeypatch):
    """默认 enabled=False：只算坐标，一个 adb 命令都不发。"""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry_run 不允许任何 adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setenv("TAP_EXECUTOR_ENABLED", "1")  # 环境变量必须打不开真点

    executor = TapExecutor()
    assert executor.enabled is False
    outcome = executor.execute(analysis)
    assert outcome["executed"] is False
    assert outcome["dry_run"] is True
    expected = [int(v) for v in analysis["suggested_boxes"][0]]
    assert outcome["tap"] == [(expected[0] + expected[2]) // 2,
                              (expected[1] + expected[3]) // 2]
    assert outcome["tile"] in {"t4", analysis["suggestion"]}  # vision label 或中文名
    assert outcome["tile_id"] == analysis["suggestion_id"]

    with pytest.raises(TypeError):
        TapExecutor(enabled="yes")  # 只有显式布尔实参才被接受


def test_enabled_taps_center_of_hand_box_after_fresh_screenshot(analysis, monkeypatch):
    """enabled=True：第一击抬牌（牌仍在、张数守恒、几何抬起），第二击才出牌。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    # 审计 B 之后的正确时序：第一击后牌是"抬起"（还在手里），不是消失。
    fake = FakeAdb([png, raise_tile(png, box), erase_tile(png, box)])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0,
                           fresh_retry_attempts=1)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is True and outcome["dry_run"] is False
    assert outcome["stage"] == "verified"
    assert outcome["before_count"] - outcome["after_count"] == 1

    taps = fake.tap_commands()
    assert len(taps) == 2
    command = taps[0]
    assert command[0] == "fake-adb"
    assert command[-4:-2] == ["input", "tap"]
    assert command[-2] == str((box[0] + box[2]) // 2)
    assert command[-1] == str((box[1] + box[3]) // 2)
    assert taps[1][-2:] == command[-2:]  # 第二次点同一牌列确认出牌
    assert ["-s", "emulator-test"] == command[1:3]
    assert all(flag == getattr(subprocess, "CREATE_NO_WINDOW", 0)
               for flag in fake.creationflags)
    # 点击前必须重新截屏（旧 suggested_boxes 不能直接点）。
    screencaps = [cmd for cmd in fake.commands if "screencap" in cmd]
    assert len(screencaps) == 3


def test_enabled_aborts_with_zero_taps_when_suggestion_missing_on_fresh_frame(
        analysis, monkeypatch):
    """建议牌在新截图上已不在：必须中止，零点击。"""
    png = frame_png()
    fresh = erase_tile(png, analysis["suggested_boxes"][0])
    fake = FakeAdb([fresh])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0,
                           fresh_retry_attempts=1)
    with pytest.raises(TapAborted, match="新截图无可执行建议|新截图识牌不完整"):
        executor.execute(analysis)
    assert fake.tap_commands() == []


def test_preclick_single_bad_frame_retries_then_executes(
        analysis, monkeypatch, tmp_path):
    """首帧坏读不杀循环：第二帧同建议通过后才允许发送点击。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    fake = FakeAdb([png, png, raise_tile(png, box), erase_tile(png, box)])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.fresh_retry_interval = 0
    calls = {"count": 0}

    def validate(_image, original):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TapAborted("模拟单帧坏读")
        return dict(original)

    outcome = executor.execute(analysis, fresh_validator=validate)
    assert outcome["executed"] is True
    assert calls["count"] == 2
    assert len(fake.tap_commands()) == 2
    evidence = _read_evidence(tmp_path)
    assert [row["ok"] for row in evidence["preclick_attempts"]] == [False, True]


def test_fresh_check_rejects_changed_hand_even_if_target_tile_is_still_present(
        analysis):
    image = cv2.imdecode(np.frombuffer(frame_png(), dtype=np.uint8), cv2.IMREAD_COLOR)
    stale = dict(analysis)
    stale["hand_ids"] = list(analysis["hand_ids"])
    stale["hand_ids"][0] = 64 if stale["hand_ids"][0] != 64 else 63
    with pytest.raises(TapAborted, match="牌面、财神或建议已变化"):
        TapExecutor()._reanalyze_fresh(image, stale)


def test_target_outside_hand_roi_is_rejected():
    """几何白名单兜底：充值/开始游戏/游戏中心区域与牌框外的点全部拒绝。"""
    executor = TapExecutor()
    hand_boxes = [[6, 768, 95, 892], [1298, 768, 1386, 892]]
    shape = (900, 1600, 3)
    # 充值横幅（顶部居中）、游戏中心角标（右上）、开始游戏大按钮（中屏）。
    dangerous = {
        (800, 120): "充值按钮",
        (1480, 60): "游戏中心入口",
        (800, 430): "开始游戏按钮",
    }
    for point, why in dangerous.items():
        with pytest.raises(TapRejected):
            executor.validate_target(point, hand_boxes, shape), why
    # 在手牌 ROI 的 y 范围内、但不在任何识别出的手牌框内：同样拒绝。
    with pytest.raises(TapRejected):
        executor.validate_target((60, 730), hand_boxes, shape)
    # 合法点：手牌框内部。
    x, y = executor.validate_target((50, 830), hand_boxes, shape)
    assert (x, y) == (50, 830)
    # 超出画面范围的坐标也要拒绝。
    with pytest.raises(TapRejected):
        executor.validate_target((1700, 800), hand_boxes, shape)


def test_post_tap_verification_fails_closed_without_retry(analysis, monkeypatch):
    """第一次点击没有抬牌：报错且不允许发送第二次确认点击。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    # 第一次点击后画面完全不变，不能发送第二次点击。
    fake = FakeAdb([png, png])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0)
    with pytest.raises(TapUnverified):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 1


def test_second_tap_must_reduce_hand_count_by_one(analysis, monkeypatch):
    """第二击没生效（手牌没少）：判失败。审计 B 后张数复核是第一判据。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    raised = raise_tile(png, box)
    fake = FakeAdb([png, raised, raised])  # 第二击后画面仍是抬牌态
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", select_delay=0,
                           verify_delay=0)
    with pytest.raises(TapUnverified, match="确认出牌后手牌数量"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 2


def test_first_click_that_already_discards_forbids_second_tap(
        analysis, monkeypatch):
    """审计 B 核心：第一击后手牌已少一张 → 判定已出牌，**禁止第二击**。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    fake = FakeAdb([png, erase_tile(png, box)])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is True
    assert outcome["stage"] == "discarded_first_click"
    assert outcome["after_count"] == outcome["before_count"] - 1
    assert "禁止第二击" in outcome["reason"]
    taps = fake.tap_commands()
    assert len(taps) == 1, "第一击已出牌时绝不允许发第二击"
    assert len([c for c in fake.commands if "screencap" in c]) == 2


def test_hint_panel_false_minus_one_still_sends_confirming_second_tap(
        analysis, monkeypatch):
    """实机回归：胡提示面板把抬起牌漏出 ROI，不能误判成已出牌。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    selected = hint_panel_after_select(png, box)
    after = confirmed_frame(erase_tile(png, box), box)
    fake = FakeAdb([png, selected, after])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", select_delay=0,
                           verify_delay=0, fresh_retry_attempts=1)

    outcome = executor.execute(analysis)

    assert outcome["executed"] is True
    assert outcome["stage"] == "verified"
    assert len(fake.tap_commands()) == 2
    assert fake.tap_commands()[0][-2:] == fake.tap_commands()[1][-2:]


def test_second_click_forbidden_when_target_not_raised(analysis, monkeypatch):
    """审计 B：第一击后张数守恒但牌没抬起（只有无关动画）→ 禁止第二击。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    # confirmed_frame 只画一条无关的红色动画条，牌既没少也没抬起。
    fake = FakeAdb([png, confirmed_frame(png, box)])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0)
    with pytest.raises(TapUnverified, match="牌没有抬起"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 1, "牌没抬起就绝不许发第二击"


# ----------------------------------------------------------------------
# 证据落盘（tap_probe）：只读仪表化，不改变任何判定与控制流
# ----------------------------------------------------------------------
def _latest_probe(session_dir: Path) -> Path:
    """Return the newest ``tap_probe/<timestamp>`` directory."""
    root = session_dir / "tap_probe"
    assert root.is_dir(), "tap_probe 目录未创建"
    dirs = sorted(root.iterdir())
    assert dirs, "tap_probe 下没有任何执行目录"
    return dirs[-1]


def _read_evidence(session_dir: Path) -> dict:
    return json.loads(
        (_latest_probe(session_dir) / "evidence.json").read_text(encoding="utf-8"))


def _executor(monkeypatch, fake, session_dir, **kwargs) -> TapExecutor:
    monkeypatch.setattr(subprocess, "run", fake)
    return TapExecutor(enabled=True, adb_path="fake-adb",
                       serial="emulator-test", select_delay=0,
                       verify_delay=0, session_dir=session_dir, **kwargs)


def test_second_tap_failure_dumps_before_selected_after_and_metrics(
        analysis, monkeypatch, tmp_path):
    """第二击后手牌没少：抛 TapUnverified，证据齐全，且两击间复核已留证。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    raised = raise_tile(png, box)
    fake = FakeAdb([png, raised, raised])
    executor = _executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapUnverified, match="确认出牌后手牌数量"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 2

    probe = _latest_probe(tmp_path)
    for name in ("before.png", "selected.png", "after.png",
                 "target_column.png", "evidence.json"):
        assert (probe / name).is_file(), f"缺少证据文件 {name}"

    data = _read_evidence(tmp_path)
    assert data["outcome"] == "TapUnverified"
    assert data["second_click_metrics"] is None  # 张数复核先一步判死
    assert "确认出牌后手牌数量" in data["reason"]
    assert data["first_click_metrics"]["changed"] is True
    assert data["tap"] == [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
    assert data["tile_id"] == analysis["suggestion_id"]
    # 两个 action_guard 必须是原始返回值：吃/碰/杠/胡 圆盘有没有出现，
    # 决定了这一帧在代点之前是不是就已经注定被门禁弃权。
    for key in ("action_guard_before", "action_guard_afterselect"):
        guard = data[key]
        assert isinstance(guard, dict), f"{key} 缺失"
        assert "blocks_discard" in guard and "reason" in guard and "boxes" in guard
        assert isinstance(guard["blocks_discard"], bool)
        assert isinstance(guard["boxes"], list)
    assert "before_count_source" in data
    assert "第一次点击之前" in data["before_count_source"]
    # 两击之间的手牌复核（审计 B）：张数、目标框、抬起判据全部留证。
    gate = data["selected_gate"]
    assert isinstance(gate, dict)
    assert gate["count"] == data["before_count"]  # 张数守恒才放的第二击
    assert gate["ready"] is True and gate["lifted"] is True
    assert gate["discarded"] is False
    assert gate["median_y1"] == 892.0
    assert data["raised_candidates"]["lift_px"] == 12
    assert any(item["raised"] for item in data["raised_candidates"]["boxes"])
    assert isinstance(data["hand_boxes_selected"], list)
    assert isinstance(data["hand_boxes_after"], list)


def test_verified_tap_dumps_evidence_with_matching_counts(
        analysis, monkeypatch, tmp_path):
    """正常成功路径：第一击抬牌 → 第二击出牌，outcome == verified。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    fake = FakeAdb([png, raise_tile(png, box), erase_tile(png, box)])
    executor = _executor(monkeypatch, fake, tmp_path)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is True and outcome["stage"] == "verified"
    assert outcome["before_count"] - outcome["after_count"] == 1

    probe = _latest_probe(tmp_path)
    for name in ("before.png", "selected.png", "after.png",
                 "target_column.png", "evidence.json"):
        assert (probe / name).is_file(), f"缺少证据文件 {name}"
    data = _read_evidence(tmp_path)
    assert data["outcome"] == "verified"
    assert data["after_count"] == data["before_count"] - 1
    assert data["second_click_metrics"]["changed"] is True


def test_abort_before_first_tap_dumps_only_before_frame(
        analysis, monkeypatch, tmp_path):
    """第一次点击前就失败：只落 before.png + evidence.json，异常类型不变。"""
    png = frame_png()
    stale = dict(analysis)
    stale["hand_ids"] = list(analysis["hand_ids"])
    stale["hand_ids"][0] = 64 if stale["hand_ids"][0] != 64 else 63
    fake = FakeAdb([png])
    executor = _executor(monkeypatch, fake, tmp_path)
    executor.fresh_retry_attempts = 1
    with pytest.raises(TapAborted, match="牌面、财神或建议已变化"):
        executor.execute(stale)
    assert fake.tap_commands() == []  # 零点击

    probe = _latest_probe(tmp_path)
    assert (probe / "before.png").is_file()
    assert not (probe / "selected.png").exists()
    assert not (probe / "after.png").exists()
    assert not (probe / "target_column.png").exists()
    data = _read_evidence(tmp_path)
    assert data["frames"]["before"] == "before.png"
    assert data["frames"]["selected"] is None
    assert data["frames"]["after"] is None
    assert data["outcome"] == "TapPrecheckRetryExhausted"
    assert data["after_count"] is None


def test_dry_run_never_creates_a_probe_directory(analysis, monkeypatch, tmp_path):
    """dry_run 是只读路径：绝不产生任何 tap_probe 目录。"""
    fake = FakeAdb([])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(session_dir=tmp_path)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is False and outcome["dry_run"] is True
    assert fake.commands == []
    assert not (tmp_path / "tap_probe").exists()


# ----------------------------------------------------------------------
# 行动行点击通道（execute_claim）：碰/吃/杠/胡/过 在 y≈600，落在出牌白名单
# （y≥720）之外，所以走独立白名单。出牌白名单本身一点都不放宽。
# ----------------------------------------------------------------------

CLAIM_BG = (20, 60, 30)
GOLD_CENTER = (1431, 610)   # 实测金盘连通域框 [1372,550,1491,671]
PASS_CENTER = (1033, 604)   # 实测过盘连通域框 [979,549,1088,660]
CANCEL_PANEL_CENTER = (1300, 380)
GOLD_BOX = (1372, 550, 1491, 671)


def _blank_frame() -> np.ndarray:
    """1600x900 空桌面：背景色落在所有圆盘 HSV 掩膜之外。"""
    image = np.zeros((900, 1600, 3), np.uint8)
    image[:] = CLAIM_BG
    return image


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def claim_frame(*, gold: bool = True, blue: bool = False,
                second_gold_at=None, blue_center=PASS_CENTER) -> bytes:
    """合成一帧带行动圆盘的画面（颜色/几何照 claim_vision 实测口径）。"""
    image = _blank_frame()
    if gold:
        cv2.circle(image, GOLD_CENTER, 60, (0, 200, 255), -1)
    if second_gold_at is not None:
        cv2.circle(image, second_gold_at, 60, (0, 200, 255), -1)
    if blue:
        cv2.circle(image, blue_center, 55, (255, 170, 0), -1)
    return _png(image)


def _claim_executor(monkeypatch, fake, session_dir, **kwargs) -> TapExecutor:
    monkeypatch.setattr(subprocess, "run", fake)
    return TapExecutor(enabled=True, adb_path="fake-adb",
                       serial="emulator-test", select_delay=0,
                       verify_delay=0, session_dir=session_dir, **kwargs)


def test_claim_tap_on_measured_disc_sends_input_tap_with_exact_coordinates(
        monkeypatch, tmp_path):
    """合法行动圆盘坐标：真的发出 input tap，坐标就是实测圆盘框内的那一点。"""
    before_png = claim_frame()
    after_png = _png(_blank_frame())  # 点了之后行动行收起
    fake = FakeAdb([before_png, after_png])
    executor = _claim_executor(monkeypatch, fake, tmp_path)

    outcome = executor.execute_claim(GOLD_CENTER, action="peng")

    assert outcome["executed"] is True and outcome["dry_run"] is False
    # 审计 A：本模块只证明"点击送达且 UI 有响应"，不证明动作语义成功。
    assert outcome["stage"] == "clicked"
    assert outcome["ui_responded"] is True
    assert outcome["semantic_verified"] is False
    assert outcome["semantic_verifier"] == "action_policy.verify_after_click"
    assert outcome["action"] == "peng" and outcome["kind"] == "gold"
    assert outcome["tap"] == list(GOLD_CENTER)
    assert outcome["box"] == list(GOLD_BOX)
    taps = fake.tap_commands()
    assert len(taps) == 1
    assert taps[0][-4:-2] == ["input", "tap"]
    assert taps[0][-2:] == [str(GOLD_CENTER[0]), str(GOLD_CENTER[1])]
    assert taps[0][0] == "fake-adb" and taps[0][1:3] == ["-s", "emulator-test"]
    # 点击前后各一次截图：绝不拿旧帧的圆盘去点。
    assert len([cmd for cmd in fake.commands if "screencap" in cmd]) == 2
    assert outcome["claim_buttons_before"] == 1
    assert outcome["claim_buttons_after"] == 0
    assert outcome["click_metrics"]["changed"] is True

    data = _read_evidence(tmp_path)
    assert data["outcome"] == "clicked"
    assert data["tap"] == list(GOLD_CENTER)
    claim = data["claim"]
    assert claim["action"] == "peng" and claim["tapped_kind"] == "gold"
    assert claim["tapped_center"] == list(GOLD_CENTER)
    assert len(claim["buttons_before"]) == 1
    assert claim["buttons_after"] == []
    assert claim["roi"] == {"x": [740, 1520], "y": [560, 660]}
    assert "洗牌" in claim["action_blacklist"] and "切牌" in claim["action_blacklist"]
    assert "cancel" in claim["kind_blacklist"]


def test_claim_tap_rejects_dangerous_points_without_any_command(
        monkeypatch, tmp_path):
    """充值 / 游戏中心 / 开始游戏：一个 adb 命令都不发（连截图都不发）。"""
    fake = FakeAdb([claim_frame(), claim_frame()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    dangerous = {
        (800, 120): "充值按钮（顶部居中）",
        (1480, 60): "游戏中心入口（右上）",
        (800, 430): "开始游戏大按钮（中屏）",
    }
    for point, why in dangerous.items():
        with pytest.raises(TapRejected):
            executor.execute_claim(point, action="peng"), why
    assert fake.commands == []


def test_claim_tap_rejects_point_outside_measured_disc_box(monkeypatch, tmp_path):
    """坐标在行动行 ROI 内、但不在 read_claims 实测框内：拒绝，零点击。"""
    fake = FakeAdb([claim_frame()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    # (760,570) 落在 y∈[560,660]、x∈[740,1520] 里，但离实测金盘框很远。
    with pytest.raises(TapRejected, match="不在 read_claims 实测的圆盘框内"):
        executor.execute_claim((760, 570), action="peng")
    # 槽位推断值（824 是 1 号槽位的 x 中心）没有实测框撑腰时同样拒收；
    # 用 dry_run 走，避免再消耗一次假截图。
    image = cv2.imdecode(
        np.frombuffer(claim_frame(), np.uint8), cv2.IMREAD_COLOR)
    with pytest.raises(TapRejected, match="不在 read_claims 实测的圆盘框内"):
        TapExecutor(session_dir=tmp_path).execute_claim(
            (824, 596), action="peng", image=image)
    assert fake.tap_commands() == []
    # 同一颗圆盘上偏离到框外的点也不放行（贴边/出框都不是稳稳落在盘上）。
    with pytest.raises(TapRejected):
        TapExecutor(session_dir=tmp_path).execute_claim(
            (1560, 610), action="peng", image=image)


def test_claim_tap_rejects_blacklisted_actions_without_any_command(
        monkeypatch, tmp_path):
    """洗牌（房卡×2）/ 切牌（房卡×4）以及充值等：永不放行，零 adb 命令。"""
    fake = FakeAdb([claim_frame(), claim_frame()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    for action in ("洗牌", "切牌", "充值", "开始游戏", "返回大厅",
                   "分享战绩", "游戏中心"):
        with pytest.raises(TapRejected, match="黑名单"):
            executor.execute_claim(GOLD_CENTER, action=action)
    # 动作名都不给也不许点。
    with pytest.raises(TapAborted):
        executor.execute_claim(GOLD_CENTER)
    assert fake.commands == []


def test_claim_tap_fails_and_dumps_evidence_when_nothing_changed(
        monkeypatch, tmp_path):
    """点击后画面没变：判失败、落证据，绝不重试第二下。"""
    same = claim_frame()
    fake = FakeAdb([same, claim_frame()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapUnverified, match="画面未变化"):
        executor.execute_claim(GOLD_CENTER, action="peng")
    assert len(fake.tap_commands()) == 1  # 失败即停，不重发

    probe = _latest_probe(tmp_path)
    for name in ("before.png", "after.png", "target_column.png",
                 "evidence.json"):
        assert (probe / name).is_file(), f"缺少证据文件 {name}"
    data = _read_evidence(tmp_path)
    assert data["outcome"] == "TapUnverified"
    assert data["tap"] == list(GOLD_CENTER)
    assert data["first_click_metrics"]["changed"] is False
    assert data["claim"]["action"] == "peng"


def test_claim_tap_rejects_cancel_disc_and_unreadable_frame(
        monkeypatch, tmp_path):
    """取消盘永不点；读不出圆盘时不点（连"过"都不点）。"""
    fake = FakeAdb([claim_frame(gold=False, blue=True), _png(_blank_frame())])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    # 只有蓝盘且没有金盘 → claim_vision 判为 cancel。
    with pytest.raises(TapRejected, match="取消盘"):
        executor.execute_claim(PASS_CENTER, action="pass")

    empty = FakeAdb([_png(_blank_frame())])
    monkeypatch.setattr(subprocess, "run", empty)
    executor_two = _claim_executor(monkeypatch, empty, tmp_path)
    with pytest.raises(TapRejected, match="没有实测到任何行动圆盘"):
        executor_two.execute_claim(GOLD_CENTER, action="peng")
    assert fake.tap_commands() == []
    assert empty.tap_commands() == []


def test_explicit_selection_cancel_clicks_unique_panel_disc_and_keeps_evidence(
        monkeypatch, tmp_path):
    """用户授权：候选面板唯一“取消”可点；点击前后证据照常落盘。"""
    before = claim_frame(gold=False, blue=True,
                         blue_center=CANCEL_PANEL_CENTER)
    fake = FakeAdb([before, _png(_blank_frame())])
    executor = _claim_executor(monkeypatch, fake, tmp_path)

    outcome = executor.execute_claim(
        CANCEL_PANEL_CENTER, action="cancel_selection", kind="cancel")

    assert outcome["executed"] is True
    assert outcome["kind"] == "cancel"
    assert outcome["ui_responded"] is True
    assert fake.tap_commands() == [[
        "fake-adb", "-s", "emulator-test", "shell", "input", "tap",
        "1300", "380",
    ]]
    probe = _latest_probe(tmp_path)
    assert (probe / "before.png").is_file()
    assert (probe / "after.png").is_file()
    assert _read_evidence(tmp_path)["claim"]["action"] == "cancel_selection"


def test_claim_tap_rejects_kind_mismatch_and_ambiguous_candidates(
        monkeypatch, tmp_path):
    """要"过"却只有金盘 → 拒绝；不给坐标而候选多于一个 → 拒绝（不猜）。"""
    fake = FakeAdb([claim_frame(), claim_frame()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapRejected, match="没有 kind='pass'"):
        executor.execute_claim(GOLD_CENTER, action="pass")

    two_golds = FakeAdb([claim_frame(second_gold_at=(1242, 610))])
    monkeypatch.setattr(subprocess, "run", two_golds)
    executor_two = _claim_executor(monkeypatch, two_golds, tmp_path)
    with pytest.raises(TapRejected, match="无法唯一定位"):
        executor_two.execute_claim(action="peng")
    assert fake.tap_commands() == []
    assert two_golds.tap_commands() == []


def test_claim_dry_run_sends_nothing_and_writes_nothing(monkeypatch, tmp_path):
    """dry_run：只复核圆盘几何，零 adb 调用、零落盘。"""
    fake = FakeAdb([])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(session_dir=tmp_path)
    image = cv2.imdecode(
        np.frombuffer(claim_frame(), np.uint8), cv2.IMREAD_COLOR)
    outcome = executor.execute_claim(GOLD_CENTER, action="peng", image=image)
    assert outcome["executed"] is False and outcome["dry_run"] is True
    assert outcome["stage"] == "plan"
    assert outcome["tap"] == list(GOLD_CENTER)
    assert outcome["kind"] == "gold"
    assert fake.commands == []
    assert not (tmp_path / "tap_probe").exists()
    # dry_run 不给帧就没法复核：fail-closed，绝不退化成盲点。
    with pytest.raises(TapAborted, match="必须显式传 image"):
        executor.execute_claim(GOLD_CENTER, action="peng")


def test_claim_tap_refuses_implicit_session_dir(monkeypatch):
    """行动行入口拒收缺省落盘目录：不许在真实语料库里建假会话。"""
    fake = FakeAdb([])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test")
    with pytest.raises(TapAborted, match="必须显式传 session_dir"):
        executor.execute_claim(GOLD_CENTER, action="peng")
    assert fake.commands == []


def test_claim_tap_rejects_when_disc_still_present_after_tap(
        monkeypatch, tmp_path):
    """审计 A：画面变了但目标圆盘仍在 → 点击没生效，判失败，绝不假通过。

    构造：after 帧圆盘原样保留，只在它的验证区域里画一条无关红色动画条
    （模拟"动画结束/超时取消"那种"画面变了但按钮还在"的假成功）。
    """
    before_png = claim_frame()
    after = cv2.imdecode(
        np.frombuffer(before_png, np.uint8), cv2.IMREAD_COLOR).copy()
    # 条在圆盘验证区域内（y0-90..y1+8）但在圆盘本体之外。
    after[GOLD_BOX[1] - 70:GOLD_BOX[1] - 35, GOLD_BOX[0]:GOLD_BOX[2]] = (0, 0, 220)
    ok, after_png = cv2.imencode(".png", after)
    assert ok
    fake = FakeAdb([before_png, after_png.tobytes()])
    executor = _claim_executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapUnverified, match="圆盘仍实测存在"):
        executor.execute_claim(GOLD_CENTER, action="peng")
    assert len(fake.tap_commands()) == 1  # 失败即停
    data = _read_evidence(tmp_path)
    assert data["outcome"] == "TapUnverified"
    assert data["first_click_metrics"]["changed"] is True  # 画面确实变了
    assert len(data["claim"]["buttons_after"]) == 1        # 但按钮还在


def test_claim_switch_off_means_execute_claim_sends_no_commands(
        monkeypatch, tmp_path):
    """审计要求：`CLAIM_TAP_CHANNEL_READY` 保持 False，开关关闭时零命令。

    这条测试把两件事钉死：开关确实是 False（谁翻上去谁红），且
    ``enabled=False`` 的 execute_claim 是只读路径——一个 adb 命令都不发。
    """
    import action_policy
    assert action_policy.CLAIM_TAP_CHANNEL_READY is True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("开关关闭时不允许任何 adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)
    image = cv2.imdecode(
        np.frombuffer(claim_frame(), np.uint8), cv2.IMREAD_COLOR)
    executor = TapExecutor(session_dir=tmp_path)
    assert executor.enabled is False
    outcome = executor.execute_claim(GOLD_CENTER, action="peng", image=image)
    assert outcome["executed"] is False and outcome["dry_run"] is True
    assert not (tmp_path / "tap_probe").exists()


def test_claim_geometry_whitelist_is_independent_of_hand_whitelist():
    """行动行白名单只放行 y∈[560,660]、x∈[740,1520]；出牌白名单不受影响。"""
    executor = TapExecutor(session_dir="unused")
    for point in ((800, 120), (1480, 60), (800, 430), (1431, 700), (700, 610)):
        with pytest.raises(TapRejected):
            executor.validate_claim_point(point)
    assert executor.validate_claim_point(GOLD_CENTER) == GOLD_CENTER
    # 出牌白名单仍然只认手牌 ROI：行动行的点照样被它拒绝（两道闸互不相干）。
    with pytest.raises(TapRejected):
        executor.validate_target(GOLD_CENTER, [[1372, 550, 1491, 671]],
                                 (900, 1600, 3))
