# -*- coding: utf-8 -*-
"""Write countdown_vision.py and table_state.py for P0 chunk A."""
from pathlib import Path

BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")

countdown_src = r'''# -*- coding: utf-8 -*-
"""Countdown visual detector for the tea table (P0-3).

Reads a small set of verified ROIs on a 1600x900 BGR frame and reports whether
a turn/claim countdown digit is visible. Exact OCR is best-effort against
synthetic digit templates; when the digit cannot be read the status is
``unreadable`` (fail-closed) rather than the old permanent ``unknown`` stub.

Statuses
--------
``absent``      no digit-like blob in the countdown ROIs
``present``     digit blob seen and seconds parsed (>= 2)
``low``         parsed seconds == 1 (opponent may click first)
``expired``     parsed seconds == 0
``unreadable``  blob-like evidence without a trusted digit parse, or bad frame

This module is pure vision: no ADB, no clicks, no disk writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import cv2
import numpy as np

FRAME_HEIGHT = 900
FRAME_WIDTH = 1600

# Verified band just above the claim-row discs (y≈534..666). Digits, when shown
# during a claim prompt, sit in the right half of this strip on tea_2p frames.
COUNTDOWN_ROIS: tuple[tuple[int, int, int, int], ...] = (
    (450, 540, 1000, 1520),  # y0,y1,x0,x1 — above claim discs
    (300, 420, 720, 980),    # table-centre seat timer pocket
)

DIGIT_MATCH_MIN = 0.55
BLOB_AREA_MIN = 40
BLOB_AREA_MAX = 4000
BLOB_H_MIN = 14
BLOB_H_MAX = 90
BLOB_W_MIN = 6
BLOB_W_MAX = 70

STATUS_ABSENT = "absent"
STATUS_PRESENT = "present"
STATUS_LOW = "low"
STATUS_EXPIRED = "expired"
STATUS_UNREADABLE = "unreadable"

__all__ = [
    "COUNTDOWN_ROIS",
    "CountdownReading",
    "STATUS_ABSENT",
    "STATUS_EXPIRED",
    "STATUS_LOW",
    "STATUS_PRESENT",
    "STATUS_UNREADABLE",
    "detect_countdown",
]


@dataclass(frozen=True)
class CountdownReading:
    """One-frame countdown verdict."""

    status: str
    seconds: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        return {
            "countdown_status": self.status,
            "countdown_seconds": self.seconds,
            "countdown_evidence": dict(self.evidence),
        }


def _render_digit(digit: int, height: int = 48) -> np.ndarray:
    """Synthetic white-on-black digit used as a weak template."""
    canvas = np.zeros((height, max(20, height // 2 + 8)), np.uint8)
    thickness = max(2, height // 12)
    cv2.putText(
        canvas, str(digit), (2, height - 6),
        cv2.FONT_HERSHEY_SIMPLEX, height / 40.0, 255, thickness, cv2.LINE_AA,
    )
    return canvas


@lru_cache(maxsize=1)
def _digit_templates() -> tuple[tuple[int, np.ndarray], ...]:
    return tuple((d, _render_digit(d)) for d in range(10))


def _match_digit(gray_roi: np.ndarray) -> tuple[int | None, float]:
    """Return (digit, score) for the best synthetic-template match."""
    if gray_roi is None or gray_roi.size == 0:
        return None, 0.0
    roi = gray_roi
    if roi.dtype != np.uint8:
        roi = roi.astype(np.uint8)
    best_digit: int | None = None
    best_score = -1.0
    rh, rw = roi.shape[:2]
    for digit, tmpl in _digit_templates():
        th, tw = tmpl.shape[:2]
        # scale template to roi height
        scale = rh / th
        nw = max(4, int(tw * scale))
        nh = max(8, int(th * scale))
        if nw >= rw or nh >= rh:
            resized = cv2.resize(tmpl, (min(rw - 1, nw), min(rh - 1, nh)),
                                 interpolation=cv2.INTER_AREA)
        else:
            resized = cv2.resize(tmpl, (nw, nh), interpolation=cv2.INTER_AREA)
        if resized.shape[0] >= rh or resized.shape[1] >= rw:
            continue
        result = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
        score = float(result.max()) if result.size else -1.0
        if score > best_score:
            best_score = score
            best_digit = digit
    if best_score < DIGIT_MATCH_MIN:
        return None, best_score
    return best_digit, best_score


def _digit_blobs(gray: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Return (x, y, w, h, area) for bright digit-like connected components."""
    # Prefer bright glyphs on darker felt.
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Also keep a high fixed threshold path for white digits.
    _, th_hi = cv2.threshold(blur, 200, 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_or(th, th_hi)
    n, _labels, stats, _cents = cv2.connectedComponentsWithStats(mask)
    out: list[tuple[int, int, int, int, int]] = []
    for i in range(1, n):
        x, y, w, h, area = (int(stats[i, c]) for c in range(5))
        if area < BLOB_AREA_MIN or area > BLOB_AREA_MAX:
            continue
        if h < BLOB_H_MIN or h > BLOB_H_MAX or w < BLOB_W_MIN or w > BLOB_W_MAX:
            continue
        aspect = w / max(h, 1)
        if aspect > 1.6 or aspect < 0.12:
            continue
        out.append((x, y, w, h, area))
    out.sort(key=lambda item: item[4], reverse=True)
    return out[:6]


def detect_countdown(image: Any) -> CountdownReading:
    """Detect claim/turn countdown on one 1600x900 BGR frame."""
    evidence: dict[str, Any] = {
        "rois": [list(r) for r in COUNTDOWN_ROIS],
        "digit_match_min": DIGIT_MATCH_MIN,
        "candidates": [],
    }
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return CountdownReading(STATUS_UNREADABLE, None, evidence)
    if image.shape[0] != FRAME_HEIGHT or image.shape[1] != FRAME_WIDTH:
        evidence["frame_shape"] = list(image.shape[:2])
        return CountdownReading(STATUS_UNREADABLE, None, evidence)

    best: tuple[int, float, tuple[int, int, int, int]] | None = None
    any_blob = False
    for y0, y1, x0, x1 in COUNTDOWN_ROIS:
        roi = image[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        blobs = _digit_blobs(gray)
        if blobs:
            any_blob = True
        for x, y, w, h, area in blobs:
            crop = gray[y:y + h, x:x + w]
            digit, score = _match_digit(crop)
            evidence["candidates"].append({
                "roi": [y0, y1, x0, x1],
                "box": [x0 + x, y0 + y, x0 + x + w, y0 + y + h],
                "area": area,
                "digit": digit,
                "score": round(score, 4),
            })
            if digit is None:
                continue
            if best is None or score > best[1]:
                best = (digit, score, (x0 + x, y0 + y, w, h))

    if best is None:
        status = STATUS_UNREADABLE if any_blob else STATUS_ABSENT
        evidence["reason"] = ("digit blob without trusted parse" if any_blob
                              else "no digit-like blob in countdown ROIs")
        return CountdownReading(status, None, evidence)

    seconds, score, box = best[0], best[1], best[2]
    evidence["best"] = {"seconds": seconds, "score": round(score, 4),
                        "box": [box[0], box[1], box[0] + box[2], box[1] + box[3]]}
    if seconds <= 0:
        return CountdownReading(STATUS_EXPIRED, 0, evidence)
    if seconds == 1:
        return CountdownReading(STATUS_LOW, 1, evidence)
    return CountdownReading(STATUS_PRESENT, seconds, evidence)
'''

table_src = r'''# -*- coding: utf-8 -*-
"""Unified per-frame table snapshot (P0-5).

Every decide / discard path should refresh a :class:`TableState` from the
**current** frame before acting. The structure is intentionally flat and
JSON-friendly so auto_play / assist ledgers can dump it without a second
schema.

This module does not talk to ADB or click anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from countdown_vision import CountdownReading, detect_countdown
from turn_gate import (
    action_ui_evidence,
    known_caishen_id,
    logical_meld_slots,
    meld_total,
)

__all__ = [
    "TableState",
    "WALL_CARDS_STOP_AT",
    "build_table_state",
]

#: Audit P0: stop auto-play when remaining wall/room cards fall to this count.
WALL_CARDS_STOP_AT = 16


@dataclass(frozen=True)
class TableState:
    """Current-frame facts required before discard/claim decisions."""

    mode: str | None = None
    status: str | None = None
    caishen: str | None = None
    caishen_id: int | None = None
    caishen_known: bool = False
    hand_ids: tuple[int, ...] = ()
    concealed_count: int | None = None
    physical_boxes: int | None = None
    meld_count: int = 0
    meld_total_physical: int = 0
    logical_meld_slots: int = 0
    action_discs_seen: bool = False
    action_disc_count: int = 0
    claim_decision: str | None = None
    countdown_status: str = "absent"
    countdown_seconds: int | None = None
    wall_cards: int | None = None
    discards_trusted: bool = False
    incoming_tile: int | None = None
    incoming_source: str | None = None
    notes: tuple[str, ...] = ()
    action_ui: dict[str, Any] = field(default_factory=dict)
    countdown_evidence: dict[str, Any] = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hand_ids"] = list(self.hand_ids)
        payload["notes"] = list(self.notes)
        return payload

    @property
    def should_stop_for_wall(self) -> bool:
        return self.wall_cards is not None and self.wall_cards <= WALL_CARDS_STOP_AT

    @property
    def should_stop_for_unknown_caishen(self) -> bool:
        return not self.caishen_known


def build_table_state(
    result: Mapping[str, Any] | None,
    image: Any = None,
    *,
    wall_cards: int | None = None,
    discards_trusted: bool = False,
    incoming_tile: int | None = None,
    incoming_source: str | None = None,
    action_ui: Mapping[str, Any] | None = None,
    countdown: CountdownReading | None = None,
) -> TableState:
    """Build a TableState from this-frame analysis + optional vision hooks."""
    result = dict(result or {})
    notes: list[str] = []

    ui = dict(action_ui) if action_ui is not None else action_ui_evidence(image)
    if countdown is None:
        countdown = detect_countdown(image)
    # Prefer live countdown detector over the action_ui stub fields.
    ui = dict(ui)
    ui["countdown_status"] = countdown.status
    ui["countdown_seconds"] = countdown.seconds

    hand_ids_raw = result.get("hand_ids") or ()
    hand_ids: list[int] = []
    if isinstance(hand_ids_raw, (list, tuple)):
        for value in hand_ids_raw:
            try:
                hand_ids.append(int(value))
            except (TypeError, ValueError):
                notes.append("hand_ids_partial")
                hand_ids = []
                break

    boxes = result.get("concealed_boxes") or result.get("hand_boxes") or []
    physical = len(boxes) if isinstance(boxes, (list, tuple)) else None
    if physical is None and hand_ids:
        physical = len(hand_ids)

    caishen = result.get("caishen")
    if caishen is not None:
        caishen = str(caishen) or None
    cid = known_caishen_id(result)
    caishen_known = cid is not None or bool(caishen)

    melds = result.get("melds")
    meld_count = 0
    if isinstance(melds, (list, tuple)):
        meld_count = len([g for g in melds if isinstance(g, (list, tuple)) and g])
    elif result.get("meld_count") is not None:
        try:
            meld_count = int(result.get("meld_count") or 0)
        except (TypeError, ValueError):
            meld_count = 0

    if wall_cards is not None and wall_cards <= WALL_CARDS_STOP_AT:
        notes.append(f"wall_cards<={WALL_CARDS_STOP_AT}")
    if not caishen_known:
        notes.append("caishen_unknown")
    if countdown.status in {"low", "expired", "unreadable"}:
        notes.append(f"countdown_{countdown.status}")

    return TableState(
        mode=(str(result["mode"]) if result.get("mode") is not None else None),
        status=(str(result["status"]) if result.get("status") is not None else None),
        caishen=caishen,
        caishen_id=cid,
        caishen_known=caishen_known,
        hand_ids=tuple(hand_ids),
        concealed_count=len(hand_ids) if hand_ids else None,
        physical_boxes=physical,
        meld_count=meld_count,
        meld_total_physical=meld_total(result),
        logical_meld_slots=logical_meld_slots(result),
        action_discs_seen=bool(ui.get("action_discs_seen")),
        action_disc_count=int(ui.get("action_disc_count") or 0),
        claim_decision=ui.get("claim_decision"),
        countdown_status=str(countdown.status),
        countdown_seconds=countdown.seconds,
        wall_cards=wall_cards,
        discards_trusted=bool(discards_trusted),
        incoming_tile=incoming_tile,
        incoming_source=incoming_source,
        notes=tuple(notes),
        action_ui=dict(ui),
        countdown_evidence=dict(countdown.evidence),
    )
'''

(BOT / "countdown_vision.py").write_text(countdown_src, encoding="utf-8")
(BOT / "table_state.py").write_text(table_src, encoding="utf-8")
print("wrote countdown_vision.py + table_state.py")