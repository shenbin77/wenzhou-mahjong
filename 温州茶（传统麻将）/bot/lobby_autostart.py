# -*- coding: utf-8 -*-
"""比赛场房间（大厅）→ 入座 → 开始游戏：只读识别 + 「下一步该点哪个控件」。

牌局结束后 ``match_end`` 点「返回大厅」，两端都会回到**比赛场房间**这一屏，
之后就没人知道该点哪里。本模块只回答两个问题：

* :func:`read_lobby_room` —— 这一帧是不是比赛场房间？房里有几个空座？
* :func:`plan_start` —— 下一步该点哪个控件？证据不足返回 ``None``。

本模块**不做任何点击**（和 :mod:`match_end` 一样只产出计划），点击与点后验证
由调用方（``auto_play_loop`` / ``TapExecutor`` 的独立通道）执行。

## 实测链路（2026-10-25 23:09~23:18，emulator-5556 实机逐步截图）

1. 房间（桌数 0/10）：一个「幽灵桌位」+ 上下两个空座「+」，坐标 (116,268)、(412,468)。
2. 点左上空座 ``tap 116 268`` → **自动建房并直接进入等候室**（房号 915995，桌数变 1/10）。
   房间卡片左上出现本人头像 + 分数，另一侧仍是空座「+」(412,468)。
3. 等候室：橙「开始游戏」``box=(663,525,937,624) 中心=(800,574)``，
   左「邀请好友」``(319,526,592,624)``，右「复制房间号」``(1004,526,1278,624)``。
   成员列表会自动弹出挡住左半屏（点空地 (832,768) 关掉）。
4. 点开始游戏 ``tap 800 574`` → 橙色按钮**消失**，只剩 邀请好友 / 复制房间号
   （= 本方已按开始，等对手；局数仍 0/16，未见房卡扣费）。对手到位后再各按一次即发牌。

取证帧全部在 ``_takeover/lobby_probe/``（``001_`` 房间 / ``011_`` 入座后 /
``021_`` 等候室 / ``032_`` 已按开始 / ``013_`` 5558 只读视角）。

## 为什么只用几何 + 模板，不读数字

顶部「人数 x/y」「桌数 x/y」是美术字，没有标定过的字形模板，读不出来；而且
``开发档案`` 已明确 **大厅人数/桌数不得用来推断桌内人数**。所以：

* ``RoomState.player_count`` / ``table_count`` 恒为 ``None``，并在
  ``counts_reason`` 里写明原因——**不猜**；
* 真正参与判定的是「桌位菱形 + 空座卡 + 加号」这套版式几何，以及桌位栅格是否
  仍然对得上实测坐标（对不上就 fail-closed）。

## 素材与阈值（全部有实测分数，见 ``_takeover/lobby_probe``）

=========== ============================================== =========== ==========
模板        来源                                           实测分数     阈值
=========== ============================================== =========== ==========
btn_join_table  ``采集/templates/ui/``（本次新采集 90x112）   1.0000/0.9988  0.97
tea2_table      ``bot/templates/lobby/``（既有，空桌菱形）   0.9935/0.9387  0.85
tea2_table_one_seated 同上（已坐人菱形，头像遮挡后）        0.9240/0.9671  0.85
tea2_start_button 同上（橙「开始游戏」）                    0.9984        0.95
=========== ============================================== =========== ==========

负样本（等候室 / 已按开始）最高分：``btn_join_table`` 0.8162、``tea2_table`` 0.3732、
``tea2_start_button`` 0.5424（按钮消失后的残留）。阈值都留了 ≥0.07 的间隙。

## 离线验证（2026-10-25，纯离线真帧，未点设备）

``bot/tests/test_lobby_autostart.py`` 拿 ``_takeover/probe/now_5556.png`` /
``now_5558.png`` 两帧房间画面钉死了输出：两帧分别是房主视角（人数 2/3、房卡 72、
右上「闭赛」）与成员视角（无人数行、房卡 2、右上「分享」），**右侧图标组整排不同**，
但桌位菱形、两个空座卡、加号、功能圆盘四者的坐标完全一致，所以计划点也一样
（upper 加号 (115,267)）。这两帧证明「几何版式」对账号视角差异是稳的。

同批验证出来的边界（都不需要改逻辑，但调用方要知道）：

* **亮度敏感**：整帧乘 0.85 就只剩 ``present=True, seats=0`` → ``plan=None``；
  乘 0.75 连房间锚点都丢。降级方向是**静默失效而不是乱点**，但意味着夜间模式/
  背光变化会让自动开桌直接不工作；
* **JPEG 敏感**：q≥70 计划点稳定在 upper 加号；q=55 时 upper 空座卡掉线，计划改指
  lower 加号 (411,467)——仍是同一桌的真实空座，不是假点，但点击点会漂；
* **假按钮防线**：纯桌面底色 + 手绘 tan 空座卡 + 白色十字，即使再把「横幅 + 绿色
  功能圆盘」都画上去，也只会 ``present=False``（``table`` 锚点不成立）；再贴一张
  真菱形进去则 ``present=True, seats=0``（空座卡模板分数不足）→ 仍 ``plan=None``。
  也就是说「点哪里」必须同时满足菱形 + 模板 0.97 的空座卡 + 栅格加号，三者缺一即弃权；
* **模态弹窗**：``lobby_confirm_5556.png``（房间上压一层「提示」对话框）被拒，原因是
  压暗把横幅与功能键两个 HSV 锚点同时打掉。这是**间接**防线，不是模态检测：
  不压暗的浮层（toast/横幅）仍可能放行，调用方必须自己保证「只在离桌后第一屏」调用；
* **房卡读不出来**：本模块不读房卡，``session_watch.classify_watch`` 的
  ``room_cards`` 目前也返回 ``None``（status ``digit_templates_incomplete``）。
  所以「房卡 < 2 拒绝开桌」这条门禁**当前无法实现**，见接线说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

__all__ = [
    "SeatCandidate",
    "RoomState",
    "StartPlan",
    "read_lobby_room",
    "plan_start",
    "VERIFIED_FRAME_SIZE",
    "START_BUTTON_BOX",
    "START_CENTER",
]

# ---------------------------------------------------------------------------
# 路径：新采集的模板放 `采集/templates/ui/`，已标定过的沿用 bot/templates/lobby/
# ---------------------------------------------------------------------------
_BOT_DIR = Path(__file__).resolve().parent
UI_TEMPLATE_DIR = _BOT_DIR.parent / "采集" / "templates" / "ui"
LOBBY_TEMPLATE_DIR = _BOT_DIR / "templates" / "lobby"
JOIN_TEMPLATE_PATHS = (UI_TEMPLATE_DIR / "btn_join_table.png",)
TABLE_TEMPLATE_PATHS = (
    LOBBY_TEMPLATE_DIR / "tea2_table.png",
    LOBBY_TEMPLATE_DIR / "tea2_table_one_seated.png",
)
START_TEMPLATE_PATHS = (LOBBY_TEMPLATE_DIR / "tea2_start_button.png",)
MEMBER_PANEL_TEMPLATE_PATHS = (LOBBY_TEMPLATE_DIR / "tea2_member_overlay.png",)

# ---------------------------------------------------------------------------
# 实测几何（1600x900 原生帧，2026-10-25 实机）
# ---------------------------------------------------------------------------
VERIFIED_FRAME_SIZE = (1600, 900)  # (width, height)

#: 桌位菱形（卡片主体）左上角与尺寸；相邻桌位组横向间距 534。
DIAMOND_ORIGIN = (124, 286)
DIAMOND_SIZE = (276, 152)
DIAMOND_DX = 534
DIAMOND_Y_TOL = 6
DIAMOND_X_TOL = 8
DIAMOND_MIN_SCORE = 0.85
#: 屏幕上最多同时出现的桌位组（实测 2 个，留 1 个余量防止误判越界）。
MAX_UNITS = 3

#: 每个桌位组上下两个座位中心的加号位置（桌位组 0 的实测值）。
SLOT_CENTERS = ((116, 268), (412, 468))
SLOT_DX = DIAMOND_DX
SLOT_TOL = 10

#: 空座卡片（浅棕圆角卡 90x112）与白色加号（35x35）的实测尺寸。
SEAT_CARD_SIZE = (90, 112)
SEAT_CARD_WH_TOL = 5
PLUS_SIZE = (35, 35)
PLUS_SIZE_TOL = 3
PLUS_MIN_CROSS = 0.90   # 横/竖中线填充率
PLUS_MAX_CORNER = 0.06  # 四角必须是空的（排除方块/文字）
TABLE_BAND = (40, 190, 1460, 560)  # x0,y0,x1,y1 只在桌位区找空座

JOIN_MIN_SCORE = 0.97

#: 等候室的橙色「开始游戏」（既有 ``lobby_start.START_BUTTON_BOX`` 同源实测；
#: 模板是 274x100，盒子必须正好 274x100，否则 ``_roi_score`` 会因为尺寸不符记 0）。
START_BUTTON_BOX = (663, 525, 937, 625)
START_CENTER = (800, 574)
START_MIN_SCORE = 0.95
#: 橙「开始游戏」在位时橙色占比 0.72（白色「开始游戏」四个字不橙），
#: 按过之后掉到 0.00，阈值取中间。
START_MIN_ORANGE = 0.55
ORANGE_HSV_LOW = (8, 120, 150)
ORANGE_HSV_HIGH = (28, 255, 255)
#: 开始按钮被按过之后，左右两个按钮仍在原位（用于区分「已按开始」与「未知画面」）。
INVITE_BOX = (319, 526, 592, 624)
COPY_BOX = (1004, 526, 1278, 624)
GREEN_HSV_LOW = (52, 90, 160)
GREEN_HSV_HIGH = (72, 255, 255)
TEAL_HSV_LOW = (68, 90, 160)
TEAL_HSV_HIGH = (90, 255, 255)
SIDE_MIN_FILL = 0.45

#: 入座后成员列表会自动弹出（挡住左半屏），实测要先用点空地关掉才能稳定点开始；
#: 遮挡时本模块不给开始计划（这一屏的「开始」点击没做过实机验证）。
MEMBER_PANEL_ROI = (0, 0, 485, 120)
MEMBER_PANEL_MIN_SCORE = 0.97
MEMBER_PANEL_CLOSE_POINT = (832, 768)  # 实测：点空地即关，落在绿毡空处

#: 房间顶部「玉姐的比赛场(888) / 口令 974333」红橙横幅。
HEADER_ROI = (400, 10, 800, 50)
HEADER_HSV_LOW = (5, 150, 170)
HEADER_HSV_HIGH = (22, 255, 255)
HEADER_MIN_FILL = 0.80
#: 右下角圆形「功能」按钮（绿色圆盘），房间态独有。
FUNC_ROI = (1440, 750, 1600, 900)
FUNC_HSV_LOW = (35, 80, 80)
FUNC_HSV_HIGH = (85, 255, 255)
FUNC_MIN_AREA = 2500
FUNC_EXPECTED_CENTER = (1528, 825)
FUNC_TOL = 45

_TEMPLATE_CACHE: dict[str, np.ndarray | None] = {}


def center(box) -> tuple[int, int]:
    x0, y0, x1, y1 = (int(v) for v in box[:4])
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def _load_gray(paths: tuple[Path, ...]) -> np.ndarray | None:
    """按顺序取第一个能读到的灰度模板；OpenCV 的 Windows 接口不认中文路径。"""
    for path in paths:
        key = str(path)
        if key in _TEMPLATE_CACHE:
            if _TEMPLATE_CACHE[key] is not None:
                return _TEMPLATE_CACHE[key]
            continue
        template = None
        if path.is_file():
            try:
                raw = np.fromfile(path, dtype=np.uint8)
                template = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
            except OSError:
                template = None
        _TEMPLATE_CACHE[key] = template
        if template is not None and template.size:
            return template
    return None


def _roi_score(gray: np.ndarray, template: np.ndarray,
               box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    if y1 > gray.shape[0] or x1 > gray.shape[1]:
        return 0.0
    crop = gray[y0:y1, x0:x1]
    if crop.shape != template.shape:
        return 0.0
    return float(cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)[0, 0])


def _fill(hsv: np.ndarray, low, high) -> float:
    if hsv.size == 0:
        return 0.0
    mask = cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))
    return float(mask.mean() / 255.0)


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeatCandidate:
    """一个可点的空座「+」。``center`` 就是加号中心 = 实测点击点。"""

    box: tuple[int, int, int, int]
    center: tuple[int, int]
    unit: int
    slot: str  # "upper" | "lower"
    join_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": list(self.box),
            "center": list(self.center),
            "unit": self.unit,
            "slot": self.slot,
            "join_score": round(self.join_score, 4),
        }


@dataclass(frozen=True)
class RoomState:
    present: bool
    reason: str
    #: 版式里有人数/桌数两行，但数字是美术字、未标定，因此恒为 None（不猜）。
    player_count: int | None = None
    table_count: int | None = None
    counts_reason: str = ""
    seats: tuple[SeatCandidate, ...] = ()
    #: 空座所在的最左桌位组里，非空的座位槽位（"upper"/"lower"）。
    occupied_slots: tuple[str, ...] = ()
    units: int = 0
    start_box: tuple[int, int, int, int] | None = None
    start_score: float = 0.0
    waiting_room: bool = False
    waiting_start_pressed: bool = False
    member_panel_open: bool = False
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "reason": self.reason,
            "player_count": self.player_count,
            "table_count": self.table_count,
            "counts_reason": self.counts_reason,
            "units": self.units,
            "seats": [seat.as_dict() for seat in self.seats],
            "occupied_slots": list(self.occupied_slots),
            "start_box": list(self.start_box) if self.start_box else None,
            "start_center": list(center(self.start_box)) if self.start_box else None,
            "start_score": round(self.start_score, 4),
            "waiting_room": self.waiting_room,
            "waiting_start_pressed": self.waiting_start_pressed,
            "member_panel_open": self.member_panel_open,
            **self.evidence,
        }


@dataclass(frozen=True)
class StartPlan:
    """下一步要点的控件。``action`` 只有两种取值。"""

    action: str  # "join_table" | "start_game"
    point: tuple[int, int]
    box: tuple[int, int, int, int] | None
    reason: str
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "point": list(self.point),
            "box": list(self.box) if self.box else None,
            "reason": self.reason,
            **self.evidence,
        }


def _detect_diamonds(gray: np.ndarray) -> list[dict]:
    """找桌位菱形（空桌 + 已坐人都算），返回按 x 排序的命中列表。"""
    template = _load_gray(TABLE_TEMPLATE_PATHS)
    best: dict[int, dict] = {}
    if template is None:
        return []
    for path in TABLE_TEMPLATE_PATHS:
        candidate = _load_gray((path,))
        if candidate is None:
            continue
        result = cv2.matchTemplate(gray, candidate, cv2.TM_CCOEFF_NORMED)
        height, width = candidate.shape[:2]
        peaks = cv2.dilate(result, np.ones((max(3, height // 3),
                                           max(3, width // 3)), np.uint8))
        ys, xs = np.where((result >= DIAMOND_MIN_SCORE) & (result == peaks))
        for y, x in zip(ys, xs):
            score = float(result[y, x])
            slot = int(x + width // 2) // DIAMOND_DX
            if slot not in best or score > best[slot]["score"]:
                best[slot] = {"box": (int(x), int(y), int(x) + width, int(y) + height),
                              "score": round(score, 4),
                              "template": path.name}
    return [best[key] for key in sorted(best)]


def _lattice_unit(x0: int) -> int | None:
    """把菱形左边界折算成桌位组序号；对不上栅格就返回 None（fail-closed）。"""
    offset = x0 - DIAMOND_ORIGIN[0]
    unit = int(round(offset / DIAMOND_DX))
    if unit < 0 or unit >= MAX_UNITS:
        return None
    if abs(offset - unit * DIAMOND_DX) > DIAMOND_X_TOL:
        return None
    return unit


def _plus_glyphs(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """桌位区里的白色加号（35x35 十字），返回原图坐标系 box 列表。"""
    x0, y0, x1, y1 = TABLE_BAND
    x1, y1 = min(x1, image.shape[1]), min(y1, image.shape[0])
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 190)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white, 8)
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[index])
        if abs(width - PLUS_SIZE[0]) > PLUS_SIZE_TOL:
            continue
        if abs(height - PLUS_SIZE[1]) > PLUS_SIZE_TOL:
            continue
        if area < 380 or area > 640:
            continue
        glyph = (labels[y:y + height, x:x + width] == index).astype(np.uint8)
        cy, cx = height // 2, width // 2
        horizontal = float(glyph[max(0, cy - 2):cy + 3, :].mean())
        vertical = float(glyph[:, max(0, cx - 2):cx + 3].mean())
        corner = float(np.concatenate((
            glyph[:5, :5].ravel(), glyph[:5, -5:].ravel(),
            glyph[-5:, :5].ravel(), glyph[-5:, -5:].ravel(),
        )).mean())
        if not (horizontal >= PLUS_MIN_CROSS and vertical >= PLUS_MIN_CROSS
                and corner <= PLUS_MAX_CORNER):
            continue
        boxes.append((x0 + x, y0 + y, x0 + x + width, y0 + y + height))
    return boxes


def _slot_of(point: tuple[int, int]) -> tuple[int, str] | None:
    """把加号中心落到「桌位组 + 上/下座位」；不在栅格上返回 None。"""
    for unit in range(MAX_UNITS):
        for slot, (sx, sy) in zip(("upper", "lower"), SLOT_CENTERS):
            ex, ey = sx + unit * SLOT_DX, sy
            if abs(point[0] - ex) <= SLOT_TOL and abs(point[1] - ey) <= SLOT_TOL:
                return unit, slot
    return None


def _detect_seats(image: np.ndarray, gray: np.ndarray,
                  units: int) -> tuple[list[SeatCandidate], list[str], dict]:
    """在已确认的桌位组栅格上找空座。非空槽位记进 ``occupied``（不猜是谁）。"""
    template = _load_gray(JOIN_TEMPLATE_PATHS)
    evidence: dict[str, Any] = {"plus_glyphs": [], "rejected": []}
    seat_cards: list[tuple[tuple[int, int, int, int], float]] = []
    if template is not None:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        tan = cv2.inRange(hsv, np.array((10, 40, 170), np.uint8),
                          np.array((30, 120, 250), np.uint8))
        count, _labels, stats, _ = cv2.connectedComponentsWithStats(tan, 8)
        for index in range(1, count):
            x, y, width, height, area = (int(v) for v in stats[index])
            if abs(width - SEAT_CARD_SIZE[0]) > SEAT_CARD_WH_TOL:
                continue
            if abs(height - SEAT_CARD_SIZE[1]) > SEAT_CARD_WH_TOL:
                continue
            if area < 5000:
                continue
            box = (x, y, x + width, y + height)
            if not (TABLE_BAND[0] <= x and x + width <= TABLE_BAND[2]):
                continue
            if not (TABLE_BAND[1] <= y and y + height <= TABLE_BAND[3]):
                continue
            score = _roi_score(gray, template, box)
            if score < JOIN_MIN_SCORE:
                evidence["rejected"].append(
                    {"box": list(box), "join_score": round(score, 4),
                     "why": "空座卡模板分数不足"})
                continue
            seat_cards.append((box, score))

    seats: list[SeatCandidate] = []
    pluses = _plus_glyphs(image)
    evidence["plus_glyphs"] = [list(box) for box in pluses]
    for box, score in seat_cards:
        plus_point = (box[0] + SEAT_CARD_SIZE[0] // 2,
                      box[1] + 47)  # 实测加号中心比卡片竖向中心高 10px
        nearby = [p for p in pluses
                  if abs(center(p)[0] - plus_point[0]) <= 8
                  and abs(center(p)[1] - plus_point[1]) <= 8]
        if not nearby:
            evidence["rejected"].append(
                {"box": list(box), "why": "卡片里没有加号（背景板/其它卡）"})
            continue
        point = center(nearby[0])
        slot = _slot_of(point)
        if slot is None or slot[0] >= units:
            evidence["rejected"].append(
                {"box": list(box), "center": list(point),
                 "why": "空座不在已确认的桌位组栅格上"})
            continue
        seats.append(SeatCandidate(box=box, center=point, unit=slot[0],
                                   slot=slot[1], join_score=score))
    seats.sort(key=lambda item: (item.center[0], item.center[1]))

    occupied: list[str] = []
    if seats:
        unit = seats[0].unit
        found = {seat.slot for seat in seats if seat.unit == unit}
        occupied = [slot for slot in ("upper", "lower") if slot not in found]
    return seats, occupied, evidence


def _read_member_panel(gray: np.ndarray) -> float:
    template = _load_gray(MEMBER_PANEL_TEMPLATE_PATHS)
    if template is None:
        return 0.0
    return _roi_score(gray, template, MEMBER_PANEL_ROI)


def _read_waiting_room(image: np.ndarray, gray: np.ndarray) -> dict:
    """等候室三联按钮版式：橙「开始游戏」是否还在。"""
    template = _load_gray(START_TEMPLATE_PATHS)
    score = 0.0 if template is None else _roi_score(gray, template, START_BUTTON_BOX)
    x0, y0, x1, y1 = START_BUTTON_BOX
    orange = _fill(cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV),
                   ORANGE_HSV_LOW, ORANGE_HSV_HIGH)
    side = {}
    for name, box, low, high in (
            ("invite", INVITE_BOX, GREEN_HSV_LOW, GREEN_HSV_HIGH),
            ("copy", COPY_BOX, TEAL_HSV_LOW, TEAL_HSV_HIGH)):
        bx0, by0, bx1, by1 = box
        side[name] = round(_fill(
            cv2.cvtColor(image[by0:by1, bx0:bx1], cv2.COLOR_BGR2HSV),
            low, high), 3)
    start_ready = score >= START_MIN_SCORE and orange >= START_MIN_ORANGE
    layout = all(value >= SIDE_MIN_FILL for value in side.values())
    return {
        "start_score": round(score, 4),
        "start_orange_fill": round(orange, 3),
        "side_fills": side,
        "start_ready": bool(start_ready),
        "layout": bool(layout),
        "waiting_room": bool(start_ready or layout),
        "start_pressed": bool(layout and not start_ready),
    }


def read_lobby_room(image: np.ndarray | None) -> RoomState:
    """识别「比赛场房间」并列出空座；不是这一屏就 ``present=False``。"""
    counts_reason = ("顶部人数/桌数为美术字，未做 OCR 标定；"
                     "按开发档案口径，大厅人数/桌数不得用来推断桌内人数，故不读也不猜")
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return RoomState(False, "画面不可用", counts_reason=counts_reason)
    # 像素类型必须先过闸：``cv2.matchTemplate`` 遇到 CV_32F 帧和 CV_8U 模板会抛
    # OpenCV 断言（实测 float32 帧直接崩在读帧阶段，而不是 fail-closed）。
    # 本模块所有阈值/HSV 都是按 uint8 标定的，其它 dtype 一律不认。
    if image.dtype != np.uint8:
        return RoomState(False, f"帧像素类型未验证 {image.dtype}",
                         counts_reason=counts_reason)
    height, width = image.shape[:2]
    if (width, height) != VERIFIED_FRAME_SIZE:
        return RoomState(False, f"帧尺寸未验证 {width}x{height}", counts_reason=counts_reason)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    hx0, hy0, hx1, hy1 = HEADER_ROI
    header_fill = _fill(hsv[hy0:hy1, hx0:hx1], HEADER_HSV_LOW, HEADER_HSV_HIGH)

    fx0, fy0, fx1, fy1 = FUNC_ROI
    green = cv2.inRange(hsv[fy0:fy1, fx0:fx1], np.array(FUNC_HSV_LOW, np.uint8),
                        np.array(FUNC_HSV_HIGH, np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(green, 8)
    func_area, func_center = 0, (0, 0)
    for index in range(1, count):
        area = int(stats[index][cv2.CC_STAT_AREA])
        if area > func_area:
            func_area = area
            func_center = (fx0 + int(centroids[index][0]), fy0 + int(centroids[index][1]))
    func_ok = (func_area >= FUNC_MIN_AREA
               and abs(func_center[0] - FUNC_EXPECTED_CENTER[0]) <= FUNC_TOL
               and abs(func_center[1] - FUNC_EXPECTED_CENTER[1]) <= FUNC_TOL)

    diamonds = _detect_diamonds(gray)
    units, off_lattice = 0, []
    for hit in diamonds:
        unit = _lattice_unit(hit["box"][0])
        if unit is None:
            off_lattice.append(hit)
            continue
        units = max(units, unit + 1)
    if diamonds:
        # 最左菱形必须是桌位组 0（桌位从最左开始填），否则栅格口径已失效。
        first = _lattice_unit(diamonds[0]["box"][0])
        if first != 0:
            off_lattice = diamonds
            units = 0

    evidence: dict[str, Any] = {
        "header_fill": round(header_fill, 3),
        "func_area": func_area,
        "func_center": list(func_center),
        "diamonds": [{"box": list(hit["box"]), "score": hit["score"],
                      "template": hit["template"]} for hit in diamonds],
        "off_lattice": [list(hit["box"]) for hit in off_lattice],
    }
    waiting = _read_waiting_room(image, gray)
    member_score = _read_member_panel(gray)
    member_open = member_score >= MEMBER_PANEL_MIN_SCORE
    evidence["waiting"] = waiting
    evidence["member_panel_score"] = round(member_score, 4)

    if waiting["waiting_room"]:
        if member_open:
            detail = ("橙色开始游戏在位，但成员列表挡住了左半屏；"
                      f"先点空地 {MEMBER_PANEL_CLOSE_POINT} 关掉面板再点开始")
        else:
            detail = ("橙色开始游戏 " + ("在位" if waiting["start_ready"] else "已按过")
                      + f"（模板 {waiting['start_score']:.3f}，橙色占比 "
                        f"{waiting['start_orange_fill']:.2f}）")
        return RoomState(
            False,
            "等候室：" + detail,
            counts_reason=counts_reason,
            start_box=(START_BUTTON_BOX
                       if waiting["start_ready"] and not member_open else None),
            start_score=waiting["start_score"],
            waiting_room=True,
            waiting_start_pressed=waiting["start_pressed"],
            member_panel_open=member_open,
            evidence=evidence,
        )

    anchors = {
        "header": header_fill >= HEADER_MIN_FILL,
        "func": bool(func_ok),
        "table": units > 0,
    }
    evidence["anchors"] = anchors
    if not all(anchors.values()):
        return RoomState(
            False,
            (f"不是比赛场房间：锚点 {anchors}（横幅 {header_fill:.2f}、"
             f"功能键 area={func_area}@{func_center}、桌位组 {units}）"),
            counts_reason=counts_reason,
            evidence=evidence,
        )

    seats, occupied, seat_evidence = _detect_seats(image, gray, units)
    evidence.update(seat_evidence)
    return RoomState(
        True,
        (f"比赛场房间：{units} 个桌位组、{len(seats)} 个空座"
         + (f"，最左桌位组非空槽位 {list(occupied)}" if occupied else "")),
        counts_reason=counts_reason,
        seats=tuple(seats),
        occupied_slots=tuple(occupied),
        units=units,
        start_box=None,
        start_score=waiting["start_score"],
        waiting_room=False,
        evidence=evidence,
    )


def plan_start(image: np.ndarray | None) -> StartPlan | None:
    """给下一步该点的控件；证据不足返回 ``None``（绝不猜坐标）。"""
    state = read_lobby_room(image)
    if state.waiting_room:
        if state.start_box is None:
            return None
        return StartPlan(
            action="start_game",
            point=START_CENTER,
            box=state.start_box,
            reason=(f"等候室橙色开始游戏在位（模板 {state.start_score:.3f}），"
                    f"点 {START_CENTER} 即本方按开始、等对手"),
            evidence={"start_score": round(state.start_score, 4),
                      "start_orange_fill": state.evidence["waiting"]["start_orange_fill"],
                      "state": "waiting_room"},
        )
    if not state.present or not state.seats:
        return None
    target = state.seats[0]  # 桌位从最左开始填，最左空座就是该坐的那一桌
    if target.unit != 0:
        # 最左桌位组没有空座却出现了更右边的空座：说明最左那桌已满，
        # 这时点右边的「+」会另建一桌，属于「不确定」，直接不给计划。
        return None
    kind = "已有一人，点它入座" if state.occupied_slots else "空桌位，点它建房并入座"
    return StartPlan(
        action="join_table",
        point=target.center,
        box=target.box,
        reason=(f"比赛场房间最左桌位组 {kind}：空座 {target.slot} "
                f"box={target.box} 加号中心 {target.center}（空座卡模板 "
                f"{target.join_score:.3f}）"),
        evidence={
            "state": "lobby_room",
            "unit": target.unit,
            "slot": target.slot,
            "join_score": round(target.join_score, 4),
            "occupied_slots": list(state.occupied_slots),
            "seat_count": len(state.seats),
            "join_existing": bool(state.occupied_slots),
            "join_rule_note": ("最左桌位组只有 1 个空座=加入已有桌；2 个空座=新建桌。"
                               "两种都点同一个「+」，但创建会占房卡，调用方需按房卡门禁决定"),
        },
    )
