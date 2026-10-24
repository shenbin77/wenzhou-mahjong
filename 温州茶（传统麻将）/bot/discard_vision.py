"""牌河 / 来牌读取：从一张 1600x900 原生 adb 帧里读出"谁刚打了哪张牌"。

为什么需要这一层
----------------
``action_policy`` 的所有吃/碰/杠/胡判定都以 :attr:`HandState.incoming_tile`
（别人刚打出的那张）为前提。没有它就只能"过"。本模块只负责**看**：不点击、
不写盘、不联网，也不做 OCR —— 只用几何定位 + ``tile_vision`` 已有的模板签名。

范围（只回答一个问题）
----------------------
给定一帧：桌上两个牌河各有几格、格子里是什么牌，以及**在我看的这一帧和上一帧
之间新出现的是哪一张、属于谁**。其余一律不管。

实测几何（不是估的）
--------------------
三局真机语料实测（``采集/assist_sessions/`` 下的 20261024_140818 /
20261023_201640 / 20261024_122607，均为 ``live/tea_2p``，1600x900）：

* **对手牌河**：牌面占 ``y = 231..306``（高 75px），**右端锚定 x1 = 1249**，
  增长时向**左**延伸。1 格时 x=1194，10 格时 x=688..1249。
* **自己牌河**：牌面占 ``y = 485..560``，**左端锚定 x0 = 351**，
  增长时向**右**延伸。1 格时 x=351，12 格时 x=351..1081。
* 节距实测 56.15px（351→407→464→520→576→632→689→801→857→914→970→1026，
  缺格处间距是节距的整数倍）。牌面宽 55px，格间分隔列只有 1–2px。
* 因此：**对手牌河最左那张、自己牌河最右那张就是最新打出的一张。**
* **归属**由手牌张数交叉验证，不靠颜色猜：自己牌河 +1 的那一帧闭手张数恰好
  17→16（我刚打出一张，如 ``20261023_205407``）；对手牌河 +1 的那一帧闭手
  张数 16→17（我刚摸牌，如 ``20261023_205409``）。且自己牌河离暗手（y≥761）
  只有 283px，对手牌河离暗手 537px。

牌河**有空洞**，这是实测结论而不是缺陷
--------------------------------------
实测 ``20261023_205610`` 起连续 3 帧，对手牌河占位恒为
``[857, 913, 969, 1082, 1138, 1194]`` —— 缺 1026 这一格，且此后新牌继续
往后（更左）排，**不回填空洞**；自己牌河同样有 ``[351, 407, 464, 576, 689]``
缺 520/632 两格。缺的格子是被吃/碰/杠鸣走的那张：牌离开牌河，但其余格子不
重新流动。所以本模块**不要求牌河连续**，只要求"缺格宽度是节距的整数倍"。

因此格子网格是**从锚定边反推**的（锚定边实测逐帧不变），而不是从任意一端数：
一张被鸣走的牌死在中间不会影响"最左/最右那张是谁"。

必须排除的干扰源
----------------
右侧「限时免费求财运」、左侧「请神 / 起 +591 / 王姐头像」、右上角「捕鱼大唐渔场」
鲨鱼动画、桌面中央骰子立方体、行动圆盘（y≈596–620）都在动，也都比牌河"热闹"。
本模块用三层把它们挡在外面：

1. **牌桌门禁**：先过 ``mode_vision.detect_tea_player_count == 2``（标题 + 规则
   行同时命中）。广告/结算/大厅帧直接弃权，不进牌河解析 —— 这正是"把广告动画
   当成倒计时"那次事故的根因，先复核画面身份再谈读数。
2. **固定几何窗**：只解析上面两条实测 y 带，x 限制在 ``316..1300``
   （左侧「起 +591」气泡实测在 x=12..145,y=482..534，右侧麦克风/聊天/求财运
   都在 x>1300）。骰子立方体（y≈308–441）、行动圆盘（y≈596–620）、暗手
   （y≥761）都在窗外。
3. **牌面颜色 + 网格校验**：牌面低饱和高亮奶油色（实测 S≈22/V≈230），绿桌面
   S≈176/V≈74、金色广告 S 很高，分得开；再要求格子宽度、节距、锚定边三项
   都符合实测值，任何一项不符即弃权。

Fail-closed 契约
----------------
* 读不出来 → ``incoming_tile=None``，``reason`` 用中文写明原因，绝不猜。
* **默认要求前后两帧**：单帧只能知道"谁是这条河最新的牌"，不能知道"它是不是
  刚打出来的"。把陈旧的一张当来牌，会让上层对着早就过去的那张去碰 —— 所以
  ``previous=None`` 时一律不给 ``incoming_tile``（牌河读数照给，供人工复核）。
* 锚定边那一格不完整（牌半透明飞入中/被动画遮住）时，宁可不报最新一张。

本模块没有做、也不打算做的事
----------------------------
* 不点击任何东西（``洗牌``房卡×2、``切牌``房卡×4 这类会花钱的按钮永远不碰）。
* 不判断碰/吃/杠/胡是否合法（那是 ``action_policy`` 的活）。
* 不读"剩余张数"、不读"局数"、不读结算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np


BASE_DIR = Path(__file__).resolve().parent
#: 与 ``tile_vision.detect_hand`` 同一套模板，保证"牌河里的牌"和"手牌"同口径。
TEA_TEMPLATE_DIR = BASE_DIR.parent / "采集" / "templates" / "tea_tiles"

#: 本项目所有真机帧都是这个尺寸；别的尺寸一律弃权（不做跨分辨率缩放猜测）。
FRAME_WIDTH = 1600
FRAME_HEIGHT = 900

# --- 实测几何 --------------------------------------------------------------
# 两条牌河的解析窗 (x0, y0, x1, y1)，单位是原生像素。
# y 带 = 牌面 + 底部金边；x 窗按"实测最长牌河 + 余量"裁，把窗外的 UI 直接切掉：
#   * 左侧「起 +591」气泡实测 x=12..145, y=482..534（在 330 以左）
#   * 右侧浮动活动按钮（金橙色光圈）实测从 x≈1180 起、y≈525 起，会压进自己
#     牌河的下缘 —— 所以自己牌河的右界收在 1170（刚好切在按钮左侧），实测最长
#     自己牌河也只到 x1=1081，留够余量；网格万一还是撞到右界则收尾不弃权
#     （少算一张只是漏一次可鸣机会，不会产生错误来牌；对手那条河相反，一律弃权）
#   * 「无序/2人」文字实测在 y≈200..222，在对手牌河上界 225 之上
# 实测最长牌河：对手到槽位 11（x0=576），自己到槽位 12（x1=1081）。
_OPPONENT_BAND = (520, 225, 1260, 315)
_SELF_BAND = (330, 470, 1170, 560)

SOURCE_OPPONENT = "opponent"
SOURCE_SELF = "self"

# 牌面实测 y 带（不含牌底金边、不含牌面上方那条 7px 装饰亮边）。
# 对手 231..306、自己 485..560，高度都是 75px，三局 2865 帧逐帧一致。
_FACE_Y = {
    "opponent": (231, 306),
    "self": (485, 560),
}

# 锚定边：格子增减时**不动**的那一端（= 这条河最早打出的那张所在的一端）。
# 实测 1249 / 351 在三局 2865 帧里逐帧一致。
_OPPONENT_ANCHOR = 1249
_SELF_ANCHOR = 351
_ANCHOR_TOLERANCE = 4

# 单格实测尺寸：牌面宽 55px，节距 56.15px，牌面高 75px（见下方 _FACE_Y）。
_TILE_WIDTH = 55.0
_TILE_PITCH = 56.15
#: 网格向外最多走这么多格；实测最长 13 格，留一倍余量。
_MAX_SLOTS_PER_RIVER = 24
#: 连续这么多格空着就判定"牌河在这里断了"，不再往外走（实测连续空洞最多 1 格）。
_MAX_HOLE_RUN = 2
#: 网格外缘与实测牌面外缘的最大允许偏差。对不上说明最外面那张没读全
#: （牌在飞入动画中），此时"最新一张"可能是倒数第二张，必须弃权。
_FAR_EDGE_TOLERANCE = 12

# 奶油色牌面：实测 S≈22 / V≈230；绿桌面 S≈176 / V≈74。
_FACE_MAX_SAT = 110
_FACE_MIN_VAL = 170
#: 某一列里至少这么多行是奶油色才算"牌面列"。实测分隔列恒为 0–1，
#: 牌内最深笔画列实测最低 1（東/中这类整列贯通的竖笔），所以这个阈值只用来
#: **定整行的外框**，不用来切牌 —— 切牌交给网格。
_COL_MIN_FACE_ROWS = 12
#: 一个格子里至少这个比例的列是牌面列，才算"这里真有牌"（空洞与真牌的区别）。
_SLOT_MIN_FACE_RATIO = 0.55
#: 某一行的奶油像素要占该格宽的这个比例以上，才算这一行的牌面还在。
#: 取 0.32 而不是 0.5：最新一张牌的上方常压着一个金色菱形闪光标（实测会在
#: 牌顶盖住约 20 列、10 行），用 0.5 会把这张牌判成"只有 46 行高"从而丢掉最新
#: 一张 —— 正是最不能丢的那一张。
_ROW_MIN_FACE_RATIO = 0.32

#: 对手牌河上方那条窄带：实测"刚落下的那张牌"会在牌顶正上方闪一个金色菱形。
#: 它只在刚打出后的一两秒内出现（实测 20261023_205456 / 205501-205505 帧无标，
#: 205452 有标），所以**不能**拿来代替前后两帧；但它能独立指出"最新那张在哪一格"，
#: 用来交叉验证网格的结论 —— 网格说 A 格、闪光标画在 B 格，就弃权。
_OPPONENT_SPARKLE_BAND = (520, 196, 1260, 231)
_SPARKLE_HUE = (10, 35)
_SPARKLE_MIN_SAT = 140
_SPARKLE_MIN_VAL = 170
_SPARKLE_MIN_AREA = 80
_SPARKLE_MAX_WIDTH = 40
_SPARKLE_MATCH_TOLERANCE = 6

# 来牌比牌河里的普通牌更"要紧"（它决定要不要点碰/吃/杠/胡），所以单独收紧。
# 实证（本模块在真帧上的读数）：被接受的来牌 score 中位数 0.93、最低 0.849、
# margin 最低 0.130，故 0.86 / 0.10 不会误伤真读数。
INCOMING_MIN_SCORE = 0.86
INCOMING_MIN_MARGIN = 0.10

#: 牌河里的牌用 ``tile_vision`` 自己标定过的阈值（0.82 / 0.055），不另设一套，
#: 免得"同一张牌在手牌里认得、在牌河里认不得"。
RIVER_MIN_SCORE = 0.82
RIVER_MIN_MARGIN = 0.055

_VISION_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiverTile:
    """牌河里的一张牌（像素框 + 识别结果）。

    Attributes:
        index: 从左往右的序号（0 = 最左）。
        box: 像素框 ``(x0, y0, x1, y1)``，原生 1600x900 坐标。
        tile_id: ``tile_vision.TILE_IDS`` 的视觉编码（与 ``hand_ids`` 同口径）；
            没认出来是 ``None``。
        label: 视觉词汇（``w4`` / ``s8`` / ``dong`` ...）；没认出来是 ``None``。
        score: 模板 top1 分数。
        margin: top1 与次名的差。
        accepted: 是否通过阈值。
        reason: 分类器给的原因。
    """

    index: int
    box: tuple[int, int, int, int]
    tile_id: int | None
    label: str | None
    score: float
    margin: float
    accepted: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（台账用）。"""
        return {
            "index": self.index,
            "box": list(self.box),
            "tile_id": self.tile_id,
            "label": self.label,
            "score": round(float(self.score), 4),
            "margin": round(float(self.margin), 4),
            "accepted": bool(self.accepted),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiverReading:
    """一条牌河的读数。

    Attributes:
        name: ``"opponent"`` 或 ``"self"``。
        tiles: 从左往右排好的牌（空洞不在里面）。
        slots: 网格格数（含空洞）。
        holes: 空洞的格子序号（被鸣走的牌留下的空位）。
        pitch: 实测节距（``None`` 表示不足两格，量不出来）。
        anchor_x: 锚定边坐标（``None`` 表示这条河是空的）。
        far_x: 另一端的坐标（远离锚定边的那一头，用来判"是否往外长了一格"）。
        valid: 几何是否可信。``False`` 时上层必须弃权。
        reason: 几何结论或弃权原因（中文）。
    """

    name: str
    tiles: tuple[RiverTile, ...] = ()
    slots: int = 0
    holes: tuple[int, ...] = ()
    pitch: float | None = None
    anchor_x: int | None = None
    far_x: int | None = None
    valid: bool = False
    reason: str = ""

    @property
    def count(self) -> int:
        """牌河里有几张牌（只数真牌，不数空洞）。"""
        return len(self.tiles)

    @property
    def ids(self) -> tuple[int, ...]:
        """已确定的牌（视觉编码），未识别的跳过。"""
        return tuple(t.tile_id for t in self.tiles
                     if t.accepted and t.tile_id is not None)

    @property
    def newest(self) -> RiverTile | None:
        """这条河最新打出的一张。

        实测：对手牌河向左延伸（右端锚定 1249），自己牌河向右延伸
        （左端锚定 351），所以"最新"分别在两端。几何不可信时返回 ``None``。
        """
        if not self.valid or not self.tiles:
            return None
        return self.tiles[0] if self.name == SOURCE_OPPONENT else self.tiles[-1]

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（台账用）。"""
        return {
            "name": self.name,
            "count": self.count,
            "slots": self.slots,
            "holes": list(self.holes),
            "pitch": None if self.pitch is None else round(float(self.pitch), 2),
            "anchor_x": self.anchor_x,
            "far_x": self.far_x,
            "valid": bool(self.valid),
            "reason": self.reason,
            "tiles": [t.as_dict() for t in self.tiles],
        }


@dataclass(frozen=True)
class TableSnapshot:
    """一帧的两条牌河读数 —— 时序比对的基线。

    ``read_discards`` 的 ``previous`` 参数可以直接吃这个对象（或吃上一次返回的
    :class:`DiscardState`，它带着 ``table``），这样活循环里每帧只解一次像素。
    """

    opponent: RiverReading
    own: RiverReading
    player_count: int | None = None
    sparkles: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（台账用）。"""
        return {
            "player_count": self.player_count,
            "sparkles": list(self.sparkles),
            "opponent": self.opponent.as_dict(),
            "own": self.own.as_dict(),
        }


@dataclass(frozen=True)
class DiscardState:
    """一帧的牌河结论。

    Attributes:
        incoming_tile: 刚打出的那张牌（视觉编码，与 ``hand_ids`` 同口径）。
            **读不出来就是 ``None``**，绝不猜。
        incoming_source: 谁打出的 —— ``"opponent"`` / ``"self"`` / ``None``。
            归属已确认但牌没认出来时仍会给出归属，便于人工复核。
        river_tiles: 两个牌河里已确定的牌（对手在前、自己后，各自从左到右）。
        opponent_river: 对手牌河里已确定的牌（放铳风险要用）。
        own_river: 自己牌河里已确定的牌。
        reason: 中文结论或弃权原因，供人工复核。
        evidence: 几何/分数的原始证据（可 JSON 落盘）。
        table: 本帧的牌河读数，可直接作为下一次调用的 ``previous``。
    """

    incoming_tile: int | None = None
    incoming_source: str | None = None
    river_tiles: tuple[int, ...] = ()
    opponent_river: tuple[int, ...] = ()
    own_river: tuple[int, ...] = ()
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    table: TableSnapshot | None = field(default=None, repr=False, compare=False)

    @property
    def has_incoming(self) -> bool:
        """是否有可用的来牌读数（``incoming_tile`` 非 ``None``）。"""
        return self.incoming_tile is not None

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 落盘的字典（台账用）。"""
        return {
            "incoming_tile": self.incoming_tile,
            "incoming_source": self.incoming_source,
            "river_tiles": list(self.river_tiles),
            "opponent_river": list(self.opponent_river),
            "own_river": list(self.own_river),
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


# ---------------------------------------------------------------------------
# 识牌器（懒加载、可注入）
# ---------------------------------------------------------------------------


def load_vision(template_dir: str | Path | None = None):
    """拿一个已加载模板的 :class:`tile_vision.TileVision`（每进程缓存一份）。

    Args:
        template_dir: 模板目录；缺省用 ``采集/templates/tea_tiles``（与
            ``tile_vision.detect_hand`` 同一个）。

    Returns:
        TileVision 实例；模板缺失时抛 ``tile_vision.NoTemplatesError``
        （配置故障必须暴露，不能悄悄变成"读不出来"）。
    """
    from tile_vision import TileVision

    directory = Path(template_dir) if template_dir is not None else TEA_TEMPLATE_DIR
    key = str(directory)
    cached = _VISION_CACHE.get(key)
    if cached is None:
        cached = TileVision(template_dir=directory,
                            min_score=RIVER_MIN_SCORE,
                            min_margin=RIVER_MIN_MARGIN)
        cached.load_templates()
        _VISION_CACHE[key] = cached
    return cached


def _classify(crop: np.ndarray, vision) -> dict[str, Any]:
    """分类一张牌，任何异常都折成"没认出来"。"""
    if crop is None or crop.size == 0:
        return {"tile_id": None, "label": None, "score": 0.0, "margin": 0.0,
                "accepted": False, "reason": "empty_crop"}
    try:
        result = vision.classify_crop(crop, caishen_label=None)
    except Exception as exc:  # noqa: BLE001 - 单张失败不能拖垮整帧
        return {"tile_id": None, "label": None, "score": 0.0, "margin": 0.0,
                "accepted": False, "reason": f"classify_error:{type(exc).__name__}"}
    return {
        "tile_id": result.get("tile_id"),
        "label": result.get("label"),
        "score": float(result.get("score") or 0.0),
        "margin": float(result.get("margin") or 0.0),
        "accepted": bool(result.get("accepted")),
        "reason": str(result.get("reason") or ""),
    }


# ---------------------------------------------------------------------------
# 纯几何：把一条牌河切成网格
# ---------------------------------------------------------------------------


def _face_mask(band: np.ndarray) -> np.ndarray:
    """奶油色牌面掩码（``uint8`` 0/1）。"""
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    return ((val > _FACE_MIN_VAL) & (sat < _FACE_MAX_SAT)).astype(np.uint8)


def find_discard_sparkles(image: np.ndarray) -> tuple[int, ...]:
    """对手牌河上方金色菱形闪光标的中心 x 列表（原生坐标，可能为空）。

    这个闪光标是游戏自己画的"刚落下这张"指示，实测总落在最新那一格的正中间
    （20261023_205452 标在 x=939，该格 913..968；205530 标在 1220，该格
    1194..1249；205546 标在 1164，该格 1138..1193）。它只闪一两秒，所以只当
    交叉验证用，不当唯一依据。

    Args:
        image: 1600x900 BGR 帧。

    Returns:
        按 x 升序的中心坐标元组；没看到闪光标就是空元组。
    """
    if image is None or not isinstance(image, np.ndarray):
        return ()
    x0, y0, x1, y1 = _OPPONENT_SPARKLE_BAND
    height, width = image.shape[:2]
    if y1 > height or x1 > width:
        return ()
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    gold = ((hue >= _SPARKLE_HUE[0]) & (hue <= _SPARKLE_HUE[1])
            & (sat > _SPARKLE_MIN_SAT) & (val > _SPARKLE_MIN_VAL))
    mask = cv2.morphologyEx(gold.astype(np.uint8), cv2.MORPH_OPEN,
                            np.ones((3, 3), np.uint8))
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    centers: list[int] = []
    for index in range(1, count):
        _, _, box_w, _, area = stats[index]
        if area < _SPARKLE_MIN_AREA or box_w > _SPARKLE_MAX_WIDTH:
            continue
        centers.append(x0 + int(round(float(centroids[index][0]))))
    return tuple(sorted(centers))


def measure_river(band: np.ndarray, x_offset: int, y_offset: int, name: str,
                  anchor: int) -> RiverReading:
    """量一条牌河：从锚定边按固定节距往外走格子，逐格验牌面。

    网格从**锚定边**反推、节距是实测常数，所以中间少几格不影响"最新那张在哪"。
    走完之后再拿"实测牌面外缘"和"网格外缘"对一次账：对不上就说明最外面那张
    没读全（牌在飞入动画里、或被遮挡），此时最新一张可能是倒数第二张 —— 弃权。

    Args:
        band: ``image[y0:y1, x0:x1]`` 的彩色切片。
        x_offset: 切片在原帧里的 x 起点（把局部坐标还原成原生坐标）。
        y_offset: 切片在原帧里的 y 起点（同上）。
        name: ``"opponent"`` / ``"self"``。
        anchor: 该条河的锚定边实测坐标（1249 / 351）。

    Returns:
        :class:`RiverReading`，牌框是**原生帧坐标**。空牌河返回
        ``valid=True, slots=0``。
    """
    if band is None or band.size == 0:
        return RiverReading(name=name, valid=False, reason="空带")
    mask = _face_mask(band)
    band_width = int(mask.shape[1])

    # y 用实测常量带（对手 231..306、自己 485..560），并且**每帧验一遍**：
    # 带内至少 80% 的行要有牌面像素。这样既不怕牌面笔画把"逐格外框"量歪，
    # 又不会在布局真的变了的时候还照着老坐标硬读 —— 宁可弃权。
    face_y0, face_y1 = _FACE_Y[name]
    local_y0, local_y1 = face_y0 - y_offset, face_y1 - y_offset
    if local_y0 < 0 or local_y1 > mask.shape[0]:
        return RiverReading(name=name, valid=False, reason="实测牌面带超出解析窗")
    in_band = mask[local_y0:local_y1, :]
    column_face_rows = np.count_nonzero(in_band, axis=0)
    column_on = column_face_rows >= _COL_MIN_FACE_ROWS
    on_columns = int(np.count_nonzero(column_on))
    if on_columns == 0:
        return RiverReading(name=name, valid=True, slots=0, anchor_x=int(anchor),
                            reason="空牌河（未见牌面）")
    row_on = (np.count_nonzero(in_band[:, column_on], axis=1)
              >= on_columns * _ROW_MIN_FACE_RATIO)
    if float(np.count_nonzero(row_on)) < row_on.size * .8:
        return RiverReading(
            name=name, valid=False,
            reason=(f"实测牌面带 y={face_y0}..{face_y1} 里只有 "
                    f"{np.count_nonzero(row_on)}/{row_on.size} 行有牌面，布局可能变了"))

    tiles: list[RiverTile] = []
    holes: list[int] = []
    walked = 0
    trailing = 0
    for slot in range(_MAX_SLOTS_PER_RIVER):
        if name == SOURCE_OPPONENT:
            slot_x0 = anchor - _TILE_WIDTH - slot * _TILE_PITCH
        else:
            slot_x0 = anchor + slot * _TILE_PITCH
        slot_x0_int = int(round(slot_x0))
        slot_x1_int = int(round(slot_x0 + _TILE_WIDTH))
        crop_x0 = slot_x0_int - x_offset
        crop_x1 = slot_x1_int - x_offset
        if crop_x0 < 0 or crop_x1 > band_width:
            # 走出解析窗：如果上一格已经是空的了，牌河显然在这里就结束了，
            # 正常收尾。
            #
            # 上一格还有牌的时候要分两种：
            # * **对手牌河**：被切掉的可能是真正的最新一张，报出去就是错的，
            #   必须弃权。对手牌河的窗按实测最长 11 格留到 12 格，够用。
            # * **自己牌河**：窗右边是那个浮动活动按钮（实测 x≈1180 起），撑不
            #   过去。自己牌河只用来判"我是不是刚打过"，少算一张的后果是"漏一次
            #   可鸣机会"，不会产生错误来牌，所以这里收尾而不是弃权。
            if trailing >= 1 or name != SOURCE_OPPONENT:
                break
            return RiverReading(
                name=name, valid=False, slots=walked, holes=tuple(holes),
                anchor_x=int(anchor),
                reason=(f"牌河从第 {slot} 格起越出解析窗，实际比实测最长还长，"
                        "最新一张不可定位"))
        width = crop_x1 - crop_x0
        filled = int(np.count_nonzero(
            column_face_rows[crop_x0:crop_x1] >= _COL_MIN_FACE_ROWS))
        walked = slot + 1
        if filled < width * _SLOT_MIN_FACE_RATIO:
            holes.append(slot)
            trailing += 1
            # 连续太多空格 = 已经走出牌河了；空洞本身是合法的（被鸣走的牌），
            # 所以只按"连续"停，不按"总数"停。
            if trailing >= _MAX_HOLE_RUN:
                break
            continue
        trailing = 0
        tiles.append(RiverTile(
            index=slot, box=(slot_x0_int, int(face_y0), slot_x1_int, int(face_y1)),
            tile_id=None, label=None, score=0.0, margin=0.0, accepted=False,
            reason=""))

    if not tiles:
        return RiverReading(name=name, valid=True, slots=0,
                            reason="空牌河（锚定边起就没有完整牌面）")

    tiles.sort(key=lambda t: t.box[0])
    last_slot = max(t.index for t in tiles)
    run = 0
    for slot in range(last_slot + 1):
        run = run + 1 if slot in holes else 0
        if run >= _MAX_HOLE_RUN:
            return RiverReading(
                name=name, valid=False, slots=walked, holes=tuple(sorted(holes)),
                anchor_x=int(anchor),
                reason=f"牌河中间断了 {run} 格以上，最新一张不可定位")

    # 实测外缘 vs 网格外缘对账：防"最外面那张只画了一半"。
    on = column_face_rows >= _COL_MIN_FACE_ROWS
    outer = np.flatnonzero(on)
    if outer.size == 0:
        return RiverReading(name=name, valid=False, slots=walked,
                            reason="网格算出有牌但整行没有牌面列")
    if name == SOURCE_OPPONENT:
        envelope_far = x_offset + int(outer[0])
        grid_far = tiles[0].box[0]
        envelope_anchor = x_offset + int(outer[-1]) + 1
        grid_anchor = tiles[-1].box[2]
    else:
        envelope_far = x_offset + int(outer[-1]) + 1
        grid_far = tiles[-1].box[2]
        envelope_anchor = x_offset + int(outer[0])
        grid_anchor = tiles[0].box[0]
    if abs(grid_anchor - anchor) > _ANCHOR_TOLERANCE:
        return RiverReading(
            name=name, valid=False, slots=walked, holes=tuple(sorted(holes)),
            anchor_x=int(grid_anchor),
            reason=f"锚定边位移（实测 {grid_anchor}，应为 {anchor}），布局不可信")
    if abs(envelope_anchor - grid_anchor) > _FAR_EDGE_TOLERANCE:
        return RiverReading(
            name=name, valid=False, slots=walked, holes=tuple(sorted(holes)),
            anchor_x=int(anchor),
            reason=f"锚定端外缘对不上（牌面 {envelope_anchor} vs 网格 {grid_anchor}）")
    if abs(envelope_far - grid_far) > _FAR_EDGE_TOLERANCE:
        return RiverReading(
            name=name, valid=False, slots=walked, holes=tuple(sorted(holes)),
            anchor_x=int(anchor),
            reason=(f"最外一张没读全（牌面外缘 {envelope_far} vs 网格 {grid_far}），"
                    "最新一张不可定位"))

    return RiverReading(
        name=name, tiles=tuple(tiles), slots=walked, holes=tuple(sorted(holes)),
        pitch=_TILE_PITCH, anchor_x=int(anchor), far_x=int(grid_far), valid=True,
        reason=(f"实测 {walked} 格 / {len(tiles)} 张"
                + (f"（缺 {'/'.join(map(str, sorted(holes)))} 格）" if holes else "")))


def _with_identities(reading: RiverReading, image: np.ndarray, vision) -> RiverReading:
    """给一条已切好的牌河补上识牌结果（按帧一次性跑完）。"""
    if not reading.tiles:
        return reading
    tiles: list[RiverTile] = []
    for tile in reading.tiles:
        x0, y0, x1, y1 = tile.box
        result = _classify(image[y0:y1, x0:x1], vision)
        tiles.append(RiverTile(
            index=tile.index, box=tile.box,
            tile_id=result["tile_id"], label=result["label"],
            score=result["score"], margin=result["margin"],
            accepted=result["accepted"], reason=result["reason"],
        ))
    return RiverReading(
        name=reading.name, tiles=tuple(tiles), slots=reading.slots,
        holes=reading.holes, pitch=reading.pitch, anchor_x=reading.anchor_x,
        far_x=reading.far_x, valid=reading.valid, reason=reading.reason,
    )


# ---------------------------------------------------------------------------
# 帧 → 牌河读数
# ---------------------------------------------------------------------------


def snapshot_table(image: np.ndarray, *, vision=None,
                   template_dir: str | Path | None = None,
                   require_tea_table: bool = True) -> TableSnapshot:
    """把一帧解析成两条牌河读数（不含任何时序推断）。

    同样的像素给同样的结果，不读写任何外部状态。

    Args:
        image: 1600x900 BGR 帧。
        vision: 可注入的识牌器（测试用）；缺省懒加载 ``tea_tiles`` 模板。
        template_dir: 模板目录（仅在没有 ``vision`` 时生效）。
        require_tea_table: 是否先过"这张画面是不是温州茶 2 人牌桌"的门禁。

    Returns:
        :class:`TableSnapshot`。画面不可信时两条河的 ``valid`` 都是 ``False``，
        并在 ``reason`` 里写明原因。
    """
    if image is None or not isinstance(image, np.ndarray):
        bad = "没有画面"
        return TableSnapshot(RiverReading(name=SOURCE_OPPONENT, valid=False, reason=bad),
                             RiverReading(name=SOURCE_SELF, valid=False, reason=bad),
                             None)
    height, width = image.shape[:2]
    if (width, height) != (FRAME_WIDTH, FRAME_HEIGHT):
        reason = f"帧尺寸不是 {FRAME_WIDTH}x{FRAME_HEIGHT}（实测 {width}x{height}）"
        return TableSnapshot(RiverReading(name=SOURCE_OPPONENT, valid=False, reason=reason),
                             RiverReading(name=SOURCE_SELF, valid=False, reason=reason),
                             None)

    player_count: int | None = None
    if require_tea_table:
        from mode_vision import detect_tea_player_count
        player_count = detect_tea_player_count(image)
        if player_count != 2:
            reason = ("画面不是温州茶牌桌（标题/规则行未命中）" if player_count is None
                      else f"当前是 {player_count} 人牌桌，本模块只认 2 人布局")
            return TableSnapshot(RiverReading(name=SOURCE_OPPONENT, valid=False, reason=reason),
                                 RiverReading(name=SOURCE_SELF, valid=False, reason=reason),
                                 player_count)

    classifier = vision if vision is not None else load_vision(template_dir)
    ox0, oy0, ox1, oy1 = _OPPONENT_BAND
    opponent = measure_river(image[oy0:oy1, ox0:ox1], ox0, oy0,
                             SOURCE_OPPONENT, _OPPONENT_ANCHOR)
    sx0, sy0, sx1, sy1 = _SELF_BAND
    own = measure_river(image[sy0:sy1, sx0:sx1], sx0, sy0, SOURCE_SELF, _SELF_ANCHOR)
    return TableSnapshot(_with_identities(opponent, image, classifier),
                         _with_identities(own, image, classifier),
                         player_count, find_discard_sparkles(image))


# ---------------------------------------------------------------------------
# 两帧 → 来牌（纯函数，测试友好）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """一条牌河在两帧之间的变化。"""

    appended: int = 0
    removed: int = 0
    usable: bool = False
    reason: str = ""


def _change(cur: RiverReading, prev: RiverReading) -> _Change:
    """比较同一条牌河的前后读数。

    "新打出了牌"必须同时满足：**张数增加** + **外框往外伸了至少半格**。
    只看张数不够 —— 那可能是空洞被补上（此时最新一张并不在最外端，报出去就是
    错的）。张数减少是**鸣牌**：被吃/碰/杠走的那张会从牌河消失（实测
    ``20261023_205534`` 对手牌河 1→0），那不是来牌。
    """
    if not cur.valid:
        return _Change(reason=f"{cur.name}: {cur.reason}")
    if not prev.valid:
        return _Change(reason=f"{prev.name} 上一帧不可信：{prev.reason}")
    if cur.count == 0 and prev.count == 0:
        return _Change(usable=True, reason=f"{cur.name} 两帧都没有牌")
    if prev.count == 0:
        return _Change(appended=cur.count, usable=True,
                       reason=f"{cur.name} 从空牌河起新出现 {cur.count} 张")
    if cur.count == 0:
        return _Change(removed=prev.count, usable=True,
                       reason=f"{cur.name} 整条牌河被清空（原有 {prev.count} 张）")
    if cur.anchor_x != prev.anchor_x:
        return _Change(reason=f"{cur.name} 锚定边移动（{prev.anchor_x}→{cur.anchor_x}）")
    delta = cur.count - prev.count
    if delta < 0:
        return _Change(removed=-delta, usable=True,
                       reason=f"{cur.name} 少 {-delta} 张（有人鸣牌）")
    if delta == 0:
        return _Change(usable=True, reason=f"{cur.name} 无变化")
    if cur.far_x is None or prev.far_x is None:
        return _Change(reason=f"{cur.name} 取不到外框坐标")
    pitch = cur.pitch or prev.pitch or _TILE_PITCH
    outward = (prev.far_x - cur.far_x) if cur.name == SOURCE_OPPONENT else (cur.far_x - prev.far_x)
    if outward < pitch * 0.5:
        return _Change(reason=(f"{cur.name} 多 {delta} 张但外框没往外伸（补了空洞），"
                               "最新一张不可定位"))
    return _Change(appended=delta, usable=True, reason=f"{cur.name} 新增 {delta} 张")


def evaluate_change(cur: TableSnapshot, prev: TableSnapshot | None) -> DiscardState:
    """根据前后两帧的牌河读数判定"来牌是哪一张、谁打的"。

    纯函数：不碰像素，只吃两个 :class:`TableSnapshot`，测试可以直接手工构造
    两个读数来覆盖全部分支。

    Args:
        cur: 当前帧读数。
        prev: 上一帧读数；``None`` 表示无法做时序比对，此时**不给来牌**。

    Returns:
        :class:`DiscardState`。
    """
    opponent_river = cur.opponent.ids
    own_river = cur.own.ids
    river_tiles = opponent_river + own_river

    def build(**kwargs: Any) -> DiscardState:
        extra = kwargs.pop("evidence", {})
        evidence: dict[str, Any] = {
            "table": cur.as_dict(),
            "previous": None if prev is None else prev.as_dict(),
        }
        evidence.update(extra)
        return DiscardState(
            river_tiles=river_tiles,
            opponent_river=opponent_river,
            own_river=own_river,
            evidence=evidence,
            table=cur,
            **kwargs,
        )

    if not cur.opponent.valid or not cur.own.valid:
        broken = cur.opponent if not cur.opponent.valid else cur.own
        return build(incoming_tile=None, incoming_source=None,
                     reason=f"牌河几何不可信：{broken.name} {broken.reason}")

    if prev is None:
        return build(incoming_tile=None, incoming_source=None,
                     reason="需要前后两帧才能确认来牌（单帧只知最新，不知是否刚打出）")

    opponent_change = _change(cur.opponent, prev.opponent)
    own_change = _change(cur.own, prev.own)
    evidence = {"opponent_change": opponent_change.reason,
                "own_change": own_change.reason,
                "appended_opponent": opponent_change.appended,
                "appended_own": own_change.appended}

    if not opponent_change.usable or not own_change.usable:
        reason = opponent_change.reason if not opponent_change.usable else own_change.reason
        return build(incoming_tile=None, incoming_source=None,
                     reason=f"前后两帧不可比对：{reason}", evidence=evidence)

    if opponent_change.appended and own_change.appended:
        return build(incoming_tile=None, incoming_source=None,
                     reason="两帧之间你和对手都出过牌，来牌归属无法确认",
                     evidence=evidence)

    if own_change.appended:
        return build(incoming_tile=None, incoming_source=SOURCE_SELF,
                     reason=f"你自己刚打出 {own_change.appended} 张牌，没有可鸣的来牌",
                     evidence=evidence)

    if not opponent_change.appended:
        if opponent_change.removed:
            return build(incoming_tile=None, incoming_source=None,
                         reason="对手的弃牌已被鸣走（从牌河移除），来牌不可读",
                         evidence=evidence)
        return build(incoming_tile=None, incoming_source=None,
                     reason="两帧之间没有新弃牌", evidence=evidence)

    newest = cur.opponent.newest
    if newest is None:
        return build(incoming_tile=None, incoming_source=SOURCE_OPPONENT,
                     reason="对手牌河格子变多但取不到最新一张", evidence=evidence)

    evidence["newest"] = newest.as_dict()
    evidence["sparkles"] = list(cur.sparkles)
    if cur.sparkles:
        # 闪光标是游戏自己画的"这张刚落下"，落在最新一格的正中间。它在的时候
        # 就是一条独立证据：网格算出的"最新一格"必须和它对得上，对不上说明最
        # 外面那张没读全，报出去的就是倒数第二张 —— 弃权。
        left, _, right, _ = newest.box
        if not any(left - _SPARKLE_MATCH_TOLERANCE <= center
                   <= right + _SPARKLE_MATCH_TOLERANCE for center in cur.sparkles):
            return build(
                incoming_tile=None, incoming_source=SOURCE_OPPONENT,
                reason=(f"闪光标在 x={list(cur.sparkles)}，与网格算出的最新一格 "
                        f"{left}..{right} 对不上，最新一张不可定位"),
                evidence=evidence)
    if not newest.accepted or newest.tile_id is None:
        return build(
            incoming_tile=None, incoming_source=SOURCE_OPPONENT,
            reason=(f"来牌没认出来（{newest.label or '无'}，score="
                    f"{newest.score:.3f}，margin={newest.margin:.3f}，{newest.reason}）"),
            evidence=evidence)
    if newest.score < INCOMING_MIN_SCORE or newest.margin < INCOMING_MIN_MARGIN:
        return build(
            incoming_tile=None, incoming_source=SOURCE_OPPONENT,
            reason=(f"来牌分数不够稳（{newest.label}，score={newest.score:.3f}，"
                    f"margin={newest.margin:.3f}），宁可弃权"),
            evidence=evidence)
    return build(incoming_tile=int(newest.tile_id), incoming_source=SOURCE_OPPONENT,
                 reason=f"对手打出 {newest.label}", evidence=evidence)


# ---------------------------------------------------------------------------
# 对外主接口
# ---------------------------------------------------------------------------


def read_discards(image: np.ndarray, previous=None, *, vision=None,
                  template_dir: str | Path | None = None,
                  require_tea_table: bool = True) -> DiscardState:
    """读出一帧的牌河与"刚打出的那张牌"。

    Args:
        image: 1600x900 BGR 帧（adb 原生截图）。
        previous: 上一帧的读数 —— 可以给上一次返回的 :class:`DiscardState`，
            也可以直接给上一帧的 ``np.ndarray``，还可以给
            :class:`TableSnapshot`。**不给就只能读出牌河，读不出来牌**：
            单帧无法区分"刚打出的"和"很久以前打的"，把陈旧的一张当来牌会让
            上层对着早就过去的那张去碰。
        vision: 可注入的识牌器（测试用）。
        template_dir: 模板目录（仅在没有 ``vision`` 时生效）。
        require_tea_table: 是否先过"是不是温州茶 2 人牌桌"的门禁。默认开。

    Returns:
        :class:`DiscardState`。读不出来时 ``incoming_tile is None`` 且
        ``reason`` 写明原因。
    """
    cur = snapshot_table(image, vision=vision, template_dir=template_dir,
                         require_tea_table=require_tea_table)
    baseline = baseline_of(previous, vision=vision, template_dir=template_dir,
                           require_tea_table=require_tea_table)
    return evaluate_change(cur, baseline)


def baseline_of(previous, *, vision=None, template_dir: str | Path | None = None,
                require_tea_table: bool = True) -> TableSnapshot | None:
    """把 ``previous`` 归一成 :class:`TableSnapshot`（认不出来就返回 ``None``）。"""
    if previous is None:
        return None
    if isinstance(previous, TableSnapshot):
        return previous
    if isinstance(previous, DiscardState):
        # 弃权状态里也带 table；当时连画面都没有，就真的没有基线。
        return previous.table
    if isinstance(previous, np.ndarray):
        return snapshot_table(previous, vision=vision, template_dir=template_dir,
                              require_tea_table=require_tea_table)
    return None
