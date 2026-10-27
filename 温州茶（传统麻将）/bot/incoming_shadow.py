"""Evidence-only incoming A experiment.

This module never returns a playable tile.  A winning hand does not prove the
identity of an ambiguous crop.  Missing provenance remains a blocker, rather
than being filled from timestamps, accepted-only rivers, or prior suggestions.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool = False
    candidate_floor: float = 0.70
    candidate_delta: float = 0.10
    baseline_max_age_s: float = 15.0


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def _cell(index):
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return divmod(index, 16)


def _river_cells(river):
    if not isinstance(river, Mapping) or river.get("valid") is not True:
        return None
    tiles = river.get("tiles")
    if not isinstance(tiles, (list, tuple)):
        return None
    cells = {}
    for tile in tiles:
        if not isinstance(tile, Mapping):
            return None
        cell, box = _cell(tile.get("index")), tile.get("box")
        if cell is None or cell in cells or not isinstance(box, (list, tuple)) or len(box) != 4:
            return None
        if not all(_finite(v) for v in box):
            return None
        cells[cell] = tuple(box)
    if river.get("count") != len(cells):
        return None
    return cells


def _lineage(context, frame_hash, config):
    out = {"baseline_age": None, "baseline_event_id": None,
           "expected_slot": None, "observed_slot": None, "river_delta": None,
           "geometry_pass": False, "lineage_pass": False,
           "trusted_hand_pass": False}
    blockers = []
    if not isinstance(context, Mapping):
        return out, ["context_missing"]
    baseline, current = context.get("baseline"), context.get("current")
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        return out, ["snapshot_provenance_missing"]
    if baseline.get("player_count") != 2 or current.get("player_count") != 2:
        blockers.append("lineage_player_count")
    out["baseline_event_id"] = baseline.get("event_id")
    for key in ("serial", "session_id", "round_epoch", "window_lineage"):
        if not baseline.get(key) or baseline.get(key) != current.get(key):
            blockers.append("lineage_" + key)
    if current.get("frame_sha256") != frame_hash:
        blockers.append("context_frame_mismatch")
    for row in (baseline, current):
        if not row.get("frame_sha256") or not row.get("event_id"):
            blockers.append("event_provenance_missing")
    t0, t1 = baseline.get("captured_at"), current.get("captured_at")
    if _finite(t0) and _finite(t1):
        out["baseline_age"] = float(t1) - float(t0)
    if (not _finite(config.baseline_max_age_s) or config.baseline_max_age_s <= 0
            or out["baseline_age"] is None
            or not 0 < out["baseline_age"] <= config.baseline_max_age_s):
        blockers.append("baseline_age")
    events = context.get("events")
    if not isinstance(events, (list, tuple)) or len(events) < 2:
        blockers.append("event_sequence_missing")
    else:
        if any(not isinstance(e, Mapping) for e in events):
            blockers.append("event_sequence_invalid")
        else:
            sequence = [e.get("sequence") for e in events]
            ids = [e.get("event_id") for e in events]
            if (any(isinstance(n, bool) or not isinstance(n, int) for n in sequence)
                    or any(b != a + 1 for a, b in zip(sequence, sequence[1:]))):
                blockers.append("event_sequence_gap")
            if (any(not isinstance(i, str) or not i for i in ids)
                    or ids[0] != baseline.get("event_id") or ids[-1] != current.get("event_id")
                    or len(set(ids)) != len(ids)):
                blockers.append("event_endpoints")
            if any(e.get("scene") not in ("in_play", "claim")
                   or e.get("reset") is not False for e in events):
                blockers.append("scene_reset_or_unknown")
            if any(e.get("reconnect") is not False for e in events):
                blockers.append("event_reconnect_or_unknown")
            keys = ("serial", "session_id", "round_epoch", "window_lineage")
            if any(any(e.get(key) != baseline.get(key) or not e.get(key)
                       for key in keys) for e in events):
                blockers.append("event_identity_changed_or_missing")
            if events[0].get("scene") != "in_play" or events[-1].get("scene") != "claim":
                blockers.append("event_window_endpoints")
    before = _river_cells(baseline.get("opponent"))
    after = _river_cells(current.get("opponent"))
    own_before = _river_cells(baseline.get("own"))
    own_after = _river_cells(current.get("own"))
    if before and after is not None and own_before is not None and own_after is not None:
        previous_cell = max(before)
        expected = (previous_cell[0] + (previous_cell[1] + 1) // 16,
                    (previous_cell[1] + 1) % 16)
        added = set(after) - set(before)
        out["expected_slot"] = list(expected)
        out["observed_slot"] = list(next(iter(added))) if len(added) == 1 else None
        out["river_delta"] = len(after) - len(before)
        if expected[0] != 0 or any(cell[0] != 0 for cell in after):
            blockers.append("grid_topology_unsupported")
        elif (added != {expected} or len(after) != len(before) + 1
              or any(after.get(cell) != box for cell, box in before.items())
              or own_before != own_after):
            blockers.append("river_topology_changed")
        else:
            out["geometry_pass"] = True
            out["expected_box"] = list(after[expected])
    else:
        blockers.append("river_snapshot_untrusted")
    # These booleans must be supplied by a proven, same-frame producer.  This
    # module deliberately does not infer them from status=advise or counts alone.
    trusted = current.get("trusted_hand")
    required = ("concealed_complete", "no_rejected_boxes", "caishen",
                "melds", "mode", "settled_frame")
    if (isinstance(trusted, Mapping)
            and all(trusted.get(k) is True for k in required)
            and trusted.get("frame_sha256") == frame_hash):
        out["trusted_hand_pass"] = True
    else:
        blockers.append("trusted_hand_context")
    out["lineage_pass"] = not any(b.startswith(("lineage_", "event_", "scene_", "context_"))
                                  for b in blockers) and "baseline_age" not in blockers
    return out, blockers


def evaluate_shadow(hand, reading, *, context=None, config=None,
                    actual_runtime_decision=None):
    """Evaluate an explicit observation; return diagnostics, never HandState."""
    cfg = config or ShadowConfig()
    if not cfg.enabled:
        return None
    report = {
        "path": "incoming_A_shadow", "schema_version": 1,
        "shadow_only": True, "fired": False, "would_accept": False,
        "execution_eligible": False, "identity_confirmed": False,
        "proposal_would_pass": False, "a3_eligible": False,
        "candidate_parameters": {"floor": cfg.candidate_floor,
                                 "delta": cfg.candidate_delta,
                                 "experimental": True},
        "plausible_candidates": [], "hu_result_per_candidate": {},
        "hu_unique_top1": False, "identity_ambiguous": True,
        "actual_runtime_decision": actual_runtime_decision,
        "later_ground_truth_if_available": None,
        "blockers": [],
    }
    blockers = report["blockers"]
    if not isinstance(reading, Mapping):
        blockers.append("reading_missing")
        return report
    report.update({k: reading.get(k) for k in
                   ("frame_sha256", "crop_sha256", "template_fingerprint",
                    "sparkle_x", "box")})
    label, score, margin = (reading.get(k) for k in ("label", "score", "margin"))
    report.update(top1=label, top1_score=score, top1_margin=margin)
    reason = str(reading.get("reason") or "")
    numeric = _finite(score) and _finite(margin) and margin >= 0
    eligible = numeric and (
        (reason == "tile_classifier_rejected:ambiguous" and score >= 0.82 and margin < 0.055)
        or (reason.startswith("tile_confidence_low:") and score >= 0.86 and margin < 0.10))
    report["a3_eligible"] = bool(eligible and reading.get("accepted") is False)
    if not report["a3_eligible"]:
        blockers.append("reason_or_score_outside_A3")
    if (not _finite(cfg.candidate_floor) or not _finite(cfg.candidate_delta)
            or cfg.candidate_delta < 0):
        blockers.append("candidate_parameters_invalid")
    if getattr(hand, "incoming_tile", None) is not None:
        blockers.append("main_incoming_present")
    if getattr(hand, "incoming_source", None) not in (None, "opponent"):
        blockers.append("incoming_source")
    if reading.get("single_opponent_sparkle") is not True:
        blockers.append("sparkle_attribution")
    if (not getattr(hand, "is_valid", False) or getattr(hand, "caishen", None) is None
            or getattr(hand, "player_count", None) != 2):
        blockers.append("hand_shape_or_mode")

    candidates = reading.get("candidates")
    expected = reading.get("candidate_labels")
    candidate_ok = (reading.get("candidates_complete") is True
                    and isinstance(candidates, (list, tuple)) and bool(candidates)
                    and isinstance(expected, (list, tuple)) and bool(expected)
                    and all(isinstance(label, str) for label in expected)
                    and bool(reading.get("frame_sha256")) and bool(reading.get("crop_sha256"))
                    and bool(reading.get("template_fingerprint")))
    from tile_vision import TILE_IDS
    if candidate_ok:
        labels = []
        scores = []
        for candidate in candidates:
            if (not isinstance(candidate, Mapping) or candidate.get("label") not in TILE_IDS
                    or candidate.get("tile_id") != TILE_IDS.get(candidate.get("label"))
                    or not _finite(candidate.get("score"))):
                candidate_ok = False
                break
            labels.append(candidate["label"])
            scores.append(float(candidate["score"]))
        candidate_ok = candidate_ok and (
            len(labels) == len(set(labels)) and set(labels) == set(expected)
            and len(expected) == len(set(expected))
            and labels[0] == label and numeric
            and abs(scores[0] - float(score)) <= 1e-6
            and scores == sorted(scores, reverse=True)
            and len(scores) >= 2 and abs((scores[0] - scores[1]) - float(margin)) <= 1e-6)
    if not candidate_ok:
        blockers.append("candidate_set_incomplete_or_inconsistent")
    elif "candidate_parameters_invalid" not in blockers:
        report["plausible_candidates"] = [
            dict(c) for c in candidates
            if c["score"] >= cfg.candidate_floor
            and float(score) - c["score"] <= cfg.candidate_delta]
        if not report["plausible_candidates"] or report["plausible_candidates"][0]["label"] != label:
            blockers.append("candidate_top1_excluded")
    lineage, lineage_blockers = _lineage(context, reading.get("frame_sha256"), cfg)
    report.update(lineage)
    blockers.extend(lineage_blockers)
    observed = _cell(reading.get("slot"))
    if (report["geometry_pass"] and
            ((list(observed) if observed is not None else None) != report["expected_slot"]
             or reading.get("box") != report.get("expected_box"))):
        blockers.append("sparkle_slot_mismatch")
        report["geometry_pass"] = False
    if candidate_ok and "hand_shape_or_mode" not in blockers:
        from tea_bot import vision_to_engine
        from action_policy import _engine
        engine = _engine()
        for candidate in report["plausible_candidates"]:
            try:
                result = bool(engine["is_win"](
                    list(hand.concealed) + [vision_to_engine(candidate["tile_id"])],
                    caishen_id=hand.caishen, melds=[list(m) for m in hand.melds]))
            except Exception:
                result = None
                blockers.append("candidate_engine_rejected")
            report["hu_result_per_candidate"][candidate["label"]] = result
        wins = [label for label, value in report["hu_result_per_candidate"].items()
                if value is True]
        report["hu_unique_top1"] = wins == [label]
    if not report["hu_unique_top1"]:
        blockers.append("hu_not_unique_top1")
    report["identity_ambiguous"] = len(report["plausible_candidates"]) != 1
    # For studying the superseded proposal only.  Even unique Hu does not turn
    # competing non-winning identities into impossible physical tiles.
    report["proposal_would_pass"] = not blockers
    report["blockers"] = sorted(set(blockers))
    report["execution_blockers"] = ["shadow_only", "identity_not_independently_confirmed"]
    return report


def _observe_incoming_shadow(image, hand, incoming_resolution, *, config=None,
                             context=None, actual_runtime_decision=None):
    """Read a fresh rejected sparkle crop only when explicitly enabled."""
    cfg = config or ShadowConfig()
    if not cfg.enabled:
        return None
    resolution = incoming_resolution if isinstance(incoming_resolution, Mapping) else {}
    first = resolution.get("sparkle_tile")
    if not isinstance(first, Mapping) or first.get("accepted") is not False:
        return {"path": "incoming_A_shadow", "shadow_only": True, "fired": False,
                "would_accept": False, "execution_eligible": False,
                "blockers": ["not_applicable"],
                "actual_runtime_decision": actual_runtime_decision}
    from discard_vision import read_opponent_sparkle_tile
    reading = read_opponent_sparkle_tile(
        image, sparkle_x=first.get("sparkle_x"), include_candidates=True)
    sides = resolution.get("sparkle") or {}
    reading["single_opponent_sparkle"] = (
        len(sides.get("opponent") or ()) == 1 and not sides.get("self")
        and resolution.get("arbitrated_source") == "opponent")
    return evaluate_shadow(hand, reading, context=context, config=cfg,
                           actual_runtime_decision=actual_runtime_decision)


def observe_incoming_shadow(image, hand, incoming_resolution, *, config=None,
                            context=None, actual_runtime_decision=None):
    """A shadow failure can only produce an error record, never stop gameplay."""
    cfg = config or ShadowConfig()
    if not cfg.enabled:
        return None
    try:
        return _observe_incoming_shadow(
            image, hand, incoming_resolution, config=cfg, context=context,
            actual_runtime_decision=actual_runtime_decision)
    except Exception as exc:
        return {"path": "incoming_A_shadow", "shadow_only": True, "fired": False,
                "would_accept": False, "execution_eligible": False,
                "blockers": ["shadow_error:" + type(exc).__name__],
                "actual_runtime_decision": actual_runtime_decision}
