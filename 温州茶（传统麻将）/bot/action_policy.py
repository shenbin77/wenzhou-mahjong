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
  调用方必须立即停机并落证据。

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
* 吃会弹出二次选择面板（面板里有 ``取消``），本版未接线，
  ``config.chi_enabled`` 默认 ``False``。
* ``tap_executor`` 的几何白名单只放行手牌 ROI（y ≥ 720），行动圆盘在 y≈600，
  因此**点击通道尚未打通**：自动点击开关打开后本模块会给出
  ``tap_point``，实际点击仍需 tap_executor 侧接线（见 :data:`CLAIM_TAP_CHANNEL_READY`）。
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
    "CLAIM_TAP_CHANNEL_READY",
    "COST_BUTTONS",
    "CLAIMABLE_INCOMING_SOURCES",
    "HandState",
    "INCOMING_SOURCE_OPPONENT",
    "INCOMING_SOURCE_SELF",
    "TEMPLATE_MARGIN_MIN",
    "TEMPLATE_TOP1_MIN",
    "blacklist_hit",
    "confirm_action",
    "decide_action",
    "engine_available",
    "evaluate_claim_candidates",
    "human_confirm_required",
    "load_claim_templates",
    "match_button_glyph",
    "never_tap",
    "read_and_decide",
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

#: 点击通道是否打通。``tap_executor`` 的几何白名单只放行手牌 ROI（y≥720），
#: 行动圆盘在 y≈600，所以当前为 False：本模块只给决策与 tap_point，不点击。
CLAIM_TAP_CHANNEL_READY: bool = False


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
        player_count: 局面人数（2 或 4）；``None`` 表示未确认。
        source: 证据来源，写进台账便于回溯。
    """

    concealed: tuple[int, ...] = ()
    melds: tuple[tuple[int, ...], ...] = ()
    caishen: int | None = None
    incoming_tile: int | None = None
    incoming_source: str | None = None
    player_count: int | None = None
    source: str = ""

    @property
    def meld_total(self) -> int:
        """副露总张数（按各组真实张数计，杠算 4 张）。"""
        return sum(len(group) for group in self.melds)

    @property
    def total(self) -> int:
        """闭手 + 副露的实际张数。"""
        return len(self.concealed) + self.meld_total

    @property
    def concealed_expected(self) -> int:
        """等待态闭手应有的张数（16 − 副露张数）。"""
        return 16 - self.meld_total

    @property
    def is_valid(self) -> bool:
        """张数守恒：闭手 + 副露 == 16（等待态）。不守恒即不许决策。"""
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
        note: 备注，写进台账。
    """

    concealed_count: int | None = None
    melds: tuple[tuple[int, ...], ...] | None = None
    claim_buttons: int | None = None
    settled: bool | None = None
    turn_owner: str | None = None
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
        note: str = "",
    ) -> "BoardState":
        """把手牌状态包装成 L3 需要的盘面快照。"""
        return cls(
            concealed_count=len(hand.concealed) if hand is not None else None,
            melds=hand.melds if hand is not None else None,
            claim_buttons=claim_buttons,
            settled=settled,
            turn_owner=turn_owner,
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
            默认 ``False``——因为模板样本尚未采集，此时由 L2 引擎消歧。
        template_provider: 模板来源（返回 ``{名字: 灰度图}``）；``None`` 用
            :data:`CLAIM_TEMPLATE_DIR`。
        forbidden_boxes: 黑名单 ROI（xyxy），命中即永不点击。
        forbidden_labels: 黑名单按钮名。
        caishen_substitutes_for_claim: 财神是否可替代吃碰杠的实体牌；
            默认 ``False``（未实测，按不替代处理＝少碰也不误碰）。
        chi_enabled: 是否启用"吃"；默认 ``False``——吃会弹二次选择面板
            （面板含 ``取消``），本版未接线。
        chi_player_counts: 允许吃的人数集合；默认 ``{4}``（2 人局无上家）。
        allow_pass_without_claim: 只有"过"而没有金/红盘时是否出手；
            默认 ``False``（与已验证口径"过从不单独出现"不符，弃权）。
        require_improvement: 吃碰杠必须不劣化向听才出手；默认 ``True``。
    """

    auto_click_enabled: bool = False
    confirmed_actions: frozenset[str] = frozenset()
    require_template_match: bool = False
    template_provider: Callable[[], Mapping[str, Any]] | None = None
    forbidden_boxes: tuple[tuple[int, int, int, int], ...] = DEFAULT_FORBIDDEN_BOXES
    forbidden_labels: frozenset[str] = BLACKLISTED_LABELS
    caishen_substitutes_for_claim: bool = False
    chi_enabled: bool = False
    chi_player_counts: frozenset[int] = frozenset({4})
    allow_pass_without_claim: bool = False
    require_improvement: bool = True


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
                f"手牌张数不守恒（闭手{len(hand.concealed)}+副露{hand.meld_total}"
                f"，等待态应为 {hand.concealed_expected}），L2 弃权"
                if isinstance(hand, HandState) else "手牌状态缺失，L2 弃权")
        return report
    tile = hand.incoming_tile
    if tile is None:
        for action in CLAIM_ACTIONS:
            report[action]["why"] = "来牌未知（无读牌链路），L2 无法验证吃碰杠胡"
        return report
    # 归属闸门：自己刚打出的牌不能拿来吃/碰/杠/胡。真机上行动行有时是在
    # 自家出牌之后刷出来的（动画残留），没有这道闸就会拿自己的弃牌去碰。
    source = hand.incoming_source
    if source is not None and source not in CLAIMABLE_INCOMING_SOURCES:
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
        report[ACTION_CHI]["why"] = "吃会弹出二次选择面板（含取消），本版未接线，不启用"
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
            return _blocked(claim_state, image, cfg,
                            "L2 否决：画面出现金盘，但引擎判定碰/吃/杠皆不可行，弃权",
                            l2_reason=why, engine=report)
        if len(feasible) > 1:
            names = "、".join(feasible)
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
    * 过：暗手与副露不变、行动圆盘消失、倒计时转到我方。

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
    """碰/吃：暗手 −2、副露 +1 组 / +3 张、按钮消失。"""
    if before.concealed_count is None or after.concealed_count is None:
        return False, "暗手张数不可测（点击前后都应有完整识牌）"
    gap = after.concealed_count - before.concealed_count
    if gap != -2:
        return False, f"{action} 后暗手 {before.concealed_count}→{after.concealed_count}，预期 −2"
    ok, why = _melds_grew(before, after, group_delta=1, tile_delta=3)
    if not ok:
        return False, why
    if after.claim_buttons is not None and after.claim_buttons != 0:
        return False, f"{action} 后行动圆盘仍有 {after.claim_buttons} 个，预期消失"
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
    if after.claim_buttons is not None and after.claim_buttons != 0:
        return False, f"杠后行动圆盘仍有 {after.claim_buttons} 个，预期消失"
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
    """过：暗手/副露不变、圆盘消失、倒计时转到我方。"""
    if before.concealed_count is not None and after.concealed_count is not None:
        if after.concealed_count != before.concealed_count:
            return False, (f"过后暗手 {before.concealed_count}→{after.concealed_count}，"
                           f"预期不变")
    if (before.meld_group_count is not None and after.meld_group_count is not None
            and before.meld_group_count != after.meld_group_count):
        return False, (f"过后副露组数 {before.meld_group_count}→{after.meld_group_count}，"
                       f"预期不变")
    if after.claim_buttons is not None and after.claim_buttons != 0:
        return False, f"过后行动圆盘仍有 {after.claim_buttons} 个，预期消失"
    if before.total is not None and after.total is not None:
        if after.total != before.total:
            return False, f"过后总张数 {before.total}→{after.total}，预期不变"
    return True, "过状态迁移已验证（暗手/副露不变、圆盘消失、倒计时转移）"
