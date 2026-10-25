# -*- coding: utf-8 -*-
"""入桌邀请必须走矩形取消专用通道，不能与行动圆盘混用。"""

from pathlib import Path
import sys

import cv2
import numpy as np


BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from invite_dialog import read_invitation  # noqa: E402


def _template(name: str) -> np.ndarray:
    path = BOT_DIR / "templates" / name
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def invitation_frame(*, phrase_at=(765, 360), cancel_at=(508, 540),
                     include_phrase=True, include_cancel=True) -> np.ndarray:
    frame = np.full((900, 1600, 3), (80, 110, 135), np.uint8)
    if include_phrase:
        phrase = _template("invite_phrase.png")
        x, y = phrase_at
        frame[y:y + phrase.shape[0], x:x + phrase.shape[1]] = phrase
    if include_cancel:
        cancel = _template("invite_cancel.png")
        x, y = cancel_at
        frame[y:y + cancel.shape[0], x:x + cancel.shape[1]] = cancel
    return frame


def test_invitation_requires_phrase_and_rectangular_cancel_in_safe_geometry():
    state = read_invitation(invitation_frame())
    assert state.present is True and state.complete is True
    assert state.cancel_center == (630, 590)
    assert state.cancel_box == (508, 540, 753, 640)
    assert state.phrase_score >= .92 and state.cancel_score >= .94


def test_phrase_without_cancel_blocks_but_publishes_no_target():
    state = read_invitation(invitation_frame(include_cancel=False))
    assert state.present is True and state.complete is False
    assert state.cancel_center is None
    assert "证据不足" in state.reason


def test_cancel_template_at_wrong_relative_position_is_not_executable():
    state = read_invitation(invitation_frame(cancel_at=(1050, 540)))
    assert state.present is True and state.complete is False
    assert state.cancel_center is None
    assert "相对几何" in state.reason


def test_blank_table_is_not_an_invitation():
    state = read_invitation(np.zeros((900, 1600, 3), np.uint8))
    assert state.present is False and state.complete is False
