"""画面指纹注册表：默认拒绝（fail-closed）的根。

## 设计原则

1. **未登记的画面一律拒绝。** :meth:`ScreenRegistry.identify` 只做"像不像
   已登记的某一屏"，不像就返回 ``screen_id=None``；上层 :mod:`uikit.resolve`
   拿到 None 直接 deny，**不做任何推断、不退化到"最后一屏"**。
2. **每个画面只能执行自己登记过的 action。** 即使画面认对了，动作名不在
   该画面的白名单里，同样 deny。这样"大厅的 + 号"永远不会被当成"结算页的
   返回大厅"。
3. **登记表里没有"点"这件事。** ActionSpec 只描述"去哪儿找、几何上限多少"，
   不产生点击；真正点不点由别的模块决定，本层只负责"看"。

## 指纹怎么算

每个画面登记一条指纹（``ScreenFingerprint``）：

* **6 条稳定 ROI 的 HSV 直方图**（8x4x4=128 桶，L1 归一）。ROI 选的是
  版式骨架：顶栏、底部带、左右边缘、中央框、中下带 —— 这些在同一个画面
  内几乎不随牌局状态变（手牌、弃牌只影响其中 1~2 条）。
* **整帧 9x8 缩略图的 dHash（64 bit）**，抓整体明暗结构。
* 相似度 ``score = 0.7 * 直方图交集均值 + 0.3 * (1 - 汉明距离/64)``。

识别时对每个已登记画面取"与该画面所有样本的最高分"，然后要求：
``best >= min_score`` **且** ``best - 第二名画面 >= min_margin``。
两个条件缺一不可 —— 只卡绝对分的系统在"两张都是绿桌"的帧上会乱认。

## 为什么用"样本"而不是一个质心

同一个画面在不同牌局状态下像素差异很大（手牌、弃牌、浮层）。用一个质心
去比，任何状态偏离都会掉分；登记多条**真实帧样本**、取最高分，等于把这
个画面的状态分布一起登记进来了。代价是内存，可以接受（每条指纹 ~3KB）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

from .palette import crop_roi, load_bgr, load_gray, validate_frame
from .shapes import (
    BADGE_LIMITS,
    BUTTON_LIMITS,
    CIRCLE_LIMITS,
    REGION_LIMITS,
    ShapeLimits,
)

__all__ = [
    "FRAME_SIZE",
    "ActionSpec",
    "ScreenProfile",
    "ScreenFingerprint",
    "ScreenMatch",
    "ScreenRegistry",
    "FINGERPRINT_ROIS",
    "MIN_SCREEN_SCORE",
    "MIN_SCREEN_MARGIN",
    "SCREEN_ACTIONS",
    "DEFAULT_EXEMPLARS",
    "TEMPLATE_DIR",
    "TAKEOVER_DIR",
    "fingerprint",
    "similarity",
    "build_default_registry",
    "forbidden_action",
]

Box = tuple[int, int, int, int]
Point = tuple[int, int]

#: 解析层只认可这一种帧尺寸。缩放会让所有实测几何失真，一律拒绝。
FRAME_SIZE = (1600, 900)

TAKEOVER_DIR = Path(__file__).resolve().parents[3] / "_takeover"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: 版式骨架 ROI：顶栏 / 底部带 / 左边缘 / 右边缘 / 中央框 / 中下带
FINGERPRINT_ROIS: tuple[Box, ...] = (
    (0, 0, 1600, 120),
    (0, 720, 1600, 900),
    (0, 120, 120, 720),
    (1480, 120, 1600, 720),
    (560, 300, 1040, 620),
    (300, 600, 1300, 760),
)
HIST_BINS = (8, 4, 4)
HIST_WEIGHT = 0.7
DHASH_WEIGHT = 0.3
#: 绝对分下限。实测同画面样本间 score 普遍 >0.93，跨画面 <0.85。
MIN_SCREEN_SCORE = 0.88
#: 第一名与第二名画面的最小差。两张"绿桌"画面（桌内等待 vs 对局）
#: 靠这条分开。
MIN_SCREEN_MARGIN = 0.015

#: 名字里带这些词的 action 一律不允许登记（解析层只做"看"，但这几个
#: 连"看"都不该成为合法动作名，防止以后有人顺手接上点击）。
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "recharge", "purchase", "buy", "pay", "share", "invite", "close_table",
    "充值", "购买", "支付", "分享", "邀请", "闭赛", "分享战绩",
)


def forbidden_action(name: str) -> str | None:
    """命中禁用词返回命中的词，否则 None。"""
    lowered = name.lower()
    for token in FORBIDDEN_TOKENS:
        if token.lower() in lowered or token in name:
            return token
    return None


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScreenFingerprint:
    """一条画面指纹。``hist`` 形状 (len(FINGERPRINT_ROIS), 128)。"""

    hist: np.ndarray
    dhash: int
    source: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "dhash": f"{self.dhash:016x}",
                "roi_hist_rois": len(self.hist)}


def _dhash(gray_small: np.ndarray) -> int:
    diff = gray_small[:, 1:] > gray_small[:, :-1]
    value = 0
    for bit in diff.reshape(-1):
        value = (value << 1) | int(bit)
    return value


def fingerprint(image: np.ndarray) -> ScreenFingerprint:
    """算一条指纹。调用方需先 :func:`palette.validate_frame`。"""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"fingerprint 需要 BGR 帧，收到 {getattr(image, 'shape', None)}")
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    rows = []
    for roi in FINGERPRINT_ROIS:
        patch = crop_roi(hsv, roi)
        hist = cv2.calcHist([patch], [0, 1, 2], None, list(HIST_BINS),
                            [0, 180, 0, 256, 0, 256]).reshape(-1).astype(np.float32)
        total = float(hist.sum())
        rows.append(hist / total if total > 0 else hist)
    gray = cv2.cvtColor(cv2.resize(image, (9, 8), interpolation=cv2.INTER_AREA),
                        cv2.COLOR_BGR2GRAY)
    return ScreenFingerprint(hist=np.stack(rows), dhash=_dhash(gray))


def similarity(left: ScreenFingerprint, right: ScreenFingerprint) -> tuple[float, float, float]:
    """返回 (综合分, 直方图分, dHash 分)，全部 0~1。"""
    if left.hist.shape != right.hist.shape:
        return (0.0, 0.0, 0.0)
    hist_sim = float(np.mean([np.minimum(a, b).sum() for a, b in zip(left.hist, right.hist)]))
    ham = bin(left.dhash ^ right.dhash).count("1")
    dhash_sim = 1.0 - ham / 64.0
    return (HIST_WEIGHT * hist_sim + DHASH_WEIGHT * dhash_sim, hist_sim, dhash_sim)


# ---------------------------------------------------------------------------
# 动作登记
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionSpec:
    """一个"可以看的目标"的定位方式。

    ``locator``：
      * ``color_shape`` —— 具名颜色 → 连通域 → 几何门槛（圆角矩形按钮/圆环）
      * ``color_region`` —— 具名颜色 → 大色块（牌桌本体、桌面、手牌带）
      * ``anchor`` —— 多尺度模板匹配（要求次名差）
    """

    name: str
    locator: str
    roi: Box
    color: str | None = None
    pick: str = "largest"
    limits: ShapeLimits | None = None
    min_candidates: int = 1
    expect_center: Box | None = None
    anchor_id: str | None = None
    anchor_min_score: float = 0.80
    anchor_min_margin: float = 0.03
    note: str = ""

    def __post_init__(self) -> None:
        if self.locator not in ("color_shape", "color_region", "anchor"):
            raise ValueError(f"{self.name}: 未知定位方式 {self.locator!r}")
        if self.locator in ("color_shape", "color_region") and not self.color:
            raise ValueError(f"{self.name}: {self.locator} 必须给 color")
        if self.locator == "anchor" and not self.anchor_id:
            raise ValueError(f"{self.name}: anchor 必须给 anchor_id")
        hit = forbidden_action(self.name)
        if hit:
            raise ValueError(f"{self.name}: 动作名命中禁用词 {hit!r}，解析层不登记")
        x0, y0, x1, y1 = self.roi
        if not (0 <= x0 < x1 <= FRAME_SIZE[0] and 0 <= y0 < y1 <= FRAME_SIZE[1]):
            raise ValueError(f"{self.name}: ROI {self.roi} 超出 {FRAME_SIZE}")
        if self.expect_center is not None:
            ex0, ey0, ex1, ey1 = self.expect_center
            if ex1 <= ex0 or ey1 <= ey0:
                raise ValueError(f"{self.name}: expect_center {self.expect_center} 非法")

    def limits_or(self, default: ShapeLimits) -> ShapeLimits:
        return self.limits or default

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "locator": self.locator,
            "roi": list(self.roi),
            "color": self.color,
            "pick": self.pick,
            "min_candidates": self.min_candidates,
            "expect_center": list(self.expect_center) if self.expect_center else None,
            "anchor_id": self.anchor_id,
            "limits": self.limits.as_dict() if self.limits else None,
            "note": self.note,
        }


@dataclass(frozen=True)
class ScreenProfile:
    screen_id: str
    actions: tuple[ActionSpec, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not self.screen_id:
            raise ValueError("screen_id 不能为空")
        names = [action.name for action in self.actions]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.screen_id}: action 名重复 {names}")

    def action(self, name: str) -> ActionSpec | None:
        for action in self.actions:
            if action.name == name:
                return action
        return None

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(action.name for action in self.actions)


@dataclass(frozen=True)
class ScreenMatch:
    """identify 的结果。``screen_id=None`` 表示拒绝。"""

    screen_id: str | None
    score: float
    runner_up: float
    margin: float
    runner_up_screen: str | None
    per_screen: dict = field(default_factory=dict)
    code: str = ""
    reason: str = ""
    source: str = ""

    @property
    def ok(self) -> bool:
        return self.screen_id is not None

    def as_dict(self) -> dict:
        return {
            "screen_id": self.screen_id,
            "score": round(self.score, 4),
            "runner_up": round(self.runner_up, 4),
            "margin": round(self.margin, 4),
            "runner_up_screen": self.runner_up_screen,
            "code": self.code,
            "reason": self.reason,
            "per_screen": {k: round(v, 4) for k, v in self.per_screen.items()},
        }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class ScreenRegistry:
    """画面指纹 + 动作白名单。构造完即可用；没有样本的画面永远认不出来。"""

    def __init__(self, *, min_score: float = MIN_SCREEN_SCORE,
                 min_margin: float = MIN_SCREEN_MARGIN,
                 frame_size: tuple[int, int] = FRAME_SIZE) -> None:
        self.min_score = float(min_score)
        self.min_margin = float(min_margin)
        self.frame_size = tuple(frame_size)
        self._profiles: dict[str, ScreenProfile] = {}
        self._prints: dict[str, list[ScreenFingerprint]] = {}
        self.missing_exemplars: list[str] = []
        self.missing_templates: list[str] = []
        self._templates: dict[str, np.ndarray] = {}

    # -- 登记 ---------------------------------------------------------
    def register_screen(self, profile: ScreenProfile) -> None:
        if profile.screen_id in self._profiles:
            raise ValueError(f"画面 {profile.screen_id} 已登记")
        self._profiles[profile.screen_id] = profile
        self._prints.setdefault(profile.screen_id, [])

    def add_exemplar(self, screen_id: str, image: np.ndarray, source: str = "") -> ScreenFingerprint:
        if screen_id not in self._profiles:
            raise KeyError(f"画面 {screen_id} 未登记，不能加样本")
        error = validate_frame(image, self.frame_size)
        if error:
            raise ValueError(f"{screen_id} 样本不可用：{error}")
        printed = fingerprint(image)
        printed = replace(printed, source=source)
        self._prints[screen_id].append(printed)
        return printed

    def add_exemplar_path(self, screen_id: str, path) -> bool:
        """读盘登记一条样本；读不到就记进 ``missing_exemplars`` 并返回 False。"""
        image = load_bgr(path)
        if image is None:
            self.missing_exemplars.append(str(path))
            return False
        error = validate_frame(image, self.frame_size)
        if error:
            self.missing_exemplars.append(f"{path}({error})")
            return False
        self.add_exemplar(screen_id, image, source=str(path))
        return True

    def add_anchor_template(self, anchor_id: str, path) -> bool:
        template = load_gray(path)
        if template is None:
            self.missing_templates.append(str(path))
            return False
        self._templates[anchor_id] = template
        return True

    _templates: dict[str, np.ndarray]

    def template(self, anchor_id: str) -> np.ndarray | None:
        return self._templates.get(anchor_id)

    # -- 查询 ---------------------------------------------------------
    def screens(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def allowed_actions(self, screen_id: str) -> tuple[str, ...]:
        profile = self._profiles.get(screen_id)
        return profile.action_names if profile else ()

    def action(self, screen_id: str, name: str) -> ActionSpec | None:
        profile = self._profiles.get(screen_id)
        return profile.action(name) if profile else None

    def exemplar_count(self, screen_id: str) -> int:
        return len(self._prints.get(screen_id, ()))

    # -- 识别 ---------------------------------------------------------
    def identify(self, image: np.ndarray) -> ScreenMatch:
        error = validate_frame(image, self.frame_size)
        if error:
            return ScreenMatch(None, 0.0, 0.0, 0.0, None, code="frame_unverified", reason=error)
        usable = {sid: prints for sid, prints in self._prints.items() if prints}
        if not usable:
            return ScreenMatch(
                None, 0.0, 0.0, 0.0, None, code="registry_empty",
                reason="注册表里没有任何可用样本",
            )
        probe = fingerprint(image)
        best_per_screen: dict[str, float] = {}
        best_source = ""
        for screen_id, prints in usable.items():
            top = -1.0
            for candidate in prints:
                score = similarity(probe, candidate)[0]
                if score > top:
                    top, best_source = score, candidate.source
            best_per_screen[screen_id] = top
        ranked = sorted(best_per_screen.items(), key=lambda item: -item[1])
        best_screen, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        second_screen = ranked[1][0] if len(ranked) > 1 else None
        margin = best_score - second_score
        if best_score < self.min_score:
            return ScreenMatch(
                None, best_score, second_score, margin, second_screen,
                per_screen=best_per_screen, code="screen_unknown",
                reason=(f"最高分 {best_score:.3f} < {self.min_score}（最像 "
                        f"{best_screen}），按未登记画面拒绝"),
                source=best_source,
            )
        if margin < self.min_margin:
            return ScreenMatch(
                None, best_score, second_score, margin, second_screen,
                per_screen=best_per_screen, code="screen_ambiguous",
                reason=(f"{best_screen} 与 {second_screen} 分差 {margin:.3f} < "
                        f"{self.min_margin}，认不准就拒绝"),
                source=best_source,
            )
        return ScreenMatch(
            best_screen, best_score, second_score, margin, second_screen,
            per_screen=best_per_screen, code="ok", reason="", source=best_source,
        )


# ---------------------------------------------------------------------------
# 具体画面的动作表（全部是"看"，没有"点"）
# ---------------------------------------------------------------------------
LOBBY_ROI_TABLE: Box = (60, 240, 520, 520)
LOBBY_ROI_PLUS_LEFT: Box = (40, 200, 200, 360)
LOBBY_ROI_PLUS_TABLE: Box = (330, 400, 500, 560)

SCREEN_ACTIONS: dict[str, ScreenProfile] = {
    "lobby": ScreenProfile(
        screen_id="lobby",
        note="比赛场选桌页（_takeover/probe/now_5556.png 这一屏）：橙色顶栏 + "
             "米黄地板 + 中间菱形牌桌 + 两处「+」空位。",
        actions=(
            ActionSpec(
                name="table_body", locator="color_region", color="cyan_cloth",
                roi=LOBBY_ROI_TABLE, pick="largest", limits=REGION_LIMITS,
                expect_center=(120, 280, 460, 480),
                note="牌桌本体：用青色桌布的最大色块定位。菱形只占外接矩形 39%，"
                     "所以 REGION_LIMITS 的 fill 下界放到 0.15。",
            ),
            ActionSpec(
                name="seat_plus_left", locator="color_shape", color="beige_panel",
                roi=LOBBY_ROI_PLUS_LEFT, pick="largest", limits=BADGE_LIMITS,
                expect_center=(60, 210, 180, 330),
                note="左上邀请位（米色卡片本体，实测 box=(70,221,160,311)，"
                     "中心 (115,266)，与 lobby_seat 已验收的落点 (115,267) 一致）。",
            ),
            ActionSpec(
                name="seat_plus_table", locator="color_shape", color="beige_panel",
                roi=LOBBY_ROI_PLUS_TABLE, pick="largest", limits=BADGE_LIMITS,
                expect_center=(350, 410, 470, 530),
                note="桌右下加桌位（实测 box=(366,421,456,511)，中心 (411,466)，"
                     "与 lobby_seat 的 (411,467) 一致）。",
            ),
        ),
    ),
    "room": ScreenProfile(
        screen_id="room",
        note="桌内等待页（_takeover/shot_5556_room.png）：整片绿桌 + 中央「开始游戏」。",
        actions=(
            ActionSpec(
                name="start_button", locator="color_shape", color="orange_button",
                roi=(480, 460, 1150, 700), pick="largest", limits=BUTTON_LIMITS,
                expect_center=(520, 480, 1100, 680),
                note="橙色「开始游戏」按钮，实测 box=(663,525,937,624)。"
                     "只定位，不点 —— 起局要与人数/规则确认联动，不归解析层。",
            ),
            ActionSpec(
                name="start_button_text", locator="anchor", anchor_id="room_start_button",
                roi=(600, 490, 1000, 660), anchor_min_score=0.80, anchor_min_margin=0.03,
                expect_center=(620, 500, 980, 650),
                note="同一颗按钮的文字锚点，用多尺度模板匹配交叉验证颜色+形状的结论。",
            ),
            ActionSpec(
                name="room_felt", locator="color_region", color="felt_green",
                roi=(700, 300, 1100, 520), pick="largest", limits=REGION_LIMITS,
                note="桌面绿区域，作为「这一屏确实是绿桌」的辅助证据。",
            ),
        ),
    ),
    "table": ScreenProfile(
        screen_id="table",
        note="对局页（_takeover/shot_5556_turn.png）：绿桌 + 手牌带 + 中间牌墩。",
        actions=(
            ActionSpec(
                name="table_felt", locator="color_region", color="felt_green",
                roi=(200, 150, 1400, 650), pick="largest", limits=REGION_LIMITS,
                note="牌桌桌面（桌面绿最大色块）。",
            ),
            ActionSpec(
                name="hand_band", locator="color_region", color="tile_face_cream",
                roi=(0, 720, 1600, 900), pick="union", limits=REGION_LIMITS,
                min_candidates=4,
                note="手牌带：底部奶油色牌面的并集外接框。只给「手牌在哪一带」，"
                     "不做任何牌面识别（那是 tile_vision 的事，精度要求完全不同）。",
            ),
            ActionSpec(
                name="claim_hu_option", locator="color_shape", color="red_badge",
                roi=(1120, 500, 1330, 690), pick="largest",
                limits=replace(CIRCLE_LIMITS, min_area=800),
                note="claim 面板「胡」的红色外环，实测 (1181,535)-(1304,657)。"
                     "绝大多数帧不在场 —— 不在场就该 deny，不能靠猜。",
            ),
            ActionSpec(
                name="claim_pass_option", locator="color_shape", color="teal_button",
                roi=(1340, 500, 1520, 690), pick="largest",
                limits=replace(CIRCLE_LIMITS, min_area=800),
                note="claim 面板「过」的青绿圆环，实测 (1385,552)-(1481,652)。",
            ),
        ),
    ),
    "match_end": ScreenProfile(
        screen_id="match_end",
        note="牌局结束页（_takeover/match_end_probe.png）：牌局结束标题 + 两张结算卡 + "
             "底部三按钮（返回大厅 / 分享战绩 / 继续游戏）。分享战绩**不登记**。",
        actions=(
            ActionSpec(
                name="leave_button", locator="color_shape", color="teal_button",
                roi=(150, 740, 1500, 900), pick="leftmost", limits=BUTTON_LIMITS,
                note="「返回大厅」：底部带里最靠左的青绿按钮。实测 "
                     "box=(246,777,554,873)，与 match_end.EXPECTED_LEAVE_CENTER 一致。"
                     "另一种版式的结算页里它整体右移，所以用 leftmost 而不是固定框。",
            ),
            ActionSpec(
                name="continue_button", locator="color_shape", color="orange_button",
                roi=(900, 740, 1500, 900), pick="rightmost",
                limits=replace(BUTTON_LIMITS, min_fill=0.50),
                note="「继续游戏」：底部带最靠右的橙色按钮。实测 "
                     "box=(1046,764,1413,873)，fill 只有 0.57（手指图标压在上面），"
                     "所以这一条把 fill 下界降到 0.50。",
            ),
        ),
    ),
}

#: 每个画面的真实帧样本（相对 ``_takeover``）。只放**人工核对过**的帧。
DEFAULT_EXEMPLARS: dict[str, tuple[str, ...]] = {
    "lobby": (
        "probe/now_5556.png",
        "probe/now_5558.png",
        "shot_5556_lobby.png",
        "shot_5558_lobby.png",
        "live_5556.png",
        "shot_5556_seat2.png",
    ),
    "room": (
        "shot_5556_room.png",
        "shot_5556_invite.png",
        "shot_5556_game.png",
        "waiting_now_5556.png",
        "state_after_ready_5556.png",
        "live_pair_emulator-5556.png",
    ),
    "table": (
        "shot_5556_turn.png",
        "shot_5556_claim.png",
        "live_emulator-5556.png",
        "round2_emulator-5556.png",
        "seam4_emulator-5556.png",
        "now2_emulator-5556.png",
    ),
    "match_end": (
        "match_end_probe.png",
        "shot_5556_now.png",
        "live_1806_emulator-5558.png",
        "shot_5558_now.png",
    ),
}

#: 锚点模板（相对 ``uikit/templates``）
DEFAULT_TEMPLATES: dict[str, str] = {
    "room_start_button": "room_start_button.png",
}


def build_default_registry(takeover_dir: Path | None = None,
                           registry: ScreenRegistry | None = None) -> ScreenRegistry:
    """用仓库里的真实帧构建默认注册表。

    样本缺失不抛异常，只记进 ``missing_exemplars`` —— 因为"少了几张样本"
    的正确后果是**认不出来（拒绝）**，而不是整个解析层崩掉。
    """
    root = Path(takeover_dir) if takeover_dir is not None else TAKEOVER_DIR
    reg = registry if registry is not None else ScreenRegistry()
    for profile in SCREEN_ACTIONS.values():
        if profile.screen_id not in reg.screens():
            reg.register_screen(profile)
    for screen_id, names in DEFAULT_EXEMPLARS.items():
        for name in names:
            reg.add_exemplar_path(screen_id, root / name)
    for anchor_id, name in DEFAULT_TEMPLATES.items():
        reg.add_anchor_template(anchor_id, TEMPLATE_DIR / name)
    return reg


def template_path(anchor_id: str) -> Path:
    return TEMPLATE_DIR / DEFAULT_TEMPLATES[anchor_id]
