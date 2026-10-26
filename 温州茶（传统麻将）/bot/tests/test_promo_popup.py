# -*- coding: utf-8 -*-
"""促销弹窗「限时优惠」关闭按钮识别（promo_popup）的离线单元测试。

正/负样本全部取自 BOSS 指认的实机探针帧（1600x900）：

* 正样本 emulator-5556 弹窗帧 ``p1_5556.png`` —— 关闭按钮在 (1466,190)；
* 负样本 ``p1_5558.png``（另一台无弹窗）、``now_5556.png``（大厅）、
  ``t_5556.png``（牌桌）—— 都不许给出落点。

只读红线：本文件不跑 adb、不点设备、不改 ``promo_popup.py``。读中文路径
图片一律走 ``cv2.imdecode(np.fromfile(...))``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import tap_executor  # noqa: E402
from promo_popup import (  # noqa: E402
    SCORE_MIN,
    PromoPopupState,
    read_promo_popup,
)
from tap_executor import (  # noqa: E402
    TapAborted,
    TapExecutor,
    TapRejected,
    TapUnverified,
)

#: 实机探针帧目录（``<仓库根>/_takeover/probe``）。
PROBE_DIR = BOT_DIR.parent.parent / "_takeover" / "probe"

#: BOSS 原图 + 实机落点：1600x900 下关闭按钮中心实测 (1466,190)。
EXPECTED_CENTER = (1466, 190)
CENTER_TOLERANCE = 3


def load_frame(name: str) -> np.ndarray:
    """按中文安全的方式读一张探针帧；读不出就跳过（不伪造样本）。"""
    path = PROBE_DIR / name
    if not path.is_file():
        pytest.skip(f"缺少实机探针帧：{path}")
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    if image is None:
        pytest.skip(f"探针帧不是有效 PNG：{path}")
    return image


# ----------------------------------------------------------------------
# 1. 正样本：弹窗帧必须给出落点，且落在实测位置
# ----------------------------------------------------------------------
def test_popup_frame_publishes_close_center_at_measured_spot():
    state = read_promo_popup(load_frame("p1_5556.png"))

    assert state.present is True
    assert state.struct_ok is True
    assert state.fingerprint_ok is True
    assert state.close_center is not None, state.reason
    assert state.close_box is not None and len(state.close_box) == 4
    assert state.score >= SCORE_MIN
    assert abs(state.close_center[0] - EXPECTED_CENTER[0]) <= CENTER_TOLERANCE
    assert abs(state.close_center[1] - EXPECTED_CENTER[1]) <= CENTER_TOLERANCE
    # 落点必须在按钮框内，且离边有余量（压边的点不算落在按钮上）。
    x0, y0, x1, y1 = state.close_box
    assert x0 + 2 <= state.close_center[0] <= x1 - 2
    assert y0 + 2 <= state.close_center[1] <= y1 - 2


def test_popup_frame_ledger_is_json_serializable():
    state = read_promo_popup(load_frame("p1_5556.png"))
    ledger = state.ledger()

    assert json.loads(json.dumps(ledger)) == ledger
    assert ledger["present"] is True
    assert ledger["fingerprint_ok"] is True
    assert ledger["close_center"] == list(state.close_center)
    assert ledger["close_box"] == list(state.close_box)


# ----------------------------------------------------------------------
# 2. 负样本：大厅 / 牌桌 / 另一台无弹窗帧一律不给落点
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, note",
    [
        ("p1_5558.png", "emulator-5558 同刻无弹窗帧"),
        ("now_5556.png", "emulator-5556 大厅帧"),
        ("t_5556.png", "emulator-5556 牌桌帧"),
    ],
)
def test_non_popup_frames_never_publish_a_close_point(name, note):
    state = read_promo_popup(load_frame(name))

    assert state.present is False, f"{note} 误报弹窗：{state.reason}"
    assert state.close_center is None
    assert state.close_box is None


# ----------------------------------------------------------------------
# 3. 坏输入：不抛异常、不给落点（fail-closed）
# ----------------------------------------------------------------------
def test_none_frame_is_rejected_without_raising():
    state = read_promo_popup(None)

    assert state.present is False
    assert state.close_center is None
    assert state.reason


def test_float32_frame_is_rejected_without_raising():
    image = np.zeros((900, 1600, 3), dtype=np.float32)
    state = read_promo_popup(image)

    assert state.present is False
    assert state.close_center is None


def test_small_frame_is_rejected_without_raising():
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    state = read_promo_popup(image)

    assert state.present is False
    assert state.close_center is None


def test_grayscale_frame_is_rejected_without_raising():
    image = np.zeros((900, 1600), dtype=np.uint8)
    state = read_promo_popup(image)

    assert state.present is False
    assert state.close_center is None


def test_resized_desktop_frame_is_not_treated_as_native_game_frame():
    image = cv2.resize(load_frame("p1_5556.png"), (1280, 720))

    state = read_promo_popup(image)

    assert state.present is False
    assert state.close_center is None
    assert "1600×900" in state.reason


def test_close_x_without_full_promo_panel_never_publishes_a_tap():
    popup = load_frame("p1_5556.png")
    table = load_frame("t_5556.png").copy()
    table[130:250, 1380:1540] = popup[130:250, 1380:1540]

    state = read_promo_popup(table)

    assert state.present is False
    assert state.close_center is None
    assert state.fingerprint_ok is False


def test_full_panel_with_unreadable_x_still_consumes_frame_without_click():
    image = load_frame("p1_5556.png").copy()
    image[120:260, 1380:1540] = 0

    state = read_promo_popup(image)

    assert state.present is True
    assert state.fingerprint_ok is True
    assert state.close_center is None
    assert "关闭 X" in state.reason


# ----------------------------------------------------------------------
# 4. 状态对象自身的契约
# ----------------------------------------------------------------------
def test_default_state_abstains_and_serializes():
    state = PromoPopupState(present=False, reason="测试")

    assert state.close_center is None
    assert state.close_box is None
    assert json.loads(json.dumps(state.ledger()))["present"] is False


# ----------------------------------------------------------------------
# 5. 执行器闭环：真弹窗帧 → 点 ❌ → 连续两帧不再命中才算出关掉
#
# 全程离线：截图、点击、前台校验全换成假件，adb 一次都不碰。
# ----------------------------------------------------------------------
class _FakeTime:
    """把执行器里的 sleep 换成空操作（模块级替换，随 monkeypatch 回滚）。"""

    @staticmethod
    def sleep(_seconds):
        return None


def make_executor(monkeypatch, tmp_path, frames, taps):
    """一个「真点」执行器：截图按 frames 队列出，点击记进 taps。"""
    executor = TapExecutor(enabled=True, adb_path=sys.executable,
                           serial="emulator-test",
                           session_dir=Path(tmp_path) / "session")
    monkeypatch.setattr(tap_executor, "time", _FakeTime)
    monkeypatch.setattr(executor, "_require_game_foreground", lambda: None)

    def screenshot():
        if not frames:
            raise AssertionError("假截图已用尽")
        return frames.pop(0) if len(frames) > 1 else frames[0]

    monkeypatch.setattr(executor, "_screenshot", screenshot)
    monkeypatch.setattr(executor, "_tap", lambda x, y: taps.append((x, y)))
    return executor


def test_execute_close_popup_needs_two_clear_frames(tmp_path, monkeypatch):
    """弹窗帧 → 点 ❌ → 连续两帧不再命中弹窗才算关掉。"""
    frames = [load_frame("p1_5556.png"), load_frame("now_5556.png")]
    taps: list[tuple[int, int]] = []
    executor = make_executor(monkeypatch, tmp_path, frames, taps)

    outcome = executor.execute_close_popup(EXPECTED_CENTER,
                                           session_dir=tmp_path)

    assert outcome["executed"] is True and outcome["dry_run"] is False
    assert outcome["stage"] == "verified" and outcome["verified"] is True
    assert outcome["action"] == "close_promo_popup"
    assert outcome["tap"] == list(EXPECTED_CENTER)
    assert outcome["promo_popup_after"]["present"] is False
    assert taps == [EXPECTED_CENTER]              # 只点一下，绝不补点


def test_execute_close_popup_raises_when_popup_never_clears(tmp_path,
                                                            monkeypatch):
    """点了弹窗还在 → TapUnverified，且绝不补第二下。"""
    frames = [load_frame("p1_5556.png")]
    taps: list[tuple[int, int]] = []
    executor = make_executor(monkeypatch, tmp_path, frames, taps)

    with pytest.raises(TapUnverified):
        executor.execute_close_popup(EXPECTED_CENTER, session_dir=tmp_path,
                                     verify_frames=3)

    assert taps == [EXPECTED_CENTER]              # 一次都没多点


def test_execute_close_popup_evidence_declares_channel_isolation(
        tmp_path, monkeypatch):
    """证据里必须白纸黑字写明这条通道与出牌通道隔离。"""
    frames = [load_frame("p1_5556.png"), load_frame("now_5556.png")]
    executor = make_executor(monkeypatch, tmp_path, frames, [])

    executor.execute_close_popup(EXPECTED_CENTER, session_dir=tmp_path)

    # session_dir 是本次调用显式给的 tmp_path，证据落在它下面的 tap_probe/。
    probes = sorted((Path(tmp_path) / "tap_probe").glob("*/evidence.json"))
    assert probes, "真点路径必须落盘证据"
    payload = json.loads(probes[-1].read_text(encoding="utf-8"))
    claim = payload["claim"]
    assert claim["channel"] == "promo_popup"
    assert claim["discard_channel_reused"] is False
    assert claim["action"] == "close_promo_popup"
    assert claim["control"] == "promo_close_x"
    assert claim["promo_popup_before"]["present"] is True
    assert claim["promo_popup_after"]["present"] is False
    # 失败也要先落盘再抛：证据目录里同样得有东西
    assert payload["outcome"] == "verified"


def test_close_popup_dry_run_needs_an_image(tmp_path):
    executor = TapExecutor(enabled=False, adb_path=sys.executable,
                           serial="emulator-test")

    with pytest.raises(TapAborted):
        executor.execute_close_popup(session_dir=tmp_path)


def test_close_popup_dry_run_plans_without_tapping(tmp_path):
    executor = TapExecutor(enabled=False, adb_path=sys.executable,
                           serial="emulator-test")

    plan = executor.execute_close_popup(image=load_frame("p1_5556.png"),
                                        session_dir=tmp_path)

    assert plan["executed"] is False and plan["dry_run"] is True
    assert plan["stage"] == "plan"
    assert plan["tap"] == list(EXPECTED_CENTER)
    assert plan["promo_popup"]["present"] is True


def test_close_popup_rejects_drifted_point(tmp_path):
    """两帧中心漂移超过 12px → 拒绝点击（旧落点不作数）。"""
    executor = TapExecutor(enabled=False, adb_path=sys.executable,
                           serial="emulator-test")

    with pytest.raises(TapRejected):
        executor.execute_close_popup((EXPECTED_CENTER[0] + 20,
                                      EXPECTED_CENTER[1]),
                                     image=load_frame("p1_5556.png"),
                                     session_dir=tmp_path)


def test_close_popup_refuses_frames_without_a_popup(tmp_path):
    """没有实测到弹窗 → 拒绝点击（不管给的坐标长什么样）。"""
    executor = TapExecutor(enabled=False, adb_path=sys.executable,
                           serial="emulator-test")

    with pytest.raises(TapRejected):
        executor.execute_close_popup(EXPECTED_CENTER,
                                     image=load_frame("now_5556.png"),
                                     session_dir=tmp_path)


# ----------------------------------------------------------------------
# 6. 优先级交叉验证：促销弹窗在屏时，更早的入桌邀请通道不许误命中
#
# tick() 里入桌邀请分支跑在促销弹窗之前（它在截完图就判，弹窗判定要等
# base 建好）。一旦促销弹窗把画面盖住，read_invitation 就是在被遮挡的
# 像素上跑的——它必须靠"邀请您进入"文案把门，绝不能把促销弹窗认成邀请。
# ----------------------------------------------------------------------
def test_invitation_reader_does_not_fire_on_the_promo_popup_frame():
    """促销弹窗帧上 read_invitation 必须不命中（旧了就会点在弹窗上）。"""
    from invite_dialog import read_invitation

    image = load_frame("p1_5556.png")
    assert read_promo_popup(image).present is True   # 先确认这帧真的是弹窗帧

    invitation = read_invitation(image)

    assert invitation.present is False
    assert invitation.complete is False
    assert invitation.cancel_center is None


@pytest.mark.parametrize("name", ["p1_5558.png", "now_5556.png", "t_5556.png"])
def test_invitation_reader_stays_quiet_on_other_probe_frames(name):
    """大厅/牌桌/另一台帧同样不许误报邀请（保持这条通道的既有口径）。"""
    from invite_dialog import read_invitation

    invitation = read_invitation(load_frame(name))

    assert invitation.present is False
    assert invitation.cancel_center is None


# ----------------------------------------------------------------------
# 7. 金丝雀：弹窗帧离"被判结算/模态"只差一个信号
#
# _promo_dismiss_event 排在 _evaluate_turn_gate 之前，就是因为门禁在这类
# 帧上求值有两个后果：判 blocked 会 turn_history.clear()，且 blocked 分支
# 直接 _stop("gate_turn") 把整场打停。这条不测 promo，而是把"差一个信号"
# 这个事实钉住：一旦它变红，说明弹窗帧已经被判成结算/模态，此时若顺序
# 被改回去，一遇弹窗就会立刻停手。
# ----------------------------------------------------------------------
def test_popup_frame_is_not_judged_settled_today():
    from turn_gate import detect_settled_screen

    settled, detail = detect_settled_screen(load_frame("p1_5556.png"))

    assert settled is False
    # 弹窗的中央面板已经算进"结算"信号，只是还没凑够数量——所以它不能再
    # 靠近一步。这里只钉"已命中 1 个"，不钉阈值本身（阈值是门禁自己的口径）。
    assert "central_panel" in detail["settled_signals"]
    assert len(detail["settled_signals"]) < detail["settled_required_signals"]
