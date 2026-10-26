"""16/16 整场总结积分识别的实机证据测试。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from match_score import (
    FULL_MATCH_ROUND_ROI,
    SCORE_ROIS,
    read_full_match_round,
    read_match_score,
)


BOT_DIR = Path(__file__).resolve().parents[1]
ROOT = BOT_DIR.parent.parent
FULL_MATCH = ROOT / "_takeover" / "match_end_probe.png"
EARLY_DISSOLVE = (
    ROOT / "_takeover" / "timeout_dialog_20261026_2150"
    / "emulator-5554_after.png"
)


def _read(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8),
                         cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


def test_full_16_round_summary_reads_signed_zero_sum_scores():
    state = read_match_score(_read(FULL_MATCH))

    assert state.ok is True, state.ledger()
    assert state.left_player == "玉姐"
    assert state.right_player == "拾则"
    assert state.left_score == -59
    assert state.right_score == 59
    assert state.score_for("玉姐") == -59
    assert state.score_for("拾则") == 59
    assert state.score_for("未知玩家") is None
    assert state.confidence >= 0.87
    assert state.evidence["absolute_values_equal"] is True
    assert state.evidence["sum_zero"] is True
    assert state.evidence["left"]["tone"] == "blue"
    assert state.evidence["right"]["tone"] == "orange"
    assert [row["digit"] for row in state.evidence["left"]["digits"]] == [5, 9]


def test_full_summary_same_frame_round_gate_reads_exact_16_of_16():
    state = read_full_match_round(_read(FULL_MATCH))

    assert state.ok is True, state.ledger()
    assert state.round_no == 16
    assert state.round_total == 16
    assert [row["digit"] for row in state.evidence["digits"]] == [1, 6, 6]


def test_three_button_summary_with_4_of_16_hud_is_rejected():
    """对抗回归：三按钮不能替代同帧 16/16 局数证据。"""
    image = _read(FULL_MATCH)
    early = _read(EARLY_DISSOLVE)
    x0, y0, x1, y1 = FULL_MATCH_ROUND_ROI
    image[y0:y1, x0:x1] = early[y0:y1, x0:x1]

    state = read_match_score(image)

    assert state.ok is False
    assert state.left_score is None
    assert state.evidence["match_end"]["present"] is True
    assert state.evidence["full_match_round"]["ok"] is False
    assert "同帧顶部 16/16" in state.reason


def test_4_of_16_early_dissolve_is_never_a_full_match_score():
    """该帧虽有清晰 -23/+23，也只有提前解散的两按钮，必须拒绝。"""
    state = read_match_score(_read(EARLY_DISSOLVE))

    assert state.ok is False
    assert state.left_score is None
    assert state.right_score is None
    assert "固定三按钮版式未确认" in state.reason
    assert state.evidence["match_end"]["present"] is False


def test_missing_score_glyph_is_rejected_by_fixed_roi_gate():
    image = _read(FULL_MATCH)
    x0, y0, x1, y1 = SCORE_ROIS["right"]
    # 只把高饱和积分字形改成低饱和奶油色，不碰底部三按钮版式。
    crop = image[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    score_pixels = (
        ((hsv[:, :, 0] >= 95) & (hsv[:, :, 0] <= 110)
         & (hsv[:, :, 1] >= 140))
        | ((hsv[:, :, 0] >= 4) & (hsv[:, :, 0] <= 18)
           & (hsv[:, :, 1] >= 140))
    )
    crop[score_pixels] = (170, 215, 242)

    state = read_match_score(image)

    assert state.ok is False
    assert "right 积分区字形数" in state.reason


def test_non_zero_sum_scores_are_rejected(monkeypatch):
    """即使两侧 ROI 都给出高置信数字，不守恒也不能落账。"""
    import match_score

    original = match_score._read_side_score

    def mismatched(image, side):
        score, confidence, evidence, reason = original(image, side)
        if side == "right" and score is not None:
            evidence = dict(evidence)
            evidence["score"] = 55
            evidence["magnitude"] = 55
            return 55, confidence, evidence, reason
        return score, confidence, evidence, reason

    monkeypatch.setattr(match_score, "_read_side_score", mismatched)
    state = match_score.read_match_score(_read(FULL_MATCH))

    assert state.ok is False
    assert state.left_score == -59
    assert state.right_score == 55
    assert state.evidence["absolute_values_equal"] is False
    assert state.evidence["sum_zero"] is False
    assert "不守恒" in state.reason


def test_wrong_frame_size_is_rejected():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)

    state = read_match_score(image)

    assert state.ok is False
    assert "帧尺寸未验证" in state.reason


def test_unknown_player_name_blocks_score_persistence_and_leave_gate():
    image = _read(FULL_MATCH)
    # 抹掉左侧昵称但保留积分与底部三按钮；不得只凭座次猜当前玩家。
    image[205:250, 600:730] = (165, 195, 235)

    state = read_match_score(image)

    assert state.ok is False
    assert state.left_player is None
    assert "left 昵称" in state.reason
