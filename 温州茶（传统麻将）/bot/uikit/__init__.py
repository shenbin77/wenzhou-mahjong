"""uikit —— 「像素 → 控件」的统一解析层（只读，不点）。

为什么存在：这个游戏是 Cocos2d-x / Lua 渲染，``uiautomator dump`` 只能拿到
一层空壳 FrameLayout + 一个 android.view.View（实测 2069 字节，零 text /
零 resource-id / 零 clickable）。系统控件树完全不可用，唯一真值来源是
1600x900 的截图像素。旧代码把 ROI 和像素阈值散落在各个画面自己的模块里，
换版式就全废。

本包把这件事收敛成一条固定链路：

    palette（具名颜色） → shapes（几何门槛） → anchor（多尺度锚点）
                       → registry（画面指纹 + 动作白名单，默认拒绝）
                       → resolve（唯一入口）

对外只需要 :func:`uikit.resolve.resolve`：

    >>> from uikit import resolve
    >>> target = resolve.resolve(image, "seat_plus_left")
    >>> target.ok, target.point, target.screen_id      # noqa: E701

未登记的画面、未登记的动作、找不到可信目标，一律返回 ``Denied``，
**不做任何推断**。

约定
----
* 只认 1600x900 的 BGR 帧，尺寸不对直接拒绝（缩放会让实测几何失真）。
* 只用具名颜色（:data:`uikit.palette.PALETTE`），模块内不写裸阈值。
* 只做「看」：不点击、不发 adb、不读设备状态、不调用 subprocess。
* 不识别手牌（``tile_vision`` 负责，精度要求完全不同）。
"""

from __future__ import annotations

from .anchor import AnchorHit, match as match_anchor
from .palette import ColorSpec, PALETTE, coverage, get_spec, load_bgr, load_gray
from .registry import (
    ActionSpec,
    FRAME_SIZE,
    ScreenRegistry,
    build_default_registry,
)
from .resolve import Denied, ResolvedTarget, default_registry, explain, resolve
from .shapes import ButtonShape, ColorRegion, find_buttons, find_regions

__all__ = [
    "AnchorHit",
    "ActionSpec",
    "ButtonShape",
    "ColorRegion",
    "ColorSpec",
    "Denied",
    "FRAME_SIZE",
    "PALETTE",
    "ResolvedTarget",
    "ScreenRegistry",
    "build_default_registry",
    "coverage",
    "default_registry",
    "explain",
    "find_buttons",
    "find_regions",
    "get_spec",
    "load_bgr",
    "load_gray",
    "match_anchor",
    "resolve",
]

__version__ = "1.0.0"
