"""2026-10-25 caishen/count gate fixes, checked on real assist decision frames.

* 财神模板标签纠错：6 个模板（s3_live2、t2_live3、t2_live4、t4_live5、t6.png、
  t7.png）标签错误。证据是同一帧手里带"财"角标的那张牌（游戏自己声明的财神），
  它的字形由独立的手牌模板读出，与这里断言的标签一致。
* 吃碰来源箭头：副露牌顶边的半透明黑圆 + 黄三角让牌面识别掉分、整帧
  "识牌不完整"，是 wrong_count 弃权的主因。去遮挡后必须读对，且没有箭头的牌
  像素与结论逐字节不变。
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from caishen_vision import detect_caishen  # noqa: E402
from tea_bot import _TEMPLATE_DIR  # noqa: E402
from tile_vision import TileVision, strip_claim_marker  # noqa: E402

CAPTURES = BOT_DIR.parent / "采集"
SESSIONS = CAPTURES / "assist_sessions"


def _frame(relative: str) -> np.ndarray:
    path = CAPTURES / relative
    if not path.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def _live(session: str, frame: str) -> str:
    return f"assist_sessions/{session}/live/tea_2p/{frame}"


# (frame, caishen) — each frame also holds a 财-marked tile in the hand.
BADGE_WITNESSED = [
    (_live("20261023_220643", "20261023_220648_732923_0b71f3.png"), "s9"),
    (_live("20261023_223759", "20261023_223806_641494_820959.png"), "t4"),
    (_live("20261023_223759", "20261023_223841_216899_40bdc9.png"), "t6"),
    ("2026-10-15/291755/r05/frame_01.png", "t6"),
    ("2026-10-13/207547/r03/frame_01.png", "t7"),
]


@pytest.mark.parametrize(("relative", "expected"), BADGE_WITNESSED)
def test_relabelled_caishen_templates_read_the_badge_witnessed_tile(relative, expected):
    observed = detect_caishen(_frame(relative))
    assert observed.label == expected
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


@pytest.mark.parametrize(("relative", "expected"), BADGE_WITNESSED)
def test_hand_badge_glyph_independently_agrees_with_table_caishen(relative, expected):
    """The hand-tile classifier (separate template set) reads the 财 tile the same way."""
    image = _frame(relative)
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label=expected)
    vision.recognize_hand(image)
    marked = [d for d in vision.diagnostics + vision.meld_diagnostics
              if d.get("reason") == "caishen_marked"]
    assert marked, "帧里应有一张带财角标的手牌"
    assert {d.get("glyph_label") for d in marked} == {expected}


def test_badge_glyph_conflict_vetoes_a_mislabelled_caishen():
    from assist_tea import _badge_glyph_conflict
    image = _frame(BADGE_WITNESSED[0][0])
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="s3")
    vision.recognize_hand(image)
    reason = _badge_glyph_conflict(vision, "s3")
    assert reason and "s9" in reason
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="s9")
    vision.recognize_hand(image)
    assert _badge_glyph_conflict(vision, "s9") is None


def test_badge_glyph_conflict_ignores_low_confidence_glyphs():
    """万字财神的数字被角标盖住，字形分低于门槛时绝不否决。"""
    from assist_tea import _badge_glyph_conflict

    class _V:
        diagnostics = [{"reason": "caishen_marked", "glyph_label": "w4",
                        "glyph_score": .79, "margin": .2}]
        meld_diagnostics = []
    assert _badge_glyph_conflict(_V(), "w7") is None


# --- claim marker ----------------------------------------------------------
# Development frames (where the defect was diagnosed).
MARKER_DEV = [
    (_live("20261024_140818", "20261024_140825_214823_e3809a.png"), (6, 761, 89, 875), "w3"),
    (_live("20261024_140818", "20261024_140825_214823_e3809a.png"), (551, 761, 635, 875), "w2"),
    (_live("20261024_140818", "20261024_140825_214823_e3809a.png"), (824, 761, 907, 875), "t2"),
    (_live("20261023_224827", "20261023_224846_880695_d0b4ad.png"), (452, 761, 534, 875), "w4"),
    (_live("20261024_133129", "20261024_133446_684009_7838db.png"), (179, 761, 262, 875), "w3"),
]
# Held-out: rooms/sessions never looked at while the fix was written; picked
# from a 1706-frame sweep (22342 crops, 1487 with a marker, 420 newly accepted,
# all visually reviewed; zero changes on crops without a marker).
MARKER_HELDOUT = [
    ("dev_auto_play/tap_probe2_114030/wait_56.png", 179, "w8"),
    ("dev_auto_play/tap_probe2_114030/wait_109.png", 93, "s2"),
    ("dev_auto_play/tap_diag_0814/07_shot.png", 6, "t2"),
    (_live("20261024_211352", "20261024_230300_124056_870135.png"), 278, "w5"),
]


@pytest.mark.parametrize(("relative", "box", "expected"), MARKER_DEV)
def test_claim_marker_tile_reads_correctly_after_stripping(relative, box, expected):
    image = _frame(relative)
    x0, y0, x1, y1 = box
    crop = image[y0:y1, x0:x1]
    cleaned, found = strip_claim_marker(crop)
    assert found
    result = TileVision(template_dir=_TEMPLATE_DIR).classify_crop(crop)
    assert result["claim_marker"] is True
    assert result["accepted"] and result["label"] == expected


@pytest.mark.parametrize(("relative", "left", "expected"), MARKER_HELDOUT)
def test_held_out_meld_tile_with_marker_is_recognised(relative, left, expected):
    image = _frame(relative)
    vision = TileVision(template_dir=_TEMPLATE_DIR)
    vision.recognize_hand(image)
    hits = [d for d in vision.meld_diagnostics if d["box"][0] == left]
    assert len(hits) == 1
    assert hits[0].get("claim_marker") is True
    assert hits[0]["accepted"] and hits[0]["label"] == expected


def test_tile_without_marker_is_returned_untouched():
    image = _frame(MARKER_DEV[0][0])
    crop = image[768:892, 1390:1478]  # concealed 1筒, no marker above it
    cleaned, found = strip_claim_marker(crop)
    assert found is False
    assert cleaned is crop
    assert "claim_marker" not in TileVision(template_dir=_TEMPLATE_DIR).classify_crop(crop)


def test_caishen_badge_tile_is_not_mistaken_for_a_marker():
    image = _frame(BADGE_WITNESSED[0][0])
    vision = TileVision(template_dir=_TEMPLATE_DIR, caishen_label="s9")
    vision.recognize_hand(image)
    for d in vision.diagnostics:
        if d.get("reason") == "caishen_marked":
            x0, y0, x1, y1 = d["box"]
            assert strip_claim_marker(image[y0:y1, x0:x1])[1] is False


def test_five_meld_frame_no_longer_abstains_on_tile_count():
    """140825：5 组副露 + 2 张暗手，旧逻辑 meld_count=0 → wrong_count。"""
    from assist_tea import analyze_snapshot
    relative = MARKER_DEV[0][0]
    _frame(relative)
    result = analyze_snapshot(CAPTURES / relative, caishen_label="",
                              confirmed_two_player=True)
    assert result.get("meld_count") == 5
    assert "识牌不完整" not in str(result.get("reason"))
