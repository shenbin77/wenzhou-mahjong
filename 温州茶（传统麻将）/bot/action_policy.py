# -*- coding: utf-8 -*-
"""吃/碰/杠/胡/过 的决策闸门（bot/action_policy.py）。

本模块回答一个问题：**画面上出现了行动圆盘时，该不该点、点哪一个、点了之后
画面有没有按预期变**。它自己不读像素、不点击、不写文件——视觉交给
``claim_vision.read_claims``，算牌交给 ``brain_v0`` 引擎，点击交给
``tap_executor``。

三重校验（BOSS 铁律，缺一即弃权）
--------------------------------
定位不能只靠"坐标 + 图 + 文字 + ID"，所以每一层都必须独立成立：

* **L1 定位**——``claim_vision.read_claims`` 给出的圆盘几何（连通域实测框，
  不是槽位推断），外加模板匹配口径（照 ``mode_vision.py``：
  ``TM_CCOEFF_NORMED``，top1 ≥ :data:`TEMPLATE_TOP1_MIN` 且 top1 与次名的差
  ≥ :data:`TEMPLATE_MARGIN_MIN`）。**不引入 OCR。**
* **L2 语义先验**——先用算牌引擎预测"这个按钮应不应该出现"。引擎说不能碰，
  画面上却出现金盘 → 弃权，绝不点（:func:`decide_action` 里的
  ``blocked_reason`` 落证）。引擎反过来还能**消歧**：L1 分不出碰/吃/杠时，
  若引擎判定只有一种可能，就认那一种；多于一种仍然弃权。
* **L3 点击后状态迁移**——:func:`verify_after_click` 校验暗手张数、副露组数、
  按钮消失、胡牌进入结算画面、倒计时转移。任一不符返回 ``False`` 与原因，
  调用方必须立即停机并落证据。按钮消失必须**实测为 0**：圆盘数量读不到
  （``None``）按证据不足判失败（fail closed）；吃的校验另带候选面板残留
  否决（:attr:`BoardState.selection_panel` 测得仍在画面上即判吃未完成）。

fail-closed
-----------
* 读不出按钮 → 返回 ``None``（连"过"都不点）。
* 出现 ``取消`` → 返回 ``None``（点取消会不可逆地中断进行中的选择）。
* 引擎不可用 / 手牌张数不守恒 / 来牌未知 → 只可能产出"过"，绝不猜碰吃杠胡。
* 黑名单（:data:`COST_BUTTONS`，含写死的 ``洗牌`` 房卡×2、``切牌`` 房卡×4）
  命中 → 永不产出动作。

人工确认闸门
------------
:class:`ActionPolicyConfig` 的两个开关同时生效：

* ``auto_click_enabled``——总闸，**默认 False（不自动点）**；
* ``confirmed_actions``——per-action 白名单，记录操作者已看过回放确认的
  动作，**默认空**。

``needs_human_confirm = (not auto_click_enabled) or (action not in
confirmed_actions)``，即默认任何动作都需要人工确认。确认一个动作用
:func:`confirm_action`（返回新的 config，不改原对象）。

未接线的地方（明确记录，不许假装能用）
--------------------------------------
* 来牌（别家打出的那张）由 ``discard_vision.read_discards`` 读取，经返回体的
  ``discards`` 字段传进来（见 :meth:`HandState.from_analysis`）。**传不进来
  时** :class:`HandState.incoming_tile` 仍是 ``None``，此时 L2 无法验证
  碰/吃/杠/胡，只有"过"可能出手 —— 这是默认的安全行为，不是缺口。
  * 来牌还带归属 :attr:`HandState.incoming_source`：``"self"``（自家刚打出的）
    或任何不在 :data:`CLAIMABLE_INCOMING_SOURCES` 里的字符串，一律不许
    吃/碰/杠/胡——行动行有时是在自家出牌之后刷出来的，没有这道闸就会拿
    自己的弃牌去碰。归属未标注（``None``）时不拦，只用来牌本身判定。
* 吃会弹出二次选择面板（面板里有 ``取消``）。面板选组的纯决策已经就绪
  （:func:`choose_chi_panel_option`：不劣化向听才选、同分取保留搭子多者，
  证据不足弃权）；循环侧「点吃圆盘 → 等面板 → 读面板选组 → 点选组 →
  L3 复核」的连点编排已集成在 ``auto_play_loop`` 的吃链里（面板读数走
  ``chi_panel_vision.read_chi_panel``，点组走 ``TapExecutor`` 的面板
  专用白名单通道）。``config.chi_enabled`` 默认仍为 ``False``，须由
  runner 以 ``recommended_runner_config(include_chi=True)`` 显式打开。
* 行动圆盘已接入 ``TapExecutor.execute_claim`` 的独立几何白名单，并由
  :data:`CLAIM_TAP_CHANNEL_READY` 标记。它不放宽普通出牌的手牌 ROI；调用方
  必须显式打开自动总闸，并在点击后完成 L3 语义验证。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import cv2
import numpy as np

from claim_vision import (
    ACTION_DISK_CENTER_X_MAX,
    ACTION_DISK_CENTER_X_MIN,
    ACTION_DISK_CENTER_Y_MAX,
    ACTION_DISK_CENTER_Y_MIN,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    KIND_CANCEL,
    KIND_GOLD,
    KIND_HU,
    KIND_PASS,
    MAX_BBOX_EDGE,
    MIN_BBOX_EDGE,
    ClaimButton,
    ClaimState,
    read_claims,
)

__all__ = [
    "ACTION_CHI",
    "ACTION_HU",
    "ACTION_KONG",
    "ACTION_NONE",
    "ACTION_PASS",
    "ACTION_PENG",
    "ALL_ACTIONS",
    "CLAIM_ACTIONS",
    "ActionDecision",
    "ActionPolicyConfig",
    "BLACKLISTED_LABELS",
    "BoardState",
    "ChiPanelChoice",
    "CLAIM_TAP_CHANNEL_READY",
    "COST_BUTTONS",
    "CLAIMABLE_INCOMING_SOURCES",
    "HandState",
    "INCOMING_SOURCE_OPPONENT",
    "INCOMING_SOURCE_SELF",
    "RECOMMENDED_CONFIRMED_ACTIONS",
    "RECOMMENDED_CONFIRMED_ACTIONS_WITH_CHI",
    "TEMPLATE_MARGIN_MIN",
    "TEMPLATE_TOP1_MIN",
    "blacklist_hit",
    "choose_chi_panel_option",
    "confirm_action",
    "decide_action",
    "engine_available",
    "evaluate_claim_candidates",
    "human_confirm_required",
    "load_claim_templates",
    "match_button_glyph",
    "never_tap",
    "read_and_decide",
    "read_sparkle_sides",
    "recommended_runner_config",
    "resolve_incoming",
    "validate_runner_config",
    "verified_claim_templates",
    "verify_after_click",
]


# ---------------------------------------------------------------------------
# 动作集合与优先级
# ---------------------------------------------------------------------------

ACTION_HU: str = "hu"
ACTION_PENG: str = "peng"
ACTION_KONG: str = "kong"
ACTION_CHI: str = "chi"
ACTION_PASS: str = "pass"

#: 需要别家来牌才能成立的"吃碰杠胡"（``ACTION_PASS`` 不需要来牌）。
CLAIM_ACTIONS: tuple[str, ...] = (ACTION_HU, ACTION_PENG, ACTION_KONG, ACTION_CHI)
#: 全部可能产出的动作。
ALL_ACTIONS: tuple[str, ...] = CLAIM_ACTIONS + (ACTION_PASS,)
#: 胡 > 杠 > 碰 > 吃 > 过；同一帧出现多个可行动作时按此序取第一个。
ACTION_PRIORITY: tuple[str, ...] = (
    ACTION_HU, ACTION_KONG, ACTION_PENG, ACTION_CHI, ACTION_PASS,
)

#: 空字符串 = 没能解析出任何动作（弃权，但保留证据）。
ACTION_NONE: str = ""

# ---------------------------------------------------------------------------
# 来牌归属（与 discard_vision.SOURCE_* 同值，此处独立声明避免硬依赖）
# ---------------------------------------------------------------------------

#: 来牌是别家打出的——**只有这种**才可能被吃/碰/杠/胡。
INCOMING_SOURCE_OPPONENT: str = "opponent"
#: 来牌是我自己刚打出的——不能拿来吃/碰/杠/胡。
INCOMING_SOURCE_SELF: str = "self"

#: 允许拿来做吃/碰/杠/胡的归属集合。``None``（调用方未标注归属）不拦；
#: 除 ``"opponent"`` 以外的任何字符串一律拦下（未识别的口径不信任）。
CLAIMABLE_INCOMING_SOURCES: frozenset[str] = frozenset({INCOMING_SOURCE_OPPONENT})

# ---------------------------------------------------------------------------
# 归属仲裁：金色闪光标（与 discard_vision 的实测常量同值）
#
# 闪光标是游戏自己画的"刚落下这张"指示，位于牌河上方。它**只回答"哪边后打"，
# 不回答"有没有新弃牌"**——无变化的帧里也有余辉（实测对手侧 17/60、自己侧
# 28/60），所以只能接在两帧差分**之后**做仲裁，绝不能单独用来判定有来牌。
# ---------------------------------------------------------------------------

#: 闪光标搜索带（xyxy，原生 1600x900 坐标）：对手牌河上方。
SPARKLE_BAND_OPPONENT: tuple[int, int, int, int] = (520, 196, 1260, 231)
#: 自己侧的同一条带：discard-vision 实测"整体下移 254px"。
SPARKLE_BAND_SELF: tuple[int, int, int, int] = (520, 450, 1260, 485)
#: 自己侧相对对手侧的下移量（实测 254px，与两条牌河牌面带的间距一致）。
SPARKLE_BAND_SELF_OFFSET: int = 254

#: 金色的 HSV 口径（照 discard_vision，不另立阈值）。
SPARKLE_HUE: tuple[int, int] = (10, 35)
SPARKLE_MIN_SAT: int = 140
SPARKLE_MIN_VAL: int = 170
#: 连通域闸门：太小是牌面噪点，太宽是别的东西（一张牌 55px，标 ~30px）。
SPARKLE_MIN_AREA: int = 80
SPARKLE_MAX_WIDTH: int = 40


# ---------------------------------------------------------------------------
# 黑名单：永不点击的按钮
# ---------------------------------------------------------------------------

#: 会花钱的按钮，实测口径写死在这里，任何路径都不许点。
COST_BUTTONS: Mapping[str, str] = {
    "洗牌": "房卡×2",
    "切牌": "房卡×4",
}

#: 不花钱但会离桌/开新局/进支付的按钮，与花钱按钮一起写死。
NEVER_TAP_BUTTONS: tuple[str, ...] = (
    "充值", "开始游戏", "返回大厅", "分享战绩", "游戏中心",
)

#: 黑名单全集（``COST_BUTTONS`` + ``NEVER_TAP_BUTTONS``）。
BLACKLISTED_LABELS: frozenset[str] = frozenset(COST_BUTTONS) | frozenset(NEVER_TAP_BUTTONS)

#: 实测坐标库。写死进黑名单的按钮一旦测到坐标就补进这里；空 tuple 表示
#: "尚未实测"，此时黑名单只按标签（模板名/调用方传入的 label）生效。
DEFAULT_FORBIDDEN_BOXES: tuple[tuple[int, int, int, int], ...] = ()

#: 点击通道是否打通。行动圆盘使用 ``TapExecutor.execute_claim`` 的独立
#: y∈[560,660] 白名单；调用方仍必须显式打开 auto-click、通过模板/L2 门禁，
#: 并在点击后调用 :func:`verify_after_click` 做 L3 语义验证。
CLAIM_TAP_CHANNEL_READY: bool = True


def never_tap(label: str | None) -> bool:
    """``label`` 命中黑名单即 True；``None``/空串按未知处理，返回 True。"""
    if not label:
        return True
    return str(label).strip() in BLACKLISTED_LABELS


def blacklist_hit(
    buttons: Sequence[ClaimButton],
    config: "ActionPolicyConfig | None" = None,
    *,
    matched_label: str | None = None,
) -> str | None:
    """L0 黑名单闸门：命中返回中文原因，未命中返回 ``None``。

    Args:
        buttons: 本帧所有已定位的圆盘（几何来自像素，不是槽位推断）。
        config: 策略配置，取 ``forbidden_boxes`` / ``forbidden_labels``。
        matched_label: 模板匹配认出的按钮名（可为 ``None``）。

    Returns:
        命中的原因字符串；``None`` 表示可以往下走 L1/L2。
    """
    cfg = config or ActionPolicyConfig()
    if matched_label:
        name = str(matched_label).strip()
        if name in cfg.forbidden_labels:
            cost = COST_BUTTONS.get(name)
            suffix = f"（{cost}）" if cost else ""
            return f"黑名单按钮 {name}{suffix}，永不点击"
    for button in buttons or ():
        if not isinstance(button, ClaimButton):
            continue
        center = tuple(button.center[:2])
        for box in cfg.forbidden_boxes:
            x0, y0, x1, y1 = (int(value) for value in box[:4])
            if x0 <= center[0] <= x1 and y0 <= center[1] <= y1:
                return f"圆盘中心 {list(center)} 落在黑名单区域 {[x0, y0, x1, y1]}，永不点击"
    return None


# ---------------------------------------------------------------------------
# L1 模板匹配（口径照 mode_vision.py）
# ---------------------------------------------------------------------------

#: 模板目录。当前目录不存在（还没有碰/吃/杠/胡的字形样本），此时模板层
#: 报 ``no_templates`` 而不是猜。
CLAIM_TEMPLATE_DIR: Path = Path(__file__).resolve().parent / "templates" / "claim"
# 已有的原生 ADB 行动按钮裁图。它们比圆盘连通域略大，所以 runner 只取
# 中央 35% 字形作为模板；该比例已在两张原生实战帧上逐按钮回放验证：
# hu/kong/peng/pass/chi top1 均 >= .977，次名间隔均远大于 .08。
VERIFIED_CLAIM_UI_DIR: Path = (
    Path(__file__).resolve().parent.parent / "采集" / "templates" / "ui")
VERIFIED_CLAIM_UI_FILES: Mapping[str, str] = {
    ACTION_CHI: "btn_chi.png",
    ACTION_PENG: "btn_peng.png",
    ACTION_KONG: "btn_gang.png",
    ACTION_HU: "btn_hu.png",
    ACTION_PASS: "btn_guo.png",
}
VERIFIED_CLAIM_INNER_RATIO: float = 0.35

TEMPLATE_TOP1_MIN: float = 0.94
TEMPLATE_MARGIN_MIN: float = 0.08

TEMPLATE_STATUS_MATCHED: str = "matched"
TEMPLATE_STATUS_AMBIGUOUS: str = "ambiguous"
TEMPLATE_STATUS_UNMATCHED: str = "unmatched"
TEMPLATE_STATUS_NO_TEMPLATES: str = "no_templates"
TEMPLATE_STATUS_SIZE_MISMATCH: str = "size_mismatch"
TEMPLATE_STATUS_NO_FRAME: str = "no_frame"


def _imread_gray(path: Path) -> np.ndarray | None:
    """中文路径安全读图：``cv2.imread`` 在非 ASCII 路径上会失败。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    return image if image is not None and image.size else None


@lru_cache(maxsize=1)
def _default_templates() -> tuple[tuple[str, np.ndarray], ...]:
    """读取 :data:`CLAIM_TEMPLATE_DIR` 下的全部灰度模板；目录不存在返回空。"""
    directory = CLAIM_TEMPLATE_DIR
    if not directory.is_dir():
        return ()
    found: list[tuple[str, np.ndarray]] = []
    for path in sorted(directory.glob("*.png")):
        image = _imread_gray(path)
        if image is not None:
            found.append((path.stem, image))
    return tuple(found)


def load_claim_templates(
    provider: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, np.ndarray]:
    """返回 ``{名字: 灰度模板}``；无模板时返回空 dict（不是错误）。"""
    if provider is not None:
        raw = provider() or {}
        loaded: dict[str, np.ndarray] = {}
        for name, image in raw.items():
            array = np.asarray(image)
            if array.ndim == 3:
                array = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
            if array.ndim == 2 and array.size:
                loaded[str(name)] = array.astype(np.uint8)
        return loaded
    return {name: image for name, image in _default_templates()}


@lru_cache(maxsize=1)
def verified_claim_templates() -> dict[str, np.ndarray]:
    """从既有原生按钮样本派生中央字形模板；缺任一文件就不返回半套。"""
    found: dict[str, np.ndarray] = {}
    for action, filename in VERIFIED_CLAIM_UI_FILES.items():
        image = _imread_gray(VERIFIED_CLAIM_UI_DIR / filename)
        if image is None:
            return {}
        height, width = image.shape[:2]
        crop_w = max(8, int(width * VERIFIED_CLAIM_INNER_RATIO))
        crop_h = max(8, int(height * VERIFIED_CLAIM_INNER_RATIO))
        x0 = (width - crop_w) // 2
        y0 = (height - crop_h) // 2
        found[action] = image[y0:y0 + crop_h, x0:x0 + crop_w].copy()
    return found


def match_button_glyph(
    image: Any,
    button: ClaimButton,
    templates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """对单个圆盘做模板匹配，口径照 ``mode_vision.py``。

    Args:
        image: 1600x900 的 BGR 帧；``None`` 或尺寸不符时记 ``no_frame``。
        button: 已定位的圆盘（几何来自像素）。
        templates: ``{名字: 灰度图}``；空或 ``None`` 时记 ``no_templates``。

    Returns:
        ``{"status", "best", "top1", "runner_up", "runner_up_score",
        "margin", "scores", "box"}``；``status`` 取值为 :data:`TEMPLATE_STATUS_MATCHED`
        （top1 ≥ 0.94 且与次名差 ≥ 0.08）、``ambiguous``、``unmatched``、
        ``no_templates``、``size_mismatch`` 或 ``no_frame``。
    """
    box = tuple(int(value) for value in button.box[:4])
    result: dict[str, Any] = {
        "status": TEMPLATE_STATUS_NO_FRAME,
        "best": None,
        "top1": None,
        "runner_up": None,
        "runner_up_score": None,
        "margin": None,
        "scores": {},
        "box": list(box),
        "top1_min": TEMPLATE_TOP1_MIN,
        "margin_min": TEMPLATE_MARGIN_MIN,
    }
    if image is None or not isinstance(image, np.ndarray) or image.ndim < 2:
        return result
    height, width = image.shape[:2]
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return result
    crop = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    gray = gray.astype(np.uint8)

    table = load_claim_templates(None) if templates is None else {
        str(name): np.asarray(img) for name, img in templates.items()
    }
    if not table:
        result["status"] = TEMPLATE_STATUS_NO_TEMPLATES
        return result

    scores: dict[str, float] = {}
    too_large: list[str] = []
    for name, template in table.items():
        array = np.asarray(template)
        if array.ndim == 3:
            array = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
        array = array.astype(np.uint8)
        if array.shape[0] > gray.shape[0] or array.shape[1] > gray.shape[1]:
            too_large.append(name)
            continue
        score = float(cv2.minMaxLoc(cv2.matchTemplate(
            gray, array, cv2.TM_CCOEFF_NORMED))[1])
        scores[name] = round(score, 6)
    result["scores"] = scores
    if not scores:
        result["status"] = TEMPLATE_STATUS_SIZE_MISMATCH
        result["skipped"] = too_large
        return result

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, top1 = ranked[0]
    runner_up, runner_score = (ranked[1] if len(ranked) > 1 else (None, 0.0))
    margin = top1 - float(runner_score)
    result.update({
        "best": best,
        "top1": round(float(top1), 6),
        "runner_up": runner_up,
        "runner_up_score": round(float(runner_score), 6),
        "margin": round(float(margin), 6),
        "skipped": too_large,
    })
    if top1 < TEMPLATE_TOP1_MIN:
        result["status"] = TEMPLATE_STATUS_UNMATCHED
    elif margin < TEMPLATE_MARGIN_MIN:
        result["status"] = TEMPLATE_STATUS_AMBIGUOUS
    else:
        result["status"] = TEMPLATE_STATUS_MATCHED
    return result


def _geometry_ok(button: ClaimButton) -> bool:
    """圆盘几何是否落在已验证的动作行口径内（照 claim_vision 的常数）。"""
    if not isinstance(button, ClaimButton):
        return False
    x0, y0, x1, y1 = (int(value) for value in button.box[:4])
    if not (0 <= x0 < x1 <= FRAME_WIDTH and 0 <= y0 < y1 <= FRAME_HEIGHT):
        return False
    if not (MIN_BBOX_EDGE <= button.width <= MAX_BBOX_EDGE):
        return False
    if not (MIN_BBOX_EDGE <= button.height <= MAX_BBOX_EDGE):
        return False
    cx, cy = int(button.center[0]), int(button.center[1])
    return (ACTION_DISK_CENTER_X_MIN <= cx <= ACTION_DISK_CENTER_X_MAX
            and ACTION_DISK_CENTER_Y_MIN <= cy <= ACTION_DISK_CENTER_Y_MAX)


# ---------------------------------------------------------------------------
# 算牌引擎接入（复用 brain_v0，不自己写麻将规则）
# ---------------------------------------------------------------------------

def _engine_root() -> Path:
    """茶规则引擎根目录，与 ``tea_bot`` 同一处。"""
    return Path(__file__).resolve().parent.parent / "代打BOT" / "majiang-bot"


@lru_cache(maxsize=1)
def _engine() -> dict[str, Any] | None:
    """加载引擎 API；不可用返回 None（L2 因此只能弃权，绝不猜）。"""
    root = _engine_root()
    if not root.is_dir():
        return None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from brain_v0.engine.tea import pattern_analyzer as pattern
        from brain_v0.engine.tea import shanten
        from brain_v0.engine.tea import tiles
    except Exception:  # noqa: BLE001 - 引擎不可用必须 fail closed，不能炸调用方
        return None
    return {
        "calc_shanten": shanten.calc_shanten,
        "is_hu": pattern.is_hu,
        "counts_of": tiles.counts_of,
        "is_honor": tiles.is_honor,
        "root": str(root),
    }


def engine_available() -> bool:
    """引擎是否可用；不可用时 L2 只可能产出"过"。"""
    return _engine() is not None


def _incoming_of(result: Mapping[str, Any], vision_to_engine) -> int | None:
    """从返回体里取"刚打出的那张牌"的引擎编码；取不到或不可信一律 ``None``。

    来牌由 ``discard_vision.read_discards`` 读取，它自己已经保证"读不出来就是
    ``None``"。这里只负责**转码前的口径校验**，接受两种形状：

    * ``result["discards"]``：:class:`discard_vision.DiscardState` 本体，或它的
      ``as_dict()``（有 ``incoming_tile`` 键）；
    * ``result["incoming_tile"]``：直接是一个视觉编码。

    Args:
        result: ``assist_tea.analyze_snapshot`` 之类的返回体。
        vision_to_engine: 视觉编码 → 引擎编码的转换函数（白 67→73）。

    Returns:
        引擎编码；无法确认时 ``None``（此时 L2 只能出"过"）。
    """
    from tile_vision import TILE_IDS

    if not isinstance(result, Mapping):
        return None
    vision_ids = set(TILE_IDS.values())
    candidates: list[Any] = []
    discards = result.get("discards")
    if isinstance(discards, Mapping):
        candidates.append(discards.get("incoming_tile"))
    elif discards is not None:
        candidates.append(getattr(discards, "incoming_tile", None))
    candidates.append(result.get("incoming_tile"))
    for raw in candidates:
        # bool 是 int 的子类，必须先排掉，否则 True 会被当成牌 1。
        if isinstance(raw, bool) or not isinstance(raw, (int, np.integer)):
            continue
        if int(raw) not in vision_ids:
            # 不在视觉词汇里：可能是上游错塞了引擎编码（白=73），绝不能硬转。
            continue
        try:
            return vision_to_engine(int(raw))
        except Exception:  # noqa: BLE001 - 转码失败等于没有来牌
            return None
    return None


def _sparkle_centers(image: np.ndarray,
                     band: tuple[int, int, int, int]) -> tuple[int, ...]:
    """数一条带里的金色闪光标中心 x（照 ``discard_vision`` 的口径）。"""
    x0, y0, x1, y1 = (int(value) for value in band[:4])
    height, width = image.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1 <= x0 or y1 <= y0:
        return ()
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    gold = ((hue >= SPARKLE_HUE[0]) & (hue <= SPARKLE_HUE[1])
            & (sat > SPARKLE_MIN_SAT) & (val > SPARKLE_MIN_VAL))
    mask = cv2.morphologyEx(gold.astype(np.uint8), cv2.MORPH_OPEN,
                            np.ones((3, 3), np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    centers: list[int] = []
    for index in range(1, count):
        box_w = int(stats[index, cv2.CC_STAT_WIDTH])
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < SPARKLE_MIN_AREA or box_w > SPARKLE_MAX_WIDTH:
            continue
        centers.append(x0 + int(round(float(centroids[index][0]))))
    return tuple(sorted(centers))


def read_sparkle_sides(image: Any) -> dict[str, Any]:
    """读两侧的金色闪光标，回答"哪一边后打的"。

    Args:
        image: 1600x900 ``uint8`` BGR 帧；尺寸/类型不符时记 ``no_frame``。

    Returns:
        ``{"status", "opponent", "self", "usable", "bands"}``。``status`` 取
        ``"opponent"`` / ``"self"`` / ``"both"`` / ``"none"`` / ``"no_frame"``；
        只有前两者 ``usable`` 为 ``True``——**两侧都有标等于没标**（无法仲裁）。
    """
    result: dict[str, Any] = {
        "status": "no_frame",
        "opponent": (),
        "self": (),
        "usable": False,
        "bands": {"opponent": list(SPARKLE_BAND_OPPONENT),
                  "self": list(SPARKLE_BAND_SELF)},
    }
    if image is None or not isinstance(image, np.ndarray):
        return result
    if image.ndim != 3 or image.shape[2] < 3 or image.dtype != np.uint8:
        return result
    if image.shape[:2] != (FRAME_HEIGHT, FRAME_WIDTH):
        return result
    opponent = _sparkle_centers(image, SPARKLE_BAND_OPPONENT)
    own = _sparkle_centers(image, SPARKLE_BAND_SELF)
    result["opponent"] = opponent
    result["self"] = own
    if opponent and own:
        status = "both"
    elif opponent:
        status = INCOMING_SOURCE_OPPONENT
    elif own:
        status = INCOMING_SOURCE_SELF
    else:
        status = "none"
    result["status"] = status
    result["usable"] = status in (INCOMING_SOURCE_OPPONENT, INCOMING_SOURCE_SELF)
    return result


def arbitrate_incoming_source(
    image: Any,
    hand: HandState | None = None,
    config: ActionPolicyConfig | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """用闪光标仲裁来牌归属；仲裁不了返回 ``None``（fail-closed）。

    只在"归属没确认"时才有意义：``hand.incoming_source`` 已经有值就不动它
    （discard_vision 的两帧差分结论优先）。闪光标读不到、两侧都有标、没有
    画面 → 一律 ``None``，调用方维持原来的弃权。

    Args:
        image: 本帧画面；``None`` 时直接放弃仲裁。
        hand: 局面快照（只读）。
        config: 策略配置；``use_sparkle_arbitration=False`` 时不做仲裁。

    Returns:
        ``(归属 or None, 闪光标证据)``。
    """
    cfg = config or ActionPolicyConfig()
    if not cfg.use_sparkle_arbitration:
        return None, {"status": "disabled", "usable": False}
    if isinstance(hand, HandState) and hand.incoming_source is not None:
        # 差分已经给出归属：闪光标只当交叉验证，不覆盖。
        return None, {"status": "already_attributed", "usable": False,
                      "incoming_source": hand.incoming_source}
    sides = read_sparkle_sides(image)
    if not sides["usable"]:
        return None, sides
    return str(sides["status"]), sides


def _opponent_river_of(result: Mapping[str, Any]) -> tuple[int, ...]:
    """取对手牌河（视觉编码）；只接受整数，其余一律当没有。"""
    if not isinstance(result, Mapping):
        return ()
    raw: Any = None
    discards = result.get("discards")
    if isinstance(discards, Mapping):
        raw = discards.get("opponent_river")
    elif discards is not None:
        raw = getattr(discards, "opponent_river", None)
    if raw is None:
        raw = result.get("opponent_river")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(int(tile) for tile in raw
                 if not isinstance(tile, bool) and isinstance(tile, (int, np.integer)))


def _newest_opponent_tile(hand: HandState) -> int | None:
    """对手牌河最新一张（视觉编码）→ 引擎编码；拿不准返回 ``None``。

    对手牌河**右端锚定、向左延伸**，所以最新一张在 ``opponent_river[0]``。
    这个约定由 ``discard_vision.RiverReading.newest`` 定义（对手取
    ``tiles[0]``），测试里直接对着它钉死，不允许漂移。
    """
    if not hand.opponent_river:
        return None
    raw = hand.opponent_river[0]
    try:
        from tea_bot import vision_to_engine
        from tile_vision import TILE_IDS
    except Exception:  # noqa: BLE001 - 转码表拿不到就不猜牌
        return None
    if int(raw) not in set(TILE_IDS.values()):
        return None
    try:
        return vision_to_engine(int(raw))
    except Exception:  # noqa: BLE001 - 转码失败等于没有来牌
        return None


def resolve_incoming(
    image: Any,
    hand: HandState,
    config: ActionPolicyConfig | None = None,
) -> tuple[HandState, dict[str, Any]]:
    """归属未确认时用闪光标仲裁，必要时补回来牌。

    * ``self`` → 判定自家后打 → L2 的归属闸门把它全拦下（**行为与原来的
      "无法确认"一致**，只是弃权理由从"无法确认"变成有依据的"自家打出"）；
    * ``opponent`` → 判定别家后打，且若差分没给出牌就从对手牌河补回来牌；
    * 仲裁不了 → 原样返回，L2 按"归属未标注 + 来牌未知"弃权。

    Returns:
        ``(可能更新过的 HandState, 证据)``。原对象不被修改（frozen dataclass）。
    """
    cfg = config or ActionPolicyConfig()
    evidence: dict[str, Any] = {"sparkle": {"status": "skipped", "usable": False},
                                "arbitrated_source": None,
                                "recovered_tile": None}
    if not isinstance(hand, HandState):
        return hand, evidence
    source, sides = arbitrate_incoming_source(image, hand, cfg)
    evidence["sparkle"] = sides
    evidence["arbitrated_source"] = source
    if source is None:
        return hand, evidence
    updated = replace(hand, incoming_source=source)
    if (source == INCOMING_SOURCE_OPPONENT and cfg.sparkle_recover_tile
            and updated.incoming_tile is None):
        tile = _newest_opponent_tile(updated)
        if tile is not None:
            updated = replace(updated, incoming_tile=tile)
            evidence["recovered_tile"] = tile
    return updated, evidence


def _incoming_source_of(result: Mapping[str, Any]) -> str | None:
    """从返回体里取来牌归属；取不到就 ``None``（不拦，见 :class:`HandState`）。

    与 :func:`_incoming_of` 同形状：优先 ``result["discards"]``（可以是
    :class:`discard_vision.DiscardState` 本体或它的 ``as_dict()``），其次
    顶层的 ``result["incoming_source"]``。只接受非空字符串，其余一律
    ``None``——宁可"未标注"也不吃下一个可疑的归属。
    """
    if not isinstance(result, Mapping):
        return None
    raw: Any = None
    discards = result.get("discards")
    if isinstance(discards, Mapping):
        raw = discards.get("incoming_source")
    elif discards is not None:
        raw = getattr(discards, "incoming_source", None)
    if raw is None:
        raw = result.get("incoming_source")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


# ---------------------------------------------------------------------------
# 手牌状态（L2 输入）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HandState:
    """算牌引擎需要的局面快照，一律用**引擎编码**（白=73）。

    Attributes:
        concealed: 闭手牌（不含副露里的牌）。
        melds: 副露组，每组 3 张（吃/碰）或 4 张（杠）。
        caishen: 本局财神的引擎 ID；``None`` 表示未确认。
        incoming_tile: 触发本次行动的来牌（别家打出的那张）；``None``
            表示未知——此时 L2 无法验证任何吃碰杠胡。
        incoming_source: 谁打出的来牌 —— :data:`INCOMING_SOURCE_OPPONENT`
            / :data:`INCOMING_SOURCE_SELF` / ``None``（未标注）。
            ``"self"`` 或非这两个值的字符串都**不可**拿来吃碰杠胡。
            ``None`` 表示调用方没给归属（例如手工构造的局面），此时不因
            归属而拦下——生产链路上 ``discard_vision`` 只要给了牌就一定
            给了归属，所以不会出现"有牌没归属"的真空。
        opponent_river: 对手牌河（视觉编码，从左到右）。只在归属仲裁判为
            ``"opponent"`` 且来牌缺失时用来补回来牌，取 ``[0]``（对手牌河右端
            锚定、向左生长，最新一张在最左）。
        player_count: 局面人数（2 或 4）；``None`` 表示未确认。
        source: 证据来源，写进台账便于回溯。
    """

    concealed: tuple[int, ...] = ()
    melds: tuple[tuple[int, ...], ...] = ()
    caishen: int | None = None
    incoming_tile: int | None = None
    incoming_source: str | None = None
    opponent_river: tuple[int, ...] = ()
    player_count: int | None = None
    source: str = ""

    @property
    def meld_total(self) -> int:
        """副露物理总张数（按各组真实张数计，杠算 4 张）。

        这是**物理账**（画面可见牌数），只用于诊断与报错文案；张数守恒与
        暗手推算必须走 :attr:`logical_meld_slots` 的**逻辑账**——杠只占一个
        面子，第 4 张不扣暗手（契约见 ``shanten.logical_slot_count``）。
        """
        return sum(len(group) for group in self.melds)

    @property
    def logical_meld_slots(self) -> int:
        """副露逻辑位：每组一律 3 张（杠与吃/碰同价，契约见 kong-math 通告）。"""
        return 3 * len(self.melds)

    @property
    def total(self) -> int:
        """闭手 + 副露的物理总张数（仅诊断用，守恒判断见 :attr:`is_valid`）。"""
        return len(self.concealed) + self.meld_total

    @property
    def concealed_expected(self) -> int:
        """等待态闭手应有的张数：16 − 3×副露组数（杠后补牌，暗手不因杠少一张）。

        与 ``brain_v0.engine.tea.shanten.expected_concealed_count(melds=...)``
        同口径；测试里直接对着契约函数钉死，防止两边漂移。
        """
        return 16 - self.logical_meld_slots

    @property
    def is_valid(self) -> bool:
        """张数守恒：闭手 == 16 − 3×副露组数（等待态）。不守恒即不许决策。"""
        return len(self.concealed) == self.concealed_expected

    def counts(self) -> dict[int, int]:
        """闭手各牌张数；引擎不可用时用等价的本地计数（只数数，不判牌型）。"""
        engine = _engine()
        if engine is not None:
            return dict(engine["counts_of"](list(self.concealed)))
        counts: dict[int, int] = {}
        for tile in self.concealed:
            counts[int(tile)] = counts.get(int(tile), 0) + 1
        return counts

    @classmethod
    def from_analysis(
        cls,
        result: Mapping[str, Any] | None,
        *,
        player_count: int | None = None,
    ) -> "HandState":
        """从 ``assist_tea.analyze_snapshot`` / ``live_capture`` 的返回体构造。

        返回体里 ``hand_ids`` 与 ``melds`` 是**视觉编码**（发=66、白=67），
        这里统一转成引擎编码（发=72、白=73）。任何一步拿不到就返回空状态，
        由 :meth:`is_valid` 判否并弃权。
        """
        if not isinstance(result, Mapping) or not result:
            return cls(source="empty")
        try:
            from tea_bot import vision_to_engine
            from tile_vision import TILE_IDS
        except Exception:  # noqa: BLE001 - 转码表拿不到就不能构造可信手牌
            return cls(source="encoder_unavailable")

        raw_hand = result.get("hand_ids") or []
        if not isinstance(raw_hand, (list, tuple)):
            raw_hand = []
        concealed = tuple(
            vision_to_engine(int(tile))
            for tile in raw_hand
            if isinstance(tile, (int, np.integer))
        )

        raw_melds = result.get("melds") or []
        if not isinstance(raw_melds, (list, tuple)):
            raw_melds = []
        melds: list[tuple[int, ...]] = []
        for group in raw_melds:
            if not isinstance(group, (list, tuple)):
                continue
            melds.append(tuple(
                vision_to_engine(int(tile))
                for tile in group
                if isinstance(tile, (int, np.integer))
            ))

        label = result.get("caishen")
        caishen: int | None = None
        if isinstance(label, str) and label:
            vision_id = TILE_IDS.get(label)
            if vision_id is not None:
                caishen = vision_to_engine(int(vision_id))

        count = player_count
        if count is None:
            mode = result.get("mode")
            if mode == "tea_2p":
                count = 2
            elif mode == "tea_4p":
                count = 4
        return cls(concealed=concealed, melds=tuple(melds), caishen=caishen,
                   incoming_tile=_incoming_of(result, vision_to_engine),
                   incoming_source=_incoming_source_of(result),
                   opponent_river=_opponent_river_of(result),
                   player_count=count,
                   source="analyze_snapshot")


# ---------------------------------------------------------------------------
# 点击后的盘面状态（L3 输入）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoardState:
    """点击前/后的可观测盘面，用于 L3 状态迁移校验。

    所有字段都允许 ``None``——不可测的字段不参与判定，但**可测却不符**
    的字段一律判失败。

    Attributes:
        concealed_count: 闭手张数（识牌读数）。
        melds: 副露组，每组牌 ID 列表。
        claim_buttons: 画面上行动圆盘的数量（``claim_vision`` 口径）。
        settled: 是否进入结算画面。
        turn_owner: 倒计时归谁：``"me"`` / ``"opponent"``。
        selection_panel: 吃的候选选择面板是否还在画面上。只用于吃的 L3
            否决：测得 ``True`` 即判吃未完成；``None``（不可测）不单独
            否决，吃改由圆盘实测为 0 + 副露 +1 组 + 暗手 −2 三项正证据
            判定（与本类「不可测不参与判定」的既有契约一致）。
        note: 备注，写进台账。
    """

    concealed_count: int | None = None
    melds: tuple[tuple[int, ...], ...] | None = None
    claim_buttons: int | None = None
    settled: bool | None = None
    turn_owner: str | None = None
    selection_panel: bool | None = None
    note: str = ""

    @property
    def meld_group_count(self) -> int | None:
        """副露组数；``melds`` 未知时返回 ``None``。"""
        return None if self.melds is None else len(self.melds)

    @property
    def meld_tile_total(self) -> int | None:
        """副露总张数；``melds`` 未知时返回 ``None``。"""
        return None if self.melds is None else sum(len(g) for g in self.melds)

    @property
    def total(self) -> int | None:
        """闭手 + 副露总张数；任一不可测返回 ``None``。"""
        if self.concealed_count is None or self.meld_tile_total is None:
            return None
        return self.concealed_count + self.meld_tile_total

    @classmethod
    def from_hand_state(
        cls,
        hand: HandState,
        *,
        claim_buttons: int | None = None,
        settled: bool | None = None,
        turn_owner: str | None = None,
        selection_panel: bool | None = None,
        note: str = "",
    ) -> "BoardState":
        """把手牌状态包装成 L3 需要的盘面快照。"""
        return cls(
            concealed_count=len(hand.concealed) if hand is not None else None,
            melds=hand.melds if hand is not None else None,
            claim_buttons=claim_buttons,
            settled=settled,
            turn_owner=turn_owner,
            selection_panel=selection_panel,
            note=note,
        )


# ---------------------------------------------------------------------------
# 决策结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionDecision:
    """一次行动决策及它的三层证据。

    Attributes:
        action: ``"hu"/"peng"/"kong"/"chi"/"pass"``；空字符串表示没能解析出
            动作（弃权，证据仍在 ``l1_evidence`` / ``l2_reason`` 里）。
        target_tile: 涉及的牌（引擎编码），"过"为 ``None``。
        confidence: 0..1 的置信度，只用于排序与台账，不参与放行判定。
        l1_evidence: L1 定位证据（几何 + 模板匹配）。
        l2_reason: L2 引擎先验的结论（中文，写进台账）。
        blocked_reason: 非空表示被拦下（黑名单 / L1 / L2），绝不可点。
        needs_human_confirm: True 表示还没过人工确认闸门，不许自动点。
        button: 要点的圆盘（几何来自像素）。
        tap_point: 建议点击坐标；``None`` 表示没有可点目标。
        engine: L2 引擎原始输出（向听前后、可行性等）。
    """

    action: str = ACTION_NONE
    target_tile: int | None = None
    confidence: float = 0.0
    l1_evidence: dict[str, Any] = field(default_factory=dict)
    l2_reason: str = ""
    blocked_reason: str = ""
    needs_human_confirm: bool = True
    button: ClaimButton | None = None
    tap_point: tuple[int, int] | None = None
    engine: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        """是否可以交给点击通道执行（仍须点击通道本身可用）。"""
        return (not self.blocked_reason) and (not self.needs_human_confirm)

    @property
    def abstained(self) -> bool:
        """是否弃权（没解析出动作，或被拦下）。"""
        return (not self.action) or bool(self.blocked_reason)

    def ledger(self) -> dict[str, Any]:
        """JSON 可序列化的台账行（不写文件，交给调用方落盘）。"""
        return {
            "action": self.action,
            "target_tile": self.target_tile,
            "confidence": round(float(self.confidence), 4),
            "l1": self.l1_evidence,
            "l2_reason": self.l2_reason,
            "blocked_reason": self.blocked_reason,
            "needs_human_confirm": self.needs_human_confirm,
            "executable": self.executable,
            "tap_point": list(self.tap_point) if self.tap_point else None,
            "button": ({
                "kind": self.button.kind,
                "box": list(self.button.box),
                "center": list(self.button.center),
            } if self.button is not None else None),
            "engine": self.engine,
        }


# ---------------------------------------------------------------------------
# 配置与人工确认闸门
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionPolicyConfig:
    """行动策略配置。默认全部关闭：**只记台账，不点任何东西**。

    Attributes:
        auto_click_enabled: 自动点击总闸，默认 ``False``。
        confirmed_actions: 已看过回放确认过的动作集合，默认空。
        require_template_match: ``True`` 时 L1 必须有模板且过闸，否则弃权；
            **默认 ``True``**（P0-2）。无按钮模板的 L2 单测须显式传 ``False``。
            ``auto_click_enabled=True`` 时无论调用方设置一律强制 ``True``。
        max_claim_age_s: 声称行快照最大年龄（秒）。``None`` 不校验；
            有 ``ClaimState.captured_at`` 且超龄时 L1 弃权。
        template_provider: 模板来源（返回 ``{名字: 灰度图}``）；``None`` 用
            :data:`CLAIM_TEMPLATE_DIR`。
        forbidden_boxes: 黑名单 ROI（xyxy），命中即永不点击。
        forbidden_labels: 黑名单按钮名。
        caishen_substitutes_for_claim: 财神是否可替代吃碰杠的实体牌；
            默认 ``False``（未实测，按不替代处理＝少碰也不误碰）。
        chi_enabled: 是否启用"吃"；默认 ``False``——吃会弹二次选择面板
            （面板含 ``取消``），面板选组纯函数见
            :func:`choose_chi_panel_option`；循环连点编排已集成在
            ``auto_play_loop`` 的吃链中（点吃圆盘 → 读面板 → 选组 →
            点组 → L3 复核），但默认仍关闭，须 runner 显式打开。
        chi_player_counts: 允许吃的人数集合；默认 ``{2, 4}``。旧口径
            「2 人局无上家」已被实测证伪：2 人局里对手就是上家，
            吃/过窗口与吃的候选面板在 2 人局反复出现（归档帧与实机
            均有证据，见 ``chi_panel_vision`` 的标定帧）。
        allow_pass_without_claim: 只有"过"而没有金/红盘时是否出手；
            默认 ``False``（与已验证口径"过从不单独出现"不符，弃权）。
        require_improvement: 吃碰杠必须不劣化向听才出手；默认 ``True``。
    """

    auto_click_enabled: bool = False
    confirmed_actions: frozenset[str] = frozenset()
    require_template_match: bool = True
    max_claim_age_s: float | None = None
    template_provider: Callable[[], Mapping[str, Any]] | None = None
    forbidden_boxes: tuple[tuple[int, int, int, int], ...] = DEFAULT_FORBIDDEN_BOXES
    forbidden_labels: frozenset[str] = BLACKLISTED_LABELS
    caishen_substitutes_for_claim: bool = False
    chi_enabled: bool = False
    chi_player_counts: frozenset[int] = frozenset({2, 4})
    allow_pass_without_claim: bool = False
    require_improvement: bool = True
    # 归属无法确认时的仲裁开关（详见 :func:`resolve_incoming`）：
    # * use_sparkle_arbitration：False 时完全不读闪光标，归属未确认就维持弃权；
    # * sparkle_recover_tile：仲裁判为 "opponent" 但两帧差分没给出牌时，是否
    #   从对手牌河最新一张（opponent_river[0]）补回来牌。补不出（牌河为空 /
    #   牌不在视觉词汇里）一律不补，按"来牌未知"弃权。
    use_sparkle_arbitration: bool = True
    sparkle_recover_tile: bool = True


def human_confirm_required(action: str, config: ActionPolicyConfig) -> bool:
    """per-action 首次人工确认闸门；默认任何动作都要人工确认。"""
    if not action:
        return True
    if not config.auto_click_enabled:
        return True
    return action not in set(config.confirmed_actions)


def confirm_action(config: ActionPolicyConfig, action: str) -> ActionPolicyConfig:
    """登记"该动作已人工看回放确认"，返回新 config（不改原对象）。"""
    if action not in ALL_ACTIONS:
        raise ValueError(f"未知动作: {action!r}")
    return replace(config,
                   confirmed_actions=frozenset(set(config.confirmed_actions) | {action}))


# ---------------------------------------------------------------------------
# runner 推荐配置（给循环集成用）
# ---------------------------------------------------------------------------

#: 首版放行的已确认动作：胡/碰/杠/过——都是单次点击即完成、且 L3
#: （:func:`verify_after_click`）能做语义级验证的动作。
RECOMMENDED_CONFIRMED_ACTIONS: frozenset[str] = frozenset({
    ACTION_HU, ACTION_PENG, ACTION_KONG, ACTION_PASS,
})

#: 吃的面板选组（:func:`choose_chi_panel_option`）就绪后的放行集合。
RECOMMENDED_CONFIRMED_ACTIONS_WITH_CHI: frozenset[str] = (
    RECOMMENDED_CONFIRMED_ACTIONS | {ACTION_CHI}
)


def recommended_runner_config(*, auto_click_enabled: bool = False,
                              include_chi: bool = False) -> ActionPolicyConfig:
    """runner 的推荐配置样例。

    返回的仍是普通 :class:`ActionPolicyConfig`，不改任何默认值语义：
    ``auto_click_enabled`` 默认仍为 ``False``——要真自动点击，调用方必须
    显式传 ``auto_click_enabled=True``，这一步就是人工闸，不许绕过。

    Args:
        auto_click_enabled: 是否打开自动点击总闸（调用方显式决定）。
        include_chi: 是否把吃一并放行（同时打开 ``chi_enabled``，面板
            选组走 :func:`choose_chi_panel_option`）。
    """
    return ActionPolicyConfig(
        auto_click_enabled=bool(auto_click_enabled),
        confirmed_actions=(RECOMMENDED_CONFIRMED_ACTIONS_WITH_CHI
                           if include_chi else RECOMMENDED_CONFIRMED_ACTIONS),
        chi_enabled=bool(include_chi),
        require_template_match=True,
        template_provider=verified_claim_templates,
    )


def validate_runner_config(config: ActionPolicyConfig) -> tuple[str, ...]:
    """校验 runner 配置是否自洽；返回问题列表，空 tuple 表示通过。

    只查配置层面的自相矛盾，不查盘面：确认了未知动作、确认了吃却没开
    ``chi_enabled``（确认永远不会生效）、开自动点击却关模板匹配、开总闸
    却一个动作都没确认（总闸等于没开）。
    """
    problems: list[str] = []
    confirmed = set(config.confirmed_actions or ())
    unknown = sorted(confirmed - set(ALL_ACTIONS))
    if unknown:
        problems.append(
            f"confirmed_actions 含未知动作 {unknown}（只认 {sorted(ALL_ACTIONS)}）")
    if ACTION_CHI in confirmed and not config.chi_enabled:
        problems.append(
            "chi 已列入 confirmed_actions 但 chi_enabled=False："
            "吃永远不会被决策，这份确认不会生效")
    if config.auto_click_enabled and not config.require_template_match:
        problems.append(
            "auto_click_enabled=True 时 require_template_match 必须为 True"
            "（decide_action 虽会强制改写，runner 不应依赖改写）")
    if config.auto_click_enabled and not confirmed:
        problems.append(
            "auto_click_enabled=True 但 confirmed_actions 为空："
            "每个动作仍会逐个要求人工确认，总闸等于没开")
    return tuple(problems)


# ---------------------------------------------------------------------------
# L2：算牌引擎先验
# ---------------------------------------------------------------------------

#: 顺子花色区间（引擎编码）。来自 ``brain_v0.engine.tea.tiles``，只用于
#: 判断"两张牌是否同花色且相邻"，不参与任何牌型/计分判定。
_SUIT_RANGES: tuple[tuple[int, int], ...] = ((1, 9), (21, 29), (41, 49))


def _suit_of(tile: int) -> tuple[int, int] | None:
    """返回 ``tile`` 所在数牌区间的 ``(lo, hi)``；字牌返回 ``None``。"""
    for low, high in _SUIT_RANGES:
        if low <= tile <= high:
            return low, high
    return None


def _available(counts: Mapping[int, int], tile: int,
               caishen: int | None, allow_sub: bool) -> int:
    """可用来吃碰杠的 ``tile`` 张数（财神是否可替代由 ``allow_sub`` 决定）。"""
    real = int(counts.get(tile, 0))
    if allow_sub and caishen is not None and tile != caishen:
        real += int(counts.get(caishen, 0))
    return real


def _chi_options(counts: Mapping[int, int], tile: int) -> list[tuple[int, int, int]]:
    """列出用 ``tile`` 吃的所有合法顺子（只数数，不判牌型）。

    财神不参与顺子替代——"财神凑顺子"未见实测，一律不算，宁可不吃。
    """
    suit = _suit_of(tile)
    if suit is None:
        return []
    low, high = suit
    options: list[tuple[int, int, int]] = []
    for left, right in ((tile - 2, tile - 1), (tile - 1, tile + 1), (tile + 1, tile + 2)):
        if left < low or right > high:
            continue
        if counts.get(left, 0) >= 1 and counts.get(right, 0) >= 1:
            options.append(tuple(sorted((left, tile, right))))  # type: ignore[arg-type]
    return options


def _shanten_of(engine: Mapping[str, Any], concealed: Sequence[int],
                melds: Sequence[Sequence[int]], caishen: int | None) -> int | None:
    """调引擎算向听；引擎拒绝（张数不守恒等）返回 ``None``。"""
    try:
        value = engine["calc_shanten"](
            list(concealed),
            caishen_id=caishen,
            melds=[list(group) for group in melds],
        )
    except Exception:  # noqa: BLE001 - 引擎拒答即视为不可判定
        return None
    return int(value)


# ---------------------------------------------------------------------------
# 吃的二次选择面板：选哪一组顺子
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChiPanelChoice:
    """候选牌面板上选哪一组顺子的纯决策结果。

    ``group is None`` 表示弃权（不点任何候选组；调用方应走 ``过`` 或
    ``取消`` 的既有安全路径，切勿乱点面板）。

    Attributes:
        group: 选中的顺子（3 张引擎牌 ID，升序）；弃权时 ``None``。
        candidate_index: ``group`` 在调用方给的候选序列里的下标。
        hand_tiles: 要从闭手里出示的 2 张（``group`` 去掉来牌）。
        incoming_tile: 本次吃的来牌（引擎牌 ID）。
        shanten_before: 吃前向听；引擎拒答时 ``None``。
        shanten_after: 选中组吃后的向听。
        kept_shapes: 选中组吃后剩余闭手的「保留搭子」张数，见
            :func:`_kept_shape_tiles`。
        evaluated: 逐候选的评估明细（台账用）：下标、组、是否合法、
            需出示的牌、向听、保留搭子、不可用的原因。
        reason: 中文说明，选中与弃权都给。
    """

    group: tuple[int, int, int] | None = None
    candidate_index: int | None = None
    hand_tiles: tuple[int, int] = ()
    incoming_tile: int | None = None
    shanten_before: int | None = None
    shanten_after: int | None = None
    kept_shapes: int | None = None
    evaluated: tuple[dict[str, Any], ...] = ()
    reason: str = ""

    @property
    def chosen(self) -> bool:
        """是否选出了一组（``False`` = 弃权）。"""
        return self.group is not None

    def ledger(self) -> dict[str, Any]:
        """台账友好的字典（全部 JSON 可序列化）。"""
        return {
            "group": list(self.group) if self.group is not None else None,
            "candidate_index": self.candidate_index,
            "hand_tiles": list(self.hand_tiles),
            "incoming_tile": self.incoming_tile,
            "shanten_before": self.shanten_before,
            "shanten_after": self.shanten_after,
            "kept_shapes": self.kept_shapes,
            "evaluated": [dict(row) for row in self.evaluated],
            "reason": self.reason,
        }


def _kept_shape_tiles(concealed: Sequence[int], caishen: int | None) -> int:
    """剩余闭手里「有伴」的张数，作为同向听时的择优口径（保留搭子）。

    一张牌算「有伴」：同值有 ≥2 张（对子/刻子的一员），或同花色 ±1/±2
    内有牌（顺子搭/嵌张），或它本身就是财神（万能牌）。只数张数不判
    牌型——精确优劣由引擎向听负责，这里只做同分时的形状保留度粗排。
    """
    counts: dict[int, int] = {}
    for value in concealed:
        tile = int(value)
        counts[tile] = counts.get(tile, 0) + 1
    kept = 0
    for tile, copies in counts.items():
        if copies >= 2 or (caishen is not None and tile == caishen):
            kept += copies
            continue
        suit = _suit_of(tile)
        if suit is None:
            continue
        low, high = suit
        for neighbour in (tile - 2, tile - 1, tile + 1, tile + 2):
            if low <= neighbour <= high and counts.get(neighbour, 0) >= 1:
                kept += copies
                break
    return kept


def choose_chi_panel_option(
    hand: HandState | None,
    candidates: Sequence[Sequence[int]] | None,
    config: ActionPolicyConfig | None = None,
    *,
    incoming_tile: int | None = None,
) -> ChiPanelChoice:
    """吃点下去、候选牌面板弹出后，选哪一组顺子（纯函数，不读像素不点击）。

    选组规则（BOSS 口径）：

    1. 只考虑**合法候选**：恰 3 张互不相同、同花色连续、恰含来牌一次，
       且要出示的 2 张必须真实在闭手里（财神不替代顺子——与
       :func:`_chi_options` / :func:`evaluate_claim_candidates` 同一口径）；
    2. 只选**不劣化向听**的组（吃后向听 ≤ 吃前向听，引擎判定）；
    3. 同向听取**保留搭子多**者（:func:`_kept_shape_tiles`），再同分取
       面板上靠前的一组（输入顺序，确定性）。

    证据不足一律弃权（fail closed）：吃未启用、人数未确认或不在
    ``chi_player_counts`` 内、手牌张数不守恒、来牌未知、来牌归属不可
    claim、引擎不可用或拒答、无候选、候选全不合法或全都劣化向听。
    弃权时 ``group is None`` 且 ``reason`` 说明原因，调用方不得点面板。

    Args:
        hand: 当前手牌状态（来牌/归属/人数从这里取）。
        candidates: 面板上的候选顺子组（视觉层读出的组，或合成组）。
        config: 策略配置；``None`` 用默认配置（``chi_enabled=False``
            → 弃权）。
        incoming_tile: 来牌覆盖值（视觉层另行确认来牌时传）；缺省用
            ``hand.incoming_tile``。
    """
    cfg = config if config is not None else ActionPolicyConfig()

    if not cfg.chi_enabled:
        return ChiPanelChoice(reason="吃未启用（chi_enabled=False），面板选组弃权")
    if not isinstance(hand, HandState):
        return ChiPanelChoice(reason="手牌状态缺失，面板选组弃权")
    if not hand.is_valid:
        return ChiPanelChoice(
            reason=(f"手牌张数不守恒（闭手 {len(hand.concealed)}、副露 "
                    f"{len(hand.melds)} 组），面板选组弃权"))
    if hand.player_count is None:
        return ChiPanelChoice(reason="人数未确认，面板选组弃权")
    if int(hand.player_count) not in {int(n) for n in cfg.chi_player_counts}:
        return ChiPanelChoice(
            reason=f"{hand.player_count} 人局不在吃评估范围，面板选组弃权")
    tile = incoming_tile if incoming_tile is not None else hand.incoming_tile
    if tile is None:
        return ChiPanelChoice(reason="来牌未知，面板选组弃权")
    tile = int(tile)
    if (hand.incoming_source is not None
            and hand.incoming_source not in CLAIMABLE_INCOMING_SOURCES):
        return ChiPanelChoice(
            incoming_tile=tile,
            reason=f"来牌归属为 {hand.incoming_source!r}，不可吃，面板选组弃权")
    engine = _engine()
    if engine is None:
        return ChiPanelChoice(incoming_tile=tile,
                              reason="算牌引擎不可用，面板选组弃权")
    if not candidates:
        return ChiPanelChoice(incoming_tile=tile,
                              reason="面板上没有候选顺子，选组弃权")

    counts = hand.counts()
    melds = [list(group) for group in hand.melds]
    before = _shanten_of(engine, hand.concealed, melds, hand.caishen)
    if before is None:
        return ChiPanelChoice(incoming_tile=tile,
                              reason="引擎拒答当前手牌向听，面板选组弃权")

    evaluated: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates):
        row: dict[str, Any] = {"index": index, "group": None, "valid": False,
                               "hand_tiles": [], "shanten_after": None,
                               "kept_shapes": None, "why": ""}
        try:
            group = tuple(sorted(int(v) for v in raw))
        except (TypeError, ValueError):
            row["why"] = "候选组不是牌 ID 序列"
            evaluated.append(row)
            continue
        row["group"] = list(group)
        if len(group) != 3 or len(set(group)) != 3:
            row["why"] = "候选组不是 3 张互不相同的牌"
            evaluated.append(row)
            continue
        if tile not in group:
            row["why"] = "候选组不含来牌"
            evaluated.append(row)
            continue
        suit = _suit_of(group[0])
        if (suit is None or _suit_of(group[1]) != suit
                or _suit_of(group[2]) != suit or group[2] - group[0] != 2):
            row["why"] = "候选组不是同花色顺子"
            evaluated.append(row)
            continue
        partners = [v for v in group if v != tile]
        row["hand_tiles"] = partners
        if any(counts.get(partner, 0) < 1 for partner in partners):
            row["why"] = "闭手里缺候选组要出示的实体牌（财神不替代顺子）"
            evaluated.append(row)
            continue
        pool = list(partners)
        remaining: list[int] = []
        for value in hand.concealed:
            current = int(value)
            if current in pool:
                pool.remove(current)
                continue
            remaining.append(current)
        after = _shanten_of(engine, remaining, melds + [list(group)],
                            hand.caishen)
        if after is None:
            row["why"] = "引擎拒答该组成吃后的向听"
            evaluated.append(row)
            continue
        row["valid"] = True
        row["shanten_after"] = int(after)
        row["kept_shapes"] = _kept_shape_tiles(remaining, hand.caishen)
        evaluated.append(row)
        if int(after) <= before:
            usable.append(row)

    if not usable:
        return ChiPanelChoice(
            incoming_tile=tile, shanten_before=before,
            evaluated=tuple(evaluated),
            reason="没有不劣化向听的合法候选顺子，面板选组弃权")
    usable.sort(key=lambda r: (r["shanten_after"], -int(r["kept_shapes"]),
                               int(r["index"])))
    best = usable[0]
    group = tuple(int(v) for v in best["group"])
    return ChiPanelChoice(
        group=group,
        candidate_index=int(best["index"]),
        hand_tiles=tuple(int(v) for v in best["hand_tiles"]),
        incoming_tile=tile,
        shanten_before=before,
        shanten_after=int(best["shanten_after"]),
        kept_shapes=int(best["kept_shapes"]),
        evaluated=tuple(evaluated),
        reason=(f"选第 {best['index']} 组 {list(group)}：向听 "
                f"{before}→{best['shanten_after']} 不劣化，"
                f"剩余闭手保留搭子 {best['kept_shapes']} 张"),
    )


def evaluate_claim_candidates(hand: HandState,
                              config: ActionPolicyConfig) -> dict[str, dict[str, Any]]:
    """L2 先验：逐个动作问引擎"这个按钮应不应该出现"。

    Returns:
        ``{action: {"feasible", "why", "shanten_before", "shanten_after",
        "delta", "improves", "concealed_after", "melds_after", "options"}}``。

        ``feasible`` 为 ``False`` 时 ``why`` 给出中文原因；引擎不可用、
        手牌不守恒或来牌未知时，吃碰杠胡一律 ``feasible=False``。
    """
    blank: dict[str, Any] = {
        "feasible": False, "why": "", "shanten_before": None,
        "shanten_after": None, "delta": None, "improves": False,
        "concealed_after": None, "melds_after": None, "options": (),
    }
    report: dict[str, dict[str, Any]] = {
        action: dict(blank) for action in CLAIM_ACTIONS
    }
    engine = _engine()
    if engine is None:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = "算牌引擎不可用，L2 无法验证，弃权"
        return report
    if not isinstance(hand, HandState) or not hand.is_valid:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = (
                f"手牌张数不守恒（闭手{len(hand.concealed)}，副露{len(hand.melds)}组/"
                f"逻辑{hand.logical_meld_slots}张，等待态应为 "
                f"{hand.concealed_expected}），L2 弃权"
                if isinstance(hand, HandState) else "手牌状态缺失，L2 弃权")
        return report
    tile = hand.incoming_tile
    if tile is None:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = "来牌未知（无读牌链路），L2 无法验证吃碰杠胡"
        return report
    # 归属闸门：吃/碰/杠/胡 只接受对手来牌。来源未知一律弃权（证据不足，
    # 不得把 None 当可执行）；自己刚打出的牌同样 fail-closed。
    source = hand.incoming_source
    if source is None:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = "来牌归属未知，证据不足，L2 弃权"
        return report
    if source not in CLAIMABLE_INCOMING_SOURCES:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = (
                f"来牌归属为 {source!r}（只有 {INCOMING_SOURCE_OPPONENT!r} "
                f"才可能被吃碰杠胡），L2 弃权")
        return report

    tile = int(tile)
    counts = hand.counts()
    caishen = hand.caishen
    allow_sub = bool(config.caishen_substitutes_for_claim)
    melds = [list(group) for group in hand.melds]
    before = _shanten_of(engine, hand.concealed, melds, caishen)
    for action in CLAIM_ACTIONS:
        report[action]["shanten_before"] = before

    # ---- 胡：直接问引擎（副露局面的胡牌判定未验收，一律不认） ----
    if hand.melds:
        report[ACTION_HU]["why"] = "副露局面胡牌判定未验收，不认胡"
    else:
        try:
            hu = bool(engine["is_hu"](list(hand.concealed) + [tile],
                                      caishen_id=caishen))
        except Exception:  # noqa: BLE001 - 引擎拒答即不可判定
            hu = False
            report[ACTION_HU]["why"] = "胡牌分析器拒绝该手牌，不认胡"
        report[ACTION_HU]["feasible"] = hu
        if hu:
            report[ACTION_HU]["why"] = "引擎判定闭手+来牌成胡"
        elif not report[ACTION_HU]["why"]:
            report[ACTION_HU]["why"] = "引擎判定闭手+来牌不成胡"

    # ---- 碰：闭手里至少两张（财神是否可替代由 config 决定） ----
    pairs = _available(counts, tile, caishen, allow_sub)
    if pairs >= 2:
        _fill_claim(report[ACTION_PENG], engine, hand, tile, melds, caishen,
                    remove_tiles=[tile, tile], new_meld=[tile, tile, tile],
                    why="闭手里有两张，可碰")
    else:
        report[ACTION_PENG]["why"] = f"闭手里只有 {pairs} 张，不能碰"

    # ---- 杠：闭手三张（明杠）；或已碰过该牌再补一张（加杠） ----
    concealed_kong = _available(counts, tile, caishen, allow_sub) >= 3
    upgrade_index = -1
    for index, group in enumerate(melds):
        if len(group) == 3 and len(set(group)) == 1 and int(group[0]) == tile:
            upgrade_index = index
            break
    if concealed_kong:
        _fill_claim(report[ACTION_KONG], engine, hand, tile, melds, caishen,
                    remove_tiles=[tile, tile, tile],
                    new_meld=[tile, tile, tile, tile],
                    why="闭手里有三张，可明杠")
    elif upgrade_index >= 0 and _available(counts, tile, caishen, allow_sub) >= 1:
        after_melds = [list(group) for group in melds]
        after_melds[upgrade_index] = [tile, tile, tile, tile]
        _fill_claim(report[ACTION_KONG], engine, hand, tile, melds, caishen,
                    remove_tiles=[tile], melds_after=after_melds,
                    why="已碰该牌且闭手里还有一张，可加杠")
    else:
        report[ACTION_KONG]["why"] = "闭手里不足三张且无可加杠的碰组，不能杠"

    # ---- 吃：会弹二次选择面板，默认关闭 ----
    if not config.chi_enabled:
        report[ACTION_CHI]["why"] = "吃未启用（chi_enabled=False），不评估吃"
    elif hand.player_count is None:
        report[ACTION_CHI]["why"] = "人数未确认，不评估吃"
    elif int(hand.player_count) not in set(config.chi_player_counts):
        report[ACTION_CHI]["why"] = f"{hand.player_count} 人局不评估吃"
    else:
        options = _chi_options(counts, tile)
        if not options:
            report[ACTION_CHI]["why"] = "闭手里凑不出含来牌的顺子，不能吃"
        else:
            report[ACTION_CHI]["options"] = tuple(options)
            first = list(options[0])
            partners = [value for value in first if value != tile]
            _fill_claim(report[ACTION_CHI], engine, hand, tile, melds, caishen,
                        remove_tiles=partners, new_meld=first,
                        why=f"可吃（{len(options)} 种顺子）")
    return report


def _fill_claim(entry: dict[str, Any], engine: Mapping[str, Any], hand: HandState,
                tile: int, melds: Sequence[Sequence[int]], caishen: int | None,
                *, remove_tiles: Sequence[int],
                new_meld: Sequence[int] | None = None,
                melds_after: Sequence[Sequence[int]] | None = None,
                why: str) -> None:
    """把"若执行该动作"的引擎结果填进 ``entry``。

    Args:
        remove_tiles: 要从闭手里拿走的实体牌（碰拿两张来牌、杠拿三张、加杠
            拿一张、吃拿顺子里另外两张）。拿不出这些牌即判不可行——绝不
            凭空补牌。
    """
    pool = [int(value) for value in remove_tiles]
    remaining: list[int] = []
    for value in hand.concealed:
        tile_id = int(value)
        if tile_id in pool:
            pool.remove(tile_id)
            continue
        remaining.append(tile_id)
    if pool:
        entry["feasible"] = False
        entry["why"] = "闭手里凑不出足够的实体牌，L2 弃权"
        return
    if melds_after is not None:
        after_melds = [list(group) for group in melds_after]
    else:
        after_melds = [list(group) for group in melds] + [list(new_meld or [])]
    after = _shanten_of(engine, remaining, after_melds, caishen)
    entry["feasible"] = after is not None
    entry["why"] = why if after is not None else f"{why}，但引擎拒答该形态，弃权"
    entry["shanten_after"] = after
    entry["concealed_after"] = tuple(remaining)
    entry["melds_after"] = tuple(tuple(group) for group in after_melds)
    if after is not None and entry["shanten_before"] is not None:
        entry["delta"] = int(after) - int(entry["shanten_before"])
        entry["improves"] = int(after) <= int(entry["shanten_before"])


def _gold_by_template_label(
    gold_buttons: Sequence[ClaimButton],
    per_button: Mapping[int, dict[str, Any]],
    report: Mapping[str, dict[str, Any]],
) -> tuple[str, ClaimButton] | None:
    """用 L1 模板认出的字去锁定金盘是碰/吃/杠中的哪一个。

    模板必须**过闸**（top1 与次名差都达标）才认；认出的动作还必须被 L2
    引擎判为可行，两个条件缺一不可。多个金盘各自认出不同动作时，按
    :data:`ACTION_PRIORITY` 取最高的那个。

    Returns:
        ``(action, button)``；认不出来返回 ``None``，交给引擎消歧。
    """
    matched: list[tuple[str, ClaimButton]] = []
    for index, matched_info in per_button.items():
        if matched_info.get("status") != TEMPLATE_STATUS_MATCHED:
            continue
        label = str(matched_info.get("best") or "")
        if label not in CLAIM_ACTIONS or label == ACTION_HU:
            continue
        if not report.get(label, {}).get("feasible"):
            continue
        for button in gold_buttons:
            if list(button.box) == list(matched_info.get("box") or ()):
                matched.append((label, button))
                break
    if not matched:
        return None
    for action in ACTION_PRIORITY:
        for label, button in matched:
            if label == action:
                return label, button
    return None


def _finish_claim(
    action: str,
    button: ClaimButton,
    target_tile: int | None,
    config: ActionPolicyConfig,
    evidence: dict[str, Any],
    info: dict[str, Any],
    report: dict[str, dict[str, Any]],
    per_button: Mapping[int, dict[str, Any]],
    pass_button: ClaimButton | None,
    claim_state: ClaimState,
    image: Any,
) -> ActionDecision:
    """吃/碰/杠的最后一道：向听不劣化才出手，否则改按过或直接弃权。"""
    if config.require_improvement and not info.get("improves"):
        gap = f"{info.get('shanten_before')}→{info.get('shanten_after')}"
        if pass_button is not None:
            return _accept(ACTION_PASS, pass_button, None, config, evidence, {},
                           report,
                           l2=f"引擎判定{action}会劣化向听（{gap}），改按过",
                           template_status=_status_for(per_button, pass_button))
        return _blocked(claim_state, image, config,
                        f"引擎判定{action}会劣化向听（{gap}），且无可点的过按钮，弃权",
                        l2_reason=str(info.get("why") or ""), engine=report)
    return _accept(action, button, target_tile, config, evidence, info, report,
                   l2=str(info.get("why") or ""),
                   template_status=_status_for(per_button, button))


# ---------------------------------------------------------------------------
# L1 + L2 → 决策
# ---------------------------------------------------------------------------

_CONF_BASE: float = 0.35
_CONF_TEMPLATE_MATCHED: float = 0.35
_CONF_TEMPLATE_ABSENT: float = 0.05
_CONF_ENGINE_CONFIRMED: float = 0.20
_CONF_IMPROVES: float = 0.10


def _confidence(*, template_status: str, engine_confirmed: bool,
                improves: bool) -> float:
    """置信度只用于排序与台账，不参与放行判定。"""
    value = _CONF_BASE
    if template_status == TEMPLATE_STATUS_MATCHED:
        value += _CONF_TEMPLATE_MATCHED
    elif template_status == TEMPLATE_STATUS_NO_TEMPLATES:
        value += _CONF_TEMPLATE_ABSENT
    if engine_confirmed:
        value += _CONF_ENGINE_CONFIRMED
    if improves:
        value += _CONF_IMPROVES
    return round(min(1.0, max(0.0, value)), 4)


def _l1_evidence(claim_state: ClaimState, image: Any,
                 config: ActionPolicyConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑 L1：几何 + 模板。返回 ``(证据, {按钮下标: 模板结果})``。"""
    buttons = [b for b in (claim_state.buttons or ()) if isinstance(b, ClaimButton)]
    templates = load_claim_templates(config.template_provider)
    per_button: dict[int, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for index, button in enumerate(buttons):
        matched = match_button_glyph(image, button, templates or None)
        per_button[index] = matched
        rows.append({
            "kind": button.kind,
            "box": list(button.box),
            "center": list(button.center),
            "size": [button.width, button.height],
            "geometry_ok": _geometry_ok(button),
            "template": matched,
        })
    statuses = [row["template"]["status"] for row in rows]
    if TEMPLATE_STATUS_NO_TEMPLATES in statuses:
        aggregate = TEMPLATE_STATUS_NO_TEMPLATES
    elif TEMPLATE_STATUS_MATCHED in statuses:
        aggregate = TEMPLATE_STATUS_MATCHED
    elif TEMPLATE_STATUS_AMBIGUOUS in statuses:
        aggregate = TEMPLATE_STATUS_AMBIGUOUS
    elif TEMPLATE_STATUS_UNMATCHED in statuses:
        aggregate = TEMPLATE_STATUS_UNMATCHED
    elif statuses:
        aggregate = TEMPLATE_STATUS_SIZE_MISMATCH
    else:
        aggregate = TEMPLATE_STATUS_NO_TEMPLATES
    evidence = {
        "buttons": rows,
        "geometry_ok": all(row["geometry_ok"] for row in rows) if rows else False,
        "template_status": aggregate,
        "template_count": len(templates),
        "template_gate": {"top1_min": TEMPLATE_TOP1_MIN,
                          "margin_min": TEMPLATE_MARGIN_MIN},
        "claim_decision": claim_state.decision,
        "claim_reason": claim_state.reason,
        "frame": {"width": FRAME_WIDTH, "height": FRAME_HEIGHT},
    }
    return evidence, per_button


def _button_of(claim_state: ClaimState, kind: str) -> ClaimButton | None:
    """取该 kind 最靠左的圆盘；没有返回 ``None``。"""
    found = [b for b in (claim_state.buttons or ())
             if isinstance(b, ClaimButton) and b.kind == kind]
    if not found:
        return None
    return min(found, key=lambda b: (b.center[0], b.center[1]))


def _blocked(claim_state: ClaimState, image: Any, config: ActionPolicyConfig,
             reason: str, l2_reason: str = "",
             engine: dict[str, Any] | None = None) -> ActionDecision:
    """构造一条"被拦下"的决策：保留证据，绝不给出可点坐标。"""
    evidence, _per_button = _l1_evidence(claim_state, image, config)
    return ActionDecision(
        action=ACTION_NONE,
        confidence=0.0,
        l1_evidence=evidence,
        l2_reason=l2_reason,
        blocked_reason=reason,
        needs_human_confirm=True,
        button=None,
        tap_point=None,
        engine=engine or {},
    )


def decide_action(
    claim_state: ClaimState | None,
    hand_state: HandState | None = None,
    config: ActionPolicyConfig | None = None,
    *,
    image: Any = None,
) -> ActionDecision | None:
    """串起 L1 定位与 L2 引擎先验，给出一个动作或弃权。

    Args:
        claim_state: ``claim_vision.read_claims`` 的结果。
        hand_state: 局面快照；``None`` 按空局面处理（只可能弃权）。
        config: 策略配置；``None`` 用默认（自动点击关闭）。
        image: 可选的本帧图像；给了才做模板匹配（L1 的第二道口径）。

    Returns:
        * ``None``——读不出按钮，或出现了 ``取消``：彻底弃权，什么都不做；
        * :class:`ActionDecision` 且 ``blocked_reason`` 非空——有按钮但被拦下
          （黑名单 / L1 不过闸 / L2 否决），同样**不点**，只落证据；
        * :class:`ActionDecision` 且 ``blocked_reason`` 为空——可以交给点击
          通道；是否真的能点还要看 ``needs_human_confirm`` 与点击通道本身。
    """
    cfg = config or ActionPolicyConfig()
    # P0-2: auto-click never runs without a real button glyph match.
    if cfg.auto_click_enabled and not cfg.require_template_match:
        cfg = replace(cfg, require_template_match=True)
    hand = hand_state if isinstance(hand_state, HandState) else HandState()
    if not isinstance(claim_state, ClaimState):
        return None
    buttons = [b for b in (claim_state.buttons or ()) if isinstance(b, ClaimButton)]
    if not claim_state.known or not buttons:
        return None

    kinds = {button.kind for button in buttons}
    # 取消盘：点它会不可逆地中断进行中的选择 → 一律弃权。
    if KIND_CANCEL in kinds:
        return None

    # L0：黑名单闸门（标签 + 实测 ROI），命中即永不产出动作。
    hit = blacklist_hit(buttons, cfg)
    if hit is None:
        templates = load_claim_templates(cfg.template_provider)
        for button in buttons:
            matched = match_button_glyph(image, button, templates or None)
            # 只有模板过闸（top1 与次名差都达标）才认这个标签，避免误伤。
            if matched.get("status") != TEMPLATE_STATUS_MATCHED:
                continue
            hit = blacklist_hit(buttons, cfg, matched_label=matched.get("best"))
            if hit is not None:
                break
    if hit is not None:
        return _blocked(claim_state, image, cfg, hit)

    evidence, per_button = _l1_evidence(claim_state, image, cfg)
    if not evidence["geometry_ok"]:
        return _blocked(claim_state, image, cfg,
                        "圆盘几何不在已验证的动作行口径内，L1 弃权")
    if cfg.require_template_match and evidence["template_status"] != TEMPLATE_STATUS_MATCHED:
        return _blocked(claim_state, image, cfg,
                        f"L1 模板未过闸（status={evidence['template_status']}，"
                        f"要求 top1≥{TEMPLATE_TOP1_MIN} 且次名差≥{TEMPLATE_MARGIN_MIN}）")
    # P0-2: stale claim-row snapshot must not drive a tap.
    age_limit = cfg.max_claim_age_s
    captured_at = getattr(claim_state, "captured_at", None)
    if age_limit is not None and captured_at is not None:
        import time as _time
        age_s = _time.time() - float(captured_at)
        evidence["claim_age_s"] = round(age_s, 3)
        evidence["max_claim_age_s"] = float(age_limit)
        if age_s > float(age_limit):
            return _blocked(claim_state, image, cfg,
                            f"L1 声称行快照过期（age={age_s:.3f}s > {age_limit}s）")

    # 归属未确认时用闪光标仲裁（"哪边后打"）；仲裁结果写进证据。
    hand, incoming_evidence = resolve_incoming(image, hand, cfg)
    evidence["incoming"] = incoming_evidence
    report = evaluate_claim_candidates(hand, cfg)
    hu_button = _button_of(claim_state, KIND_HU)
    pass_button = _button_of(claim_state, KIND_PASS)
    gold_buttons = [b for b in buttons if b.kind == KIND_GOLD]

    # ---- 胡：引擎必须点头 ----
    if hu_button is not None:
        info = report[ACTION_HU]
        if info["feasible"]:
            return _accept(ACTION_HU, hu_button, hand.incoming_tile, cfg,
                           evidence, info, report,
                           l2=info["why"], template_status=_status_for(per_button, hu_button))
        return _blocked(claim_state, image, cfg,
                        f"L2 否决：画面出现胡盘，但{info['why']}",
                        l2_reason=info["why"], engine=report)

    # ---- 金盘：碰/吃/杠；L1 分不出来，先用模板认字，再用引擎消歧 ----
    if gold_buttons:
        by_label = _gold_by_template_label(gold_buttons, per_button, report)
        if by_label:
            action, button = by_label
            info = report[action]
            return _finish_claim(action, button, hand.incoming_tile, cfg, evidence,
                                 info, report, per_button, pass_button,
                                 claim_state, image)
        feasible = [action for action in (ACTION_KONG, ACTION_PENG, ACTION_CHI)
                    if report[action]["feasible"]]
        if not feasible:
            why = "；".join(f"{action}: {report[action]['why']}"
                            for action in (ACTION_KONG, ACTION_PENG, ACTION_CHI))
            # 第一版连续代打：无法安全确认吃/碰/杠时，若“过”本身已通过
            # 独立模板闸，就明确放弃本次机会继续牌局。过不会改变手牌/副露，
            # 比让行动面板永久卡住更保守；读不清过按钮仍保持原弃权。
            pass_status = _status_for(per_button, pass_button)
            if (cfg.auto_click_enabled and pass_button is not None
                    and (not cfg.require_template_match
                         or pass_status == TEMPLATE_STATUS_MATCHED)):
                return _accept(
                    ACTION_PASS, pass_button, None, cfg, evidence, {}, report,
                    l2=f"吃碰杠均未获完整证据（{why}）；安全选择过",
                    template_status=pass_status)
            return _blocked(claim_state, image, cfg,
                            "L2 否决：画面出现金盘，但引擎判定碰/吃/杠皆不可行，弃权",
                            l2_reason=why, engine=report)
        if len(feasible) > 1:
            names = "、".join(feasible)
            pass_status = _status_for(per_button, pass_button)
            if (cfg.auto_click_enabled and pass_button is not None
                    and (not cfg.require_template_match
                         or pass_status == TEMPLATE_STATUS_MATCHED)):
                return _accept(
                    ACTION_PASS, pass_button, None, cfg, evidence, {}, report,
                    l2=f"L2 同时有 {len(feasible)} 种可能（{names}），不猜；安全选择过",
                    template_status=pass_status)
            return _blocked(claim_state, image, cfg,
                            f"L1 分不出金盘是碰还是吃/杠，L2 有 {len(feasible)} 种可能（{names}），"
                            f"弃权（不猜）",
                            l2_reason="；".join(f"{a}: {report[a]['why']}" for a in feasible),
                            engine=report)
        action = feasible[0]
        info = report[action]
        button = min(gold_buttons, key=lambda b: b.center[0])
        return _finish_claim(action, button, hand.incoming_tile, cfg, evidence,
                             info, report, per_button, pass_button,
                             claim_state, image)

    # ---- 只有"过"：与已验证口径（过从不单独出现）不符，默认弃权 ----
    if pass_button is not None:
        if not cfg.allow_pass_without_claim:
            return _blocked(claim_state, image, cfg,
                            "只检出过按钮而没有金/红盘，与已验证口径不符，弃权")
        return _accept(ACTION_PASS, pass_button, None, cfg, evidence, {}, report,
                       l2="仅有过按钮，按过处理",
                       template_status=_status_for(per_button, pass_button))
    return None


def _status_for(per_button: Mapping[int, dict[str, Any]],
                button: ClaimButton | None) -> str:
    """取该按钮的模板状态；找不到时按 ``no_templates`` 计（不加分）。"""
    if button is None:
        return TEMPLATE_STATUS_NO_TEMPLATES
    for matched in per_button.values():
        if list(matched.get("box") or ()) == list(button.box):
            return str(matched.get("status") or TEMPLATE_STATUS_NO_TEMPLATES)
    return TEMPLATE_STATUS_NO_TEMPLATES


def _accept(action: str, button: ClaimButton, target_tile: int | None,
            config: ActionPolicyConfig, evidence: dict[str, Any],
            info: dict[str, Any], report: dict[str, dict[str, Any]], *,
            l2: str, template_status: str) -> ActionDecision:
    """构造一条可执行的决策（是否真能点仍看人工确认闸门）。"""
    improves = bool(info.get("improves")) if action != ACTION_HU else True
    confidence = _confidence(template_status=template_status,
                             engine_confirmed=action != ACTION_PASS,
                             improves=improves or action == ACTION_HU)
    return ActionDecision(
        action=action,
        target_tile=None if action == ACTION_PASS else target_tile,
        confidence=confidence,
        l1_evidence=evidence,
        l2_reason=l2,
        blocked_reason="",
        needs_human_confirm=human_confirm_required(action, config),
        button=button,
        tap_point=(int(button.center[0]), int(button.center[1])),
        engine={action: dict(info), "all": report},
    )


def read_and_decide(
    image: Any,
    hand_state: HandState | None = None,
    config: ActionPolicyConfig | None = None,
) -> ActionDecision | None:
    """``read_claims`` + ``decide_action`` 的一步式入口（纯函数，不落盘）。"""
    return decide_action(read_claims(image), hand_state, config, image=image)


# ---------------------------------------------------------------------------
# L3：点击后的状态迁移校验
# ---------------------------------------------------------------------------

def verify_after_click(
    before_state: BoardState | None,
    after_state: BoardState | None,
    action: str,
) -> tuple[bool, str]:
    """L3：点击后的盘面是否按预期迁移。

    预期（任一可测项不符即判失败）：

    * 碰/吃：暗手 −2、多出一组副露、行动圆盘消失；
    * 杠：暗手 −3 且多出一组副露（明杠），或暗手 −1 且某组由 3 张变 4 张
      （加杠）；
    * 胡：进入结算画面，或至少行动圆盘消失；
    * 过：副露不变、行动圆盘消失、倒计时转到我方；暗手保持不变，或在
      “过”后立即轮到我方摸牌时合法 +1。

    证据口径（fail closed）：行动圆盘消失必须由 ``after.claim_buttons``
    实测为 0 来证明——该字段为 ``None``（不可测）时，碰/吃/杠/过一律判
    未证实。吃另查 ``after.selection_panel``：测得仍在画面上即判吃未
    完成；不可测时不单独否决，靠圆盘/副露/暗手三项正证据兜底。胡的口径
    不变：结算画面或圆盘消失任一成立即可，两者都不可测时判未证实。

    Args:
        before_state: 点击前的盘面快照。
        after_state: 点击后的盘面快照。
        action: 实际执行（或计划执行）的动作。

    Returns:
        ``(是否通过, 中文原因)``。不通过时调用方必须立即停机并落证据。
    """
    if action not in ALL_ACTIONS:
        return False, f"未知动作 {action!r}，无法校验点击结果"
    if not isinstance(before_state, BoardState):
        return False, "缺少点击前的盘面证据，无法校验（fail closed）"
    if not isinstance(after_state, BoardState):
        return False, "缺少点击后的盘面证据，无法校验（fail closed）"

    ok, why = _verify_common(before_state, after_state, action)
    if not ok:
        return False, why
    if action == ACTION_HU:
        return _verify_hu(before_state, after_state)
    if action == ACTION_PASS:
        return _verify_pass(before_state, after_state)
    if action == ACTION_KONG:
        return _verify_kong(before_state, after_state)
    return _verify_simple_claim(before_state, after_state, action)


def _verify_common(before: BoardState, after: BoardState,
                   action: str) -> tuple[bool, str]:
    """所有动作共用的判据：结算画面与倒计时归属。"""
    if after.settled is True and action != ACTION_HU:
        return False, (f"点击后进入结算画面，与 {action} 的预期不符"
                       f"（只有胡才应进结算）")
    if after.turn_owner is not None and after.turn_owner != "me" and action != ACTION_HU:
        return False, (f"倒计时未转移到我方（after={after.turn_owner}），"
                       f"{action} 未生效")
    return True, ""


def _discs_gone(after: BoardState, action: str) -> tuple[bool, str]:
    """行动圆盘必须被**实测**为 0；读不到（``None``）按证据不足判失败。"""
    if after.claim_buttons is None:
        return False, f"{action} 后行动圆盘数量不可测，无法验证（fail closed）"
    if after.claim_buttons != 0:
        return False, f"{action} 后行动圆盘仍有 {after.claim_buttons} 个，预期消失"
    return True, ""


def _melds_grew(before: BoardState, after: BoardState,
                *, group_delta: int, tile_delta: int) -> tuple[bool, str]:
    """副露是否按 ``组数 +group_delta``、``张数 +tile_delta`` 增长。"""
    if before.meld_group_count is None or after.meld_group_count is None:
        if before.meld_tile_total is None or after.meld_tile_total is None:
            return False, "副露证据不可测（点击前后都应有副露读数）"
        if after.meld_tile_total - before.meld_tile_total != tile_delta:
            return False, (f"副露张数 {before.meld_tile_total}→{after.meld_tile_total}，"
                           f"预期 +{tile_delta}")
        return True, ""
    group_gap = after.meld_group_count - before.meld_group_count
    if group_gap != group_delta:
        return False, (f"副露组数 {before.meld_group_count}→{after.meld_group_count}，"
                       f"预期 +{group_delta}")
    if before.meld_tile_total is not None and after.meld_tile_total is not None:
        tile_gap = after.meld_tile_total - before.meld_tile_total
        if tile_gap != tile_delta:
            return False, (f"副露张数 {before.meld_tile_total}→{after.meld_tile_total}，"
                           f"预期 +{tile_delta}")
    return True, ""


def _verify_simple_claim(before: BoardState, after: BoardState,
                         action: str) -> tuple[bool, str]:
    """碰/吃：暗手 −2、副露 +1 组 / +3 张、圆盘实测消失；吃另查面板残留。"""
    if before.concealed_count is None or after.concealed_count is None:
        return False, "暗手张数不可测（点击前后都应有完整识牌）"
    gap = after.concealed_count - before.concealed_count
    if gap != -2:
        return False, f"{action} 后暗手 {before.concealed_count}→{after.concealed_count}，预期 −2"
    ok, why = _melds_grew(before, after, group_delta=1, tile_delta=3)
    if not ok:
        return False, why
    if action == ACTION_CHI and after.selection_panel is True:
        return False, "吃后候选选择面板仍在画面上，吃未完成"
    ok, why = _discs_gone(after, action)
    if not ok:
        return False, why
    if before.total is not None and after.total is not None:
        if after.total - before.total != 1:
            return False, f"{action} 后总张数 {before.total}→{after.total}，预期 +1"
    return True, f"{action} 状态迁移已验证（暗手 −2、副露 +1 组、圆盘消失）"


def _verify_kong(before: BoardState, after: BoardState) -> tuple[bool, str]:
    """杠：明杠（暗手 −3、副露 +1 组/+4 张）或加杠（暗手 −1、张数 +1）。"""
    if before.concealed_count is None or after.concealed_count is None:
        return False, "暗手张数不可测（点击前后都应有完整识牌）"
    gap = after.concealed_count - before.concealed_count
    expected_total: int
    if gap == -3:
        ok, why = _melds_grew(before, after, group_delta=1, tile_delta=4)
        if not ok:
            return False, why
        expected_total = 1      # 明杠：接了别家那张，总张数 16 → 17
    elif gap == -1:
        ok, why = _melds_grew(before, after, group_delta=0, tile_delta=1)
        if not ok:
            return False, why
        expected_total = 0      # 加杠：把闭手里那张并进已有碰组，总张数不变
    else:
        return False, (f"杠后暗手 {before.concealed_count}→{after.concealed_count}，"
                       f"预期 −3（明杠）或 −1（加杠）")
    ok, why = _discs_gone(after, ACTION_KONG)
    if not ok:
        return False, why
    if before.total is not None and after.total is not None:
        if after.total - before.total != expected_total:
            return False, (f"杠后总张数 {before.total}→{after.total}，"
                           f"预期 +{expected_total}")
    return True, "杠状态迁移已验证（暗手 −3 或 −1、副露张数吻合、圆盘消失）"


def _verify_hu(before: BoardState, after: BoardState) -> tuple[bool, str]:
    """胡：进入结算画面（优先），至少也要行动圆盘消失。"""
    if after.settled is True:
        return True, "胡后进入结算画面，状态迁移已验证"
    if after.claim_buttons == 0:
        return True, "胡后行动圆盘消失（结算画面未读数，按圆盘消失判定）"
    if after.settled is None and after.claim_buttons is None:
        return False, "胡后既无结算画面读数也无圆盘读数，无法验证（fail closed）"
    return False, (f"胡后未进入结算画面（settled={after.settled}），"
                   f"且行动圆盘仍有 {after.claim_buttons} 个")


def _verify_pass(before: BoardState, after: BoardState) -> tuple[bool, str]:
    """过：圆盘消失；暗手不变，或按钮消失后立即摸牌而 +1。"""
    if before.concealed_count is not None and after.concealed_count is not None:
        concealed_gap = after.concealed_count - before.concealed_count
        if concealed_gap not in (0, 1):
            return False, (f"过后暗手 {before.concealed_count}→{after.concealed_count}，"
                           f"预期不变或立即摸牌 +1")
    if (before.meld_group_count is not None and after.meld_group_count is not None
            and before.meld_group_count != after.meld_group_count):
        return False, (f"过后副露组数 {before.meld_group_count}→{after.meld_group_count}，"
                       f"预期不变")
    ok, why = _discs_gone(after, ACTION_PASS)
    if not ok:
        return False, why
    if before.total is not None and after.total is not None:
        total_gap = after.total - before.total
        if total_gap not in (0, 1):
            return False, (f"过后总张数 {before.total}→{after.total}，"
                           "预期不变或立即摸牌 +1")
    return True, ("过状态迁移已验证（副露不变、圆盘消失、倒计时转移；"
                  "暗手不变或立即摸牌 +1）")
