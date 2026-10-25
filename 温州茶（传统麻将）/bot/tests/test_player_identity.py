"""Player/account identity gate uses only the bottom-left local-seat name."""
from __future__ import annotations

from pathlib import Path
import sys

import cv2
import numpy as np

BOT = Path(__file__).resolve().parent.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from player_identity import MANIFEST_PATH, detect_player_identity


def _template_frame(filename: str) -> np.ndarray:
    raw = np.fromfile(MANIFEST_PATH.parent / filename, dtype=np.uint8)
    crop = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    assert crop is not None and crop.shape[:2] == (42, 78)
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    frame[638:680, 32:110] = crop
    return frame


def test_known_local_names_match_reviewed_templates():
    yujie = detect_player_identity(_template_frame("yujie.png"), "玉姐")
    shize = detect_player_identity(_template_frame("shize.png"), "拾则")
    assert yujie.matched and yujie.detected == "玉姐" and yujie.score >= .99
    assert shize.matched and shize.detected == "拾则" and shize.score >= .99


def test_other_known_player_is_hard_mismatch():
    result = detect_player_identity(_template_frame("shize.png"), "玉姐")
    assert result.status == "mismatch"
    assert result.expected == "玉姐" and result.detected == "拾则"
    assert "配置玩家" in result.detail


def test_hidden_name_and_unknown_account_fail_closed():
    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    hidden = detect_player_identity(blank, "玉姐")
    unsupported = detect_player_identity(_template_frame("yujie.png"), "新账号")
    assert hidden.status == "unknown" and "未显示" in hidden.detail
    assert unsupported.status == "unsupported" and "无已复核昵称模板" in unsupported.detail


def test_wrong_resolution_is_never_verified():
    result = detect_player_identity(np.zeros((720, 1280, 3), dtype=np.uint8), "玉姐")
    assert result.status == "unknown" and not result.matched
