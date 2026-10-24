"""Read the fixed red-财 tile on native 1600x900 replay and live frames.

The templates are literal crops from retained local material: replay
recordings (lossy H.264) and lossless ADB screenshots of real sessions.
A label may own several templates from different sources; each label is
scored by its best template so that new sources never invalidate the
older ones. Unknown, occluded, or differently sized frames return no
label.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

_DIR = Path(__file__).resolve().parent / "templates" / "caishen"
_MIN_SCORE = 0.98
_MIN_MARGIN = 0.02

# manifest.json 的 (mtime_ns, st_size) -> 已加载模板。每次调用先 stat 一次
# （微秒级），文件一变就重新加载：往 manifest 补模板无需重启辅助器。
# 加载逻辑本身（哈希校验、尺寸校验）与阈值、ROI 保持原样。
_TEMPLATE_CACHE: dict[tuple[int, int], tuple[tuple[int, int, int, int],
                                            dict[str, tuple[np.ndarray, ...]]]] = {}


@dataclass(frozen=True)
class CaishenRead:
    label: str | None
    score: float
    runner_up: float
    reason: str


def _templates() -> tuple[tuple[int, int, int, int], dict[str, tuple[np.ndarray, ...]]]:
    """Load the manifest and return the ROI plus every template per label.

    A label may legitimately appear more than once: replay-frame crops and
    ADB screenshot crops of the same tile live side by side, so the mapping
    is label -> tuple of templates rather than label -> single template.
    The result is cached on the manifest's mtime and size, so newly added
    templates take effect on the next frame without a restart.
    """
    manifest_path = _DIR / "manifest.json"
    try:
        stat = manifest_path.stat()
    except OSError:
        stat = None
    if stat is not None:
        cached = _TEMPLATE_CACHE.get((stat.st_mtime_ns, stat.st_size))
        if cached is not None:
            return cached
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    roi = tuple(manifest["roi"])
    x0, y0, x1, y1 = roi
    loaded: dict[str, list[np.ndarray]] = {}
    for item in manifest["templates"]:
        data = (_DIR / item["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["asset_sha256"]:
            raise ValueError(f"财神模板哈希不符: {item['file']}")
        raw = np.frombuffer(data, dtype=np.uint8)
        tile = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
        if tile is None or tile.shape != (y1 - y0, x1 - x0):
            raise ValueError(f"财神模板尺寸错误: {item['file']}")
        loaded.setdefault(item["label"], []).append(tile)
    value = (roi, {label: tuple(tiles) for label, tiles in loaded.items()})
    _TEMPLATE_CACHE.clear()
    if stat is not None:
        _TEMPLATE_CACHE[(stat.st_mtime_ns, stat.st_size)] = value
    return value


def detect_caishen(image: np.ndarray) -> CaishenRead:
    """Return a label only when the observed sprite strongly matches one tile."""
    if image is None or image.shape != (900, 1600, 3):
        return CaishenRead(None, 0.0, 0.0, "画面尺寸不是已验证的 1600×900")
    (x0, y0, x1, y1), templates = _templates()
    crop = image[y0:y1, x0:x1]
    badge = crop[:22, :30]
    red = (badge[:, :, 2] > 175) & (badge[:, :, 1] < 160) & (badge[:, :, 0] < 110)
    if int(red.sum()) < 70:
        return CaishenRead(None, 0.0, 0.0, "未看清财字角标")
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # Best template wins per label, so extra sources only ever help a label.
    per_label = {
        label: max(float(cv2.matchTemplate(gray, tile, cv2.TM_CCOEFF_NORMED)[0, 0])
                   for tile in tiles)
        for label, tiles in templates.items()
    }
    ranked = sorted(per_label.items(), key=lambda item: (-item[1], item[0]))
    best_label, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < _MIN_SCORE or best_score - second_score < _MIN_MARGIN:
        return CaishenRead(None, best_score, second_score, "财神小牌匹配不够确定")
    return CaishenRead(best_label, best_score, second_score, "")
