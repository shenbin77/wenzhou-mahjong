"""具名颜色区间表：像素解析层唯一的"颜色真值"来源。

## 为什么要有这一层

这个游戏是 Cocos2d-x / Lua 渲染，``adb shell uiautomator dump`` 只能拿到
一层空壳 ``FrameLayout`` + 一个 ``android.view.View``，**没有任何 text /
resource-id / clickable 节点**（2026-10-25 实测 2069 字节）。所以系统控件树
不可用，唯一真值来源就是 1600x900 的截图像素。

旧代码把颜色阈值散落在各处自己写（``match_end._button_fill_mask`` 用
``V>=150 & S>=40``、``turn_gate`` 又有自己一套），换版式就全废，而且
``V>=150 & S>=40`` 这种口径会把整片米黄地板一起吃进去。本模块把**实测**
出来的颜色收敂成一张表，其他模块只允许引用名字，不再自己写阈值。

## 取样口径

每个 ``ColorSpec`` 的 ``note`` 都写明取样自哪张真实帧、哪个矩形、用什么
统计量（中位数 + P5/P95 分位）。阈值 = 实测 P5/P95 向外取整后**再放宽**，
不是凭空调出来的。取样脚本与原始数字见 ``_takeover/uikit_report.md``。

HSV 在 OpenCV 下的取值范围是 ``H∈[0,180], S∈[0,255], V∈[0,255]``。
红色横跨 0 度，因此允许 ``hsv_lo[0] > hsv_hi[0]`` 表示**绕回**（170→10），
见 :func:`hue_spans`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

__all__ = [
    "ColorSpec",
    "PALETTE",
    "COLOR_NAMES",
    "get_spec",
    "hue_spans",
    "mask_hsv",
    "mask_bgr",
    "mask",
    "coverage",
    "dominant_hsv",
    "load_bgr",
    "load_gray",
    "validate_frame",
]

HSVRange = tuple[tuple[int, int, int], tuple[int, int, int]]
BGRRange = tuple[tuple[int, int, int], tuple[int, int, int]]


@dataclass(frozen=True)
class ColorSpec:
    """一段具名颜色区间。

    ``hsv_lo``/``hsv_hi`` 是闭区间；``hsv_lo[0] > hsv_hi[0]`` 表示色相绕回。
    ``bgr_lo``/``bgr_hi`` 是同一批取样像素在 BGR 空间的闭区间，用于
    "HSV 分不开但 BGR 分得开" 的兜底（例如 :func:`mask` 的 ``space="bgr"``）。
    ``min_area`` 是"这条颜色有意义"的最小连通域面积，单位是像素。
    """

    name: str
    hsv_lo: tuple[int, int, int]
    hsv_hi: tuple[int, int, int]
    min_area: int
    note: str
    bgr_lo: tuple[int, int, int] | None = None
    bgr_hi: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        for label, triple in (("hsv_lo", self.hsv_lo), ("hsv_hi", self.hsv_hi)):
            if len(triple) != 3:
                raise ValueError(f"{self.name}.{label} 必须是三元组")
            if not 0 <= triple[0] <= 180:
                raise ValueError(f"{self.name}.{label} 色相 {triple[0]} 超出 [0,180]")
            if not 0 <= triple[1] <= 255 or not 0 <= triple[2] <= 255:
                raise ValueError(f"{self.name}.{label} S/V 超出 [0,255]")
        # 色相允许绕回，S/V 必须 lo<=hi。
        if self.hsv_lo[1] > self.hsv_hi[1]:
            raise ValueError(f"{self.name} S 下界大于上界")
        if self.hsv_lo[2] > self.hsv_hi[2]:
            raise ValueError(f"{self.name} V 下界大于上界")
        if self.min_area < 0:
            raise ValueError(f"{self.name}.min_area 不能为负")
        if (self.bgr_lo is None) != (self.bgr_hi is None):
            raise ValueError(f"{self.name} 的 bgr_lo/bgr_hi 必须同时给出或同时省略")
        if self.bgr_lo is not None and self.bgr_hi is not None:
            for lo, hi in zip(self.bgr_lo, self.bgr_hi):
                if lo > hi:
                    raise ValueError(f"{self.name} BGR 下界大于上界")

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "hsv_lo": list(self.hsv_lo),
            "hsv_hi": list(self.hsv_hi),
            "bgr_lo": list(self.bgr_lo) if self.bgr_lo else None,
            "bgr_hi": list(self.bgr_hi) if self.bgr_hi else None,
            "min_area": self.min_area,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 颜色表
#
# 取样帧路径统一写成 `_takeover/...`（相对仓库根）。"P50"=中位数，
# "P5/P95"=5%/95% 分位。阈值取 P5/P95 再外扩一点，宁可放宽一点让人工
# 复核，也不要把判定藏在看不到的常量里 —— 真正的收紧放在 shapes.py 的
# 形状约束上（填充率 / 长宽比 / 四边留白），那才是能拒绝"整片地板"的地方。
# ---------------------------------------------------------------------------
PALETTE: tuple[ColorSpec, ...] = (
    ColorSpec(
        name="tile_face_cream",
        hsv_lo=(15, 12, 188),
        hsv_hi=(32, 40, 238),
        min_area=900,
        note=(
            "奶油色手牌面。取样 _takeover/shot_5556_turn.png 手牌带 "
            "(80,795)-(1480,880) 内亮低饱和像素：H P5/P50/P95=17/23/23，"
            "S=19/25/25，V=204/214/223，中位 BGR=(193,209,214)。阈值在 P95 外扩"
            "（V 上界 +15），因为出牌/结算时整带会被压暗到 V≈150。"
        ),
        bgr_lo=(176, 193, 199),
        bgr_hi=(208, 224, 231),
    ),
    ColorSpec(
        name="felt_green",
        hsv_lo=(64, 135, 45),
        hsv_hi=(80, 205, 140),
        min_area=20000,
        note=(
            "桌面绿（对局页与桌内等待页的大底色）。取样两处："
            "_takeover/shot_5556_turn.png (850,250)-(1050,400) 桌心 "
            "H71/S168/V109，中位 BGR=(64,109,37)；_takeover/shot_5556_game.png "
            "左右边缘 (40,300)-(160,700) H71/S184/V72，V 只有 60~83。"
            "所以 V 区间必须跨 45~140，只用一个 V 下界会把暗边整片丢掉。"
        ),
        bgr_lo=(24, 52, 8),
        bgr_hi=(88, 132, 54),
    ),
    ColorSpec(
        name="orange_button",
        hsv_lo=(8, 120, 212),
        hsv_hi=(24, 195, 255),
        min_area=2500,
        note=(
            "橙色主按钮（唯一允许当作「主操作」的亮色块）。取样两处："
            "_takeover/match_end_probe.png 底部「继续游戏」框 (1046,764)-(1413,873) "
            "H15/S149/V233；_takeover/shot_5556_room.png「开始游戏」框 "
            "(663,525)-(937,624) H15/S174/V229，中位 BGR=(72,148,229)。"
            "注意顶栏红橙底 (H8/S205/V218) 落在 S 上界之外，故意排除。"
        ),
        bgr_lo=(52, 126, 206),
        bgr_hi=(114, 180, 250),
    ),
    ColorSpec(
        name="cyan_cloth",
        hsv_lo=(86, 205, 85),
        hsv_hi=(100, 255, 155),
        min_area=4000,
        note=(
            "青色桌布（大厅菱形牌桌的布面）。取样 _takeover/probe/now_5556.png "
            "(200,350)-(330,420)：H93/S234/V122，中位 BGR=(122,112,10)。"
            "与 felt_green 靠色相分开（93 vs 71），与 teal_button 靠 S/V 分开"
            "（S234/V122 vs S160/V200）。"
        ),
        bgr_lo=(92, 82, 0),
        bgr_hi=(148, 138, 28),
    ),
    ColorSpec(
        name="beige_panel",
        hsv_lo=(10, 85, 156),
        hsv_hi=(18, 102, 188),
        min_area=2000,
        note=(
            "米色/浅棕面板底（大厅两个空位的「+」卡片本体）。取样 "
            "_takeover/probe/now_5556.png 两处卡片 (74,225)-(156,300) 与 "
            "(369,425)-(453,505)，两边完全一致：H14/S94/V171，中位 BGR=(108,137,171)。"
            "这块颜色是 UI 平涂，离散度极小（S 恒等于 94），所以窗口可以很窄；"
            "地板色 S=111~136 落在窗口外 —— 这是「地板不会被当成按钮」的第一道防线。"
        ),
        bgr_lo=(96, 125, 158),
        bgr_hi=(122, 150, 184),
    ),
    ColorSpec(
        name="cream_panel",
        hsv_lo=(17, 30, 240),
        hsv_hi=(26, 58, 255),
        min_area=600,
        note=(
            "米色面板底/高光条（「+」字形、左侧抽屉面板、结算标题条）。取样 "
            "_takeover/probe/now_5556.png「+」字形 (95,245)-(140,290) H21/S41/V249，"
            "中位 BGR=(209,237,249)；左侧抽屉 _takeover/shot_5556_room.png "
            "(110,500)-(400,640) H20/S43/V255。"
            "与 tile_face_cream 用 V 分开（>=240 vs 188~238）。"
        ),
        bgr_lo=(192, 222, 236),
        bgr_hi=(228, 255, 255),
    ),
    ColorSpec(
        name="white_text",
        hsv_lo=(0, 0, 244),
        hsv_hi=(180, 26, 255),
        min_area=40,
        note=(
            "白色数字/描边（顶栏「玉姐的比赛场」等白字）。取样 "
            "_takeover/probe/now_5556.png (140,30)-(700,100) 内高亮像素："
            "S 0~24，V 247~255，中位 BGR=(254,243,242)。"
            "H 全区间是因为白字带抗锯齿彩边，卡 H 会碎成一片噪点。"
        ),
        bgr_lo=(236, 230, 228),
        bgr_hi=(255, 255, 255),
    ),
    ColorSpec(
        name="red_badge",
        hsv_lo=(170, 185, 160),
        hsv_hi=(10, 255, 255),
        min_area=120,
        note=(
            "红色角标/红点/红描边环（未读提示、claim 面板的「胡」外环）。"
            "取样 _takeover/probe/now_5556.png 右侧小红点 (865,5)-(890,31)，"
            "中位 BGR=(51,98,200)；claim 帧「胡」按钮外环 "
            "(1181,535)-(1304,657) 同区间。色相绕回 170→10。"
        ),
        bgr_lo=(0, 38, 148),
        bgr_hi=(84, 132, 255),
    ),
    ColorSpec(
        name="teal_button",
        hsv_lo=(80, 125, 168),
        hsv_hi=(94, 195, 222),
        min_area=2500,
        note=(
            "青绿按钮（结算页「返回大厅」「分享战绩」，以及 claim 的「过」圆环）。"
            "取样 _takeover/match_end_probe.png 底部两按钮 (246,777)-(554,873) 与 "
            "(646,777)-(954,872)：H86/S160/V200，中位 BGR=(185,200,75)。"
            "与 felt_green 色相相邻但 V 高出 90，靠 V 上界 222 分开。"
        ),
        bgr_lo=(158, 178, 42),
        bgr_hi=(218, 228, 112),
    ),
    ColorSpec(
        name="tile_back_yellow",
        hsv_lo=(14, 212, 218),
        hsv_hi=(24, 255, 255),
        min_area=800,
        note=(
            "黄色牌背（对局页顶栏对家手牌背）。取样 _takeover/shot_5556_turn.png "
            "(400,15)-(1100,55) H18/S236/V236，中位 BGR=(18,151,236)。"
            "目前只用于「这一屏是不是对局」的辅助证据，不参与定位。"
        ),
        bgr_lo=(0, 122, 212),
        bgr_hi=(46, 182, 255),
    ),
)

COLOR_NAMES: tuple[str, ...] = tuple(spec.name for spec in PALETTE)
_BY_NAME = {spec.name: spec for spec in PALETTE}


def get_spec(name: str) -> ColorSpec:
    """按名字取颜色规格；未登记的名字直接报错，不猜。"""
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"未登记的具名颜色 {name!r}；已登记：{COLOR_NAMES}") from exc


def hue_spans(spec: ColorSpec) -> tuple[tuple[int, int], ...]:
    """把色相区间拆成 1~2 段闭区间，处理红色绕回。"""
    lo, hi = spec.hsv_lo[0], spec.hsv_hi[0]
    if lo <= hi:
        return ((lo, hi),)
    return ((lo, 180), (0, hi))


def mask_hsv(image: np.ndarray, spec: ColorSpec) -> np.ndarray:
    """HSV 掩膜，返回 uint8 的 0/1 图（与 shapes 的连通域输入口径一致）。"""
    _require_bgr(image, "mask_hsv")
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = np.zeros(hsv.shape[:2], dtype=bool)
    for lo, hi in hue_spans(spec):
        hue |= (hsv[:, :, 0] >= lo) & (hsv[:, :, 0] <= hi)
    sat = (hsv[:, :, 1] >= spec.hsv_lo[1]) & (hsv[:, :, 1] <= spec.hsv_hi[1])
    val = (hsv[:, :, 2] >= spec.hsv_lo[2]) & (hsv[:, :, 2] <= spec.hsv_hi[2])
    return (hue & sat & val).astype(np.uint8)


def mask_bgr(image: np.ndarray, spec: ColorSpec) -> np.ndarray:
    """BGR 掩膜（同一批取样像素的另一组分位区间），要求 spec 带 bgr 区间。"""
    _require_bgr(image, "mask_bgr")
    if spec.bgr_lo is None or spec.bgr_hi is None:
        raise ValueError(f"{spec.name} 没有登记 BGR 区间")
    inside = np.ones(image.shape[:2], dtype=bool)
    for channel in range(3):
        inside &= (image[:, :, channel] >= spec.bgr_lo[channel])
        inside &= (image[:, :, channel] <= spec.bgr_hi[channel])
    return inside.astype(np.uint8)


def mask(image: np.ndarray, spec: ColorSpec | str, space: str = "hsv") -> np.ndarray:
    """统一入口：``space="hsv"``（默认）或 ``"bgr"``。spec 可传名字。"""
    resolved = get_spec(spec) if isinstance(spec, str) else spec
    if space == "hsv":
        return mask_hsv(image, resolved)
    if space == "bgr":
        return mask_bgr(image, resolved)
    raise ValueError(f"未知颜色空间 {space!r}，只支持 'hsv' / 'bgr'")


def coverage(image: np.ndarray, spec: ColorSpec | str, roi: tuple[int, int, int, int] | None = None,
             space: str = "hsv") -> float:
    """该颜色在整帧或 ROI 内的占比（0~1），用于画面指纹与证据记录。"""
    resolved = get_spec(spec) if isinstance(spec, str) else spec
    crop = crop_roi(image, roi)
    if crop.size == 0:
        return 0.0
    return float(mask(crop, resolved, space=space).mean())


def dominant_hsv(image: np.ndarray, roi: tuple[int, int, int, int] | None = None) -> tuple[int, int, int]:
    """ROI 内 HSV 中位数，只用于证据留痕，不参与判定。"""
    crop = crop_roi(image, roi)
    if crop.size == 0:
        return (0, 0, 0)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    med = np.median(hsv, axis=0)
    return (int(med[0]), int(med[1]), int(med[2]))


def crop_roi(image: np.ndarray, roi: tuple[int, int, int, int] | None) -> np.ndarray:
    """按 (x0,y0,x1,y1) 裁剪；越界自动夹到帧内，不做任何缩放。"""
    if roi is None:
        return image
    height, width = image.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in roi)
    x0, x1 = max(0, min(x0, width)), max(0, min(x1, width))
    y0, y1 = max(0, min(y0, height)), max(0, min(y1, height))
    if x1 <= x0 or y1 <= y0:
        return image[0:0, 0:0]
    return image[y0:y1, x0:x1]


def validate_frame(image: object, expected: tuple[int, int] | None = None) -> str | None:
    """帧可用性检查，返回错误原因（None 表示可用）。

    解析层一律 fail-closed：尺寸不对、通道不对、空帧都直接拒绝，
    绝不"缩放到 1600x900 再试"——缩放会让所有实测几何瞬间失真。
    """
    if image is None:
        return "画面不可用（None）"
    if not isinstance(image, np.ndarray):
        return f"画面类型不对：{type(image).__name__}"
    if image.ndim != 3 or image.shape[2] != 3:
        return f"画面通道不对：shape={image.shape}"
    if image.size == 0:
        return "画面为空"
    height, width = image.shape[:2]
    if expected is not None and (width, height) != tuple(expected):
        return f"帧尺寸未验证 {width}x{height}（要求 {expected[0]}x{expected[1]}）"
    return None


def _require_bgr(image: np.ndarray, where: str) -> None:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"{where} 需要 HxWx3 的 BGR ndarray，收到 {getattr(image, 'shape', None)}")


def load_bgr(path) -> np.ndarray | None:
    """读图。``cv2.imread`` 在中文路径下会静默返回 None，这里必须是
    ``np.fromfile`` + ``imdecode``。读不到就返回 None，由调用方决定拒绝。"""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if raw.size == 0:
        return None
    return cv2.imdecode(raw, cv2.IMREAD_COLOR)


def load_gray(path) -> np.ndarray | None:
    """同 :func:`load_bgr`，返回灰度图（锚点模板用）。"""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if raw.size == 0:
        return None
    return cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)


def iter_specs(names: Iterable[str] | None = None) -> tuple[ColorSpec, ...]:
    """按名字列表取 spec；None 表示全部。"""
    if names is None:
        return PALETTE
    return tuple(get_spec(name) for name in names)
