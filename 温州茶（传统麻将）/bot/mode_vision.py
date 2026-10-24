"""Confirm the target tea or red-Zhong rule text before offering advice.

Templates are literal native-resolution crops of the rule text:
``tea_2p.png`` from 2026-10-16/740195/r08/frame_01.png x=814:874,y=190:228,
``tea_4p.png`` from 2026-10-13/323028/r01/frame_01.png x=920:990,y=190:228.
``tea_no_zhong_fa.png`` from 2026-10-19/984226/r01/
room984226_round1_3d58eb17_2.png x=971:1097,y=141:185.
``tea_title.png`` from 2026-10-16/740195/r08/frame_01.png
x=307:450,y=149:184. ``shu_zhong.png`` from
熟识牌对照_20261018/frame_233s_14.png x=826:978,y=205:245.
Unknown, obscured, or differently sized screens remain unknown.
Shu templates come from 熟识牌对照_20261018/frame_233s_14.png and require
the observed 放炮麻将 title and trailing 4-player rule on the same line.
"""

import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "mode"

# 玩法模板判定口径（人数模板 0.92 / 其它 0.94，两者都要求次名差 ≥0.08）。
PLAYER_SCORE_MIN = 0.92
PLAYER_MARGIN_MIN = 0.08
TITLE_SCORE_MIN = 0.94
DECK_SCORE_MIN = 0.94          # 「无中发规则」条幅的单模板阈值（无竞争性模板）


def _read_template(path: Path) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(path)
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        raise ValueError(f"Invalid player-count template: {path}")
    return image


@lru_cache(maxsize=1)
def _templates():
    result = {}
    for players in (2, 4):
        path = _TEMPLATE_DIR / f"tea_{players}p.png"
        result[players] = _read_template(path)
    return result


@lru_cache(maxsize=1)
def _tea_title_template() -> np.ndarray:
    return _read_template(_TEMPLATE_DIR / "tea_title.png")


def _has_tea_title(gray: np.ndarray) -> bool:
    title_area = gray[140:190, 280:520]
    score = float(cv2.minMaxLoc(cv2.matchTemplate(
        title_area, _tea_title_template(), cv2.TM_CCOEFF_NORMED
    ))[1])
    return score >= TITLE_SCORE_MIN


def score_tea_player_count(image: np.ndarray) -> tuple[Optional[int], float, float]:
    """Return ``(players, best_score, margin)`` for the visible rule text.

    Same arithmetic and thresholds as :func:`detect_tea_player_count`, but it
    also hands back the two numbers the decision was made from, so callers can
    log them. ``(None, score, margin)`` means "not confirmed"; when the frame is
    unusable (wrong size, no 茶 title) every number is ``0.0``.
    """
    if image is None or image.shape[:2] != (900, 1600):
        return None, 0.0, 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    if not _has_tea_title(gray):
        return None, 0.0, 0.0
    rule_strip = gray[140:245, 280:1320]
    scores = {}
    for players, template in _templates().items():
        scores[players] = float(cv2.minMaxLoc(
            cv2.matchTemplate(rule_strip, template, cv2.TM_CCOEFF_NORMED)
        )[1])
    best = max(scores, key=scores.get)
    other = 4 if best == 2 else 2
    margin = scores[best] - scores[other]
    if scores[best] >= PLAYER_SCORE_MIN and margin >= PLAYER_MARGIN_MIN:
        return best, scores[best], margin
    return None, scores[best], margin


def detect_tea_player_count(image: np.ndarray) -> int | None:
    """Return 2, 4, or None from the visible rule text; never infer from tile count."""
    players, _score, _margin = score_tea_player_count(image)
    return players


@lru_cache(maxsize=1)
def _tea_no_zhong_fa_template() -> np.ndarray:
    return _read_template(_TEMPLATE_DIR / "tea_no_zhong_fa.png")


def score_tea_no_zhong_fa(image: np.ndarray) -> tuple[bool, float]:
    """Return ``(confirmed, score)`` for the visible no-Zhong/Fa rule text.

    Same threshold as :func:`detect_tea_no_zhong_fa`, but it also hands back the
    template score so the sticky deck gate can record what it confirmed on.
    Unusable frames (wrong size, no 茶 title) give ``(False, 0.0)``.

    Note there is **no competing template** for the "this deck has 中/发" case,
    so unlike the player count this gate can only ever answer "confirmed" or
    "not established" -- see :class:`TeaDeckLatch`.
    """
    if image is None or image.shape[:2] != (900, 1600):
        return False, 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    if not _has_tea_title(gray):
        return False, 0.0
    rule_strip = gray[140:245, 280:1320]
    score = float(cv2.minMaxLoc(cv2.matchTemplate(
        rule_strip, _tea_no_zhong_fa_template(), cv2.TM_CCOEFF_NORMED
    ))[1])
    return score >= DECK_SCORE_MIN, score


def detect_tea_no_zhong_fa(image: np.ndarray) -> bool:
    """Confirm the visible no-Zhong/Fa rule for the currently supported tea deck."""
    return score_tea_no_zhong_fa(image)[0]


@lru_cache(maxsize=1)
def _shu_templates():
    # Native crops from 熟识牌对照_20261018/frame_233s_14.png (1600x948):
    # title x=405:545, red-Zhong x=826:978, four-player x=1117:1200;
    # all at y=205:245 on the same rule line.
    return (_read_template(_TEMPLATE_DIR / "shu_title.png"),
            _read_template(_TEMPLATE_DIR / "shu_zhong.png"),
            _read_template(_TEMPLATE_DIR / "shu_four.png"))


def detect_shu_rule_text(image: np.ndarray) -> bool:
    """Require the observed 放炮麻将 title and 4-player rule on one line."""
    if image is None or image.shape[1] != 1600 or image.shape[0] not in (900, 948):
        return False
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    rule_strip = gray[110:260, 250:1350]
    matches = []
    for template in _shu_templates():
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(rule_strip, template, cv2.TM_CCOEFF_NORMED)
        )
        matches.append((float(score), location[0], location[1]))
    title, zhong, four = matches
    return (title[0] >= TITLE_SCORE_MIN and zhong[0] >= TITLE_SCORE_MIN
            and four[0] >= TITLE_SCORE_MIN
            and 380 <= zhong[1] - title[1] <= 460
            and 250 <= four[1] - zhong[1] <= 330
            and abs(zhong[2] - title[2]) <= 8
            and abs(four[2] - title[2]) <= 8)


# ---------------------------------------------------------------------------
# 玩法门禁粘滞（TeaModeLatch）
# ---------------------------------------------------------------------------
# 为什么要粘滞
# ============
# ``detect_tea_player_count`` 是逐帧独立的模板匹配。一间房的玩法（2 人茶 / 4 人）
# 在一局内不会变，但它读的那条规则文字（作物 x=814:874,y=190:228）正好落在
# 吃/碰/杠/胡 行动圆盘下面：圆盘一出现，模板分从 0.999 掉到 0.754~0.894，低于
# 0.92 阈值 → ``tea_bot._default_vision`` 抛 ValueError → **整帧弃权**。
# 实测 17 个真局会话 4455 帧：355 帧检出行动圆盘，其中 118 帧（33.2%）因此被
# 判成"画面不认识"。按钮出现的那一帧恰恰是最需要分析的那一帧。
#
# 粘滞的语义
# ==========
# 玩法一旦被某一帧**明确确认**（top1 ≥0.92 且次名差 ≥0.08），就把结论锁存下来，
# 后续帧复用，**不再逐帧重判**。三条铁律：
#
# 1. fail-closed 不变：一次都没确认过的画面返回 ``players=None``，调用方照旧弃权。
#    粘滞只是"复用已确认的结论"，永远不会凭空产出结论。
# 2. 正向矛盾立刻翻牌：任何一帧明确确认了**另一种**玩法（例如在 4 人房里读出 4），
#    锁存立刻作废并按新证据重锁。绝不出现"锁了 2 就再也认不出 4"。
# 3. 作用域信号变化即重置：见 :func:`frame_round_identity`。
#
# 重置判据（安全关键）
# ====================
# 第 2 条只能挡住"看得见的另一种玩法"。真正危险的是换了房/换了局而画面又因为
# 遮挡读不出玩法——那时逐帧判定只会给 None，粘滞若不放手就会一直按旧玩法决策。
# 所以粘滞同时挂在**局身份**上，身份一变立刻失效并要求重新确认：
#
# * ``hud``：房号/局数 HUD 指纹（ crop x=15:225,y=55:140 灰度 //8），被"买底
#   请神顶"的红色视频头像遮挡时按未知处理（与 ``assist_window.tea_round_fingerprint``
#   同一 crop、同一量化、同一容差 ≤8 字节）。**这段区域远离行动圆盘（y≈533..710），
#   所以按钮出现时它依然可读 —— 这正是它能当重置判据的原因。**
# * ``caishen``：桌面财神小牌标签。温州茶每局重翻财神，标签变化 = 换局/换房。
# * ``ttl_s``：距上次确认超过 TTL 一律失效，兜住"长时间没有有效帧"的场景。
#
# 可靠性判断（写给运维，不含糊）：HUD 指纹会被头像长期遮挡、财神也可能读不出，
# 两者都未知时不产生任何重置信号，此时**只有第 2 条（正向矛盾）和 TTL 兜底**。
# 这不是"完全可靠"，但它的失效方式是可接受的：要踩雷必须先满足「换到一个玩法
# 不同的房间」**且**「新房间的玩法文字被持续遮挡到逐帧也读不出」——后一条成立时，
# 逐帧判定本来也是 None（= 弃权），粘滞并没有让它变得更危险；反过来，只要新房
# 间有一次清晰帧，第 2 条立刻翻牌。
# ---------------------------------------------------------------------------

LATCH_SOURCE_CONFIRMED = "confirmed_now"
LATCH_SOURCE_LATCHED = "latched"
LATCH_SOURCE_UNKNOWN = "unknown"

DEFAULT_LATCH_TTL_S = 600.0

# 局身份保险 —— 必须与 assist_window.tea_round_fingerprint 保持同一套 crop/量化。
# 这里按 BGR 帧重新实现，是因为本层只能拿到 image，拿不到 assist_window 需要的
# 文件路径；tests/test_mode_latch.py 用真帧把两边钉在一起，任一侧改动都会报警。
_HUD_CROP = (15, 55, 225, 140)          # x0, y0, x1, y1
_HUD_QUANT = 8
_AVATAR_CROP = (35, 95, 115, 140)       # 会把房号挡住的红色视频头像位置
_AVATAR_RED_PIXELS_MAX = 100
_AVATAR_RED_MIN = 90
_AVATAR_RED_DOMINANCE = 1.4
_HUD_SAME_MAX_DIFF = 8                  # 与 assist_window.same_tea_round 同容差

#: Read-only pixel ROI that carries the 财神 signal (documentation only; see
#: :func:`visible_caishen_label`). Same region ``assist_window`` relies on.
_CAISHEN_CROP = (622, 348, 677, 423)


def hud_fingerprint(image: np.ndarray) -> bytes | None:
    """Frame-based twin of ``assist_window.tea_round_fingerprint``.

    Same crop, same //8 quantization, same "occluded ⇒ unknown" rule. Returns
    ``None`` when the frame is not a verified 1600x900 table or when the red
    video avatar covers the room number.
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    if image.shape[:2] != (900, 1600):
        return None
    ax0, ay0, ax1, ay1 = _AVATAR_CROP
    avatar = image[ay0:ay1, ax0:ax1]
    blue, green, red = (avatar[:, :, 0].astype(np.int16),
                        avatar[:, :, 1].astype(np.int16),
                        avatar[:, :, 2].astype(np.int16))
    red_pixels = int(((red > _AVATAR_RED_MIN)
                      & (red > green * _AVATAR_RED_DOMINANCE)
                      & (red > blue * _AVATAR_RED_DOMINANCE)).sum())
    if red_pixels > _AVATAR_RED_PIXELS_MAX:
        return None
    x0, y0, x1, y1 = _HUD_CROP
    gray = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return bytes((gray // _HUD_QUANT).ravel().tolist())


def same_hud_fingerprint(first: bytes | None, second: bytes | None) -> bool:
    """Tolerant compare; unknown (``None``) never counts as a mismatch.

    Deliberately aligned with ``auto_play_loop.RoundState.same_round``: a signal
    that cannot be read must not be able to reset the latch on its own.
    """
    if first is None or second is None:
        return True
    if len(first) != len(second):
        return False
    return sum(a != b for a, b in zip(first, second)) <= _HUD_SAME_MAX_DIFF


def visible_caishen_label(image: np.ndarray) -> str | None:
    """Read the table's 财神 tile label from pixels alone; ``None`` when unreadable.

    ``caishen_vision.detect_caishen`` requires the whole 1600x900 frame (it does
    its own crop), so the crop constant below is documentation only — it records
    which part of the table carries the signal.
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    if image.shape[:2] != (900, 1600):
        return None
    try:
        from caishen_vision import detect_caishen
    except Exception:  # noqa: BLE001 - 只读依赖，取不到就当没信号
        return None
    try:
        reading = detect_caishen(image)
    except Exception:  # noqa: BLE001 - 读数异常同样按"没信号"处理
        return None
    label = getattr(reading, "label", None)
    return str(label) if label else None


def frame_round_identity(image: np.ndarray) -> tuple[bytes | None, str | None]:
    """Return ``(hud_fingerprint, caishen_label)`` — the latch's reset scope."""
    return hud_fingerprint(image), visible_caishen_label(image)


def build_action_ui(image: np.ndarray, *, mode_score: float = 0.0,
                    mode_margin: float = 0.0, raw_players: int | None = None,
                    players: int | None = None, source: str = LATCH_SOURCE_UNKNOWN,
                    latched: bool = False, resets: int = 0, age_frames: int = 0,
                    age_s: float = 0.0, deck_score: float = 0.0,
                    deck_latched: bool = False,
                    deck_source: str = LATCH_SOURCE_UNKNOWN) -> dict:
    """Per-frame record of "what happened in the button area + both rule gates".

    Purely descriptive: it never decides anything. Built for the watchdog so a
    single ledger line answers "were discs up, what did the two rule gates score,
    and did we lean on the latches this frame". Disc reading comes from
    ``claim_vision.read_claims`` — the same read-only detector the claim policy
    uses — so the two can never disagree about what was on screen.

    Every argument is optional: a caller that only knows about one gate still
    gets a full-shaped record, with the other gate's fields left at neutral
    values.
    """
    seen = False
    count = 0
    discs: list[dict[str, Any]] = []
    gold_ratio: float | None = None
    decision: str | None = None
    reason = ""
    try:
        from claim_vision import read_claims
    except Exception:  # noqa: BLE001 - 读不到就只出玩法那一半，不自造结论
        read_claims = None
    if read_claims is not None:
        try:
            state = read_claims(image)
        except Exception as exc:  # noqa: BLE001 - 行动行读失败不等于"没有按钮"
            reason = f"行动行读取失败：{exc}"
            state = None
        if state is not None:
            seen = bool(state.known)
            count = len(state.buttons)
            discs = [{"kind": button.kind,
                      "box": [int(v) for v in button.box],
                      "center": [int(v) for v in button.center]}
                     for button in state.buttons]
            decision = state.decision
            reason = state.reason
            ratios = (state.evidence or {}).get("pixel_ratio") or {}
            gold_ratio = ratios.get("gold")
    return {
        "action_discs_seen": seen,
        "action_disc_count": count,
        "action_discs": discs,
        "claim_decision": decision,
        "claim_reason": reason,
        "action_gold_pixel_ratio": gold_ratio,
        "deck_score": round(float(deck_score), 6),
        "deck_score_min": DECK_SCORE_MIN,
        "deck_latched": bool(deck_latched),
        "deck_source": deck_source,
        "mode_score": round(float(mode_score), 6),
        "mode_margin": round(float(mode_margin), 6),
        "mode_score_min": PLAYER_SCORE_MIN,
        "mode_margin_min": PLAYER_MARGIN_MIN,
        "mode_raw_players": raw_players,
        "mode_players": players,
        "mode_source": source,
        "mode_latched": latched,
        "mode_latch_resets": resets,
        "mode_latch_age_frames": age_frames,
        "mode_latch_age_s": round(float(age_s), 3),
    }


class RuleReading:
    """One frame's verdict from one sticky rule gate.

    Attributes:
        gate: Which gate produced it (``"mode"`` or ``"deck"``).
        value: The latched/confirmed value. For ``"mode"`` it is ``2``/``4`` or
            ``None``; for ``"deck"`` it is ``True`` (no-Zhong/Fa confirmed) or
            ``None``. ``None`` always means "callers MUST abstain".
        source: ``confirmed_now`` (this frame's own pixels confirmed it),
            ``latched`` (reused a previous confirmation), or ``unknown``.
        score / margin: The template numbers behind the raw per-frame reading.
        raw_value: What this frame would have said WITHOUT the latch.
        latched: Convenience flag for ``source == "latched"``.
        action_ui: JSON-serialisable per-frame observability record.
    """

    __slots__ = ("gate", "value", "source", "score", "margin", "raw_value",
                 "latched", "age_frames", "age_s", "resets", "reason",
                 "action_ui")

    def __init__(self, *, gate: str, value, source: str, score: float,
                 margin: float, raw_value, latched: bool, age_frames: int,
                 age_s: float, resets: int, reason: str, action_ui: dict):
        self.gate = str(gate)
        self.value = value
        self.source = str(source)
        self.score = float(score)
        self.margin = float(margin)
        self.raw_value = raw_value
        self.latched = bool(latched)
        self.age_frames = int(age_frames)
        self.age_s = float(age_s)
        self.resets = int(resets)
        self.reason = str(reason)
        self.action_ui = dict(action_ui)

    # -- mode-gate aliases（``TeaModeLatch`` 的返回体按"人数"讲话）--
    @property
    def players(self) -> int | None:
        """The player count when this reading came from the mode gate."""
        return self.value if self.value in (2, 4) else None

    @property
    def raw_players(self) -> int | None:
        """``players`` for the raw (non-latched) per-frame verdict."""
        return self.raw_value if self.raw_value in (2, 4) else None

    @property
    def confirmed(self) -> bool:
        """Whether this reading may be acted on."""
        return self.value is not None

    def as_dict(self) -> dict:
        """JSON-serialisable summary (ledger / log friendly)."""
        return {
            "gate": self.gate,
            "value": self.value,
            "source": self.source,
            "score": round(self.score, 6),
            "margin": round(self.margin, 6),
            "raw_value": self.raw_value,
            "latched": self.latched,
            "age_frames": self.age_frames,
            "age_s": round(self.age_s, 3),
            "resets": self.resets,
            "reason": self.reason,
            "action_ui": dict(self.action_ui),
            # 兼容早期按"人数"取数的调用方
            "players": self.players,
            "raw_players": self.raw_players,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (f"RuleReading(gate={self.gate!r}, value={self.value!r}, "
                f"source={self.source!r}, score={self.score:.3f}, "
                f"raw={self.raw_value!r}, "
                f"age={self.age_frames}f/{self.age_s:.1f}s)")


#: 早期命名：玩法人数门禁的读数。两组 name 指向同一个类，互不冲突。
ModeReading = RuleReading


class TeaRuleLatch:
    """Shared sticky-gate machinery: one latched value + one invalidation policy.

    Subclasses supply ``_measure`` (what to read off the pixels); everything
    about scope tracking, TTL, observability and reset semantics lives here so
    that **every** rule gate is invalidated by exactly the same signals. Nothing
    is ever invented: without a confirmation inside the current scope this always
    reports ``value=None``.

    Args:
        ttl_s: Latch lifetime in seconds. Expiry is a safety net for "no usable
            frame for a long time" (room switched while the loop was blind).
        clock: Monotonic clock, injected in tests.
    """

    #: Which gate this is -- subclasses override.
    gate = "mode"

    def __init__(self, *, ttl_s: float = DEFAULT_LATCH_TTL_S,
                 clock: Callable[[], float] | None = None):
        self.ttl_s = max(0.0, float(ttl_s))
        self._clock = clock or time.monotonic
        self.reset(reason="init")

    # ---------------------------------------------------------------- state
    def reset(self, reason: str = "explicit") -> None:
        """Drop the confirmation. The next caller must re-confirm from pixels."""
        self._value = None
        self._score = 0.0
        self._margin = 0.0
        self._hud: bytes | None = None
        self._caishen: str | None = None
        self._confirmed_at: float | None = None
        self._frames = 0
        self._resets = getattr(self, "_resets", 0) + (0 if reason == "init" else 1)
        self._last_reason = str(reason)
        self._last_action_ui: dict = {}

    @property
    def latched_value(self):
        """Currently locked value, or ``None`` when nothing is latched."""
        return self._value

    @property
    def latched_players(self) -> int | None:
        """``latched_value`` as a player count (``None`` for non-mode gates)."""
        return self._value if self._value in (2, 4) else None

    @property
    def resets(self) -> int:
        """How many times this latch has been invalidated."""
        return self._resets

    def snapshot(self) -> dict:
        """Observability view: latched? what? how old? how did it score?"""
        now = self._clock()
        age = None if self._confirmed_at is None else now - self._confirmed_at
        expired = self._is_expired(now)
        return {
            "gate": self.gate,
            "latched": self._value is not None and not expired,
            "value": None if expired else self._value,
            "players": None if expired else self.latched_players,
            "score": round(self._score, 6),
            "margin": round(self._margin, 6),
            "age_s": None if age is None else round(age, 3),
            "age_frames": self._frames,
            "resets": self._resets,
            "ttl_s": self.ttl_s,
            "expired": expired,
            "last_reason": self._last_reason,
            "hud_known": self._hud is not None,
            "caishen": self._caishen,
        }

    # ------------------------------------------------------------ internals
    def _is_expired(self, now: float) -> bool:
        if self._value is None or self.ttl_s <= 0:
            return False
        return self._confirmed_at is not None and (now - self._confirmed_at) > self.ttl_s

    def _measure(self, image: np.ndarray) -> tuple[object, float, float]:
        """Read this gate off one frame: ``(value, score, margin)``.

        ``value`` is ``None`` when the frame does not establish anything.
        """
        raise NotImplementedError

    def _out_of_scope(self, hud: bytes | None, caishen: str | None) -> str:
        """Name the reset reason if this frame belongs to another scope."""
        if self._value is None:
            return ""
        if not same_hud_fingerprint(self._hud, hud):
            return "hud_fingerprint_changed"
        if caishen and self._caishen and caishen != self._caishen:
            return "caishen_changed"
        return ""

    def _finish(self, *, value, source: str, score: float, margin: float,
                raw_value, reason: str, image: np.ndarray, now: float,
                action_ui: dict | None = None) -> RuleReading:
        age_s = 0.0 if self._confirmed_at is None else now - self._confirmed_at
        latched = source == LATCH_SOURCE_LATCHED
        if action_ui is None:
            action_ui = build_action_ui(
                image, mode_score=score, mode_margin=margin,
                raw_players=raw_value if raw_value in (2, 4) else None,
                players=value if value in (2, 4) else None,
                source=source, latched=latched, resets=self._resets,
                age_frames=self._frames, age_s=age_s)
        action_ui = dict(action_ui)
        action_ui["mode_score"] = round(float(score), 6)
        action_ui["mode_latched"] = latched
        action_ui["mode_source"] = source
        action_ui["mode_latch_age_frames"] = self._frames
        action_ui["mode_latch_age_s"] = round(float(age_s), 3)
        action_ui["mode_latch_resets"] = self._resets
        self._last_action_ui = action_ui
        return RuleReading(gate=self.gate, value=value, source=source,
                           score=score, margin=margin, raw_value=raw_value,
                           latched=latched, age_frames=self._frames,
                           age_s=age_s, resets=self._resets, reason=reason,
                           action_ui=action_ui)

    def observe(self, image: np.ndarray, *, now: float | None = None,
                scope: tuple[bytes | None, str | None] | None = None,
                action_ui: dict | None = None) -> RuleReading:
        """Read one frame and return its :class:`RuleReading`.

        Order matters and is part of the safety contract: scope reset is decided
        **before** a raw miss is allowed to fall back to the latch, and a raw
        confirmation always wins over whatever was latched.

        Args:
            image: The frame to read.
            now: Injected clock value (tests).
            scope: Precomputed ``(hud, caishen)``; pass it when several gates are
                read from the same frame so they share one scope computation and
                therefore one invalidation decision.
            action_ui: Prebuilt observability record to reuse, so N gates on one
                frame cost one disc read instead of N.
        """
        moment = self._clock() if now is None else float(now)
        raw_value, raw_score, raw_margin = self._measure(image)
        hud, caishen = frame_round_identity(image) if scope is None else scope

        # ① 换房/换局/超时 → 立刻失效，本帧不许吃锁存的结论。
        reason = self._out_of_scope(hud, caishen)
        if not reason and self._is_expired(moment):
            reason = "latch_ttl_expired"
        if reason:
            self.reset(reason=reason)

        # ② 正向证据优先：本帧自己确认了，就按本帧的来（含"另一种玩法"翻牌）。
        if raw_value is not None:
            flipped = self._value is not None and self._value != raw_value
            self._value = raw_value
            self._score = raw_score
            self._margin = raw_margin
            self._hud = hud
            self._caishen = caishen
            self._confirmed_at = moment
            self._frames = 0
            self._last_reason = ("value_flipped" if flipped else "confirmed")
            return self._finish(
                value=raw_value, source=LATCH_SOURCE_CONFIRMED,
                score=raw_score, margin=raw_margin, raw_value=raw_value,
                reason=self._last_reason, image=image, now=moment,
                action_ui=action_ui)

        # ③ 本帧读不出，但有有效锁存 → 复用（这才是修掉按钮帧弃权的那一步）。
        if self._value is not None:
            self._frames += 1
            if hud is not None:
                self._hud = hud
            if caishen:
                self._caishen = caishen
            self._last_reason = "latched_value_reused"
            return self._finish(
                value=self._value, source=LATCH_SOURCE_LATCHED,
                score=self._score, margin=self._margin, raw_value=None,
                reason=self._last_reason, image=image, now=moment,
                action_ui=action_ui)

        # ④ 一次都没确认过 → 仍然弃权（fail closed 不变）。
        return self._finish(
            value=None, source=LATCH_SOURCE_UNKNOWN,
            score=raw_score, margin=raw_margin, raw_value=None,
            reason="not_confirmed", image=image, now=moment,
            action_ui=action_ui)


class TeaModeLatch(TeaRuleLatch):
    """玩法人数（2 人茶 / 4 人）的粘滞门禁。

    详见模块顶部「玩法门禁粘滞」一节。这里的锁存值与人数的语义一一对应：
    正向读出 4 就等于翻牌，绝不会被锁着的 2 压住。
    """

    gate = "mode"

    def _measure(self, image: np.ndarray) -> tuple[object, float, float]:
        players, score, margin = score_tea_player_count(image)
        return players, score, margin


class TeaDeckLatch(TeaRuleLatch):
    """牌集规则「无中发」的粘滞门禁 —— 与人数**不同维度**，各自独立一把锁。

    两者的锁存值互不相干（一间房的人数变了不代表牌集也变了，反之亦然），但
    **重置判据必须共用**：换房换局时两把锁要走同一套 HUD/财神/TTL 信号一起失效，
    这个"一起失效"由 :class:`TeaRuleGates` 保证。

    与第二把锁有关的诚实说明（不吹）：人数那把锁有「正向矛盾翻牌」这条最强的
    安全兜底（读到 4 就一定不是 2），而**这把锁没有**。原因是游戏只提供"无中发"
    这一张规则条幅模板，不存在"有中发"的竞争模板，所以这一 concerns 只能回答
    "确认了"或"没确认"，无法从像素上读出相反的结论。它的安全兜底因此只有：
    ① 作用域信号（HUD/财神）变化即失效；② TTL 超时即失效；③ 任何一帧条幅清晰
    可读时都会用当帧的真值重新确认（分数 ≥0.94 才认，认不出来不算驳斥）。
    风险和控制方式与第一把锁同构：只有"换到一个牌集不同、且条幅恰好被持续
    遮挡的新房间且**财神与 HUD 也没变**"才可能踩雷，而那种情形下逐帧判定本来
    也是 False（= 弃权）。
    """

    gate = "deck"

    def _measure(self, image: np.ndarray) -> tuple[object, float, float]:
        confirmed, score = score_tea_no_zhong_fa(image)
        # 只有 True 是"确认过的事实"；读不出时给 None（= 不许锁成 True，
        # 也不许当成"这张桌子有中发"的证据）。
        return (True if confirmed else None), score, 0.0


class TeaRuleGates:
    """All sticky rule gates of one 温州茶 room, behind **one** invalidation entry.

   为什么要有这一层：玩法人数和牌集规则是两个维度，各自一把锁，但如果它们各自
   判断何时失效，就可能出现"人数锁因为换局作废了、牌集锁还拿着上一局的结论"这种
   最危险的组合。这里把两把锁的作废动作收成唯一入口：``reset()`` 一定作用在
   两把锁上，``observe()`` 也一次性给两把锁喂同一份 **局身份**（同一帧只算一次
   HUD/财神）和同一份 ``action_ui``（同一帧只读一次行动圆盘）。
    """

    def __init__(self, *, ttl_s: float = DEFAULT_LATCH_TTL_S,
                 clock: Callable[[], float] | None = None):
        self.mode = TeaModeLatch(ttl_s=ttl_s, clock=clock)
        self.deck = TeaDeckLatch(ttl_s=ttl_s, clock=clock)

    def reset(self, reason: str = "explicit") -> None:
        """Invalidate **every** gate at once. 两条锁永不各自为政。"""
        self.mode.reset(reason=reason)
        self.deck.reset(reason=reason)

    def observe(self, image: np.ndarray, *,
                now: float | None = None) -> tuple[RuleReading, RuleReading]:
        """Read one frame through both gates; returns ``(mode, deck)`` readings."""
        moment = self.mode._clock() if now is None else float(now)
        scope = frame_round_identity(image)
        # action_ui 只建一次再喂给两把锁：不然每帧的行动圆盘要被读两遍。
        shared_ui: dict | None = None
        mode = self.mode.observe(image, now=moment, scope=scope)
        shared_ui = dict(mode.action_ui)
        shared_ui.update({
            "deck_score": round(float(self.deck.snapshot()["score"]), 6),
            "deck_score_min": DECK_SCORE_MIN,
        })
        deck = self.deck.observe(image, now=moment, scope=scope,
                                 action_ui=shared_ui)
        shared_ui["deck_score"] = round(float(deck.score), 6)
        shared_ui["deck_latched"] = deck.latched
        shared_ui["deck_source"] = deck.source
        mode.action_ui.update({key: shared_ui[key] for key in
                               ("deck_score", "deck_latched", "deck_source")})
        deck.action_ui.update({key: shared_ui[key] for key in
                               ("deck_score", "deck_latched", "deck_source")})
        return mode, deck

    def snapshot(self) -> dict:
        """Both gates' state in one JSON-friendly dict."""
        return {"mode": self.mode.snapshot(), "deck": self.deck.snapshot()}


#: Process-wide gates used by ``tea_bot._default_vision``.
#: ``assist_tea.analyze_snapshot`` builds a **new TeaBot per frame**, so the
#: latches have to outlive any one bot instance to be of any use. Scope-reset
#: signals (HUD fingerprint + caishen) keep them honest across rooms.
RULE_GATES = TeaRuleGates()
MODE_LATCH = RULE_GATES.mode
DECK_LATCH = RULE_GATES.deck


def confirm_tea_player_count(image: np.ndarray) -> int | None:
    """Sticky wrapper around :func:`detect_tea_player_count` via :data:`MODE_LATCH`."""
    return MODE_LATCH.observe(image).players


def confirm_tea_no_zhong_fa(image: np.ndarray) -> bool:
    """Sticky wrapper around :func:`detect_tea_no_zhong_fa` via :data:`DECK_LATCH`."""
    return DECK_LATCH.observe(image).value is True


def latest_action_ui() -> dict:
    """Last frame's action/mode record from :data:`MODE_LATCH` (``{}`` before the first read)."""
    return dict(getattr(MODE_LATCH, "_last_action_ui", {}) or {})


def reset_tea_mode_latch(reason: str = "manual") -> None:
    """Invalidate **every** sticky rule gate (call on room/mode change).

    名字保留 ``..._mode_latch`` 是为了不打断既有调用方，但它作废的是整组锁。
    """
    RULE_GATES.reset(reason=reason)
