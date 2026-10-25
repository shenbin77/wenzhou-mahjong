"""Fail-closed visual verification of the local player's nickname.

The emulator serial is only a transport address.  It must never be treated as
proof of which game account is logged in.  This module reads the fixed
bottom-left *local player* name from a native 1600x900 table frame and compares
it with reviewed account templates.  Opponent names and room-owner labels are
outside the ROI by design.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import cv2
import numpy as np


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "player_identity"
MANIFEST_PATH = TEMPLATE_DIR / "manifest.json"
MIN_SCORE = 0.82
MIN_MARGIN = 0.20


@dataclass(frozen=True)
class PlayerIdentity:
    status: str
    expected: str
    detected: str | None
    score: float
    margin: float
    detail: str

    @property
    def matched(self) -> bool:
        return self.status == "match"


def _read_manifest(path: Path) -> tuple[tuple[int, int, int, int], dict[str, Path]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        x, y, width, height = (int(value) for value in data["roi"])
        templates = {
            str(item["account"]).strip(): Path(path).parent / str(item["file"])
            for item in data["templates"]
            if str(item.get("account") or "").strip()
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"玩家昵称模板清单无效：{exc}") from exc
    if min(x, y, width, height) < 0 or width == 0 or height == 0:
        raise RuntimeError("玩家昵称模板 ROI 无效")
    return (x, y, width, height), templates


def _nickname_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 44)
        & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 115)
    ).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    cleaned = np.zeros_like(mask)
    for index in range(1, count):
        _x, _y, _width, height, area = stats[index]
        if area >= 5 and height >= 3:
            cleaned[labels == index] = 1
    return cleaned


def _dice_with_shift(observed: np.ndarray, template: np.ndarray) -> float:
    if observed.shape != template.shape:
        return 0.0
    best = 0.0
    height, width = observed.shape
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            matrix = np.float32([[1, 0, dx], [0, 1, dy]])
            shifted = cv2.warpAffine(
                template, matrix, (width, height),
                flags=cv2.INTER_NEAREST, borderValue=0,
            )
            denominator = int(observed.sum() + shifted.sum())
            score = (2.0 * int((observed & shifted).sum()) / denominator
                     if denominator else 0.0)
            best = max(best, score)
    return best


def detect_player_identity(
    image: np.ndarray | None,
    expected_account: str,
    *,
    manifest_path: Path = MANIFEST_PATH,
) -> PlayerIdentity:
    """Compare the visible local nickname with ``expected_account``.

    ``unknown`` is deliberately different from ``mismatch``: an overlay or a
    lobby without the local-seat name must wait, while a confidently recognised
    different name is a hard safety failure.
    """
    expected = str(expected_account or "").strip()
    if not expected:
        return PlayerIdentity("unsupported", expected, None, 0.0, 0.0,
                              "未设置预期玩家昵称")
    try:
        (x, y, width, height), paths = _read_manifest(manifest_path)
    except RuntimeError as exc:
        return PlayerIdentity("unsupported", expected, None, 0.0, 0.0, str(exc))
    if expected not in paths:
        return PlayerIdentity(
            "unsupported", expected, None, 0.0, 0.0,
            f"玩家“{expected}”尚无已复核昵称模板，不能自动核验",
        )
    if image is None or image.ndim != 3 or image.shape[:2] != (900, 1600):
        shape = "无法解码" if image is None else f"{image.shape[1]}×{image.shape[0]}"
        return PlayerIdentity("unknown", expected, None, 0.0, 0.0,
                              f"当前画面{shape}，要求1600×900")

    observed = _nickname_mask(image[y:y + height, x:x + width])
    if int(observed.sum()) < 80:
        return PlayerIdentity(
            "unknown", expected, None, 0.0, 0.0,
            "左下本人昵称未显示或被遮挡",
        )

    scores: dict[str, float] = {}
    for account, path in paths.items():
        raw = np.fromfile(path, dtype=np.uint8) if path.is_file() else np.array([])
        template_image = (cv2.imdecode(raw, cv2.IMREAD_COLOR)
                          if raw.size else None)
        if template_image is None or template_image.shape[:2] != (height, width):
            continue
        scores[account] = _dice_with_shift(
            observed, _nickname_mask(template_image))
    if expected not in scores or not scores:
        return PlayerIdentity("unsupported", expected, None, 0.0, 0.0,
                              "玩家昵称模板缺失或尺寸无效")

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    detected, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - runner_up
    if score < MIN_SCORE or margin < MIN_MARGIN:
        return PlayerIdentity(
            "unknown", expected, None, score, margin,
            f"本人昵称证据不足（最高{score:.2f}，差值{margin:.2f}）",
        )
    if detected != expected:
        return PlayerIdentity(
            "mismatch", expected, detected, score, margin,
            f"配置玩家是“{expected}”，画面本人是“{detected}”",
        )
    return PlayerIdentity(
        "match", expected, detected, score, margin,
        f"画面本人昵称已核验为“{detected}”（{score:.2f}）",
    )
