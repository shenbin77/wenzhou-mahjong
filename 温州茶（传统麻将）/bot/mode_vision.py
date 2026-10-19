"""Identify the player count printed on Zhejiang tea replay frames.

Templates are literal native-resolution crops of the rule text:
``tea_2p.png`` from 2026-10-16/740195/r08/frame_01.png x=814:874,y=190:228,
``tea_4p.png`` from 2026-10-13/323028/r01/frame_01.png x=920:990,y=190:228.
Unknown, obscured, or differently sized screens remain unknown.
"""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "mode"


@lru_cache(maxsize=1)
def _templates():
    result = {}
    for players in (2, 4):
        path = _TEMPLATE_DIR / f"tea_{players}p.png"
        if not path.is_file():
            raise FileNotFoundError(path)
        image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or image.size == 0:
            raise ValueError(f"Invalid player-count template: {path}")
        result[players] = image
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
