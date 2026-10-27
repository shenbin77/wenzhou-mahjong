"""Native regression for 01:30:59 peng verification in room 032077.

The first post-click frame is truly ambiguous and must remain rejected. The
second contains a readable nine; marker cleanup must not paint its neighbours.
Only recorded PNG bytes are used for identity and L3 state evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import cv2
import numpy as np
import pytest

import live_capture
from action_policy import ACTION_PENG, verify_after_click
from auto_play_loop import AutoPlayLoop, CLAIM_SETTLE_INTERVAL_S
from tile_vision import TileVision, strip_claim_marker

FIXTURES = Path(__file__).parent / "fixtures" / "claim_marker_013059"
TEMPLATES = (Path(live_capture.__file__).resolve().parent.parent
             / "采集" / "templates" / "tea_tiles")
BEFORE = "20261028_013052_441499_before_1924ce.png"
CLICK_BEFORE = "20261028_013054_449853_before_b77988.png"
AMBIGUOUS = "20261028_013057_921839_after_claim_2eecee.png"
READABLE = "20261028_013058_912180_after_claim_da501a.png"
FIRST_NINE_BOX = (278, 761, 362, 875)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("native fixture regression must never launch a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def native(name):
    path = FIXTURES / name
    raw = path.read_bytes()
    provenance = json.loads((FIXTURES / "provenance.json").read_text("utf-8"))
    assert hashlib.sha256(raw).hexdigest() == provenance[name]["sha256"]
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape == (900, 1600, 3)
    return path, raw, image


def vision(name):
    _path, _raw, image = native(name)
    v = TileVision(template_dir=TEMPLATES)
    assert (v.min_score, v.min_margin) == (.82, .055)
    hand = v.recognize_hand(image)
    return v, hand


def readonly_loop(tmp_path, names):
    """Use the real settle and board methods; replace only capture/file I/O."""
    frames = [native(name) for name in names]
    sequence = iter(frames)
    by_bytes = {raw: path for path, raw, _image in frames}
    loop = object.__new__(AutoPlayLoop)
    loop._shot = lambda: next(sequence)[1]
    loop._save_frame = lambda raw, _tag: by_bytes[raw]
    loop._analyze = lambda raw, path: live_capture.analyze_bytes(
        raw, mode="tea_2p", image_path=path, log_dir=tmp_path)
    sleeps = []
    loop._sleep = sleeps.append
    return loop, sleeps


def board(loop, name):
    path, raw, image = native(name)
    return loop._board_from_frame(raw, path, image, note="native marker regression")


@pytest.mark.parametrize("name", [BEFORE, CLICK_BEFORE, AMBIGUOUS, READABLE])
def test_required_native_frame_provenance(name):
    native(name)


def test_clipped_triangle_does_not_paint_the_observed_non_yellow_halo():
    _path, _raw, image = native(READABLE)
    x0, y0, x1, y1 = FIRST_NINE_BOX
    crop = image[y0:y1, x0:x1]
    original = crop.copy()
    cleaned, found = strip_claim_marker(crop)
    assert found is True and np.any(cleaned != crop)
    # These seven real pixels were overwritten by the old dilated mask.
    # They do not prove hidden glyph identity; they must simply stay visible.
    halo = [(35, 7), (36, 8), (36, 9), (37, 11),
            (46, 15), (38, 17), (39, 17)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    for x, y in halo:
        assert int(hsv[y, x, 2]) <= 150
        assert np.array_equal(cleaned[y, x], crop[y, x])
    assert np.array_equal(crop, original)


def test_native_ambiguous_frame_is_not_filled_from_two_neighbouring_nines():
    v, hand = vision(AMBIGUOUS)
    assert len(hand) == 11
    assert v.detection_diagnostics["meld_groups"] == [3, 3]
    assert len(v.last_meld_boxes) == 6
    assert [d["label"] for d in v.meld_diagnostics[:3]] == ["t7", "t8", "t9"]
    first, second, third = v.meld_diagnostics[3:]
    assert first["label"] == "w8"
    assert first["accepted"] is False and first["tile_id"] is None
    assert first["reason"] == "ambiguous" and first["margin"] < .055
    assert second["label"] == third["label"] == "w9"
    assert second["accepted"] is True and third["accepted"] is True
    assert v.last_complete is False


def test_native_readable_frame_keeps_both_exact_meld_groups():
    v, hand = vision(READABLE)
    assert len(hand) == 11
    assert v.detection_diagnostics["meld_groups"] == [3, 3]
    assert [row[0] for row in v.last_melds] == [27, 28, 29, 9, 9, 9]
    assert all(d["accepted"] for d in v.meld_diagnostics)
    first = v.meld_diagnostics[3]
    assert first["claim_marker"] is True
    assert first["label"] == "w9" and first["tile_id"] == 9
    assert first["score"] >= .82 and first["margin"] >= .055
    assert v.last_complete is True


def test_real_l3_waits_for_readable_second_frame_without_any_click(tmp_path):
    loop, sleeps = readonly_loop(tmp_path, [AMBIGUOUS, READABLE])
    before = board(loop, BEFORE)
    assert before.concealed_count == 13
    assert before.melds == ((27, 28, 29),)
    unclear = board(loop, AMBIGUOUS)
    assert unclear.melds == () and unclear.meld_group_count == 0
    assert verify_after_click(before, unclear, ACTION_PENG)[0] is False
    verified, reason, after, samples = loop._verify_claim_settled(
        before, ACTION_PENG, tag="native", note="recorded peng")
    assert verified, reason
    assert [s["verified"] for s in samples] == [False, True]
    assert [s["meld_groups"] for s in samples] == [0, 2]
    assert after.concealed_count == 11
    assert after.melds == ((27, 28, 29), (9, 9, 9))
    assert after.claim_buttons == 0 and after.selection_panel is False
    assert sleeps == [CLAIM_SETTLE_INTERVAL_S]


def test_repeated_real_ambiguous_frame_still_stops_at_original_settle_boundary(tmp_path):
    loop, sleeps = readonly_loop(tmp_path, [AMBIGUOUS, AMBIGUOUS])
    before = board(loop, BEFORE)
    verified, reason, after, samples = loop._verify_claim_settled(
        before, ACTION_PENG, tag="native", note="unreadable peng")
    assert verified is False
    assert len(samples) == 2 and all(not s["verified"] for s in samples)
    assert after.melds == () and after.concealed_count == 11
    assert "副露组数" in reason
    assert sleeps == [CLAIM_SETTLE_INTERVAL_S]


def test_readable_then_ambiguous_same_instance_does_not_inherit_identity():
    v = TileVision(template_dir=TEMPLATES)
    v.recognize_hand(native(READABLE)[2])
    assert v.last_complete is True
    v.recognize_hand(native(AMBIGUOUS)[2])
    assert v.last_complete is False
    assert v.meld_diagnostics[3]["accepted"] is False
    assert v.meld_diagnostics[3]["tile_id"] is None
