"""Real pixels and raw analyzer returns; controlled corruption tests are explicit."""
from __future__ import annotations
import copy
import hashlib

import pytest

from action_policy import HandState
from incoming_shadow import ShadowConfig, observe_incoming_shadow, _lineage
from incoming_shadow_context import IncomingShadowObserver, hand_observation_evidence
from shadow_context_test_support import Clock, feed, native_sources, no_external_io


def observer(clock=None, **kwargs):
    clock = clock or Clock()
    return IncomingShadowObserver(serial="offline-source", session_id="offline-session",
                                  run_uuid="replay", monotonic_ns=clock.mono,
                                  wall_ns=clock.wall, **kwargs)


def test_native_wait_claim_has_independent_full_evidence_and_no_strict_trust(native_sources):
    o = observer()
    feed(o, native_sources["before"], finish=True)
    assert o.summary()["last_completed_cycle"]["complete"]
    report = feed(o, native_sources["claim"], phase="claim", finish=True)
    context = report["context"]
    baseline, current = context["baseline"], context["current"]
    assert [e["sequence"] for e in context["events"]] == [1, 2]
    assert [e["scene"] for e in context["events"]] == ["in_play", "claim"]
    assert len(baseline["opponent"]["tiles"]) == 13
    assert len(current["opponent"]["tiles"]) == 14
    assert len(current["own"]["tiles"]) == 15
    assert sum(t["accepted"] is not True for t in current["own"]["tiles"]) == 2
    assert current["river_source"] == "independent_current_frame_snapshot_table"
    assert current["png_sha256"] == hashlib.sha256(native_sources["claim"]["raw"]).hexdigest()
    assert current["pixel_sha256"] == hashlib.sha256(native_sources["claim"]["image"].tobytes()).hexdigest()
    assert current["png_sha256"] != current["pixel_sha256"]
    assert current["frame_sha256"] == current["pixel_sha256"]
    assert current["host_request_started_monotonic_ns"] < current["host_provider_returned_monotonic_ns"]
    assert current["host_byte_received_monotonic_ns"] is None
    assert current["captured_at_semantics"] == "host_provider_return_monotonic_seconds"
    assert current["hud_evidence"]["missing_template_digits"] == ["3", "8"]
    assert current["hud_evidence"]["trusted_room_identity"] is None
    assert current["round_epoch"] is None
    assert current["reconnect"] is None and current["reset"] is None
    assert current["hand_evidence"]["complete_observation_data"]
    assert current["trusted_hand"]["concealed_complete"] is None
    assert current["trusted_hand"]["settled_frame"] is None
    assert report["execution_eligible"] is False
    shadow = observe_incoming_shadow(
        native_sources["claim"]["image"], HandState.from_analysis(native_sources["claim"]["analysis"]),
        {}, config=ShadowConfig(enabled=True), context=context)
    assert shadow["would_accept"] is False and shadow["execution_eligible"] is False
    # Native accepted incoming is correctly outside the rejected-sparkle experiment.
    assert shadow["blockers"] == ["not_applicable"]
    gates, blockers = _lineage(context, current["pixel_sha256"], ShadowConfig(enabled=True))
    assert gates["geometry_pass"] is True
    assert gates["lineage_pass"] is False and gates["trusted_hand_pass"] is False
    assert "context_frame_mismatch" not in blockers
    assert "trusted_hand_context" in blockers
    assert "lineage_round_epoch" in blockers


def test_current_frame_readers_run_once_even_when_claim_then_finally(native_sources, monkeypatch):
    import discard_vision, room_round_vision
    calls = []
    table, room = discard_vision.snapshot_table, room_round_vision.read_room_round
    def observed_table(*a, **kw):
        calls.append("river")
        return table(*a, **kw)
    def observed_room(*a, **kw):
        calls.append("hud")
        return room(*a, **kw)
    monkeypatch.setattr(discard_vision, "snapshot_table", observed_table)
    monkeypatch.setattr(room_round_vision, "read_room_round", observed_room)
    o = observer()
    feed(o, native_sources["before"], finish=True)
    feed(o, native_sources["claim"], phase="claim", finish=True)
    assert calls == ["river", "hud", "river", "hud"]


def test_sequential_registry_cannot_supply_independent_baseline(native_sources):
    from discard_vision import forget_discards, read_discards_sequential
    key = "shadow-stage-registry-ignored"
    forget_discards(key)
    read_discards_sequential(native_sources["before"]["image"], key=key)
    read_discards_sequential(native_sources["claim"]["image"], key=key)
    o = observer()
    report = feed(o, native_sources["claim"], phase="claim")
    assert report["context"]["baseline"] is None
    assert "independent_baseline_missing" in report["producer_blockers"]
    forget_discards(key)


@pytest.mark.parametrize("boundary", [
    "foreground", "capture", "analysis", "scene", "round_scope",
    "internal_capture", "transaction", "round_reset", "uncertain", "hint_panel",
])
def test_observed_boundaries_clear_real_baseline(native_sources, boundary):
    o = observer()
    feed(o, native_sources["before"], finish=True)
    assert o._baseline is not None
    o.call("begin_cycle")
    if boundary == "foreground":
        o.call("foreground", "different.foreground")
    elif boundary == "capture":
        token = o.call("capture_started")
        o.call("capture_returned", token, error="OSError")
    elif boundary == "analysis":
        o.call("analysis_returned", b"", "", error="RuntimeError")
    elif boundary == "scene":
        token = o.call("capture_started")
        frame = native_sources["modal"]
        o.call("capture_returned", token, frame["raw"])
        o.call("scene", frame["image"], frame["scene"], frame["path"])
    elif boundary == "round_scope":
        o.call("runtime_scope", round_seq=2, hud_key=None, caishen="t8", discards_trusted=True)
    elif boundary == "internal_capture":
        o.call("capture_started")
        o.call("capture_started")
    elif boundary == "transaction":
        o.call("transaction", "execute_claim")
    elif boundary == "round_reset":
        o.call("runtime_event", {"event": "round_reset"})
    elif boundary == "uncertain":
        o.call("runtime_event", {"event": "idle", "guard": "uncertain"})
    elif boundary == "hint_panel":
        o.call("runtime_event", {"event": "idle", "turn_gate": {"state": "wait", "code": "hint_panel"}})
    assert o._baseline is None
    o.call("end_cycle", {"event": "idle", "status": "wait"})
    report = feed(o, native_sources["claim"], phase="claim")
    assert report["context"]["baseline"] is None
    assert "independent_baseline_missing" in report["producer_blockers"]


def test_raw_underread_and_ledger_recovery_never_complete_observation(native_sources):
    o = observer()
    feed(o, native_sources["before"], finish=True)
    report = feed(o, native_sources["ambiguous"], phase="claim", finish=True,
                  recovery={"applied": True, "source_frame": str(native_sources["before"]["path"])})
    current = report["context"]["current"]
    assert current["hand_evidence"]["hand_ids"] == []
    assert "ledger_hand_is_not_same_frame" in report["producer_blockers"]
    assert "raw_analysis_not_complete" in report["producer_blockers"]
    assert current["observation_complete"] is False
    assert o._baseline is None
    assert o.summary()["last_completed_cycle"]["complete"] is False


def test_even_full_raw_hand_with_recovery_flag_is_not_ledger_proof(native_sources):
    o = observer()
    feed(o, native_sources["before"], finish=True)
    report = feed(o, native_sources["claim"], phase="claim", recovery={"applied": True})
    assert report["context"]["baseline"] is None
    assert "ledger_hand_is_not_same_frame" in report["producer_blockers"]


def test_host_gap_and_row_zero_limits_remain_blocked(native_sources):
    clock = Clock()
    o = observer(clock)
    feed(o, native_sources["before"], finish=True)
    clock.advance(16)
    report = feed(o, native_sources["claim"], phase="claim")
    assert "baseline_host_age_invalid_or_expired" in report["producer_blockers"]
    o.call("end_cycle", {"event": "idle", "guard": "action_visible", "claim": {}})
    o = observer()
    feed(o, native_sources["before"], finish=True)
    # Deliberate corrupt-slot injection into otherwise native captured evidence.
    o._baseline["opponent"]["tiles"][-1]["index"] = 16
    report = feed(o, native_sources["claim"], phase="claim")
    assert "row_beyond_zero_unsupported" in report["producer_blockers"]
    assert report["execution_eligible"] is False
    o = observer()
    feed(o, native_sources["before"], finish=True)
    o._baseline["opponent"]["tiles"] = []
    report = feed(o, native_sources["claim"], phase="claim")
    assert "first_river_tile_baseline_unsupported" in report["producer_blockers"]


def test_scope_change_and_history_bounds(native_sources):
    o = observer(max_captures=3, max_events=5)
    feed(o, native_sources["before"], finish=True)
    o.call("begin_cycle", serial="new-serial", session_id="new-session")
    assert o._baseline is None
    assert o.serial == "new-serial" and o.session_id == "new-session"
    assert "capture_scope_changed" in o.summary()["cycle_breaks"]
    o.call("end_cycle", {})
    for _ in range(40):
        o.call("begin_cycle")
        token = o.call("capture_started")
        o.call("capture_returned", token, b"not an image")
        o.call("end_cycle", {})
    assert len(o.captures) == 3 and len(o.events) == 5
    assert o.capture_seq == 41
    assert o._cycle is None and o._baseline is None


def test_source_hash_and_rejected_raw_diagnostic_are_not_complete(native_sources):
    frame = native_sources["before"]
    raw = copy.deepcopy(frame["analysis"])
    raw["vision_diagnostics"][0]["accepted"] = False
    evidence, trusted, blockers = hand_observation_evidence(
        raw, frame_path=frame["path"], frame_sha256="pixels")
    assert "raw_diagnostics_rejected_or_invalid" in blockers
    assert evidence["complete_observation_data"] is False
    assert trusted["no_rejected_boxes"] is False
    o = observer()
    feed(o, frame, finish=True)
    o.call("begin_cycle")
    token = o.call("capture_started")
    o.call("capture_returned", token, frame["raw"])
    o.call("scene", frame["image"], frame["scene"], frame["path"])
    o.call("analysis_returned", native_sources["claim"]["raw"], frame["path"], frame["analysis"])
    assert o._baseline is None
    assert "analysis_not_primary_frame" in o.summary()["cycle_breaks"]
