# -*- coding: utf-8 -*-
"""Read-only room/round HUD reader for tea follow path.

Reuses the same avatar-occlusion gate as ``assist_window.tea_round_fingerprint``.
Digit OCR is template-matched against ``templates/room_hud_digits`` harvested
from a verified HUD crop. Missing / low-confidence digits → None (fail-closed).
Never invents 房号 or 局数.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

__all__ = [
    "RoomRoundReading",
    "HUD_CROP",
    "ROOM_DIGIT_ROI",
    "ROUND_DIGIT_ROI",
    "DIGIT_SCORE_MIN",
    "read_room_round",
]

# Full-frame crop matching assist_window.tea_round_fingerprint HUD box.
HUD_CROP = (15, 55, 225, 140)  # x0,y0,x1,y1
# Relative to HUD crop: digit bands to the right of Chinese labels.
ROOM_DIGIT_ROI = (95, 10, 195, 40)
ROUND_DIGIT_ROI = (95, 45, 175, 78)
DIGIT_SCORE_MIN = 0.72
_AVATAR_CROP = (35, 95, 115, 140)
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "room_hud_digits"
_TEMPLATE_CACHE: dict[str, list[np.ndarray]] | None = None


@dataclass(frozen=True)
class RoomRoundReading:
    room_id: str | None = None
    round_no: int | None = None
    round_total: int | None = None
    hud_visible: bool = False
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _avatar_occludes(image: np.ndarray) -> bool:
    x0, y0, x1, y1 = _AVATAR_CROP
    if image.shape[0] < y1 or image.shape[1] < x1:
        return True
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return True
    b, g, r = cv2.split(crop)
    red = (r > 90) & (r > g * 1.4) & (r > b * 1.4)
    return int(red.sum()) > 100


def _load_templates() -> dict[str, list[np.ndarray]]:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE
    templates: dict[str, list[np.ndarray]] = {}
    if _TEMPLATE_DIR.is_dir():
        for path in sorted(_TEMPLATE_DIR.glob("d*.png")):
            # d7_0.png → digit "7"
            name = path.stem
            if len(name) < 2 or not name[1].isdigit():
                continue
            digit = name[1]
            data = np.fromfile(str(path), dtype=np.uint8)
            tmpl = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if tmpl is None or tmpl.size == 0:
                continue
            templates.setdefault(digit, []).append(tmpl)
    _TEMPLATE_CACHE = templates
    return templates


def _normalize(crop: np.ndarray, height: int = 24) -> np.ndarray:
    h, w = crop.shape[:2]
    scale = height / max(h, 1)
    nw = max(1, int(round(w * scale)))
    return cv2.resize(crop, (nw, height), interpolation=cv2.INTER_AREA)


def _match_digit(crop: np.ndarray, templates: dict[str, list[np.ndarray]]) -> tuple[str | None, float]:
    if crop.size == 0 or not templates:
        return None, 0.0
    probe = _normalize(crop)
    best_ch: str | None = None
    best_s = -1.0
    for ch, tmpls in templates.items():
        for tmpl in tmpls:
            W = max(probe.shape[1], tmpl.shape[1])
            a = np.zeros((24, W), np.uint8)
            b = np.zeros((24, W), np.uint8)
            a[:, : probe.shape[1]] = probe
            b[:, : tmpl.shape[1]] = tmpl
            score = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())
            if score > best_s:
                best_s = score
                best_ch = ch
    if best_s < DIGIT_SCORE_MIN:
        return None, best_s
    return best_ch, best_s


def _segment_digits(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Return left-to-right digit-like boxes inside a binary ROI."""
    num, _labels, stats, _cents = cv2.connectedComponentsWithStats(binary, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    h_roi = binary.shape[0]
    for i in range(1, num):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 18 or w < 3 or h < 8:
            continue
        if h > h_roi * 0.95 or w > 28:
            continue
        # Slash / punctuation between round digits: skip very thin tall marks later
        boxes.append((x, y, w, h))
    boxes.sort(key=lambda b: b[0])
    return boxes


def _read_digit_string(roi_gray: np.ndarray, templates: dict[str, list[np.ndarray]]) -> tuple[str | None, list[float]]:
    if roi_gray.size == 0:
        return None, []
    _, thr = cv2.threshold(roi_gray, 140, 255, cv2.THRESH_BINARY)
    boxes = _segment_digits(thr)
    if not boxes:
        return None, []
    chars: list[str] = []
    scores: list[float] = []
    for x, y, w, h in boxes:
        # Skip slash-like separators (narrow).
        if w <= 5 and h >= 12:
            continue
        crop = thr[y : y + h, x : x + w]
        ch, score = _match_digit(crop, templates)
        if ch is None:
            return None, scores + [score]
        chars.append(ch)
        scores.append(score)
    if not chars:
        return None, scores
    return "".join(chars), scores


def read_room_round(image: Any) -> RoomRoundReading:
    """Parse 房间号 / 局数 from a 1600x900 BGR frame. Fail-closed."""
    if image is None:
        return RoomRoundReading(reason="无画面")
    try:
        arr = np.asarray(image)
    except Exception:
        return RoomRoundReading(reason="画面不可读")
    if arr.ndim != 3 or arr.shape[0] != 900 or arr.shape[1] != 1600:
        return RoomRoundReading(reason="非 1600x900 牌桌帧")
    if _avatar_occludes(arr):
        return RoomRoundReading(hud_visible=False, reason="房号局数HUD被头像遮挡")
    x0, y0, x1, y1 = HUD_CROP
    hud = arr[y0:y1, x0:x1]
    gray = cv2.cvtColor(hud, cv2.COLOR_BGR2GRAY)
    templates = _load_templates()
    if not templates:
        return RoomRoundReading(hud_visible=True, reason="房号局数字形模板缺失")

    rx0, ry0, rx1, ry1 = ROOM_DIGIT_ROI
    ox0, oy0, ox1, oy1 = ROUND_DIGIT_ROI
    room_s, room_scores = _read_digit_string(gray[ry0:ry1, rx0:rx1], templates)
    round_s, round_scores = _read_digit_string(gray[oy0:oy1, ox0:ox1], templates)

    room_id = room_s if room_s and room_s.isdigit() and 4 <= len(room_s) <= 8 else None
    round_no = None
    round_total = None
    if round_s and round_s.isdigit():
        # "216" from 2/16 with slash skipped → 216; prefer split if length>=2
        if len(round_s) == 1:
            round_no = int(round_s)
        elif len(round_s) == 2:
            # ambiguous 21 vs 2/1 — fail closed
            round_no = None
        elif len(round_s) == 3:
            # typical 2/16 → 216
            round_no = int(round_s[0])
            round_total = int(round_s[1:])
        elif len(round_s) == 4:
            round_no = int(round_s[:2])
            round_total = int(round_s[2:])
        else:
            round_no = None

    if room_id is None and round_no is None:
        return RoomRoundReading(
            hud_visible=True,
            reason="房号局数数字未识别",
            evidence={"room_scores": room_scores, "round_scores": round_scores,
                      "room_raw": room_s, "round_raw": round_s},
        )
    return RoomRoundReading(
        room_id=room_id,
        round_no=round_no,
        round_total=round_total,
        hud_visible=True,
        reason="ok",
        evidence={"room_scores": room_scores, "round_scores": round_scores,
                  "room_raw": room_s, "round_raw": round_s},
    )
