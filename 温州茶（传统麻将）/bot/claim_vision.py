"""Read-only interpretation of the claim-action row of the 温州茶 2-player screen.

Scope and limits
----------------
This module answers exactly one question for one native-resolution frame:

    "Which claim-action discs are visible, and what is the most conservative
     decision that follows from them?"

It is deliberately narrow:

* It never taps, never talks to ADB, never touches the filesystem and never
  reads the manifest. ``read_claims`` is a pure function.
* It performs no OCR and adds no dependency: only ``cv2`` and ``numpy``.
* It is **fail-closed**. Anything it cannot establish from pixels comes back as
  ``decision=None``. A missed claim is always preferable to a misleading tap.

Provenance of every number below
--------------------------------
Geometry and colour constants were calibrated **offline** on archived native
ADB screenshots (1600x900, same coordinate domain as ``tap_executor.py``) taken
from these read-only sessions under ``采集/assist_sessions/``::

    20261023_201640  20261023_223209  20261023_235121  20261023_225317
    20261023_225827  20261024_002903  20261024_122607

Calibration set sizes are recorded next to each threshold. Frame counts drift
because one of those sessions was still being appended to while this module was
written (see the per-run totals quoted in ``THRESHOLD_PROVENANCE``).

Two documents are inputs to this module and are treated as prior art, not as
things to re-derive:

* ``采集/workbuddy_review_20261024/action_inventory.md`` -- 3172-frame,
  10-session inventory that established that the buttons are **right-aligned
  slots** with centres near x = 824 / 1033 / 1242 / 1431 and y = 596..620, that
  ``过`` never appears alone, and that a lone blue disc is ``取消``.
* The operator's screenshot of the 吃/碰 selection panel, which established that
  ``取消`` is **not confined to the action row** -- it also appears higher up,
  beside the candidate-tile panel. That screenshot was a Windows window capture
  (it included the emulator's own toolbar), so only the qualitative fact was
  taken from it, never any coordinate.

What "过" vs "取消" really means here
-------------------------------------
``过`` and ``取消`` are both blue discs and are **not separable by colour**.
They are separated here by *two* independent signals:

1. Co-occurrence -- a blue disc sharing the frame with a gold or dark-red disc
   behaves like the action row's ``过``; a blue disc with no company is ``取消``.
2. Position -- a blue disc whose centre is outside the action-row band
   (:data:`ACTION_BAND_Y0` .. :data:`ACTION_BAND_Y1`) is treated as ``取消``
   **even when gold or red discs are visible**. This is the selection-panel case:
   the panel may show a gold confirm button while its blue ``取消`` sits higher
   up. Tapping ``取消`` would abort an in-progress selection, which is not
   reversible, so this gate intentionally prefers losing the claim.

Whenever any ``取消`` is reported, the decision is forced to ``None``.

Deliberately unresolved: 碰 / 吃 / 杠
-------------------------------------
All three are gold discs with the same 126x132-ish footprint. The inventory showed
they need glyph templates, and 杠 had only one independent positive sample, so
this module refuses to guess which is which: they are reported as
:data:`KIND_GOLD`. Splitting them requires real template samples that this
module does not have.

**This module has never been validated against a live tap.** It reports what it
observed on offline frames only. No claim is made that any decision produced
here is safe to execute in a real game.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple, Sequence
import time

import cv2
import numpy as np

__all__ = [
    "ClaimButton",
    "ClaimState",
    "decision_priority",
    "read_claims",
]


# --------------------------------------------------------------------------
# Frame geometry
# --------------------------------------------------------------------------

FRAME_WIDTH: int = 1600
FRAME_HEIGHT: int = 900

# Search window for gold / dark-red discs. These are the claim buttons and they
# live in the action row only, so this window stays tight to suppress the large
# gold "action accepted" animation glyphs and the 胡牌 panel.
# Measured disc extents over every located disc: bbox top 533..554, centre-y
# 588..610. Widest observed excursion in either direction is well inside this
# window; the only thing that ever reached past it was the animation glyph at
# bbox top 491, which is rejected by ACTION_DISK_TOP_Y_MIN anyway.
ACTION_ROI_Y0: int = 480
ACTION_ROI_Y1: int = 710
ACTION_ROI_X0: int = 660
ACTION_ROI_X1: int = 1590

# Search window for blue discs, widened upward because 取消 also appears next to
# the candidate-tile selection panel, above the action row. Vertical coverage
# follows the operator's report; no coordinate was taken from that screenshot.
BLUE_ROI_Y0: int = 300
BLUE_ROI_Y1: int = 720
BLUE_ROI_X0: int = 740
BLUE_ROI_X1: int = 1580

# A blue disc is only eligible to be 过 when its centre sits inside this band.
# Everything else about a blue disc says 取消.
ACTION_BAND_Y0: int = 510
ACTION_BAND_Y1: int = 700


# --------------------------------------------------------------------------
# Colour thresholds. OpenCV HSV: H in 0..179, S and V in 0..255.
#
# Calibration: the whole-frame pixel ratios inside the action-row ROI were
# measured on 3564 archived frames (see THRESHOLD_PROVENANCE for the split).
# --------------------------------------------------------------------------

GOLD_HUE_MIN: int = 12
GOLD_HUE_MAX: int = 38
GOLD_SAT_MIN: int = 90
GOLD_VAL_MIN: int = 150

RED_HUE_WRAP_MAX: int = 8      # dark-red is H<=8 after wrapping past H>=172
RED_HUE_WRAP_MIN: int = 172
RED_SAT_MIN: int = 110
RED_VAL_MIN: int = 60

BLUE_HUE_MIN: int = 85
BLUE_HUE_MAX: int = 115
BLUE_SAT_MIN: int = 90
BLUE_VAL_MIN: int = 120


# --------------------------------------------------------------------------
# Blob gates
# --------------------------------------------------------------------------

MIN_DISK_AREA: int = 4200
# Margins measured on archived frames: every accepted disc is 4990..13160 px
# (per kind: cancel 4990..5016, pass 5479..5495, hu 7597..10771, gold
# 12611..13160), while the largest table-texture blob rejected by earlier sweeps
# was 3644 px. This threshold leaves 790 px below the smallest real disc and
# 556 px above the largest observed non-disc blob.
MIN_BBOX_EDGE: int = 80
MAX_BBOX_EDGE: int = 200
MIN_ASPECT: float = 0.75
MAX_ASPECT: float = 1.33

# Action-row specific gates. Every value below is a hard boundary around the
# observed population with its margin recorded, so that the cost of moving it
# is visible rather than guessed.
ACTION_DISK_TOP_Y_MIN: int = 505
ACTION_DISK_CENTER_Y_MIN: int = 578
ACTION_DISK_CENTER_Y_MAX: int = 645
ACTION_DISK_CENTER_X_MIN: int = 754
ACTION_DISK_CENTER_X_MAX: int = 1501

MORPH_CLOSE_KERNEL: int = 17
MORPH_OPEN_KERNEL: int = 5

# Fraction of the bounding box kept when sampling a disc's interior: 0.28..0.72
# of each axis, i.e. the central 44% box. The action glyph itself carries no
# useful colour, so the interior is sampled instead of the whole disc.
CORE_SAMPLE_LO: float = 0.28
CORE_SAMPLE_HI: float = 0.72


# --------------------------------------------------------------------------
# Disc kinds and decisions
# --------------------------------------------------------------------------

KIND_HU: str = "hu"
KIND_GOLD: str = "gold"
KIND_PASS: str = "pass"
KIND_CANCEL: str = "cancel"

# Declared so downstream can match exhaustively, but never emitted by this
# revision: telling them apart needs glyph templates this module does not have.
RESERVED_KINDS: tuple[str, ...] = ("kong", "peng", "chi")

DECISION_HU: str = "hu"
DECISION_PASS: str = "pass"

# Slot centres measured in action_inventory.md across 10 sessions. Used only to
# place the x-window above and to annotate evidence. A disc's box is always the
# measured bounding box of its own connected component; slots never synthesise
# a box and never snap one.
SLOT_CENTER_X: tuple[int, ...] = (824, 1033, 1242, 1431)
SLOT_X_TOLERANCE: int = 70

THRESHOLD_PROVENANCE: dict[str, Any] = {
    "source": "采集/assist_sessions (offline read-only PNG replay, no ADB, no tap)",
    "sessions": (
        "20261023_201640", "20261023_223209", "20261023_235121",
        "20261023_225317", "20261023_225827", "20261024_002903",
        "20261024_122607",
    ),
    # Two sweeps, deliberately reported separately: sessions on disk grew between
    # them because 20261024_122607 was still being appended to while this was
    # measured, so the two counts differ by design rather than by mistake.
    "threshold_calibration": {
        "frames": 3564,
        "gold_mask_discs_located": 328,
        "hu_core_red": [0.1734, 0.5274],
        "gold_core_red": [0.0022, 0.0639],
        "hu_core_red_min": 0.10,
        "hu_core_red_min_margin": [0.0361, 0.0734],
    },
    "validation_sweep": {
        "frames": 3891,
        "decision": {"hu": 103, "pass": 157, "None": 3631},
        "discs": {"hu": 103, "gold": 230, "pass": 260, "cancel": 41},
        "blue_center_y_seen": [600, 604],
        "blue_discs_outside_action_row": 0,
    },
    "note": ("Blue discs were only ever observed inside the action row on these "
             "frames; the widened blue window costs nothing here and is kept as "
             "insurance for the selection-panel layout reported by the operator."),
}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimButton:
    """One action disc located by connected-component analysis.

    Attributes:
        kind: One of ``"hu"``, ``"gold"``, ``"pass"``, ``"cancel"``. ``"gold"``
            means "a gold claim disc, but which of 碰/吃/杠 it is cannot be
            established without glyph templates".
        box: ``(x0, y0, x1, y1)`` measured bounding box in full-frame 1600x900
            coordinates. Always derived from pixels; never snapped to a slot.
        center: ``(cx, cy)`` centre of that measured box, same coordinate
            domain as ``tap_executor.py`` expects.
    """

    kind: str
    box: tuple[int, int, int, int]
    center: tuple[int, int]

    @property
    def width(self) -> int:
        """Width of the measured box in pixels."""
        return self.box[2] - self.box[0]

    @property
    def height(self) -> int:
        """Height of the measured box in pixels."""
        return self.box[3] - self.box[1]


@dataclass(frozen=True)
class ClaimState:
    """What the claim row showed, and the most conservative reading of it.

    Attributes:
        buttons: Every disc that survived the gates, sorted left to right.
        decision: ``"hu"``, ``"pass"`` or ``None``. ``None`` always means
            "do nothing" -- whether because nothing was visible, because nothing
            was legible, or because acting would risk tapping 取消.
        reason: Human-readable justification, written into the operator ledger.
        evidence: JSON-serialisable measurements backing ``decision``.
        known: ``True`` when at least one action disc was actually located.
            ``False`` means "nothing readable here", which is not evidence that
            there is no claim to answer.
    """

    buttons: tuple[ClaimButton, ...]
    decision: str | None
    reason: str
    evidence: dict[str, Any]
    known: bool
    captured_at: float | None = None


class _Disc(NamedTuple):
    """Intermediate measurement kept private to this module."""

    box: tuple[int, int, int, int]
    center: tuple[int, int]
    area: int
    fill: float


HU_CORE_RED_MIN: float = 0.10
"""Interior red-pixel fraction that separates 胡 from 碰/吃/杠.

Measured over all 328 gold-mask discs located in the calibration sweep, by
sampling the central 44% box of each disc's own bounding box:

* gold discs (碰/吃/杠 and their combinations): core red ``0.0022 .. 0.0639``
* 胡 discs:                                    core red ``0.1734 .. 0.5274``

The band ``0.0639 .. 0.1734`` is empty. This threshold sits at ``0.10``, giving
``0.036`` clearance above the strongest gold disc and ``0.073`` below the
weakest 胡 disc. Two corroborating signals were measured but are deliberately
not used as the primary rule: 胡 discs are smaller in the gold mask
(``4985..10771`` px) than gold discs (``12611..13160`` px), and they carry a
lower interior gold fraction. Colour ratio is kept because it is the quantity
that survives the disc being partly occluded by its own glyph.
"""


THRESHOLD_PROVENANCE["threshold_calibration"]["hu_core_red_min"] = HU_CORE_RED_MIN


def _colour_mask(
    hsv: np.ndarray,
    hue_range: tuple[int, int],
    sat_min: int,
    val_min: int,
) -> np.ndarray:
    """Build a binary HSV mask; ``hue_range`` may wrap by having lo > hi."""
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    hue_lo, hue_hi = hue_range
    if hue_lo <= hue_hi:
        hue_ok = (hue >= hue_lo) & (hue <= hue_hi)
    else:
        hue_ok = (hue >= hue_lo) | (hue <= hue_hi)
    return ((hue_ok & (sat >= sat_min) & (val >= val_min))).astype(np.uint8) * 255


def _locate_discs(
    mask: np.ndarray,
    origin_x: int,
    origin_y: int,
    *,
    action_row: bool,
) -> list[_Disc]:
    """Locate disc-shaped connected components inside one colour mask.

    Morphological closing bridges the gap left by each disc's own glyph so that
    a disc stays one component; opening removes single-pixel speckle left by the
    table texture. Every returned box is the component's own measured bounding
    box expressed back in full-frame coordinates.

    Args:
        mask: Binary mask already cropped to the region of interest.
        origin_x: ROI left edge, added back to get frame coordinates.
        origin_y: ROI top edge, added back to get frame coordinates.
        action_row: Apply the action-row position gates. Blue discs are searched
            outside the action row on purpose, so they skip them.

    Returns:
        Discs that survived every applicable gate.
    """
    closed = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (MORPH_CLOSE_KERNEL, MORPH_CLOSE_KERNEL)),
    )
    opened = cv2.morphologyEx(
        closed,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (MORPH_OPEN_KERNEL, MORPH_OPEN_KERNEL)),
    )
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(opened, 8)
    height, width = opened.shape[:2]

    found: list[_Disc] = []
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        left = int(stats[index, cv2.CC_STAT_LEFT])
        top = int(stats[index, cv2.CC_STAT_TOP])
        box_w = int(stats[index, cv2.CC_STAT_WIDTH])
        box_h = int(stats[index, cv2.CC_STAT_HEIGHT])

        if area < MIN_DISK_AREA:
            continue
        if not (MIN_BBOX_EDGE <= box_w <= MAX_BBOX_EDGE
                and MIN_BBOX_EDGE <= box_h <= MAX_BBOX_EDGE):
            continue
        aspect = box_w / float(box_h)
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            continue
        # Touching the crop edge means the component is clipped, so its measured
        # box would be wrong. Action discs are never clipped by these windows.
        if left == 0 or top == 0 or left + box_w >= width or top + box_h >= height:
            continue

        top_y = top + origin_y
        center_x = int(centroids[index][0]) + origin_x
        center_y = int(centroids[index][1]) + origin_y
        if action_row:
            if not (ACTION_DISK_CENTER_Y_MIN <= center_y <= ACTION_DISK_CENTER_Y_MAX):
                continue
            if top_y < ACTION_DISK_TOP_Y_MIN:
                continue
            if not (ACTION_DISK_CENTER_X_MIN <= center_x <= ACTION_DISK_CENTER_X_MAX):
                continue

        found.append(_Disc(
            box=(left + origin_x, top_y,
                 left + box_w + origin_x, top_y + box_h),
            center=(center_x, center_y),
            area=area,
            fill=round(area / float(box_w * box_h), 3),
        ))
    return found


def _core_ratios(image: np.ndarray, box: tuple[int, int, int, int]) -> tuple[float, float]:
    """Return ``(red_ratio, gold_ratio)`` inside the central 44% of ``box``."""
    x0, y0, x1, y1 = box
    box_w = x1 - x0
    box_h = y1 - y0
    core_y0 = y0 + int(box_h * CORE_SAMPLE_LO)
    core_y1 = y0 + int(box_h * CORE_SAMPLE_HI)
    core_x0 = x0 + int(box_w * CORE_SAMPLE_LO)
    core_x1 = x0 + int(box_w * CORE_SAMPLE_HI)
    patch = image[core_y0:core_y1, core_x0:core_x1]
    if patch.size == 0:
        return 0.0, 0.0

    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(np.int16)
    sat = hsv[:, :, 1].astype(np.int16)
    val = hsv[:, :, 2].astype(np.int16)
    total = hue.size
    red = (((hue <= RED_HUE_WRAP_MAX) | (hue >= RED_HUE_WRAP_MIN))
           & (sat >= RED_SAT_MIN) & (val >= RED_VAL_MIN)).sum()
    gold = ((hue >= GOLD_HUE_MIN) & (hue <= GOLD_HUE_MAX)
            & (sat >= GOLD_SAT_MIN) & (val >= GOLD_VAL_MIN)).sum()
    return float(red) / float(total), float(gold) / float(total)


def _slot_index(center_x: int) -> int:
    """Index of the nearest slot within tolerance, or ``-1``. Annotation only."""
    distances = [abs(center_x - slot) for slot in SLOT_CENTER_X]
    best = min(range(len(distances)), key=lambda i: distances[i])
    return best if distances[best] <= SLOT_X_TOLERANCE else -1


def _pixel_ratio(mask: np.ndarray) -> float:
    """Share of ROI pixels lit in one colour mask, rounded for the ledger."""
    return round(float((mask > 0).sum()) / float(mask.size), 6)


def _decide(buttons: Sequence[ClaimButton]) -> tuple[str | None, str]:
    """Single source of truth for the decision and its reason.

    Both :func:`read_claims` and :func:`decision_priority` go through here, so
    the two cannot drift apart.
    """
    kinds = {button.kind for button in buttons}
    if not kinds:
        return None, "未检出任何行动圆盘"
    if KIND_CANCEL in kinds:
        # Tapping 取消 aborts an in-progress selection irreversibly, so any
        # cancel anywhere in the frame vetoes acting at all.
        return None, "存在取消盘，一律不出手"
    if KIND_HU in kinds:
        return DECISION_HU, "检出暗红盘（胡），胡优先"
    if KIND_PASS in kinds:
        return DECISION_PASS, "检出金色盘且同行有过按钮，按过处理"
    return None, "仅检出金色盘而无可点的过按钮，不出手"


def _thresholds_evidence() -> dict[str, Any]:
    """Snapshot of the decision constants, for the ledger."""
    return {
        "gold": {"hue": [GOLD_HUE_MIN, GOLD_HUE_MAX],
                 "sat_min": GOLD_SAT_MIN, "val_min": GOLD_VAL_MIN},
        "red": {"hue_max": RED_HUE_WRAP_MAX, "hue_min": RED_HUE_WRAP_MIN,
                "sat_min": RED_SAT_MIN, "val_min": RED_VAL_MIN},
        "blue": {"hue": [BLUE_HUE_MIN, BLUE_HUE_MAX],
                 "sat_min": BLUE_SAT_MIN, "val_min": BLUE_VAL_MIN},
        "hu_core_red_min": HU_CORE_RED_MIN,
        "core_sample": [CORE_SAMPLE_LO, CORE_SAMPLE_HI],
        "min_disk_area": MIN_DISK_AREA,
        "bbox_edge": [MIN_BBOX_EDGE, MAX_BBOX_EDGE],
        "aspect": [MIN_ASPECT, MAX_ASPECT],
        "action_disk_top_y_min": ACTION_DISK_TOP_Y_MIN,
        "action_disk_center_y": [ACTION_DISK_CENTER_Y_MIN, ACTION_DISK_CENTER_Y_MAX],
        "action_disk_center_x": [ACTION_DISK_CENTER_X_MIN, ACTION_DISK_CENTER_X_MAX],
        "action_band_y": [ACTION_BAND_Y0, ACTION_BAND_Y1],
    }


def _unknown(reason: str) -> ClaimState:
    """Build the fail-closed state used whenever nothing could be established."""
    decision, decided_reason = _decide(())
    evidence: dict[str, Any] = {
        "frame": None,
        "roi": {
            "action": [ACTION_ROI_Y0, ACTION_ROI_Y1, ACTION_ROI_X0, ACTION_ROI_X1],
            "blue": [BLUE_ROI_Y0, BLUE_ROI_Y1, BLUE_ROI_X0, BLUE_ROI_X1],
        },
        "pixel_ratio": {},
        "thresholds": _thresholds_evidence(),
        "candidate_discs": [],
        "buttons": [],
        "rule": ("过与取消同为蓝盘：靠共现（蓝盘须与红/金盘同框）"
                 "加位置（蓝盘中心须落在动作行带内）双重判定"),
    }
    return ClaimState(
        buttons=(),
        decision=decision,
        reason=f"{reason}；{decided_reason}",
        evidence=evidence,
        known=False,
        captured_at=time.time(),
    )


def read_claims(image: Any) -> ClaimState:
    """Read the claim row of one frame and return a conservative decision.

    Args:
        image: A 1600x900 ``uint8`` BGR frame. Anything else (wrong size, wrong
            channel count, wrong dtype, ``None``) is reported as unknown rather
            than coerced.

    Returns:
        A :class:`ClaimState`. ``known`` is ``True`` only when at least one disc
        was located by pixels. ``decision`` is never populated from inference:
        when the evidence does not pin a button down, nothing is recommended.
    """
    if image is None:
        return _unknown("帧为空")
    if not isinstance(image, np.ndarray):
        return _unknown("帧不是 numpy 数组")
    if image.ndim != 3 or image.shape[2] < 3:
        return _unknown(f"帧通道数不符（ndim={image.ndim}）")
    if image.dtype != np.uint8:
        return _unknown(f"帧 dtype 不是 uint8（{image.dtype}）")
    height, width = image.shape[:2]
    if (height, width) != (FRAME_HEIGHT, FRAME_WIDTH):
        return _unknown(f"帧尺寸 {width}x{height} 不是 {FRAME_WIDTH}x{FRAME_HEIGHT}")

    action_roi = image[ACTION_ROI_Y0:ACTION_ROI_Y1, ACTION_ROI_X0:ACTION_ROI_X1]
    action_hsv = cv2.cvtColor(action_roi, cv2.COLOR_BGR2HSV)
    gold_mask = _colour_mask(action_hsv, (GOLD_HUE_MIN, GOLD_HUE_MAX),
                             GOLD_SAT_MIN, GOLD_VAL_MIN)
    red_mask = _colour_mask(action_hsv, (RED_HUE_WRAP_MIN, RED_HUE_WRAP_MAX),
                            RED_SAT_MIN, RED_VAL_MIN)

    blue_roi = image[BLUE_ROI_Y0:BLUE_ROI_Y1, BLUE_ROI_X0:BLUE_ROI_X1]
    blue_hsv = cv2.cvtColor(blue_roi, cv2.COLOR_BGR2HSV)
    blue_mask = _colour_mask(blue_hsv, (BLUE_HUE_MIN, BLUE_HUE_MAX),
                             BLUE_SAT_MIN, BLUE_VAL_MIN)

    # A dark-red disc also carries a gold rim, so the gold mask is what locates
    # it; colour is only used afterwards to tell the two apart. Locating red
    # components directly proved unreliable -- the red interior is broken up by
    # its own glyph and rarely forms a single large component.
    gold_discs = _locate_discs(gold_mask, ACTION_ROI_X0, ACTION_ROI_Y0, action_row=True)
    blue_discs = _locate_discs(blue_mask, BLUE_ROI_X0, BLUE_ROI_Y0, action_row=False)

    candidates: list[dict[str, Any]] = []
    buttons: list[ClaimButton] = []

    for disc in gold_discs:
        core_red, core_gold = _core_ratios(image, disc.box)
        kind = KIND_HU if core_red >= HU_CORE_RED_MIN else KIND_GOLD
        candidates.append({
            "mask": "gold",
            "box": list(disc.box),
            "center": list(disc.center),
            "area": disc.area,
            "fill": disc.fill,
            "core_red": round(core_red, 6),
            "core_gold": round(core_gold, 6),
            "slot": _slot_index(disc.center[0]),
            "resolved_kind": kind,
        })
        buttons.append(ClaimButton(kind=kind, box=disc.box, center=disc.center))

    for disc in blue_discs:
        in_row = ACTION_BAND_Y0 <= disc.center[1] <= ACTION_BAND_Y1
        # Position gate outranks co-occurrence: an out-of-band blue disc is
        # always the selection panel's 取消, even with gold discs on screen.
        kind = KIND_PASS if (in_row and gold_discs) else KIND_CANCEL
        candidates.append({
            "mask": "blue",
            "box": list(disc.box),
            "center": list(disc.center),
            "area": disc.area,
            "fill": disc.fill,
            "in_action_row": in_row,
            "slot": _slot_index(disc.center[0]),
            "resolved_kind": kind,
        })
        buttons.append(ClaimButton(kind=kind, box=disc.box, center=disc.center))

    buttons.sort(key=lambda button: (button.center[0], button.center[1]))
    tuple_buttons = tuple(buttons)
    decision, reason = _decide(tuple_buttons)

    evidence: dict[str, Any] = {
        "frame": {"width": int(width), "height": int(height)},
        "roi": {
            "action": [ACTION_ROI_Y0, ACTION_ROI_Y1, ACTION_ROI_X0, ACTION_ROI_X1],
            "blue": [BLUE_ROI_Y0, BLUE_ROI_Y1, BLUE_ROI_X0, BLUE_ROI_X1],
        },
        "pixel_ratio": {
            "gold": _pixel_ratio(gold_mask),
            "red": _pixel_ratio(red_mask),
            "blue": _pixel_ratio(blue_mask),
        },
        "thresholds": _thresholds_evidence(),
        "candidate_discs": candidates,
        "buttons": [
            {"kind": button.kind,
             "box": list(button.box),
             "center": list(button.center)}
            for button in tuple_buttons
        ],
        "rule": ("过与取消同为蓝盘：靠共现（蓝盘须与红/金盘同框）"
                 "加位置（蓝盘中心须落在动作行带内）双重判定"),
        "provenance": THRESHOLD_PROVENANCE,
    }
    return ClaimState(
        buttons=tuple_buttons,
        decision=decision,
        reason=reason,
        evidence=evidence,
        known=bool(tuple_buttons),
        captured_at=time.time(),
    )


def decision_priority(state: ClaimState) -> str | None:
    """Collapse a state to ``"hu"`` > ``"pass"`` > ``None``.

    Args:
        state: A state produced by :func:`read_claims`.

    Returns:
        The same value as ``state.decision``. The two are produced by one shared
        helper, so they cannot disagree; the tests lock that invariant down.

        Anything that is not a :class:`ClaimState` yields ``None``. Raising would
        take down whatever loop calls this -- during a live hand that is worse
        than abstaining -- so an unusable input simply reads as "no action".
    """
    buttons = getattr(state, "buttons", None)
    if not isinstance(buttons, (tuple, list)):
        return None
    if not buttons:
        return None
    decision, _reason = _decide([b for b in buttons if isinstance(b, ClaimButton)])
    return decision
