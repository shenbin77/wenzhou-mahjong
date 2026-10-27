"""Shadow A records uncertainty without changing runtime incoming evidence."""
import copy
from pathlib import Path

import cv2
import numpy as np
import pytest

import action_policy
import discard_vision
from action_policy import HandState
from incoming_shadow import ShadowConfig, evaluate_shadow, observe_incoming_shadow

FIXTURES = Path(__file__).parent / "fixtures" / "incoming_shadow"
POSITIVE = "20261027_174850_853803_before_e60abb.png"
NEGATIVE = "20261027_182723_607182_before_531165.png"
HAND = HandState(concealed=(8, 1, 2, 6, 7, 9, 9, 45, 46, 47, 21, 22, 23),
                 melds=((25, 26, 27),), caishen=8, player_count=2)
ON = ShadowConfig(enabled=True)


def image(name):
    path = FIXTURES / name
    assert path.is_file(), f"Required native fixture missing: {path}"
    return cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)


def reading(name=POSITIVE):
    result = discard_vision.read_opponent_sparkle_tile(image(name), include_candidates=True)
    result["single_opponent_sparkle"] = True
    return result


def test_default_off_does_not_read_or_evaluate(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("disabled shadow did work")
    monkeypatch.setattr(discard_vision, "read_opponent_sparkle_tile", fail)
    monkeypatch.setattr(action_policy, "_engine", fail)
    assert observe_incoming_shadow(None, HAND, {}) is None
    assert evaluate_shadow(HAND, {}) is None
    assert action_policy.ActionPolicyConfig().incoming_shadow_enabled is False


def test_real_low_confidence_case_keeps_all_candidates_and_runtime_unchanged():
    frame = image(POSITIVE)
    hand_before = copy.deepcopy(HAND)
    resolved_off, evidence_off = action_policy.resolve_incoming(frame, HAND)
    raw = reading()
    assert raw["accepted"] is False
    # This native crop no longer undergoes meld-arrow cleanup.  Its raw
    # top1 is a diagnostic only: both strict incoming thresholds still fail.
    assert raw["label"] == "w2"
    assert raw["score"] == pytest.approx(0.820012, abs=2e-6)
    assert raw["margin"] == pytest.approx(0.069256, abs=2e-6)
    assert raw["score"] < discard_vision.INCOMING_MIN_SCORE
    assert raw["margin"] < discard_vision.INCOMING_MIN_MARGIN
    assert raw["reason"] == "tile_confidence_low:0.820/0.069"
    assert len(raw["candidates"]) == len(raw["candidate_labels"]) >= 32
    assert len(raw["frame_sha256"]) == len(raw["crop_sha256"]) == 64
    report = evaluate_shadow(HAND, raw, config=ON, actual_runtime_decision="pass")
    assert report["a3_eligible"] is False
    assert report["proposal_would_pass"] is False
    assert len(report["plausible_candidates"]) > 1
    assert report["identity_ambiguous"] is True
    assert report["identity_confirmed"] is False
    assert report["would_accept"] is False
    assert report["execution_eligible"] is False
    assert "context_missing" in report["blockers"]
    observed = observe_incoming_shadow(frame, HAND, evidence_off, config=ON,
                                       actual_runtime_decision="pass")
    assert observed["actual_runtime_decision"] == "pass"
    resolved_on, evidence_on = action_policy.resolve_incoming(frame, HAND)
    assert (resolved_on, evidence_on) == (resolved_off, evidence_off)
    assert resolved_off.incoming_tile is None
    assert HAND == hand_before
    assert HAND.incoming_tile is None


def test_real_systematic_wrong_read_remains_outside_A():
    report = evaluate_shadow(HAND, reading(NEGATIVE), config=ON)
    assert report["top1"] == "w4"
    assert report["a3_eligible"] is False
    assert report["would_accept"] is False


@pytest.mark.parametrize("score,margin,reason", [
    (0.8316, 0.0589, "tile_confidence_low:0.832/0.059"),
    (float("nan"), 0.04, "tile_classifier_rejected:ambiguous"),
    (float("inf"), 0.04, "tile_classifier_rejected:ambiguous"),
    (0.90, 0.02, "tile_classifier_rejected:low_score"),
    (0.85, 0.11, "tile_confidence_low:0.850/0.110"),
])
def test_rejection_reason_never_implies_safe_score(score, margin, reason):
    report = evaluate_shadow(HAND, {"score": score, "margin": margin,
                                   "reason": reason, "accepted": False}, config=ON)
    assert not report["a3_eligible"]
    assert not report["execution_eligible"]


def test_full_set_catches_a_third_winning_candidate(monkeypatch):
    raw = reading()
    top = raw["label"]
    other = [c["label"] for c in raw["candidates"] if c["label"] != top][:2]
    top3 = [top] + other
    scores = {label: value for label, value in zip(top3, (0.87, 0.83, 0.82))}
    raw["candidates"] = sorted(
        [dict(c, score=scores.get(c["label"], 0.10)) for c in raw["candidates"]],
        key=lambda c: -c["score"])
    # Explicit synthetic ambiguous diagnostics exercise A3 independently of
    # the current native frame's low-score rejection reason.
    raw.update(score=0.87, margin=0.04, accepted=False, tile_id=None,
               reason="tile_classifier_rejected:ambiguous")
    win_ids = {raw["candidates"][0]["tile_id"], raw["candidates"][2]["tile_id"]}
    monkeypatch.setattr(action_policy, "_engine", lambda: {
        "is_win": lambda tiles, **kwargs: tiles[-1] in win_ids})
    report = evaluate_shadow(HAND, raw, config=ON)
    assert report["a3_eligible"] is True
    assert report["hu_result_per_candidate"][top3[0]] is True
    assert report["hu_result_per_candidate"][top3[1]] is False
    assert report["hu_result_per_candidate"][top3[2]] is True
    assert not report["hu_unique_top1"]
    assert not report["proposal_would_pass"]


@pytest.mark.parametrize("damage", ["drop_candidate", "wrong_top1", "nan_candidate"])
def test_incomplete_or_inconsistent_candidate_evidence_blocks(damage):
    raw = reading()
    if damage == "drop_candidate":
        raw["candidates"] = raw["candidates"][:-1]
    elif damage == "wrong_top1":
        raw["label"] = "w1"
    else:
        raw["candidates"][2]["score"] = float("nan")
    report = evaluate_shadow(HAND, raw, config=ON)
    assert "candidate_set_incomplete_or_inconsistent" in report["blockers"]
    assert not report["proposal_would_pass"]


def context_for(raw):
    def river(indices, own=False):
        return {"valid": True, "count": len(indices),
                "tiles": [{"index": i, "box": [i * 56, 485, i * 56 + 55, 560]
                           if own else [1194 - round(i * 56.15), 231,
                                        1249 - round(i * 56.15), 306]}
                          for i in indices]}
    baseline = {"serial": "emulator-test", "session_id": "replay", "round_epoch": "room:1",
                "window_lineage": "window-1", "frame_sha256": "b" * 64,
                "event_id": "event-20", "captured_at": 10.0, "player_count": 2,
                "opponent": river(range(6)), "own": river(range(3), own=True)}
    current = dict(baseline, frame_sha256=raw["frame_sha256"], event_id="event-21",
                   captured_at=12.0, opponent=river(range(7)))
    current["opponent"]["tiles"][-1]["box"] = raw["box"]
    current["trusted_hand"] = dict.fromkeys(
        ("concealed_complete", "no_rejected_boxes", "caishen", "melds", "mode", "settled_frame"), True)
    current["trusted_hand"]["frame_sha256"] = raw["frame_sha256"]
    identity = {key: baseline[key] for key in
                ("serial", "session_id", "round_epoch", "window_lineage")}
    return {"baseline": baseline, "current": current,
            "events": [{**identity, "sequence": 20, "event_id": "event-20", "scene": "in_play",
                        "reset": False, "reconnect": False},
                       {**identity, "sequence": 21, "event_id": "event-21", "scene": "claim",
                        "reset": False, "reconnect": False}]}


@pytest.mark.parametrize("damage,blocker", [
    ("gap", "event_sequence_gap"), ("reset", "scene_reset_or_unknown"),
    ("epoch", "lineage_round_epoch"), ("stale", "baseline_age"),
    ("untrusted_hand", "trusted_hand_context"), ("extra_discard", "river_topology_changed"),
])
def test_lineage_and_hand_gates_fail_closed(damage, blocker):
    raw = reading()
    context = context_for(raw)
    if damage == "gap":
        context["events"][-1]["sequence"] = 22
    elif damage == "reset":
        context["events"][-1]["reset"] = True
    elif damage == "epoch":
        context["current"]["round_epoch"] = "room:2"
    elif damage == "stale":
        context["current"]["captured_at"] = 26.0
    elif damage == "untrusted_hand":
        context["current"]["trusted_hand"]["no_rejected_boxes"] = False
    else:
        river = context["current"]["opponent"]
        river["tiles"].append({"index": 7, "box": [800, 231, 855, 306]})
        river["count"] += 1
    report = evaluate_shadow(HAND, raw, context=context, config=ON)
    assert blocker in report["blockers"]
    assert not report["proposal_would_pass"]


def test_observer_error_is_evidence_only(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("classifier unavailable")
    monkeypatch.setattr(discard_vision, "read_opponent_sparkle_tile", fail)
    report = observe_incoming_shadow(None, HAND, {"sparkle_tile": {"accepted": False}}, config=ON)
    assert report["blockers"] == ["shadow_error:RuntimeError"]
    assert not report["execution_eligible"]


@pytest.mark.parametrize("damage", ["reconnect", "missing_reconnect", "midstream_identity"])
def test_event_provenance_is_required_for_every_observation(damage):
    raw = reading()
    context = context_for(raw)
    if damage == "reconnect":
        context["events"][-1]["reconnect"] = True
    elif damage == "missing_reconnect":
        context["events"][-1].pop("reconnect")
    else:
        context["events"][-1]["session_id"] = "another-session"
    report = evaluate_shadow(HAND, raw, context=context, config=ON)
    assert not report["lineage_pass"]
    assert not report["proposal_would_pass"]


def test_row_boundary_is_explicitly_unsupported_not_an_x_delta_guess():
    raw = reading()
    context = context_for(raw)
    before = context["baseline"]["opponent"]
    before["tiles"] = [{"index": 15, "box": [350, 231, 405, 306]}]
    before["count"] = 1
    after = context["current"]["opponent"]
    after["tiles"] = before["tiles"] + [{"index": 16, "box": [1194, 310, 1249, 385]}]
    after["count"] = 2
    raw.update(slot=16, box=[1194, 310, 1249, 385])
    report = evaluate_shadow(HAND, raw, context=context, config=ON)
    assert report["expected_slot"] == [1, 0]
    assert "grid_topology_unsupported" in report["blockers"]
    assert not report["geometry_pass"]
    assert not report["proposal_would_pass"]
