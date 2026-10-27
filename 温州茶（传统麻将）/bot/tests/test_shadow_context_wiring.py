"""Actual AutoPlayLoop paths; only host/process/physical executor I/O is substituted."""
from __future__ import annotations
import builtins
import copy
from dataclasses import replace
import hashlib
import sys

import pytest

from shadow_context_test_support import native_sources, no_external_io
import incoming_shadow_context as producer
from action_policy import ActionPolicyConfig, recommended_runner_config
from auto_play_loop import AutoPlayLoop
from harvest_tea import EXPECTED_PACKAGE
from tap_executor import TapAborted


class NoTap:
    def __getattr__(self, name):
        if name.startswith("execute") or name == "_tap":
            raise AssertionError("unexpected physical executor path")
        raise AttributeError(name)


def make_loop(tmp_path, native_sources, *, enabled=True, frames=None, live=False, tap=None):
    queue = list(frames or [native_sources["before"]["raw"], native_sources["claim"]["raw"]])
    calls = []
    def shot():
        calls.append("capture")
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    loop = AutoPlayLoop(
        enabled=live, serial="offline-context", adb_path=sys.executable,
        session_dir=tmp_path, shot_fn=shot, foreground_fn=lambda: EXPECTED_PACKAGE,
        tap_executor=tap or NoTap(), read_discards=True, sleep_fn=lambda _: None,
        claim_policy=ActionPolicyConfig(incoming_shadow_enabled=enabled),
        clock_fn=lambda: 10.0)
    # Seed only the existing runtime's local round key using the actual frame reader.
    # This does not supply verified round_epoch / trusted HUD to the observer.
    key, caishen = loop._probe_round_key(native_sources["before"]["path"])
    loop.round.hud_key, loop.round.caishen = key, caishen
    loop._resource_guard_tick = lambda: None
    return loop, calls


def test_actual_tick_wait_then_claim_records_every_capture_not_gate_tick(native_sources, tmp_path):
    loop, calls = make_loop(tmp_path, native_sources)
    wait = loop.tick()
    assert wait["event"] == "idle" and wait["status"] == "wait"
    tick_before_claim = loop.tick_index
    claim = loop.tick()
    observer = loop._incoming_shadow_observer
    shadow = claim["claim"]["incoming_resolution"]["shadow"]
    context = shadow["producer_evidence"]["context"]
    assert calls == ["capture", "capture"]
    assert loop.tick_index == tick_before_claim  # guard returns before turn-gate tick counter
    assert observer.capture_seq == 2
    assert context["baseline"]["capture_seq"] == 1
    assert context["current"]["capture_seq"] == 2
    assert observer.summary()["last_completed_cycle"]["complete"]
    assert shadow["execution_eligible"] is False
    assert context["current"]["trusted_hand"]["concealed_complete"] is None
    assert context["current"]["source"]["frame_path"] == claim["frame"]


def test_default_off_never_imports_or_calls_producer(native_sources, tmp_path, monkeypatch):
    loop, calls = make_loop(tmp_path, native_sources, enabled=False)
    real_import = builtins.__import__
    def forbidden_import(name, *args, **kwargs):
        assert name not in ("incoming_shadow", "incoming_shadow_context")
        return real_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("default-off producer work")
    monkeypatch.setattr(builtins, "__import__", forbidden_import)
    monkeypatch.setattr(producer, "_sha", forbidden)
    monkeypatch.setattr(producer.IncomingShadowObserver, "__init__", forbidden)
    loop.tick()
    event = loop.tick()
    assert calls == ["capture", "capture"]
    assert "shadow" not in event["claim"]["incoming_resolution"]
    assert not hasattr(loop, "_incoming_shadow_observer")


@pytest.mark.parametrize("failure", ["foreground", "capture", "analysis", "modal", "resource", "stopped"])
def test_actual_early_exits_notify_and_clear_baseline(native_sources, tmp_path, monkeypatch, failure):
    second = native_sources["claim"]["raw"]
    if failure == "capture":
        second = OSError("offline capture failure")
    if failure == "modal":
        second = native_sources["modal"]["raw"]
    loop, _ = make_loop(tmp_path, native_sources, frames=[native_sources["before"]["raw"], second])
    loop.tick()
    o = loop._incoming_shadow_observer
    assert o._baseline is not None
    if failure == "foreground":
        # Replace the underlying provider retained by the installed binding.
        loop._incoming_shadow_binding.original_foreground = lambda: "wrong.package"
    elif failure == "analysis":
        def fail(*args, **kwargs):
            raise RuntimeError("offline analyze failure")
        loop._incoming_shadow_binding.original_analyze = fail
    elif failure == "resource":
        loop._resource_guard_tick = lambda: {"event": "idle", "reason": "resource boundary"}
    elif failure == "stopped":
        loop.stopped = True
    event = loop.tick()
    assert o._cycle is None
    assert o._baseline is None
    assert not o.summary()["last_completed_cycle"]["complete"]
    assert event["event"] in ("idle", "stop", "error", "skip")


def test_actual_ledger_repair_cannot_replace_raw_snapshot(native_sources, tmp_path, monkeypatch):
    loop, _ = make_loop(tmp_path, native_sources,
                        frames=[native_sources["before"]["raw"], native_sources["claim"]["raw"]])
    loop.tick()
    # An actual full hu analyzer result is available from a different frame.
    # The production ledger recall then repairs a deliberately underread current
    # raw result; the shadow must continue to report only those 9 current tiles.
    memory_source = native_sources["claim"]["analysis"]
    assert memory_source["status"] == "hu"
    loop._hand_ledger.remember(
        memory_source, round_seq=loop.round.seq, caishen=loop.round.caishen,
        frame=str(native_sources["claim"]["path"]))
    original = loop._incoming_shadow_binding.original_analyze
    def raw_failure(raw, path):
        result = original(raw, path)
        result.update(status="abstain", hand_ids=result["hand_ids"][:-1],
                      vision_diagnostics=result["vision_diagnostics"][:-1])
        return result
    loop._incoming_shadow_binding.original_analyze = raw_failure
    event = loop.tick()
    current = event["claim"]["incoming_resolution"]["shadow"]["producer_evidence"]["context"]["current"]
    assert event["claim"]["hand_recovery"]["applied"] is True
    assert len(current["hand_evidence"]["hand_ids"]) == 9
    assert "ledger_hand_is_not_same_frame" in current["observation_blockers"]
    assert not current["observation_complete"]
    assert current["trusted_hand"]["concealed_complete"] is None
    assert not loop._incoming_shadow_observer.summary()["last_completed_cycle"]["complete"]


def test_builtin_capture_has_byte_anchor_and_failed_decode_keeps_original_hash(native_sources, tmp_path):
    for raw, suffix in [(native_sources["before"]["raw"], "valid"), (b"invalid-png", "invalid")]:
        loop, calls = make_loop(tmp_path / suffix, native_sources)
        loop._shot = loop._adb_screenshot
        adb_calls = []
        def fake_adb(*args):
            adb_calls.append(args)
            return raw
        loop._run_adb = fake_adb
        loop.tick()
        capture = loop._incoming_shadow_observer.captures[-1]
        assert adb_calls == [("exec-out", "screencap", "-p")]
        assert capture["png_sha256"] == hashlib.sha256(raw).hexdigest()
        assert (capture["host_request_started_monotonic_ns"]
                <= capture["host_byte_received_monotonic_ns"]
                <= capture["host_provider_returned_monotonic_ns"])
        assert capture["byte_receive_boundary"] == "AutoPlayLoop._run_adb_return_before_decode"
        if suffix == "invalid":
            assert capture["capture_error"] == "RuntimeError"
            assert loop._incoming_shadow_observer._baseline is None


def test_tick_observer_fault_changes_only_diagnostics(native_sources, tmp_path, monkeypatch):
    baseline, baseline_calls = make_loop(tmp_path / "off", native_sources, enabled=False)
    off_events = [baseline.tick(), baseline.tick()]
    faulty, faulty_calls = make_loop(tmp_path / "error", native_sources)
    def fail(*args, **kwargs):
        raise RuntimeError("isolated source-reader error")
    monkeypatch.setattr(producer.IncomingShadowObserver, "_snapshot", fail)
    on_events = [faulty.tick(), faulty.tick()]
    assert baseline_calls == faulty_calls == ["capture", "capture"]
    for left, right in zip(off_events, on_events):
        assert left["event"] == right["event"]
        assert left["status"] == right["status"]
        assert left.get("guard") == right.get("guard")
    assert off_events[1]["claim"]["action"] == on_events[1]["claim"]["action"]
    assert off_events[1]["claim"]["tap_point"] == on_events[1]["claim"]["tap_point"]
    assert faulty._incoming_shadow_observer.fault == "observer_error:RuntimeError"
    assert faulty._incoming_shadow_observer._baseline is None


def test_claim_off_on_and_producer_exception_preserve_exact_tap_and_decision(
        native_sources, tmp_path, monkeypatch):
    import incoming_shadow
    frame = native_sources["ambiguous"]
    raw_analysis = copy.deepcopy(frame["analysis"])
    pixels = frame["image"].tobytes()
    calls, raw_hands = [], []
    class Tap:
        def execute_claim(self, point, **kwargs):
            calls.append({
                "point": point, "action": kwargs.get("action"), "kind": kwargs.get("kind"),
                "session_dir": str(kwargs.get("session_dir")),
                "image_sha": hashlib.sha256(kwargs["image"].tobytes()).hexdigest(),
            })
            raise TapAborted("offline physical boundary")
    config = recommended_runner_config(auto_click_enabled=True)
    def run(enabled):
        loop = AutoPlayLoop(
            enabled=True, serial="offline-shadow", adb_path=sys.executable,
            session_dir=tmp_path, claim_auto_click=True, tap_executor=Tap(), read_discards=False,
            claim_policy=replace(config, incoming_shadow_enabled=enabled))
        return loop._claim_policy_event(frame["image"], frame["path"], frame["analysis"])
    off = run(False)
    real_observe = incoming_shadow.observe_incoming_shadow
    def observed(image, hand, *args, **kwargs):
        raw_hands.append(hand)
        return real_observe(image, hand, *args, **kwargs)
    monkeypatch.setattr(incoming_shadow, "observe_incoming_shadow", observed)
    on = run(True)
    report = on["claim"]["incoming_resolution"].pop("shadow")
    assert report["execution_eligible"] is False
    assert on == off
    def fail(*args, **kwargs):
        raise RuntimeError("isolated producer failed")
    monkeypatch.setattr(producer.IncomingShadowObserver, "prepare_claim", fail)
    error = run(True)
    report = error["claim"]["incoming_resolution"].pop("shadow")
    assert report["execution_eligible"] is False
    assert error == off
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert frame["analysis"] == raw_analysis and frame["image"].tobytes() == pixels
    assert len(raw_hands) == 2


def test_disable_detaches_providers_and_forgets_chain(native_sources, tmp_path):
    loop, calls = make_loop(tmp_path, native_sources,
                            frames=[native_sources["before"]["raw"]] * 2)
    original_shot, original_tap = loop._shot, loop._tap
    loop.tick()
    old = loop._incoming_shadow_observer
    assert old._baseline is not None
    loop._claim_policy = replace(loop._claim_policy, incoming_shadow_enabled=False)
    loop.tick()
    assert loop._shot is original_shot and loop._tap is original_tap
    assert loop._incoming_shadow_observer is None
    assert old._baseline is None
    assert old.capture_seq == 1 and calls == ["capture", "capture"]


def test_claim_reader_failure_is_not_a_complete_claim_observation(
        native_sources, tmp_path, monkeypatch):
    import auto_play_loop
    loop, _ = make_loop(tmp_path, native_sources)
    loop.tick()
    assert loop._incoming_shadow_observer._baseline is not None
    def fail(*args, **kwargs):
        raise RuntimeError("claim reader failed")
    monkeypatch.setattr(auto_play_loop, "read_claims", fail)
    event = loop.tick()
    assert event["claim"]["status"] == "read_failed"
    observer = loop._incoming_shadow_observer
    assert observer._baseline is None
    assert observer.summary()["last_completed_cycle"]["complete"] is False


def test_external_stop_and_outside_tick_capture_clear_baseline(native_sources, tmp_path):
    loop, calls = make_loop(tmp_path, native_sources)
    loop.tick()
    observer = loop._incoming_shadow_observer
    assert observer._baseline is not None
    loop._shot()
    assert observer._baseline is None and observer.capture_seq == 2
    other, _ = make_loop(tmp_path / "stop", native_sources)
    other.tick()
    assert other._incoming_shadow_observer._baseline is not None
    other._stop("user_stop", "offline stop outside tick")
    assert other._incoming_shadow_observer._baseline is None


def test_executor_transaction_proxy_preserves_arguments_and_original_result(
        native_sources, tmp_path):
    class Tap:
        calls = []
        marker = object()
        def execute_claim(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.marker
    tap = Tap()
    loop, _ = make_loop(tmp_path, native_sources, tap=tap)
    loop.tick()
    observer = loop._incoming_shadow_observer
    assert observer._baseline is not None
    result = loop._tap.execute_claim((10, 20), action="pass", image=native_sources["claim"]["image"])
    assert result is tap.marker
    assert tap.calls[0][0] == ((10, 20),)
    assert tap.calls[0][1]["action"] == "pass"
    assert tap.calls[0][1]["image"] is native_sources["claim"]["image"]
    assert observer._baseline is None
    assert any(e.get("reason") == "execution_or_internal_verification_transaction"
               for e in observer.events)
