"""Synthetic-frame tests for bot/claim_vision.py.

Every frame here is drawn with numpy/cv2 so the suite stays independent of any
captured PNG: no archived screenshot is needed to reproduce these results. Real
frames were used to calibrate the constants in claim_vision.py, not as fixtures.

The tests also pin the measured constants. Changing a threshold without changing
its documented margin is meant to fail here first.
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import claim_vision as cv_mod  # noqa: E402
from claim_vision import ClaimState, decision_priority, read_claims  # noqa: E402


FRAME_W, FRAME_H = cv_mod.FRAME_WIDTH, cv_mod.FRAME_HEIGHT

# The three colours the detector looks for, expressed in HSV then converted so
# the drawn pixels land inside the documented thresholds rather than near them.
GOLD_BGR = cv2.cvtColor(np.uint8([[[25, 200, 220]]]), cv2.COLOR_HSV2BGR)[0][0]
RED_BGR = cv2.cvtColor(np.uint8([[[4, 190, 130]]]), cv2.COLOR_HSV2BGR)[0][0]
BLUE_BGR = cv2.cvtColor(np.uint8([[[95, 200, 160]]]), cv2.COLOR_HSV2BGR)[0][0]
TABLE_BGR = (20, 50, 30)

GOLD_RADIUS = 66     # real gold / red discs measure about 126x132 px
BLUE_RADIUS = 50     # 过 and 取消 measure about 100x100 px
RING_WIDTH = 22      # the gold rim that survives around a 胡 disc's red core


def blank() -> np.ndarray:
    """A dark 1600x900 table with nothing on it."""
    frame = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    return frame


def draw_disc(frame: np.ndarray, center: tuple[int, int], radius: int,
              bgr: np.ndarray) -> None:
    cv2.circle(frame, center, radius, tuple(int(c) for c in bgr), thickness=-1)


def draw_gold(frame: np.ndarray, center: tuple[int, int]) -> None:
    draw_disc(frame, center, GOLD_RADIUS, GOLD_BGR)


def draw_hu(frame: np.ndarray, center: tuple[int, int]) -> None:
    """A gold-rimmed dark-red disc, matching how 胡 actually renders."""
    draw_disc(frame, center, GOLD_RADIUS, RED_BGR)
    cv2.circle(frame, center, GOLD_RADIUS, tuple(int(c) for c in GOLD_BGR),
               thickness=RING_WIDTH)


def draw_blue(frame: np.ndarray, center: tuple[int, int]) -> None:
    draw_disc(frame, center, BLUE_RADIUS, BLUE_BGR)


ROW_Y = 600                       # centre-y of the action row
LEFT, MID, RIGHT = 830, 1035, 1242    # the three claim slots that get used
PASS_X = 1431                     # the 过 slot


# ---------------------------------------------------------------------------
# Frame validity
# ---------------------------------------------------------------------------

def test_wrong_frame_size_is_unknown():
    state = read_claims(np.zeros((720, 1280, 3), np.uint8))
    assert state.buttons == ()
    assert state.decision is None
    assert state.known is False
    assert "1280x720" in state.reason


@pytest.mark.parametrize("frame", [
    None,
    "not an image",
    np.zeros((FRAME_H, FRAME_W), np.uint8),
    np.zeros((FRAME_H, FRAME_W, 3), np.float32),
])
def test_unusable_frames_are_unknown(frame):
    state = read_claims(frame)
    assert state.buttons == ()
    assert state.decision is None
    assert state.known is False


def test_empty_table_is_known_false_and_abstains():
    state = read_claims(blank())
    assert state.known is False
    assert state.decision is None
    assert decision_priority(state) is None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_gold_only_without_pass_button_abstains():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    state = read_claims(frame)

    assert state.known is True
    assert [b.kind for b in state.buttons] == ["gold"]
    # Nothing was read as 过, so there is nothing safe to tap.
    assert state.decision is None


def test_gold_plus_row_blue_reads_pass():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_blue(frame, (PASS_X, ROW_Y))
    state = read_claims(frame)

    kinds = {b.kind for b in state.buttons}
    assert kinds == {"gold", "pass"}
    assert state.decision == "pass"
    assert decision_priority(state) == "pass"


def test_hu_outranks_everything():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_hu(frame, (LEFT, ROW_Y))
    draw_blue(frame, (PASS_X, ROW_Y))
    state = read_claims(frame)

    kinds = {b.kind for b in state.buttons}
    assert kinds == {"hu", "gold", "pass"}
    assert state.decision == "hu"
    assert decision_priority(state) == "hu"


def test_hu_is_found_in_every_slot():
    for x in cv_mod.SLOT_CENTER_X:
        frame = blank()
        draw_hu(frame, (x, ROW_Y))
        state = read_claims(frame)
        assert [b.kind for b in state.buttons] == ["hu"], f"slot {x}"
        assert state.decision == "hu"


def test_lone_blue_in_the_row_is_cancel_and_abstains():
    frame = blank()
    draw_blue(frame, (PASS_X, ROW_Y))
    state = read_claims(frame)

    assert [b.kind for b in state.buttons] == ["cancel"]
    assert state.known is True
    # This is the decision that must never become a tap: 取消 aborts the choice
    # the player is already making.
    assert state.decision is None


def test_two_gold_discs_are_reported_separately_and_in_order():
    frame = blank()
    draw_gold(frame, (MID, ROW_Y))
    draw_gold(frame, (RIGHT, ROW_Y))
    state = read_claims(frame)

    assert [b.kind for b in state.buttons] == ["gold", "gold"]
    centers = [b.center[0] for b in state.buttons]
    assert centers == sorted(centers)
    assert centers[0] < centers[1]


# ---------------------------------------------------------------------------
# 取消 outside the action row (selection panel) -- position gate
# ---------------------------------------------------------------------------

PANEL_Y = 380


def test_blue_at_panel_height_alone_is_cancel():
    frame = blank()
    draw_blue(frame, (1300, PANEL_Y))
    state = read_claims(frame)

    assert [b.kind for b in state.buttons] == ["cancel"]
    assert state.decision is None


def test_blue_at_panel_height_with_gold_is_still_cancel():
    """The position gate must outrank the co-occurrence rule.

    A selection panel can show a gold confirm button while its own blue 取消
    sits higher up. Reading that blue disc as 过 would tap 取消 and abort the
    selection, so it has to stay classified as cancel even with gold on screen.
    """
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_blue(frame, (1300, PANEL_Y))
    state = read_claims(frame)

    by_kind = {b.kind for b in state.buttons}
    assert by_kind == {"gold", "cancel"}
    assert "pass" not in by_kind
    assert state.decision is None


def test_blue_in_the_row_with_gold_is_pass():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_blue(frame, (PASS_X, ROW_Y))
    assert {b.kind for b in read_claims(frame).buttons} == {"gold", "pass"}


# ---------------------------------------------------------------------------
# Measured boxes, not slot coordinates
# ---------------------------------------------------------------------------

def test_box_is_measured_not_snapped_to_a_slot():
    off_slot_x = 1150       # deliberately between two slots
    frame = blank()
    draw_gold(frame, (off_slot_x, ROW_Y))
    (button,) = read_claims(frame).buttons

    assert abs(button.center[0] - off_slot_x) <= 2
    assert abs(button.center[1] - ROW_Y) <= 2
    assert abs(button.width - 2 * GOLD_RADIUS) <= 3
    assert abs(button.height - 2 * GOLD_RADIUS) <= 3
    box_x0, _, box_x1, _ = button.box
    assert abs((box_x0 + box_x1) / 2 - off_slot_x) <= 2


def test_cancel_box_reflects_where_it_was_drawn():
    frame = blank()
    draw_blue(frame, (1300, PANEL_Y))
    (button,) = read_claims(frame).buttons

    assert abs(button.center[1] - PANEL_Y) <= 2
    assert abs(button.center[0] - 1300) <= 2


# ---------------------------------------------------------------------------
# Resistance to false positives
# ---------------------------------------------------------------------------

def test_large_red_region_outside_the_action_row_is_ignored():
    """A red table/cloth patch above the button row must not read as 胡."""
    frame = blank()
    cv2.rectangle(frame, (300, 180), (1400, 470), tuple(int(c) for c in RED_BGR), -1)
    state = read_claims(frame)

    assert state.buttons == ()
    assert state.known is False
    assert state.decision is None


def test_gold_patch_just_above_the_row_is_ignored():
    """The big gold action-feedback glyph sits higher than the real buttons."""
    frame = blank()
    cv2.rectangle(frame, (700, 300), (1100, 470), tuple(int(c) for c in GOLD_BGR), -1)
    state = read_claims(frame)

    assert state.buttons == ()
    assert state.decision is None


def test_red_disc_outside_the_slot_band_is_ignored():
    """The 胡牌选择弹窗 has its own red 胡 option left of slot 824."""
    frame = blank()
    draw_hu(frame, (727, ROW_Y))
    state = read_claims(frame)

    assert state.buttons == ()
    assert state.decision is None


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

SCENARIOS = {
    "empty": lambda f: None,
    "gold_only": lambda f: draw_gold(f, (RIGHT, ROW_Y)),
    "gold_pass": lambda f: (draw_gold(f, (RIGHT, ROW_Y)), draw_blue(f, (PASS_X, ROW_Y))),
    "hu_gold_pass": lambda f: (draw_gold(f, (RIGHT, ROW_Y)),
                               draw_hu(f, (LEFT, ROW_Y)),
                               draw_blue(f, (PASS_X, ROW_Y))),
    "cancel_row": lambda f: draw_blue(f, (PASS_X, ROW_Y)),
    "cancel_panel": lambda f: draw_blue(f, (1300, PANEL_Y)),
    "panel_cancel_with_gold": lambda f: (draw_gold(f, (RIGHT, ROW_Y)),
                                         draw_blue(f, (1300, PANEL_Y))),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_decision_priority_matches_decision(name):
    frame = blank()
    SCENARIOS[name](frame)
    state = read_claims(frame)

    assert isinstance(state, ClaimState)
    assert decision_priority(state) == state.decision
    assert decision_priority(state) in (None, "hu", "pass")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_evidence_is_json_serialisable(name):
    frame = blank()
    SCENARIOS[name](frame)
    payload = json.dumps(read_claims(frame).evidence, ensure_ascii=False)
    assert len(payload) > 0


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_read_claims_does_not_mutate_the_frame(name):
    frame = blank()
    SCENARIOS[name](frame)
    before = frame.copy()
    read_claims(frame)
    assert np.array_equal(frame, before)


def test_read_claims_is_repeatable():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_blue(frame, (PASS_X, ROW_Y))
    first = read_claims(frame)
    second = read_claims(frame)
    assert first.buttons == second.buttons
    assert first.decision == second.decision


def test_decision_priority_is_fail_closed_on_foreign_input():
    assert decision_priority(None) is None
    assert decision_priority("state") is None
    assert decision_priority(42) is None


def test_reason_is_populated_for_every_branch():
    frame = blank()
    draw_gold(frame, (RIGHT, ROW_Y))
    draw_blue(frame, (PASS_X, ROW_Y))
    assert read_claims(frame).reason
    assert read_claims(blank()).reason
    assert read_claims(np.zeros((720, 1280, 3), np.uint8)).reason


# ---------------------------------------------------------------------------
# Locked constants: touching a threshold must fail here and force a re-read of
# the margins documented next to it.
# ---------------------------------------------------------------------------

def test_documented_colour_thresholds():
    assert (cv_mod.GOLD_HUE_MIN, cv_mod.GOLD_HUE_MAX) == (12, 38)
    assert (cv_mod.GOLD_SAT_MIN, cv_mod.GOLD_VAL_MIN) == (90, 150)
    assert (cv_mod.RED_HUE_WRAP_MAX, cv_mod.RED_HUE_WRAP_MIN) == (8, 172)
    assert (cv_mod.RED_SAT_MIN, cv_mod.RED_VAL_MIN) == (110, 60)
    assert (cv_mod.BLUE_HUE_MIN, cv_mod.BLUE_HUE_MAX) == (85, 115)
    assert (cv_mod.BLUE_SAT_MIN, cv_mod.BLUE_VAL_MIN) == (90, 120)


def test_documented_geometry_gates():
    assert cv_mod.MIN_DISK_AREA == 4200
    assert (cv_mod.MIN_BBOX_EDGE, cv_mod.MAX_BBOX_EDGE) == (80, 200)
    assert (cv_mod.MIN_ASPECT, cv_mod.MAX_ASPECT) == (0.75, 1.33)
    assert cv_mod.ACTION_DISK_TOP_Y_MIN == 505
    assert (cv_mod.ACTION_DISK_CENTER_Y_MIN, cv_mod.ACTION_DISK_CENTER_Y_MAX) == (578, 645)
    assert (cv_mod.ACTION_DISK_CENTER_X_MIN, cv_mod.ACTION_DISK_CENTER_X_MAX) == (754, 1501)
    assert (cv_mod.ACTION_BAND_Y0, cv_mod.ACTION_BAND_Y1) == (510, 700)


def test_documented_hu_split_and_search_windows():
    assert cv_mod.HU_CORE_RED_MIN == 0.10
    assert (cv_mod.CORE_SAMPLE_LO, cv_mod.CORE_SAMPLE_HI) == (0.28, 0.72)
    # Blue must be searched higher than the action row because the selection
    # panel's 取消 lives above it.
    assert cv_mod.BLUE_ROI_Y0 < cv_mod.ACTION_ROI_Y0
    assert cv_mod.BLUE_ROI_Y1 >= cv_mod.ACTION_ROI_Y1
    (cv_mod.SLOT_CENTER_X)  # documented, not asserted: it is annotation only


def test_colours_used_by_the_tests_sit_inside_the_documented_thresholds():
    """Guards the fixture: a mis-drawn colour would silently weaken every test."""

    def hsv_of(bgr: np.ndarray) -> tuple[int, int, int]:
        pixel = cv2.cvtColor(np.uint8([[list(bgr)]]), cv2.COLOR_BGR2HSV)[0][0]
        return int(pixel[0]), int(pixel[1]), int(pixel[2])

    gh, gs, gv = hsv_of(GOLD_BGR)
    assert cv_mod.GOLD_HUE_MIN <= gh <= cv_mod.GOLD_HUE_MAX
    assert gs >= cv_mod.GOLD_SAT_MIN and gv >= cv_mod.GOLD_VAL_MIN

    rh, rs, rv = hsv_of(RED_BGR)
    assert rh <= cv_mod.RED_HUE_WRAP_MAX or rh >= cv_mod.RED_HUE_WRAP_MIN
    assert rs >= cv_mod.RED_SAT_MIN and rv >= cv_mod.RED_VAL_MIN

    bh, bs, bv = hsv_of(BLUE_BGR)
    assert cv_mod.BLUE_HUE_MIN <= bh <= cv_mod.BLUE_HUE_MAX
    assert bs >= cv_mod.BLUE_SAT_MIN and bv >= cv_mod.BLUE_VAL_MIN
