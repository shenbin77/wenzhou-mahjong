"""Native river pointers must not use meld-source-arrow preprocessing.

Positive/negative pixels are unaltered recordings. The two explicitly shifted
marker cases are controlled geometry corruptions and cannot serve as gold labels.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import discard_vision as river
from tile_vision import TileVision, strip_claim_marker

FIXTURES = Path(__file__).parent / "fixtures" / "sparkle_marker_context"
BEFORE = "20261023_205431_651989_577fbe.png"
POSITIVE = "20261023_205433_718038_eae185.png"
AMBIGUOUS = "20261023_205435_935131_1cbfb6.png"
CLEAR = "20261023_205438_138403_2170d5.png"
MODAL = "20261023_205440_104194_589297.png"
BOX = (1026, 231, 1081, 306)


def frame(name):
    path = FIXTURES / name
    assert path.is_file(), f"required native fixture missing: {path}"
    raw = path.read_bytes()
    manifest = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == manifest[name]["sha256"]
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape == (900, 1600, 3)
    return image


def crop(image, box=BOX):
    x0, y0, x1, y1 = box
    return image[y0:y1, x0:x1]


@pytest.mark.parametrize("name", [POSITIVE, CLEAR])
def test_native_river_and_sparkle_paths_meet_original_incoming_gates(name):
    before, current = frame(BEFORE), frame(name)
    original = current.tobytes()
    state = river.read_discards(current, before)
    assert state.incoming_source == river.SOURCE_OPPONENT and state.incoming_tile == 9
    newest = state.table.opponent.newest
    assert newest.index == 3 and newest.box == BOX
    assert newest.label == "w9" and newest.accepted
    assert newest.score >= river.INCOMING_MIN_SCORE
    assert newest.margin >= river.INCOMING_MIN_MARGIN
    sparkle = river.read_opponent_sparkle_tile(current, include_candidates=True)
    assert sparkle["accepted"] is True and sparkle["tile_id"] == 9
    assert sparkle["label"] == "w9" and sparkle["box"] == list(BOX)
    assert sparkle["score"] >= river.INCOMING_MIN_SCORE
    assert sparkle["margin"] >= river.INCOMING_MIN_MARGIN
    assert sparkle["candidates_complete"] is True
    assert len(sparkle["candidates"]) == len(sparkle["candidate_labels"])
    assert sparkle["candidates"][0]["label"] == "w9"
    assert current.tobytes() == original


def test_real_unclear_next_frame_abstains_despite_same_old_baseline_and_prior_positive():
    baseline = river.snapshot_table(frame(BEFORE))
    assert baseline.opponent.count == 3
    good = river.read_discards(frame(POSITIVE), baseline)
    assert good.incoming_tile == 9
    image = frame(AMBIGUOUS)
    # Use the original three-slot baseline, so lack of a new slot cannot hide
    # an accidental identity acceptance or stateful inheritance from good.
    bad = river.read_discards(image, baseline)
    assert bad.table.opponent.count == 4
    assert bad.evidence["appended_opponent"] == 1
    assert bad.incoming_source == river.SOURCE_OPPONENT
    assert bad.incoming_tile is None
    assert bad.table.opponent.newest.accepted is False
    assert bad.table.opponent.newest.tile_id is None
    sparkle = river.read_opponent_sparkle_tile(image, include_candidates=True)
    assert sparkle["accepted"] is False and sparkle["tile_id"] is None
    assert sparkle["score"] < river.RIVER_MIN_SCORE
    assert sparkle["margin"] < river.RIVER_MIN_MARGIN


def test_pointer_does_not_supply_missing_temporal_provenance():
    image = frame(POSITIVE)
    single = river.read_discards(image)
    repeated = river.read_discards(image, image)
    assert single.table.opponent.newest.tile_id == 9
    assert single.incoming_tile is None
    assert repeated.incoming_tile is None
    assert "没有新弃牌" in repeated.reason


def test_real_modal_still_fails_table_and_sparkle_gates():
    image = frame(MODAL)
    state = river.read_discards(image, frame(BEFORE))
    assert state.incoming_tile is None
    assert state.table.opponent.valid is False
    assert river.read_opponent_sparkle_tile(image)["accepted"] is False


def test_neighbour_tiles_and_real_concealed_eights_keep_their_labels():
    image = frame(POSITIVE)
    state = river.snapshot_table(image)
    identities = {tile.index: tile.tile_id for tile in state.opponent.tiles}
    assert identities == {0: 63, 1: 61, 2: 29, 3: 9}
    hand = TileVision(template_dir=river.TEA_TEMPLATE_DIR)
    for box in [(468, 768, 556, 892), (560, 768, 648, 892)]:
        result = hand.classify_crop(crop(image, box))
        assert result["accepted"] is True and result["tile_id"] == 8
    wrong_requested = river.read_opponent_sparkle_tile(image, sparkle_x=1109)
    assert wrong_requested["accepted"] is False
    assert wrong_requested["reason"] == "requested_sparkle_not_present"


def test_misaligned_observed_pointer_cannot_promote_raw_nine():
    image = frame(POSITIVE).copy()
    # Controlled corruption: move only the observed pointer band by 12 px.
    image[196:231, 520:1260] = np.roll(image[196:231, 520:1260], 12, axis=1)
    assert river.load_vision().classify_crop(crop(image))["tile_id"] == 9
    reading = river.read_opponent_sparkle_tile(image)
    assert reading["accepted"] is False and reading["tile_id"] is None
    assert reading["reason"].startswith("sparkle_grid_mismatch")


def test_pointer_shifted_to_neighbour_is_not_the_new_discard():
    image = frame(POSITIVE).copy()
    # A whole-grid shift can match another real tile, but the two-frame newest
    # slot must remain independent and refuse that different location.
    image[196:231, 520:1260] = np.roll(image[196:231, 520:1260], 56, axis=1)
    state = river.read_discards(image, frame(BEFORE))
    assert state.table.opponent.newest.tile_id == 9
    assert state.incoming_tile is None
    assert "对不上" in state.reason


def test_default_meld_preprocessing_is_retained_and_bank_sharing_has_no_context_leak():
    image = frame(POSITIVE)
    raw = crop(image)
    cleaned, found = strip_claim_marker(raw)
    assert found is True and np.any(cleaned != raw)
    default = TileVision(template_dir=river.TEA_TEMPLATE_DIR)
    old = default.classify_crop(raw)
    specialized = river.load_vision()
    fixed = specialized.classify_crop(raw)
    assert default.allow_claim_marker is True
    assert specialized.allow_claim_marker is False
    assert default._matrix is specialized._matrix
    assert old["claim_marker"] is True and old["accepted"] is False
    assert fixed["label"] == "w9" and fixed["accepted"] is True
    assert "claim_marker" not in fixed
    # Calling the river classifier must not change the hand/meld instance.
    assert default.classify_crop(raw) == old


def test_old_signature_custom_classifier_receives_unmodified_crop():
    image = frame(POSITIVE)
    seen = []
    class LegacyClassifier:
        def classify_crop(self, value, caishen_label=None):
            seen.append((value.tobytes(), caishen_label))
            return {"tile_id": 9, "label": "w9", "score": .99, "margin": .20,
                    "accepted": True, "reason": "custom_classifier"}
    reading = river.read_opponent_sparkle_tile(image, vision=LegacyClassifier())
    assert reading["accepted"] is True
    assert seen == [(crop(image).tobytes(), None)]


@pytest.mark.parametrize("score,margin", [(.85, .20), (.99, .099)])
def test_existing_strict_incoming_gate_still_rejects_weak_injected_evidence(score, margin):
    class LegacyClassifier:
        def classify_crop(self, value, caishen_label=None):
            return {"tile_id": 9, "label": "w9", "score": score, "margin": margin,
                    "accepted": True, "reason": "custom_classifier"}
    reading = river.read_opponent_sparkle_tile(frame(POSITIVE), vision=LegacyClassifier())
    assert reading["accepted"] is False
    assert reading["reason"].startswith("tile_confidence_low")
