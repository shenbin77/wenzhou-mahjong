# -*- coding: utf-8 -*-
"""Read reviewed seconds only from the native central tea-table timer.

The API is unchanged. This isolated slice first proves the panel at its fixed
native position, then compares the ENTIRE digit slot with retained real crops.
It never searches a broad area for a convenient digit. Only 1, 2 and 3 have
independent-frame coverage in this slice; zero, other values, multiple digits,
occlusion and uncertain source evidence return seconds=None. No click policy
or deadline rule lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

FRAME_HEIGHT = 900
FRAME_WIDTH = 1600
PANEL_ROI = (716, 315, 884, 486)  # x0, y0, x1, y1, native pixels
DIGIT_SLOT_ROI = (767, 374, 833, 427)
COUNTDOWN_ROIS = ((374, 427, 767, 833),)  # legacy y0,y1,x0,x1 format
PANEL_ANCHORS = {
    "top": (14, 3, 154, 13),
    "bottom": (14, 157, 154, 169),
    "left": (2, 14, 13, 157),
    "right": (155, 14, 167, 157),
}
PANEL_MATCH_MIN = 0.97
DIGIT_MATCH_MIN = 0.98
DIGIT_MARGIN_MIN = 0.10
REVIEWED_SECONDS = (1, 2, 3)
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "countdown"
STATUS_ABSENT = "absent"
STATUS_PRESENT = "present"
STATUS_LOW = "low"
STATUS_EXPIRED = "expired"  # API compatibility; zero is not reviewed here.
STATUS_UNREADABLE = "unreadable"

__all__ = [
    "COUNTDOWN_ROIS", "CountdownReading", "STATUS_ABSENT", "STATUS_EXPIRED",
    "STATUS_LOW", "STATUS_PRESENT", "STATUS_UNREADABLE", "detect_countdown",
]


@dataclass(frozen=True)
class CountdownReading:
    status: str
    seconds: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        return {
            "countdown_status": self.status,
            "countdown_seconds": self.seconds,
            "countdown_evidence": dict(self.evidence),
        }


def _crop(image: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = box
    return image[y0:y1, x0:x1]


@lru_cache(maxsize=8)
def _decode_asset(raw: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("template PNG cannot be decoded")
    # Bytes-backed storage cannot be made writable by another observation.
    return np.frombuffer(image.tobytes(), np.uint8).reshape(image.shape)


def _load_asset(record: dict, expected_shape: tuple[int, int, int]) -> np.ndarray:
    name = str(record["file"])
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("template file must be a local filename")
    raw = (TEMPLATE_DIR / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["asset_sha256"]:
        raise ValueError("template hash mismatch")
    image = _decode_asset(raw)
    if image.shape != expected_shape:
        raise ValueError("template dimensions do not match native ROI")
    return image


def _load_templates() -> tuple[np.ndarray, dict[int, np.ndarray]]:
    # Re-read and hash the few small assets. Cache only decoding by exact bytes:
    # changed/missing/corrupt assets can never silently reuse an old template.
    manifest = json.loads((TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("countdown manifest must be an object")
    if (manifest.get("schema") != "countdown_templates/v1"
            or manifest.get("native_shape") != [900, 1600, 3]
            or tuple(manifest.get("panel_roi_xyxy", ())) != PANEL_ROI
            or tuple(manifest.get("slot_roi_xyxy", ())) != DIGIT_SLOT_ROI
            or tuple(manifest.get("reviewed_seconds", ())) != REVIEWED_SECONDS):
        raise ValueError("unreviewed countdown manifest")
    panel = _load_asset(manifest["panel"], (171, 168, 3))
    digits = {}
    for record in manifest["digits"]:
        seconds = int(record["seconds"])
        if seconds not in REVIEWED_SECONDS or seconds in digits:
            raise ValueError("duplicate or unreviewed countdown template")
        digits[seconds] = _load_asset(record, (53, 66, 3))
    if tuple(sorted(digits)) != REVIEWED_SECONDS:
        raise ValueError("missing reviewed countdown template")
    return panel, digits


def _prove_timer_source(image: np.ndarray, reference: np.ndarray) -> dict:
    current = cv2.cvtColor(_crop(image, PANEL_ROI), cv2.COLOR_BGR2GRAY)
    expected = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    scores = {}
    for name, box in PANEL_ANCHORS.items():
        observed_edge = _crop(current, box)
        reference_edge = _crop(expected, box)
        score = float(cv2.matchTemplate(
            observed_edge, reference_edge, cv2.TM_CCOEFF_NORMED)[0, 0])
        scores[name] = score if np.isfinite(score) else -1.0
    return {
        "verified": all(value >= PANEL_MATCH_MIN for value in scores.values()),
        "panel_roi_xyxy": list(PANEL_ROI),
        "required_edges": list(PANEL_ANCHORS),
        "edge_scores": {key: round(value, 6) for key, value in scores.items()},
        "minimum_edge_score": PANEL_MATCH_MIN,
    }


def _gold_mask(slot: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(slot, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 40)
            & (hsv[:, :, 1] >= 55) & (hsv[:, :, 2] >= 110))


def _read_complete_slot(image: np.ndarray, templates: dict[int, np.ndarray]) -> dict:
    observed = _gold_mask(_crop(image, DIGIT_SLOT_ROI))
    ys, xs = np.where(observed)
    if not len(xs):
        return {"seconds": None, "reason": "timer_slot_has_no_reviewed_glyph"}
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    x0, y0, x1, y1 = box
    # The entire slot is retained. Two digits or clipped/extra glyphs are never
    # cropped down to the largest component and mistaken for a single second.
    complete_single = (
        11 <= x1 - x0 <= 28 and 29 <= y1 - y0 <= 38
        and 30 <= (x0 + x1) / 2 <= 36
        and 6 <= y0 and y1 <= 46
    )
    evidence = {"slot_roi_xyxy": list(DIGIT_SLOT_ROI),
                "whole_slot_mask_pixels": int(observed.sum()),
                "mask_box_in_slot": list(box), "complete_single": complete_single}
    if not complete_single:
        return {**evidence, "seconds": None,
                "reason": "unsupported_or_incomplete_timer_number"}
    ranked = []
    for seconds, template in templates.items():
        expected = _gold_mask(template)
        denominator = int(observed.sum()) + int(expected.sum())
        dice = 2.0 * int((observed & expected).sum()) / max(1, denominator)
        ranked.append((dice, seconds))
    ranked.sort(reverse=True)
    best, seconds = ranked[0]
    margin = best - ranked[1][0]
    evidence.update({
        "score_metric": "whole_slot_gold_mask_dice", "score_min": DIGIT_MATCH_MIN,
        "margin_min": DIGIT_MARGIN_MIN,
        "candidates": [{"seconds": value, "score": round(score, 6)}
                       for score, value in ranked],
        "margin": round(margin, 6),
    })
    if best < DIGIT_MATCH_MIN or margin < DIGIT_MARGIN_MIN:
        return {**evidence, "seconds": None, "reason": "unreviewed_or_ambiguous_timer_glyph"}
    absolute_box = [DIGIT_SLOT_ROI[0] + x0, DIGIT_SLOT_ROI[1] + y0,
                    DIGIT_SLOT_ROI[0] + x1, DIGIT_SLOT_ROI[1] + y1]
    return {**evidence, "seconds": seconds, "best": {
        "seconds": seconds, "score": round(best, 6), "box": absolute_box},
        "reason": "verified_central_timer_and_complete_reviewed_glyph"}


def detect_countdown(image: Any) -> CountdownReading:
    evidence = {"source": "central_timer_native", "rois": [list(r) for r in COUNTDOWN_ROIS],
                "source_verified": False, "reviewed_seconds": list(REVIEWED_SECONDS)}
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.shape != (FRAME_HEIGHT, FRAME_WIDTH, 3)):
        evidence["reason"] = "invalid_native_bgr_frame"
        return CountdownReading(STATUS_UNREADABLE, None, evidence)
    if not bool(np.any(_crop(image, PANEL_ROI))):
        evidence["reason"] = "no_timer_panel_pixels"
        return CountdownReading(STATUS_ABSENT, None, evidence)
    try:
        panel, templates = _load_templates()
        source = _prove_timer_source(image, panel)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, cv2.error) as exc:
        evidence["reason"] = "countdown_template_error:" + type(exc).__name__
        return CountdownReading(STATUS_UNREADABLE, None, evidence)
    evidence["timer_source"] = source
    if not source["verified"]:
        evidence["reason"] = "central_timer_source_unproven_or_occluded"
        return CountdownReading(STATUS_UNREADABLE, None, evidence)
    evidence["source_verified"] = True
    slot = _read_complete_slot(image, templates)
    evidence["digit_slot"] = slot
    evidence["reason"] = slot["reason"]
    seconds = slot.get("seconds")
    if seconds is None:
        return CountdownReading(STATUS_UNREADABLE, None, evidence)
    evidence["best"] = slot["best"]
    return CountdownReading(STATUS_LOW if seconds == 1 else STATUS_PRESENT,
                            seconds, evidence)
