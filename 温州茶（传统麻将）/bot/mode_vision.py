"""Confirm the target tea or red-Zhong rule text before offering advice.

Templates are literal native-resolution crops of the rule text:
``tea_2p.png`` from 2026-10-16/740195/r08/frame_01.png x=814:874,y=190:228,
``tea_4p.png`` from 2026-10-13/323028/r01/frame_01.png x=920:990,y=190:228.
Unknown, obscured, or differently sized screens remain unknown.
Shu templates come from 熟识牌对照_20261018/frame_233s_14.png and require
the observed 放炮麻将 title and trailing 4-player rule on the same line.
"""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "mode"


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


def detect_tea_player_count(image: np.ndarray) -> int | None:
    """Return 2, 4, or None from the visible rule text; never infer from tile count."""
    if image is None or image.shape[:2] != (900, 1600):
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    rule_strip = gray[140:245, 280:1320]
    scores = {}
    for players, template in _templates().items():
        scores[players] = float(cv2.minMaxLoc(
            cv2.matchTemplate(rule_strip, template, cv2.TM_CCOEFF_NORMED)
        )[1])
    best = max(scores, key=scores.get)
    other = 4 if best == 2 else 2
    if scores[best] >= .92 and scores[best] - scores[other] >= .08:
        return best
    return None


@lru_cache(maxsize=1)
def _shu_templates():
    # Native crops from 熟识牌对照_20261018/frame_233s_14.png (1600x948):
    # title x=405:545,y=205:245; trailing four-player rule x=1117:1200,y=205:245.
    return (_read_template(_TEMPLATE_DIR / "shu_title.png"),
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
    title, four = matches
    return (title[0] >= .94 and four[0] >= .94
            and 600 <= four[1] - title[1] <= 800
            and abs(four[2] - title[2]) <= 8)
