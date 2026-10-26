# -*- coding: utf-8 -*-
"""把「离桌后自动开新一场」这条断链钉死在真帧上（纯离线，绝不点设备）。

## 这份测试在防什么

``lobby_autostart`` 只回答「房间画面上该点哪个空座」，**不点击**。点击是由
``auto_play_loop._lobby_open_event`` 经 ``TapExecutor.execute_open_table``
这条**独立通道**完成的。接错一步的代价是：在别人正在打的真局里建桌、扣
房卡、或者直接点到「充值」/「闭赛」。所以这里逐条钉门禁：

* **G1** 没离过桌（``_left_table=False``）→ 整条通道不许有任何反应；
* **G2** 牌桌内 / 结算 / 弹窗 / 等候室 / 大厅帧 → 一律不出计划；
* **G3** 落点必须 unit==0、在自己空座卡框内、框在桌位带内、不在五个禁区；
* **G4** 点后必须连续两帧不再是「房间且有空座」，否则 ``TapUnverified`` 停手；
* **G5** 同屏最多 2 次点击、两次至少隔 2 帧；
* **G6 房卡门**：房卡一个数都读不出来，因此**只在 join_existing=True（并入
  已有空座、不扣卡）时放行**；两帧正样本都是「桌数 0/10、无桌可并」，所以
  它们在阶段一**必须**是 ``lobby_open_blocked: cards_unreadable`` + 零点击；
* **G7** 开关默认关，关着时只记计划不点；
* **G8** 走 ``execute_open_table`` 独立白名单，台账里显式写
  ``discard_channel_reused=False``，不许借道出牌通道。

## 样本（全部 1600x900 真帧，纯离线）

正样本（房间）：``_takeover/probe/now_5556.png``（房主）、``now_5558.png``（成员）
——两帧右侧图标组完全不同，但菱形/空座卡/加号/功能圆盘坐标一致，计划点都是
upper 加号 (115,267)，且 ``join_existing=False``（点它=新建桌=扣房卡）。

正样本（已有一人的桌）：``_takeover/lobby_probe/011_after_tap_seatUL_1s.png``、
``013_now_5558_after_table_created.png`` —— ``join_existing=True``，计划点 lower
加号 (411,467)，**并入已有桌不扣卡**，这是阶段一唯一允许点的形态。

负样本：``probe/t_5556``/``t_5558``/``s_5556``（牌桌内）、``shot_5556_now``
（大厅/其它屏）、``match_end_probe``（结算）、``lobby_confirm_5556``（弹窗）、
``lobby_probe/021``（等候室，交给 ``lobby_start`` 通道，本通道不许碰）。
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

import lobby_autostart as L  # noqa: E402
import tap_executor as T  # noqa: E402
from auto_play_loop import (  # noqa: E402
    LOBBY_OPEN_BUDGET,
    LOBBY_OPEN_ENABLED_DEFAULT,
    LOBBY_OPEN_MIN_GAP_FRAMES,
    LOBBY_OPEN_UNRECOGNISED_FRAMES,
    AutoPlayLoop,
)
from action_guard import ActionGuardResult  # noqa: E402
from harvest_tea import EXPECTED_PACKAGE  # noqa: E402
from tap_executor import (  # noqa: E402
    OPEN_TABLE_ACTION,
    TapAborted,
    TapExecutor,
    TapRejected,
    TapUnverified,
)

REPO = Path(__file__).resolve().parents[3]
PROBE = REPO / "_takeover" / "probe"
LOBBY_PROBE = REPO / "_takeover" / "lobby_probe"
TAKEOVER = REPO / "_takeover"

#: 正样本：房间，点它 = 新建桌 = 扣房卡（阶段一必须被房卡门挡死）。
ROOM_HOST = PROBE / "now_5556.png"
ROOM_MEMBER = PROBE / "now_5558.png"
#: 正样本：已有一人的桌，点它 = 并入，不扣卡（阶段一唯一允许点的形态）。
JOIN_EXISTING_A = LOBBY_PROBE / "011_after_tap_seatUL_1s.png"
JOIN_EXISTING_B = LOBBY_PROBE / "013_now_5558_after_table_created.png"
#: 负样本。
NEGATIVES = {
    "in_table_5556": PROBE / "t_5556.png",
    "in_table_5558": PROBE / "t_5558.png",
    "in_table_s5556": PROBE / "s_5556.png",
    "lobby_screen": TAKEOVER / "shot_5556_now.png",
    "settlement": TAKEOVER / "match_end_probe.png",
    "modal_over_room": TAKEOVER / "lobby_confirm_5556.png",
    "waiting_room": LOBBY_PROBE / "021_after_close_member_panel.png",
}

#: 实测计划点（1600x900）。
NEW_TABLE_POINT = (115, 267)      # 空桌 upper 加号：点它=建房并扣房卡
JOIN_SEAT_POINT = (411, 467)      # 已坐一人的桌的 lower 空座：并入，不扣卡

_CACHE: dict[str, np.ndarray] = {}


def _read(path: Path) -> np.ndarray:
    """中文路径读图铁律：``cv2.imread`` 会静默返回 None。"""
    key = str(path)
    if key not in _CACHE:
        assert path.is_file(), f"取证帧缺失：{path}"
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image is not None, f"取证帧读不出：{path}"
        _CACHE[key] = image
    return _CACHE[key].copy()


def _inside(point, box) -> bool:
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


# ---------------------------------------------------------------------------
# 离线护栏：任何真实 adb / subprocess 调用都让测试失败
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("离线测试禁止任何真实 subprocess/adb 调用")

    monkeypatch.setattr(subprocess, "run", forbidden)


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------
class FakeTap:
    """``TapExecutor`` 替身：只记录 ``execute_open_table`` 的调用，不点设备。"""

    def __init__(self, outcome=None, error=None):
        self.outcome = outcome or {
            "executed": True, "dry_run": False, "stage": "verified",
            "action": OPEN_TABLE_ACTION, "tap": list(JOIN_SEAT_POINT),
            "box": [366, 421, 456, 533], "verified": True,
        }
        self.error = error
        self.open_calls: list[dict] = []

    def execute_open_table(self, point=None, **kwargs):
        self.open_calls.append({"point": point, **kwargs})
        if self.error is not None:
            raise self.error
        return dict(self.outcome)


def _png_bytes(image: np.ndarray | None = None) -> bytes:
    """一张合法的 1600x900 PNG（tick 会真的 imdecode 它）。"""
    if image is None:
        image = np.zeros((900, 1600, 3), np.uint8)
        image[:, :] = (40, 70, 40)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


#: 让 tick 能走到回合门禁的 analyze 返回值（房间里没有真手牌，只求不被
#: 「财神未知 / 事实不足」的早期分支吃掉）。
ADVISE_PAYLOAD = {
    "status": "advise", "caishen": "w1", "hand_ids": [1] * 14,
    "suggested_boxes": [[90, 780, 170, 888]], "mode": "tea_2p",
    "vision_diagnostics": [],
}


def _make_loop(tmp_path, *, enabled: bool = False, tap=None,
               lobby_open_enabled: bool = False, frame=None,
               **kwargs) -> AutoPlayLoop:
    raw = _png_bytes(frame)
    return AutoPlayLoop(
        enabled=enabled,
        adb_path=sys.executable,          # 只做 is_file() 检查，不会被执行
        serial="emulator-test",
        session_dir=Path(tmp_path) / "session",
        max_actions=kwargs.pop("max_actions", 20),
        duration_s=kwargs.pop("duration_s", 600.0),
        interval_s=kwargs.pop("interval_s", 0.0),
        tap_executor=tap if tap is not None else FakeTap(),
        shot_fn=lambda: raw,
        analyze_fn=lambda _raw, _path: dict(ADVISE_PAYLOAD),
        foreground_fn=lambda: EXPECTED_PACKAGE,
        action_guard_fn=lambda _image: ActionGuardResult("clear"),
        round_key_fn=lambda _path: (b"hud-" + bytes(range(32)), None),
        sleep_fn=lambda _seconds: None,
        clock_fn=lambda: 0.0,
        lobby_open_enabled=lobby_open_enabled,
        **kwargs,
    )


def _ledger(loop: AutoPlayLoop) -> list[dict]:
    path = loop.ledger_path
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _events(loop: AutoPlayLoop, name: str) -> list[dict]:
    return [row for row in _ledger(loop) if row.get("event") == name]


def _left(loop: AutoPlayLoop) -> AutoPlayLoop:
    """把循环置于「已离桌、正停在房间」的状态。"""
    loop._left_table = True
    loop._reset_lobby_open_budget()
    return loop


def _run(loop: AutoPlayLoop, image: np.ndarray):
    return loop._lobby_open_event(
        image, Path(loop.session_dir) / "f.png", {}, 0.0)


# ---------------------------------------------------------------------------
# 0. 语料守卫 + 门禁常量
# ---------------------------------------------------------------------------
def test_frame_corpus_present():
    for path in (ROOM_HOST, ROOM_MEMBER, JOIN_EXISTING_A, JOIN_EXISTING_B,
                 *NEGATIVES.values()):
        assert path.is_file(), f"缺少取证帧 {path}"


def test_gate_defaults_are_fail_closed(tmp_path):
    """G7 默认关、G5 预算 2 —— 这两个数是「绝不自动开新场」的底线。"""
    assert LOBBY_OPEN_ENABLED_DEFAULT is False
    assert LOBBY_OPEN_BUDGET == 2
    assert LOBBY_OPEN_MIN_GAP_FRAMES >= 2
    assert LOBBY_OPEN_UNRECOGNISED_FRAMES >= 10
    loop = _make_loop(tmp_path)
    assert loop.lobby_open_enabled is False


def test_gate_defaults_are_fail_closed():
    """G7 默认关、G5 预算 2 —— 这两个数是「绝不自动开新场」的底线。"""
    assert LOBBY_OPEN_ENABLED_DEFAULT is False
    assert LOBBY_OPEN_BUDGET == 2
    assert LOBBY_OPEN_MIN_GAP_FRAMES >= 2
    assert LOBBY_OPEN_UNRECOGNISED_FRAMES >= 10
    loop = _make_loop(Path(".").resolve())
    assert loop.lobby_open_enabled is False


# ---------------------------------------------------------------------------
# 1. G1 离桌门
# ---------------------------------------------------------------------------
def test_left_table_false_never_triggers(tmp_path):
    """没离过桌 → 整条通道不许有任何反应（局内误判全被这道门挡死）。"""
    for path in (ROOM_HOST, ROOM_MEMBER, JOIN_EXISTING_A):
        tap = FakeTap()
        loop = _make_loop(tmp_path, enabled=True, tap=tap,
                          lobby_open_enabled=True)
        assert loop._left_table is False
        assert _run(loop, _read(path)) is None
        assert tap.open_calls == []
        assert _ledger(loop) == []


# ---------------------------------------------------------------------------
# 2. G2/G6 房卡门：房间正样本认得出，但绝不允许点（新建桌会扣房卡）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [ROOM_HOST, ROOM_MEMBER])
def test_room_frames_are_recognised_but_never_clicked(tmp_path, path):
    """阶段一的预期形态：认出房间、算清该点哪里、记台账，**零点击**。

    两帧正样本都是「桌数 0/10、两个空座」→ ``join_existing=False`` → 点它
    是新建桌、要扣房卡；而房卡一个数都读不出来。所以必须弃权。
    """
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))

    assert _run(loop, _read(path)) is None
    assert tap.open_calls == [], "新建桌（会扣房卡）竟然被点了"

    blocked = _events(loop, "lobby_open_blocked")
    assert len(blocked) == 1, blocked
    assert blocked[0]["reason"] == "cards_unreadable"
    assert "新建桌" in blocked[0]["detail"]
    # 该点哪里必须照样算清楚写进台账（阶段一唯一的价值就在这条记录里）。
    assert blocked[0]["lobby_room"]["present"] is True
    assert blocked[0]["lobby_plan"]["action"] == "join_table"
    assert tuple(blocked[0]["lobby_plan"]["point"]) == NEW_TABLE_POINT
    assert blocked[0]["lobby_plan"]["join_existing"] is False
    assert blocked[0]["lobby_open_enabled"] is True
    assert loop.stopped is False


def test_room_frames_are_reported_once_per_screen(tmp_path):
    """房间画面会一动不动停几十帧：台账按「一屏一次」记，不许每帧刷。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    image = _read(ROOM_HOST)
    for _ in range(30):
        assert _run(loop, image) is None
    assert len(_events(loop, "lobby_open_blocked")) == 1
    assert tap.open_calls == []


# ---------------------------------------------------------------------------
# 3. G2 负样本：一律不出计划
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_negative_frames_never_plan(tmp_path, name):
    """牌桌内 / 大厅 / 结算 / 弹窗 / 等候室 → 不出计划、零点击、不写台账。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    image = _read(NEGATIVES[name])

    assert _run(loop, image) is None
    assert tap.open_calls == []
    assert _events(loop, "lobby_open") == []
    assert _events(loop, "lobby_open_plan") == []
    assert _events(loop, "lobby_open_blocked") == []


def test_solid_frames_never_plan(tmp_path):
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    for color in ((0, 0, 0), (255, 255, 255), (40, 70, 40)):
        assert _run(loop, np.full((900, 1600, 3), color, np.uint8)) is None
    assert tap.open_calls == []


def test_unrecognised_screen_reports_after_a_bounded_wait(tmp_path):
    """不是房间却一直不给计划：攒够帧数报一次 unrecognised，别无限静等。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    image = _read(NEGATIVES["lobby_screen"])
    for _ in range(LOBBY_OPEN_UNRECOGNISED_FRAMES - 1):
        assert _run(loop, image) is None
        assert _events(loop, "lobby_open_blocked") == []
    # 第 N 帧才报一次：既不是每帧刷，也不是无限静等。
    assert _run(loop, image) is None
    blocked = _events(loop, "lobby_open_blocked")
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "unrecognised"
    assert tap.open_calls == []


# ---------------------------------------------------------------------------
# 4. 放行形态：join_existing=True（并入已有空座，不扣卡）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [JOIN_EXISTING_A, JOIN_EXISTING_B])
def test_join_existing_clicks_once_and_verifies(tmp_path, path):
    """唯一允许点的形态：已有一人的桌的空座。点一次，且必须验证。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))

    event = _run(loop, _read(path))

    assert event is not None and event["event"] == "lobby_open"
    assert event["authorized"] is True
    assert tuple(event["tap"]) == JOIN_SEAT_POINT
    assert event["verified"] is True
    assert event["lobby_open_taps"] == 1
    assert event["lobby_plan"]["join_existing"] is True
    # G8：走的是房间入座专用通道，不是出牌通道。
    assert len(tap.open_calls) == 1
    assert tap.open_calls[0]["point"] == JOIN_SEAT_POINT
    assert "session_dir" in tap.open_calls[0]
    assert _events(loop, "lobby_open_blocked") == []


def test_join_existing_is_blocked_when_switch_off(tmp_path):
    """G7：开关关着时只把「该点哪个空座」记进台账，零点击。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=False))
    image = _read(JOIN_EXISTING_A)

    event = _run(loop, image)
    assert event is not None and event["event"] == "lobby_open_plan"
    assert event["authorized"] is False
    assert tuple(event["tap"]) == JOIN_SEAT_POINT
    assert tap.open_calls == []
    # 同一屏只记一次
    assert _run(loop, image) is None
    assert len(_events(loop, "lobby_open_plan")) == 1


def test_dry_run_records_plan_without_executing(tmp_path):
    """dry_run：把计划交给执行器复核（G3 双保险），但不点。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=False, tap=tap))
    assert loop.dry_run is True

    event = _run(loop, _read(JOIN_EXISTING_A))

    assert event is not None and event["event"] == "lobby_open_plan"
    assert event["dry_run"] is True
    assert event["verified"] is None
    assert len(tap.open_calls) == 1
    assert "image" in tap.open_calls[0], "dry_run 必须显式传 image，拒绝盲点"


# ---------------------------------------------------------------------------
# 5. G4 点后验证 / G5 预算门
# ---------------------------------------------------------------------------
def test_unverified_tap_stops_the_loop_without_retrying(tmp_path):
    """点后无法证明已离开房间 → 停手，**绝不补点第二下**。"""
    tap = FakeTap(error=TapUnverified("点后画面仍是房间"))
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))

    event = _run(loop, _read(JOIN_EXISTING_A))

    assert event is not None and event["event"] == "stop"
    assert event["gate"] == "lobby_open_unverified"
    assert loop.stopped is True
    assert len(tap.open_calls) == 1          # 只点了一下，没有补点
    assert loop._lobby_open_taps == 0        # 未验证不计入预算


def test_budget_exhausted_blocks_further_clicks(tmp_path):
    """G5：同一屏点满预算仍看到空座 → 记 budget_exhausted，停手不补点。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    loop._lobby_open_taps = LOBBY_OPEN_BUDGET

    assert _run(loop, _read(JOIN_EXISTING_A)) is None
    assert tap.open_calls == []
    blocked = _events(loop, "lobby_open_blocked")
    assert len(blocked) == 1 and blocked[0]["reason"] == "budget_exhausted"


def test_min_gap_between_two_clicks(tmp_path):
    """G5：两次入座点击之间至少隔 LOBBY_OPEN_MIN_GAP_FRAMES 帧。"""
    tap = FakeTap()
    loop = _left(_make_loop(tmp_path, enabled=True, tap=tap,
                            lobby_open_enabled=True))
    loop._lobby_open_last_tick = 0
    loop.tick_index = LOBBY_OPEN_MIN_GAP_FRAMES - 1

    assert _run(loop, _read(JOIN_EXISTING_A)) is None
    assert tap.open_calls == []
    loop.tick_index = LOBBY_OPEN_MIN_GAP_FRAMES
    assert _run(loop, _read(JOIN_EXISTING_A)) is not None
    assert len(tap.open_calls) == 1


def test_budget_resets_when_a_new_round_starts(tmp_path):
    """换局 = 新的一屏，开新场预算必须重新起算。"""
    loop = _left(_make_loop(tmp_path, enabled=True, tap=FakeTap(),
                            lobby_open_enabled=True))
    loop._lobby_open_taps = LOBBY_OPEN_BUDGET
    loop._reset_lobby_open_budget()
    assert loop._lobby_open_taps == 0
    assert loop._lobby_open_seen == set()


# ---------------------------------------------------------------------------
# 6. TapExecutor 独立通道（G3/G4/G8）
# ---------------------------------------------------------------------------
def _executor(tmp_path, **kwargs) -> TapExecutor:
    return TapExecutor(enabled=False, adb_path=sys.executable,
                       serial="emulator-test",
                       session_dir=Path(tmp_path) / "probe", **kwargs)


def test_executor_dry_run_rejects_new_table_by_default(tmp_path):
    """房卡门写死在执行器里：新建桌（join_existing=False）默认一律拒绝。"""
    execu = _executor(tmp_path)
    with pytest.raises(TapRejected) as info:
        execu.execute_open_table(image=_read(ROOM_HOST))
    assert "房卡" in str(info.value)
    assert "新建桌" in str(info.value)


def test_executor_dry_run_can_be_explicitly_relaxed(tmp_path):
    """显式关掉房卡门才给计划（阶段二补上房卡 OCR 后再由调用方决定）。"""
    execu = _executor(tmp_path)
    plan = execu.execute_open_table(image=_read(ROOM_HOST),
                                    require_join_existing=False)
    assert plan["action"] == OPEN_TABLE_ACTION
    assert tuple(plan["tap"]) == NEW_TABLE_POINT
    assert plan["lobby_plan"]["point"] == list(NEW_TABLE_POINT)
    assert plan["dry_run"] is True


@pytest.mark.parametrize("path", [JOIN_EXISTING_A, JOIN_EXISTING_B])
def test_executor_dry_run_joins_the_existing_seat(tmp_path, path):
    """并入已有空座：计划点必须是同一桌的 lower 空座。"""
    execu = _executor(tmp_path)
    plan = execu.execute_open_table(image=_read(path))
    assert plan["action"] == OPEN_TABLE_ACTION
    assert tuple(plan["tap"]) == JOIN_SEAT_POINT
    box = tuple(plan["box"])
    assert box[0] <= JOIN_SEAT_POINT[0] <= box[2]
    assert box[1] <= JOIN_SEAT_POINT[1] <= box[3]
    assert plan["lobby_plan"]["join_existing"] is True


def test_executor_rejects_non_room_frames(tmp_path):
    execu = _executor(tmp_path)
    for name in ("in_table_5556", "in_table_5558", "in_table_s5556",
                 "lobby_screen", "settlement"):
        with pytest.raises(TapRejected):
            execu.execute_open_table(image=_read(NEGATIVES[name]))


def test_executor_rejects_requested_point_outside_the_seat_box(tmp_path):
    """调用方给的坐标不在 fresh 帧实测空座卡框内 → 拒绝。"""
    execu = _executor(tmp_path)
    with pytest.raises(TapRejected) as info:
        execu.execute_open_table((1150, 700), image=_read(JOIN_EXISTING_A))
    assert "空座卡框" in str(info.value)


def test_executor_rejects_unit_other_than_zero(tmp_path):
    """只能点最左桌位组：右边那组点下去是另建一桌。"""
    state = L.RoomState(present=True, reason="合成", units=2)
    plan = L.StartPlan(action="join_table", point=(411, 467),
                       box=(366, 421, 456, 533), reason="",
                       evidence={"unit": 1, "join_existing": True})
    with pytest.raises(TapRejected) as info:
        TapExecutor._resolve_open_table(state, plan, None)
    assert "桌位组 0" in str(info.value)


def test_executor_rejects_no_tap_zones(tmp_path, monkeypatch):
    """落点落进房间五个禁区之一 → 拒绝（充值 / 闭赛 / 功能圆盘 …）。"""
    monkeypatch.setattr(T, "LOBBY_OPEN_BAND", (0, 0, 1600, 900))
    state = L.RoomState(present=True, reason="合成", units=1)
    plan = L.StartPlan(action="join_table", point=(1558, 205),
                       box=(1520, 140, 1600, 280), reason="",
                       evidence={"unit": 0, "join_existing": True})
    with pytest.raises(TapRejected) as info:
        TapExecutor._resolve_open_table(state, plan, None)
    assert "禁区" in str(info.value)


def test_no_tap_zones_still_cover_the_real_controls():
    """禁区矩形必须真的盖住实物；且与桌位带互不相交（几何前提）。"""
    samples = {
        (1420, 740, 1600, 900): [(1523, 827), (1528, 825)],      # 功能圆盘
        (1470, 10, 1600, 290): [(1552, 52), (1500, 110)],        # 更多/促销条
        (470, 45, 595, 105): [(528, 73)],                        # 充值
        (1520, 140, 1600, 280): [(1558, 205), (1528, 205)],      # 闭赛/分享
        (1380, 90, 1500, 190): [(1455, 164)],                    # 大海钓场
    }
    for zone, points in samples.items():
        assert zone in T.LOBBY_NO_TAP_ZONES, f"禁区缺失 {zone}"
        for point in points:
            assert _inside(point, zone), f"{zone} 盖不住实物 {point}"
    for zone in T.LOBBY_NO_TAP_ZONES:
        band = T.LOBBY_OPEN_BAND
        assert (band[2] <= zone[0] or zone[2] <= band[0]
                or band[3] <= zone[1] or zone[3] <= band[1]), (
            f"禁区 {zone} 与桌位带 {band} 相交，约束前提失效")


def test_executor_enabled_path_taps_and_verifies_two_clear_frames(
        tmp_path, monkeypatch):
    """G4：真点后必须**连续两帧**不再是「房间且有空座」才算生效。"""
    execu = TapExecutor(enabled=True, adb_path=sys.executable,
                        serial="emulator-test",
                        session_dir=Path(tmp_path) / "probe")
    room = _read(JOIN_EXISTING_A)
    blank = np.zeros((900, 1600, 3), np.uint8)
    monkeypatch.setattr(execu, "_require_game_foreground", lambda: None)
    monkeypatch.setattr(execu, "_screenshot",
                        lambda: _screens.pop(0) if _screens else blank)
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(execu, "_tap", lambda x, y: taps.append((x, y)))
    _screens = [room, room, blank, blank]

    outcome = execu.execute_open_table(verify_delay=0.0, verify_frames=4)

    assert outcome["executed"] is True
    assert outcome["action"] == OPEN_TABLE_ACTION
    assert outcome["verified"] is True
    assert tuple(outcome["tap"]) == JOIN_SEAT_POINT
    assert taps == [JOIN_SEAT_POINT], "只许点一下"


def test_executor_raises_unverified_when_room_persists(tmp_path, monkeypatch):
    """点了没变化（画面仍是「房间且有空座」）→ TapUnverified，绝不补点。"""
    execu = TapExecutor(enabled=True, adb_path=sys.executable,
                        serial="emulator-test",
                        session_dir=Path(tmp_path) / "probe")
    room = _read(JOIN_EXISTING_A)
    monkeypatch.setattr(execu, "_require_game_foreground", lambda: None)
    monkeypatch.setattr(execu, "_screenshot", lambda: room)
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(execu, "_tap", lambda x, y: taps.append((x, y)))

    with pytest.raises(TapUnverified):
        execu.execute_open_table(verify_delay=0.0, verify_frames=2)
    assert taps == [JOIN_SEAT_POINT], "未验证时绝不能补点第二下"


def test_open_table_channel_declares_independence(tmp_path, monkeypatch):
    """G8：台账里必须显式写着「没有复用出牌通道」+ 独立 channel 名。

    证据落在 ``tap_probe`` 的 claim 里；这里直接读回 JSON 核对，防止以后
    有人把这条通道改回 ``validate_target`` 之后忘了改声明。
    """
    execu = TapExecutor(enabled=True, adb_path=sys.executable,
                        serial="emulator-test",
                        session_dir=Path(tmp_path) / "probe")
    room = _read(JOIN_EXISTING_A)
    blank = np.zeros((900, 1600, 3), np.uint8)
    frames = [room, blank, blank]
    monkeypatch.setattr(execu, "_require_game_foreground", lambda: None)
    monkeypatch.setattr(execu, "_screenshot",
                        lambda: frames.pop(0) if frames else blank)
    monkeypatch.setattr(execu, "_tap", lambda x, y: None)
    execu.execute_open_table(verify_delay=0.0, verify_frames=4)

    blobs = [path.read_text(encoding="utf-8") for path in
             (Path(tmp_path) / "probe").rglob("*.json")]
    joined = "\n".join(blobs)
    assert joined, "没有落盘任何证据"
    assert '"discard_channel_reused": false' in joined
    assert '"channel": "lobby_open_table"' in joined
    assert OPEN_TABLE_ACTION in joined


def test_dry_run_without_image_is_refused(tmp_path):
    """dry_run 不给 image 就是盲点，必须拒绝。"""
    execu = _executor(tmp_path)
    with pytest.raises(TapAborted):
        execu.execute_open_table()


# ---------------------------------------------------------------------------
# 7. 主循环真的调用了它（接线本身，不只是方法存在）
# ---------------------------------------------------------------------------
def test_tick_calls_the_lobby_channel_after_match_end(tmp_path):
    """``tick()`` 里 ``_lobby_open_event`` 必须在 blocked 判断之前被调用。

    这是「断链有没有真的接上」的唯一一条测试：只测方法存在是不够的，
    必须证明主循环真的会走到它。这里用真帧（房间画面）+ 真实门禁，
    只把开新场方法换成探针。
    """
    import auto_play_loop as A

    calls: list[str] = []
    real = A.AutoPlayLoop._lobby_open_event

    def spy(self, image, frame_path, base, started):
        calls.append("lobby")
        return None

    A.AutoPlayLoop._lobby_open_event = spy
    try:
        loop = _left(_make_loop(tmp_path, enabled=True, tap=FakeTap(),
                                lobby_open_enabled=True,
                                frame=_read(ROOM_HOST)))
        loop.tick()
    finally:
        A.AutoPlayLoop._lobby_open_event = real
    assert calls == ["lobby"], "主循环没有调用开新场通道"
