# -*- coding: utf-8 -*-
"""把 ``lobby_autostart`` 钉死在真帧上：正样本要认得、负样本要全拒、落点要合规。

## 这份测试在防什么

``lobby_autostart`` 决定「代打打满 16 局 → 返回大厅 → 停在比赛场房间」之后该点哪里。
它一旦把画面认错，代价是**在别人的真局里点一下屏幕**，所以这里的每一条断言都围绕
两件事：**别认错屏**、**别点错地方**。

## 样本（全部 1600x900 真帧，纯离线，不点设备）

正样本：``_takeover/probe/now_5556.png``（房主视角：人数 2/3、可用房卡 72、右上「闭赛」）、
``_takeover/probe/now_5558.png``（成员视角：**没有人数行**、可用房卡 2、右上「分享」）。
两帧右侧整排图标不同，但菱形/空座卡/加号/功能圆盘坐标完全一致——这一点本身就是断言
（模块的判定必须只依赖那几个几何件，不能依赖右侧图标组）。

负样本：``t_5556`` / ``t_5558`` / ``s_5556``（牌桌内）、``match_end_probe``（牌局结束）、
``lobby_confirm_5556``（房间上压着「提示」对话框）、``lobby_probe/020``「成员面板挡屏」、
``lobby_probe/031``「已按开始」。

合成样本：纯色帧、手绘假按钮帧、以及「真部件贴到灰底」的组合帧——最后一条用来证明
计划确实只由菱形+空座卡+加号+锚点这四件东西算出来，别处怎么画都不影响。

## 容差

* 座位中心允许 **±2px**：模块里的 ``SLOT_CENTERS`` 是人工读图得到的点击点 (116,268)，
  而检测出来的加号是 35x35 连通块的 ``center()``，整数下取整后落在 (115,267)，
  两者天生差 1px；再给 1px 余量容忍重编码抖动。
* 模板分数只断下界（≥ 实测最低值 - 0.01），不断等值：JPEG/缩放会让分数有 1e-3 级浮动。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lobby_autostart as L  # noqa: E402

# ---------------------------------------------------------------------------
# 样本路径
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[3]
PROBE = REPO / "_takeover" / "probe"
LOBBY_PROBE = REPO / "_takeover" / "lobby_probe"

ROOM_HOST = PROBE / "now_5556.png"      # 正样本：房主视角
ROOM_MEMBER = PROBE / "now_5558.png"    # 正样本：成员视角
IN_TABLE = (PROBE / "t_5556.png", PROBE / "t_5558.png", PROBE / "s_5556.png")

#: 实测点击点（1600x900）。两帧一致，所以只写一份。
PLAN_POINT = (115, 267)
PLAN_BOX = (70, 221, 160, 333)
LOWER_SEAT_POINT = (411, 467)
CENTER_TOL = 2

#: 禁区（1600x900 实测像素，坐标来源见 ``_takeover/lobby_autostart_wiring.md``）。
#: 计划落点绝不能进这些框：右下圆形「功能」、右上竖排图标（会员/成员详情/比赛战绩/
#: 排行榜/设置/更多）、顶部「充值」、右上「闭赛」（房主）或「分享」（成员）、
#: 「大海钓场」活动入口。任何一个被误点都会开新面板、扣费或直接结束比赛场。
NO_TAP_ZONES = {
    "func_circle": (1420, 740, 1600, 900),
    "right_icon_column": (1470, 10, 1600, 290),
    "topup_button": (470, 45, 595, 105),
    "close_or_share_button": (1520, 140, 1600, 280),
    "activity_entry": (1380, 90, 1500, 190),
}

#: 顶部「可用房卡:xx」里的数字区（实测两帧差异像素 bbox 向外放宽 13px）。
#: 本模块不读它，这里只用来证明「房卡读数变了，计划不变」。
CARD_DIGIT_ROI = (380, 55, 430, 95)

_CACHE: dict[str, np.ndarray] = {}


def _read(path: Path) -> np.ndarray:
    """中文路径读图铁律：``cv2.imread`` 会静默返回 None。"""
    key = str(path)
    if key not in _CACHE:
        assert path.is_file(), f"取证帧缺失，测试样本已被移动：{path}"
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image is not None, f"取证帧读不出：{path}"
        _CACHE[key] = image
    return _CACHE[key].copy()


def _room() -> np.ndarray:
    return _read(ROOM_HOST)


def _inside(point, box) -> bool:
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _is_measured_seat(point) -> bool:
    """落点是否落在两个实测空座中心之一（±2px，见模块文档的容差理由）。"""
    return any(abs(point[0] - ref[0]) <= CENTER_TOL
               and abs(point[1] - ref[1]) <= CENTER_TOL
               for ref in (PLAN_POINT, LOWER_SEAT_POINT))


def _assert_plan_is_safe(plan, label: str) -> None:
    """所有计划都要过的通用安全断言：落点在自己的框内、在桌位带内、不在禁区。"""
    assert plan is not None, f"{label}: 预期有计划"
    x, y = plan.point
    assert 0 <= x < 1600 and 0 <= y < 900, f"{label}: 落点跑出画面 {plan.point}"
    assert plan.box is not None, f"{label}: 计划必须带框"
    x0, y0, x1, y1 = plan.box
    assert x0 < x < x1 and y0 < y < y1, (
        f"{label}: 落点 {plan.point} 不在自己的框 {plan.box} 内")
    band = L.TABLE_BAND
    assert band[0] <= x0 and x1 <= band[2] and band[1] <= y0 and y1 <= band[3], (
        f"{label}: 框 {plan.box} 跑出桌位带 {band}")
    for name, zone in NO_TAP_ZONES.items():
        assert not _inside(plan.point, zone), (
            f"{label}: 落点 {plan.point} 落进禁区「{name}」{zone}")


# ---------------------------------------------------------------------------
# 0. 语料守卫
# ---------------------------------------------------------------------------
def test_frame_corpus_present():
    """样本帧是硬依赖：被移走时必须显式失败，不能静默跳过。"""
    for path in (ROOM_HOST, ROOM_MEMBER, *IN_TABLE,
                 REPO / "_takeover" / "match_end_probe.png",
                 LOBBY_PROBE / "021_after_close_member_panel.png"):
        assert path.is_file(), f"缺少取证帧 {path}"


# ---------------------------------------------------------------------------
# 1. 正样本：房间画面
# ---------------------------------------------------------------------------
def test_room_host_view_detected():
    """房主视角：认成房间、2 个空座、计划点 upper 加号。"""
    state = L.read_lobby_room(_read(ROOM_HOST))
    assert state.present is True, state.reason
    assert state.waiting_room is False
    assert state.units == 1
    assert len(state.seats) == 2
    assert [seat.slot for seat in state.seats] == ["upper", "lower"]
    assert [seat.unit for seat in state.seats] == [0, 0]
    assert state.occupied_slots == ()

    upper, lower = state.seats
    assert abs(upper.center[0] - PLAN_POINT[0]) <= CENTER_TOL
    assert abs(upper.center[1] - PLAN_POINT[1]) <= CENTER_TOL
    assert tuple(upper.box) == PLAN_BOX
    assert abs(lower.center[0] - LOWER_SEAT_POINT[0]) <= CENTER_TOL
    assert abs(lower.center[1] - LOWER_SEAT_POINT[1]) <= CENTER_TOL
    # 空座卡模板分数：实测 1.0000 / 0.9988，下界留 0.005 给重编码抖动。
    assert upper.join_score >= 0.99
    assert lower.join_score >= 0.99

    plan = L.plan_start(_read(ROOM_HOST))
    assert plan is not None
    assert plan.action == "join_table"
    assert abs(plan.point[0] - PLAN_POINT[0]) <= CENTER_TOL
    assert abs(plan.point[1] - PLAN_POINT[1]) <= CENTER_TOL
    assert plan.evidence["state"] == "lobby_room"
    assert plan.evidence["join_existing"] is False
    assert plan.evidence["unit"] == 0
    _assert_plan_is_safe(plan, "房主视角")


def test_room_member_view_detected():
    """成员视角：右侧图标完全是另一套（分享/比赛战绩/排行榜/更多），结论必须一样。"""
    state = L.read_lobby_room(_read(ROOM_MEMBER))
    assert state.present is True, state.reason
    assert state.units == 1
    assert len(state.seats) == 2
    assert [seat.slot for seat in state.seats] == ["upper", "lower"]

    plan = L.plan_start(_read(ROOM_MEMBER))
    assert plan is not None and plan.action == "join_table"
    assert abs(plan.point[0] - PLAN_POINT[0]) <= CENTER_TOL
    assert abs(plan.point[1] - PLAN_POINT[1]) <= CENTER_TOL
    _assert_plan_is_safe(plan, "成员视角")


def test_two_views_differ_but_agree():
    """两帧右侧 / 顶栏明显不同，但座位几何与计划点逐字段一致。

    这条是「版式判定不依赖账号角色」的证据：如果以后有人把右侧图标加进判定，
    这条会立刻红。
    """
    host = _read(ROOM_HOST)
    member = _read(ROOM_MEMBER)
    right = NO_TAP_ZONES["right_icon_column"]
    diff = cv2.absdiff(host[right[1]:right[3], right[0]:right[2]],
                       member[right[1]:right[3], right[0]:right[2]])
    assert int((diff.max(axis=2) > 40).sum()) > 3000, "两帧右上区域应当明显不同"
    # 房卡数字（实测差异像素 bbox：x∈[393,419]、y∈[65,83]）：72 vs 2。
    card = CARD_DIGIT_ROI
    diff_card = cv2.absdiff(host[card[1]:card[3], card[0]:card[2]],
                            member[card[1]:card[3], card[0]:card[2]])
    assert int((diff_card.max(axis=2) > 40).sum()) > 150, "两帧房卡数应当不同"

    a = L.read_lobby_room(host)
    b = L.read_lobby_room(member)
    assert [(s.box, s.center, s.slot, s.unit) for s in a.seats] == \
           [(s.box, s.center, s.slot, s.unit) for s in b.seats]
    assert L.plan_start(host).point == L.plan_start(member).point


def test_counts_are_abstained_not_guessed():
    """人数/桌数/房卡一律不读：字段必须是 None 且写明理由，不许瞎猜。"""
    for path in (ROOM_HOST, ROOM_MEMBER):
        state = L.read_lobby_room(_read(path))
        assert state.player_count is None
        assert state.table_count is None
        assert "美术字" in state.counts_reason
        assert "不得用来推断" in state.counts_reason
        # 台账里不能出现任何房卡字段：本模块不读房卡，不要给调用方错觉。
        blob = json.dumps(state.ledger(), ensure_ascii=False)
        assert "room_card" not in blob and "房卡" not in blob


# ---------------------------------------------------------------------------
# 2. 计划落点合规
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [ROOM_HOST, ROOM_MEMBER])
def test_plan_inside_own_box_and_out_of_no_tap_zones(path):
    plan = L.plan_start(_read(path))
    _assert_plan_is_safe(plan, path.name)
    # 「功能」圆盘实测中心 (1528,825)：join 计划必须离它 300px 以上。
    assert abs(plan.point[0] - 1528) + abs(plan.point[1] - 825) > 300


def test_start_game_plan_is_safe():
    """等候室的「开始游戏」计划同样不许漂到禁区。"""
    plan = L.plan_start(_read(LOBBY_PROBE / "021_after_close_member_panel.png"))
    assert plan is not None
    assert plan.action == "start_game"
    assert plan.point == L.START_CENTER == (800, 574)
    assert tuple(plan.box) == L.START_BUTTON_BOX
    assert _inside(plan.point, plan.box)
    for name, zone in NO_TAP_ZONES.items():
        assert not _inside(plan.point, zone), f"开始游戏落点落进禁区「{name}」"


def test_no_tap_zones_still_cover_the_real_controls():
    """禁区矩形必须真的盖住实物，否则上面那些「不在禁区」的断言就是空转。

    样本点全部取自两帧正样本的目视坐标（1600x900）：
    更多 (1552,52)、充值 (528,73)、闭赛/分享 (1558/1528,205)、大海钓场 (1455,164)、
    功能 (1523,827)。UI 一挪，这条先红，逼着重新量。
    """
    samples = {
        "func_circle": [(1523, 827), (1528, 825)],
        "right_icon_column": [(1552, 52), (1500, 110)],   # 更多 / 免费促销条
        "topup_button": [(528, 73)],
        "close_or_share_button": [(1558, 205), (1528, 205)],  # 闭赛 / 分享
        "activity_entry": [(1455, 164)],
    }
    for name, points in samples.items():
        for point in points:
            assert _inside(point, NO_TAP_ZONES[name]), (
                f"禁区「{name}」已经盖不住实物 {point}，坐标标定过期")
    # 桌位带与所有禁区互不相交：join 计划的框在带内，因此天然碰不到这些控件。
    band = L.TABLE_BAND
    for name, zone in NO_TAP_ZONES.items():
        disjoint = (band[2] <= zone[0] or zone[2] <= band[0]
                    or band[3] <= zone[1] or zone[3] <= band[1])
        assert disjoint, f"禁区「{name}」与桌位带相交，约束前提失效"


# ---------------------------------------------------------------------------
# 3. 负样本：牌桌内 / 结算 / 弹窗
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,path", [
    ("t_5556", PROBE / "t_5556.png"),
    ("t_5558", PROBE / "t_5558.png"),
    ("s_5556", PROBE / "s_5556.png"),
    ("shot_5556_game", REPO / "_takeover" / "shot_5556_game.png"),
    ("shot_5556_deal", REPO / "_takeover" / "shot_5556_deal.png"),
])
def test_in_table_frames_rejected(name, path):
    """牌桌内画面：不得判成房间、不得等候室、绝不给计划。"""
    image = _read(path)
    state = L.read_lobby_room(image)
    assert state.present is False, f"{name}: 牌桌内被认成房间"
    assert state.waiting_room is False, f"{name}: 牌桌内被认成等候室"
    assert state.seats == ()
    assert state.evidence.get("anchors", {}).get("table") is False, (
        f"{name}: 桌位菱形锚点应当不成立")
    assert L.plan_start(image) is None, f"{name}: 牌桌内竟然给出了计划"


def test_settlement_frame_rejected():
    """牌局结束（返回大厅/分享战绩/继续游戏）必须拒绝。"""
    image = _read(PROBE.parent / "match_end_probe.png")
    state = L.read_lobby_room(image)
    assert state.present is False
    assert state.waiting_room is False
    assert L.plan_start(image) is None


@pytest.mark.parametrize("name", ["lobby_confirm_5556.png",
                                  "lobby_confirm2_5556.png",
                                  "lobby_tap_5556.png"])
def test_modal_over_room_rejected(name):
    """房间上压着「提示」对话框时不许给计划。

    注意防线是**间接**的：模态的压暗同时打掉横幅（V>170）与功能键（绿色）两个
    HSV 锚点，于是 fail-closed。所以这里断言的是「当前实测被拒」，不是「模块会
    检测模态」——不压暗的浮层得靠调用方门禁，见接线说明的风险清单。
    """
    image = _read(REPO / "_takeover" / name)
    state = L.read_lobby_room(image)
    assert state.present is False
    assert L.plan_start(image) is None


# ---------------------------------------------------------------------------
# 4. 合成样本：纯色 / 假按钮
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("color", [
    (0, 0, 0),          # 纯黑
    (255, 255, 255),    # 纯白
    (30, 90, 200),      # 顶部横幅橙
    (109, 158, 201),    # 桌面米色地板（实测均值）
    (60, 200, 120),     # 绿毡
    (128, 128, 128),    # 中性灰
])
def test_solid_frames_never_plan(color):
    image = np.full((900, 1600, 3), color, np.uint8)
    state = L.read_lobby_room(image)
    assert state.present is False
    assert state.waiting_room is False
    assert L.plan_start(image) is None


def _paint_plus(image, x: int, y: int, width: int = 90, height: int = 112) -> None:
    """在 (x,y) 画一张 tan 空座卡 + 白色十字（尺寸照抄实测 90x112 / 35x35）。"""
    image[y:y + height, x:x + width] = (152, 198, 224)
    cx, cy = x + width // 2, y + height // 2 - 9
    image[cy - 17:cy + 18, cx - 2:cx + 3] = 255
    image[cy - 2:cy + 3, cx - 17:cx + 18] = 255


def _anchored_blank() -> np.ndarray:
    """灰底 + 假横幅 + 假功能圆盘：三个锚点里「横幅」「功能键」成立，桌位待定。"""
    image = np.full((900, 1600, 3), 90, np.uint8)
    image[10:50, 400:800] = (30, 90, 200)
    cv2.circle(image, (1528, 825), 38, (120, 200, 60), -1)
    return image


def test_background_with_fake_buttons_never_plans():
    """整片桌面底 + 手绘空座卡与加号（含假横幅/假功能盘）→ 必须拒绝。

    防的就是「把背景纹理当按钮」：没有真桌位菱形，``table`` 锚点不成立。
    """
    image = _anchored_blank()
    _paint_plus(image, 70, 221)
    _paint_plus(image, 366, 421)
    state = L.read_lobby_room(image)
    assert state.present is False, state.reason
    assert state.evidence["anchors"] == {"header": True, "func": True, "table": False}
    assert L.plan_start(image) is None


def test_fake_seat_card_with_real_diamond_abstains():
    """真菱形 + 手绘空座卡：认得出是房间，但空座卡模板分数不够 → 不给计划。

    空座卡必须过 ``btn_join_table`` 0.97，手绘一张 0.3273 的“像那么回事”的卡
    骗不过去。这是「宁可不点，也不点错」的最后一道闸。
    """
    image = _anchored_blank()
    image[286:438, 124:400] = _room()[286:438, 124:400]
    _paint_plus(image, 70, 221)
    _paint_plus(image, 366, 421)
    state = L.read_lobby_room(image)
    assert state.present is True, state.reason
    assert state.seats == ()
    rejected = state.evidence["rejected"]
    assert len(rejected) == 2
    for item in rejected:
        assert item["why"] == "空座卡里没有加号（背景板/其它卡）" or "模板分数不足" in item["why"]
        assert item["join_score"] < L.JOIN_MIN_SCORE
    assert L.plan_start(image) is None


def test_composite_real_parts_reproduce_the_same_plan():
    """把真菱形 + 两张真空座卡贴到灰底上，计划点必须与真帧一致 (115,267)。

    证明计划只由那四个几何件算出来：背景、任务图标、人物、牌河全都不参与。
    """
    room = _room()
    image = _anchored_blank()
    image[286:438, 124:400] = room[286:438, 124:400]
    image[221:333, 70:160] = room[221:333, 70:160]
    image[421:533, 366:456] = room[421:533, 366:456]
    state = L.read_lobby_room(image)
    assert state.present is True
    assert [seat.slot for seat in state.seats] == ["upper", "lower"]
    plan = L.plan_start(image)
    assert plan is not None and plan.action == "join_table"
    assert abs(plan.point[0] - PLAN_POINT[0]) <= CENTER_TOL
    assert abs(plan.point[1] - PLAN_POINT[1]) <= CENTER_TOL
    _assert_plan_is_safe(plan, "合成组合帧")


# ---------------------------------------------------------------------------
# 5. 锚点消融：三个锚点各自都是必要条件
# ---------------------------------------------------------------------------
def test_anchor_ablation_each_one_is_load_bearing():
    room = _room()

    no_header = room.copy()
    no_header[10:50, 400:800] = 0
    state = L.read_lobby_room(no_header)
    assert state.present is False
    assert state.evidence["anchors"] == {"header": False, "func": True, "table": True}
    assert L.plan_start(no_header) is None

    no_func = room.copy()
    no_func[750:900, 1440:1600] = 0
    state = L.read_lobby_room(no_func)
    assert state.present is False
    assert state.evidence["anchors"] == {"header": True, "func": False, "table": True}
    assert L.plan_start(no_func) is None

    no_diamond = room.copy()
    no_diamond[286:438, 124:400] = (70, 120, 150)
    state = L.read_lobby_room(no_diamond)
    assert state.present is False
    assert state.evidence["anchors"] == {"header": True, "func": True, "table": False}
    assert L.plan_start(no_diamond) is None


# ---------------------------------------------------------------------------
# 6. fail-closed：尺寸 / 输入类型
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size", [(1280, 720), (1024, 576), (800, 450),
                                  (1600, 901), (1600, 899), (900, 1600)])
def test_wrong_frame_size_fail_closed(size):
    """尺寸不是 1600x900 一律不认：所有几何常量都只对这一种原生分辨率标定过。"""
    image = cv2.resize(_room(), size)
    state = L.read_lobby_room(image)
    assert state.present is False
    assert "帧尺寸未验证" in state.reason
    assert L.plan_start(image) is None


def test_bad_inputs_fail_closed_without_raising():
    """None / 非数组 / 灰度 / float / uint16 / bool 都必须返回 present=False。

    float32 曾经会抛 OpenCV 断言（``matchTemplate`` 只吃 CV_8U/CV_32F 同型），
    在循环里就是一次崩溃而不是弃权——这里钉住它。
    """
    bad = [
        ("none", None),
        ("gray2d", np.zeros((900, 1600), np.uint8)),
        ("list", [[1, 2, 3]]),
        ("str", "not-an-image"),
        ("float32", np.zeros((900, 1600, 3), np.float32)),
        ("uint16", np.zeros((900, 1600, 3), np.uint16)),
        ("bool", np.zeros((900, 1600, 3), bool)),
        ("empty", np.zeros((0, 0, 3), np.uint8)),
    ]
    for name, value in bad:
        state = L.read_lobby_room(value)
        assert state.present is False, f"{name} 竟然被判成房间"
        assert L.plan_start(value) is None, f"{name} 竟然给出了计划"


def test_float_room_frame_does_not_raise():
    """把真帧转成 float32 再喂进去：必须弃权，而不是崩。"""
    image = _room().astype(np.float32)
    state = L.read_lobby_room(image)
    assert state.present is False
    assert "像素类型" in state.reason
    assert L.plan_start(image) is None


# ---------------------------------------------------------------------------
# 7. 已坐人桌：只点同一桌的空座，绝不另建一桌
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["011_after_tap_seatUL_1s.png",
                                  "013_now_5558_after_table_created.png"])
def test_occupied_table_joins_lower_seat_only(name):
    """左桌已有一人 → 点同一桌的下方空座（加入），而不是右边那个桌位组。"""
    image = _read(LOBBY_PROBE / name)
    state = L.read_lobby_room(image)
    assert state.present is True, state.reason
    assert state.units == 2, "建房后屏幕上会多出一个空桌位组"
    assert state.occupied_slots == ("upper",)
    plan = L.plan_start(image)
    assert plan is not None and plan.action == "join_table"
    assert plan.evidence["unit"] == 0, "只允许点最左桌位组"
    assert plan.evidence["join_existing"] is True
    assert abs(plan.point[0] - LOWER_SEAT_POINT[0]) <= CENTER_TOL
    assert abs(plan.point[1] - LOWER_SEAT_POINT[1]) <= CENTER_TOL
    _assert_plan_is_safe(plan, name)


def test_leftmost_table_full_refuses_right_table():
    """最左桌位组没有空座、右边还有一个 → 不给计划（点右边会另建一桌、扣房卡）。"""
    image = _read(LOBBY_PROBE / "013_now_5558_after_table_created.png")
    image[421:533, 366:456] = (92, 142, 176)   # 抹掉左桌下方空座卡
    state = L.read_lobby_room(image)
    assert state.present is True, state.reason
    assert state.seats, "右边那组空座仍应被检出（用来证明拒绝不是因为看不见）"
    assert all(seat.unit == 1 for seat in state.seats)
    assert L.plan_start(image) is None


# ---------------------------------------------------------------------------
# 8. 等候室阶段：面板挡屏 / 已按开始
# ---------------------------------------------------------------------------
def test_waiting_room_with_member_panel_does_not_plan():
    """成员列表挡住左半屏时不给开始计划（这一屏的点击没做过实机验证）。"""
    for name in ("020_before_close_member_panel.png", "012_after_tap_seatUL_3s.png"):
        image = _read(LOBBY_PROBE / name)
        state = L.read_lobby_room(image)
        assert state.present is False
        assert state.waiting_room is True
        assert state.member_panel_open is True, f"{name}: 没认出成员面板"
        assert state.start_box is None
        assert L.plan_start(image) is None, f"{name}: 面板挡屏竟然给出了开始计划"


def test_waiting_room_start_pressed_never_repeats():
    """本方已按开始（橙色按钮消失）之后不许再给开始计划。"""
    for name in ("031_after_tap_start_1s.png", "032_after_tap_start_3s.png",
                 "034_5556_recheck_8s.png"):
        image = _read(LOBBY_PROBE / name)
        state = L.read_lobby_room(image)
        assert state.waiting_room is True
        assert state.waiting_start_pressed is True, f"{name}: 没认出「已按开始」"
        assert L.plan_start(image) is None, f"{name}: 按过之后又给了一次开始计划"


# ---------------------------------------------------------------------------
# 9. 台账契约：可 JSON 化 + 幂等
# ---------------------------------------------------------------------------
def test_ledger_json_serialisable_and_idempotent():
    image = _read(ROOM_HOST)
    first = L.read_lobby_room(image)
    second = L.read_lobby_room(image)
    assert first == second, "同一帧两次调用结果必须逐字段相同（模板缓存不得污染）"
    blob = json.dumps(first.ledger(), ensure_ascii=False)
    assert "比赛场房间" in blob
    plan = L.plan_start(image)
    assert json.dumps(plan.ledger(), ensure_ascii=False)


def test_low_light_degrades_to_abstain_not_misclick():
    """亮度扰动：变暗只会让模块「看不见」，不会让它点到别处。

    实测 0.85 倍亮度时空座卡掉线（present=True, seats=0）；0.75 倍连房间锚点都丢。
    两种都必须是 plan=None——这是本模块最重要的一条性格：宁可不动。
    """
    room = _read(ROOM_HOST)
    for gain in (0.85, 0.75, 1.15):
        lut = np.clip(np.arange(256) * gain, 0, 255).astype(np.uint8)
        image = cv2.LUT(room, lut)
        plan = L.plan_start(image)
        if plan is not None:
            # 还能看清时，只允许落在两个真实空座上。
            assert _is_measured_seat(plan.point), f"gamma={gain}: {plan.point}"
            _assert_plan_is_safe(plan, f"gamma={gain}")


def test_jpeg_recompression_stays_on_real_seats():
    """JPEG 重编码（scrcpy 逐帧是 JPEG）：允许座位漂，但不许漂出真实空座。"""
    room = _read(ROOM_HOST)
    for quality in (95, 85, 70, 55):
        ok, buffer = cv2.imencode(".jpg", room,
                                  [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        assert ok
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        state = L.read_lobby_room(image)
        assert state.present is True, f"q={quality} 竟然认不出房间"
        plan = L.plan_start(image)
        assert plan is not None, f"q={quality} 没有给出计划"
        assert _is_measured_seat(plan.point), (
            f"q={quality}: 落点 {plan.point} 不在任何一个实测空座上")
        _assert_plan_is_safe(plan, f"jpeg q={quality}")
