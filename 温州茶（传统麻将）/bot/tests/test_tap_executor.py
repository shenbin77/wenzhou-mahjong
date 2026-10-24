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
    """enabled=True：同一牌点两次，第二次后才验证真正离手。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    selected = erase_tile(png, box)
    fake = FakeAdb([png, selected, confirmed_frame(selected, box)])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", verify_delay=0)
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
                           serial="emulator-test", verify_delay=0)
    with pytest.raises(TapAborted, match="新截图无可执行建议|新截图识牌不完整"):
        executor.execute(analysis)
    assert fake.tap_commands() == []


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


def test_second_tap_must_make_raised_tile_disappear(analysis, monkeypatch):
    """第二次点击没生效时，即使识别计数已少一，也必须判失败。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    selected = erase_tile(png, box)
    fake = FakeAdb([png, selected, selected])
    monkeypatch.setattr(subprocess, "run", fake)
    executor = TapExecutor(enabled=True, adb_path="fake-adb",
                           serial="emulator-test", select_delay=0,
                           verify_delay=0)
    with pytest.raises(TapUnverified, match="抬起牌仍未消失"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 2


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
    """第二次点击后画面完全不变：抛 TapUnverified，且五样证据齐全。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    selected = erase_tile(png, box)
    fake = FakeAdb([png, selected, selected])
    executor = _executor(monkeypatch, fake, tmp_path)
    with pytest.raises(TapUnverified, match="抬起牌仍未消失"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 2

    probe = _latest_probe(tmp_path)
    for name in ("before.png", "selected.png", "after.png",
                 "target_column.png", "evidence.json"):
        assert (probe / name).is_file(), f"缺少证据文件 {name}"

    data = _read_evidence(tmp_path)
    assert data["outcome"] == "TapUnverified"
    assert data["second_click_metrics"]["changed"] is False
    assert data["reason"] == "第二次点击后抬起牌仍未消失，未证明真正出牌"
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
    # 抬牌诊断：至少给出暗手框列表与“疑似抬起”的中位判据。
    assert isinstance(data["raised_candidates"], dict)
    assert "median_y1" in data["raised_candidates"]
    assert data["raised_candidates"]["lift_px"] == 12
    assert isinstance(data["hand_boxes_selected"], list)
    assert isinstance(data["hand_boxes_after"], list)


def test_verified_tap_dumps_evidence_with_matching_counts(
        analysis, monkeypatch, tmp_path):
    """正常成功路径：outcome == verified，after_count == before_count - 1。"""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    selected = erase_tile(png, box)
    fake = FakeAdb([png, selected, confirmed_frame(selected, box)])
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
    assert data["outcome"] == "TapAborted"
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
                second_gold_at=None) -> bytes:
    """合成一帧带行动圆盘的画面（颜色/几何照 claim_vision 实测口径）。"""
    image = _blank_frame()
    if gold:
        cv2.circle(image, GOLD_CENTER, 60, (0, 200, 255), -1)
    if second_gold_at is not None:
        cv2.circle(image, second_gold_at, 60, (0, 200, 255), -1)
    if blue:
        cv2.circle(image, PASS_CENTER, 55, (255, 170, 0), -1)
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
    assert outcome["stage"] == "verified"
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
    assert data["outcome"] == "verified"
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
