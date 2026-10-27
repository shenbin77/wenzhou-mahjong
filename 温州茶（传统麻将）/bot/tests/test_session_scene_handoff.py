# -*- coding: utf-8 -*-
"""Same immutable real frames drive startup, live routing and pass evidence.

Only I/O, clocks and physical executors are replaced. Scene/HUD/hand facts are
read from preserved pixels; the legacy lobby click path must be unreachable.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from action_policy import ACTION_PASS
from assist_window import TeaAssistWindow, _TransitionSubmission
from auto_play_loop import AutoPlayLoop
from harvest_tea import EXPECTED_PACKAGE
from lobby_preflight import classify_session_scene
from match_end import finish_match_summary, precise_settlement_evidence
from match_score import read_match_score
from round_transition import RoundTransitionExecutor
from tap_executor import TapUnverified
from turn_gate import detect_settled_screen

FIXTURES = Path(__file__).parent / "fixtures" / "session_scenes"
SCENES = [
    ("final_summary_16_of_16.png", "match_end", True),
    ("competition_lobby.png", "lobby", False),
    ("active_midhand_meld.png", "in_game", False),
    ("invitation_over_table.png", "invitation", False),
    ("renew_table_modal.png", "observe", False),
]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    cv2.setNumThreads(1)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline scene regression must not invoke subprocess/ADB")
    monkeypatch.setattr(subprocess, "run", forbidden)


def read(name):
    path = FIXTURES / name
    assert path.is_file(), path
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2] == (900, 1600)
    return image


@pytest.mark.parametrize("filename,expected,settled", SCENES)
def test_real_scene_priority_and_exact_settlement(filename, expected, settled):
    frame = read(filename)
    scene = classify_session_scene(frame)
    assert scene.kind == expected, scene.ledger()
    assert precise_settlement_evidence(frame)[0] is settled
    manifest = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    row = next(row for row in manifest["frames"] if row["file"] == filename)
    assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == row["sha256"]


def no_gameplay(*_args, **_kwargs):
    raise AssertionError("non-game scene reached gameplay facts / legacy lobby click channel")


def loop_for(tmp_path, filename, *, tap=None, real_analyze=False):
    loop = AutoPlayLoop(
        enabled=True, adb_path=sys.executable, serial="offline-scene",
        session_dir=tmp_path, player_name="烘熊瞎", allow_return_lobby=True,
        tap_executor=tap or SimpleNamespace(),
        shot_fn=lambda: (FIXTURES / filename).read_bytes(),
        analyze_fn=None if real_analyze else no_gameplay,
        foreground_fn=lambda: EXPECTED_PACKAGE,
        action_guard_fn=no_gameplay, round_key_fn=no_gameplay,
        read_discards=False, sleep_fn=lambda _seconds: None,
        clock_fn=lambda: 0.0)
    loop._lobby_open_event = no_gameplay
    return loop


def test_actual_lobby_hands_off_before_missing_hand_or_hud(tmp_path):
    loop = loop_for(tmp_path, "competition_lobby.png")
    event = loop.tick()
    assert event["event"] == "stop"
    assert event["gate"] == "scene_handoff"
    assert event["scene"]["kind"] == "lobby"
    assert loop.actions_done == 0


def test_unknown_real_modal_observes_without_stopping_or_fabricating_hand(tmp_path):
    frame = read("renew_table_modal.png")
    assert detect_settled_screen(frame)[0] is True
    loop = loop_for(tmp_path, "renew_table_modal.png")
    first, second = loop.tick(), loop.tick()
    assert first["event"] == second["event"] == "idle"
    assert first["scene"]["kind"] == "observe"
    assert loop.stopped is False and loop.actions_done == 0


def test_actual_summary_persists_then_exits_then_hands_off(tmp_path):
    calls = []
    class Tap:
        def execute_leave_table(self, **kwargs):
            rows = (tmp_path / "match_results.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(rows) == 1
            assert json.loads(rows[0])["player_score"] == 4
            calls.append("leave")
            return {"executed": True, "verified": True}
    loop = loop_for(tmp_path, "final_summary_16_of_16.png", tap=Tap())
    event = loop.tick()
    assert event["event"] == "left_table"
    assert event["player_score"] == 4
    assert calls == ["leave"]
    # No second click if an old summary frame is observed again.
    assert loop.tick()["event"] == "idle"
    assert calls == ["leave"]
    loop._shot = lambda: (FIXTURES / "competition_lobby.png").read_bytes()
    assert loop.tick()["gate"] == "scene_handoff"


@pytest.mark.parametrize("filename,status", [
    ("competition_lobby.png", "startup_not_settlement"),
    ("active_midhand_meld.png", "startup_in_game"),
    ("renew_table_modal.png", "startup_observe"),
    ("final_summary_16_of_16.png", "startup_blocked"),
])
def test_startup_uses_same_real_scene_facts(filename, status, tmp_path):
    executor = RoundTransitionExecutor(capture=lambda: read(filename),
                                       tap=no_gameplay, report_dir=tmp_path)
    result = TeaAssistWindow._recover_startup_round_transition(executor, Event())
    assert result.status == status, result
    assert not result.clicked


def test_cold_summary_does_not_confuse_scoreboard_name_with_local_identity(tmp_path):
    executor = RoundTransitionExecutor(
        capture=lambda: read("final_summary_16_of_16.png"), tap=no_gameplay,
        report_dir=tmp_path)
    executor._serial = "offline-scene"
    result = TeaAssistWindow._recover_startup_round_transition(
        executor, Event(), player_name="烘熊瞎", allow_return_lobby=True)
    assert result.status == "startup_observe"
    assert result.evidence["player_identity"]["status"] == "unknown"
    assert not result.clicked and not (tmp_path / "match_results.jsonl").exists()


@pytest.mark.parametrize("filename,expected", [
    ("renew_table_modal.png", False),
    ("final_summary_16_of_16.png", True),
])
def test_pass_completion_uses_real_settlement_not_generic_dimming(tmp_path, filename, expected):
    loop = loop_for(tmp_path, filename, real_analyze=True)
    before_path = FIXTURES / "active_midhand_meld.png"
    before = loop._board_from_frame(before_path.read_bytes(), before_path,
                                    read(before_path.name), note="real pre-action board")
    ok, reason, board, samples = loop._verify_claim_settled(
        before, ACTION_PASS, tag="after_pass", note="real after-action frame")
    assert ok is expected, (reason, samples)
    assert samples and not any("error" in row for row in samples)
    if expected:
        assert samples[-1]["settlement_proof"]["settlement_kind"] == "match_end"
    else:
        assert all(row["settled"] is True and not row["verified"] for row in samples)
        assert "未证明明确结算" in reason


def test_summary_transaction_never_replays_unknown_exit(tmp_path):
    rows, calls = [], []
    def leave(_image):
        assert len(rows) == 1
        calls.append("leave")
        raise TapUnverified("physical click result unknown")
    result = finish_match_summary(
        read("final_summary_16_of_16.png"), player_name="烘熊瞎", serial="offline",
        frame_path=FIXTURES / "final_summary_16_of_16.png", score_reader=read_match_score,
        persist_result=rows.append, execute_leave=leave, authorized=lambda: True)
    assert result["status"] == "blocked"
    assert result["gate"] == "leave_table_unverified"
    assert result["recorded"] is True and calls == ["leave"]


def test_summary_revocation_after_persistence_prevents_exit():
    token, rows = Event(), []
    def persist(row):
        rows.append(row)
        token.set()
    result = finish_match_summary(
        read("final_summary_16_of_16.png"), player_name="烘熊瞎", serial="offline",
        frame_path=FIXTURES / "final_summary_16_of_16.png", score_reader=read_match_score,
        persist_result=persist, execute_leave=no_gameplay, authorized=lambda: not token.is_set())
    assert len(rows) == 1 and result["recorded"] is True
    assert result["status"] == "blocked" and result["gate"] == "user_stop"


def test_zero_click_observation_keeps_one_timer_and_revocation_invalidates_it(tmp_path):
    from test_assist_full_auto import _window
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_resume_mode = "full_auto"
    window._round_transition_future = None
    calls = []
    window._begin_startup_round_transition = lambda mode: calls.append(mode)
    result = SimpleNamespace(status="startup_observe", reason="transient pixels")
    window._on_round_transition_result(result)
    window._on_round_transition_result(result)
    assert window._full_auto_wanted and window._auto_resume_pending
    assert len(window.root.scheduled) == 1
    callback = window.root.scheduled[0][1]
    window._revoke_round_transition_authority()
    # Even a new explicit authorization cannot revive an old timer.
    callback()
    assert calls == []


@pytest.mark.parametrize("authorized", [True, False])
def test_finished_worker_hands_off_only_with_current_authority(tmp_path, authorized):
    from test_assist_full_auto import _window
    window = _window(tmp_path)
    window._full_auto_wanted = authorized
    window._auto_resume_pending = False
    window.full_auto_running = True
    window._auto_loop = object()
    window._auto_future = SimpleNamespace(done=lambda: True, result=lambda: {
        "stop_gate": "scene_handoff", "stop_reason": "verified lobby", "actions": 0})
    window._style_start_button = lambda _value: None
    window._set_hud_expanded = lambda _value: None
    window._apply_preview_layout = lambda _value: None
    window._refresh_auto_gate = lambda **kwargs: None
    calls = []
    def handoff(mode):
        assert window._auto_loop is None and window._auto_future is None
        calls.append(mode)
    window._begin_startup_round_transition = handoff
    window._poll_full_auto()
    assert calls == (["full_auto"] if authorized else [])


def test_production_claim_shadow_off_on_and_internal_error_preserve_decision_and_tap(
        tmp_path, monkeypatch):
    """Same actual claim pixels/analysis; only physical executor I/O is faked."""
    import builtins
    import copy
    from dataclasses import replace
    import incoming_shadow
    from action_policy import recommended_runner_config
    from tap_executor import TapAborted

    path = (Path(__file__).parent / "fixtures" / "incoming_shadow" /
            "20261027_174850_853803_before_e60abb.png")
    assert path.is_file(), path
    raw = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    config = recommended_runner_config(auto_click_enabled=True)
    observed, attempts = [], []
    class Tap:
        def execute_claim(self, point, **kwargs):
            attempts.append({
                "point": point, "action": kwargs.get("action"),
                "kind": kwargs.get("kind"), "session_dir": str(kwargs.get("session_dir")),
                "image_sha": hashlib.sha256(kwargs["image"].tobytes()).hexdigest(),
            })
            # Stop at the physical-I/O boundary; no post-click frame is invented.
            raise TapAborted("offline executor boundary")
    def make(enabled):
        return AutoPlayLoop(
            enabled=True, serial="offline-shadow", adb_path=sys.executable,
            session_dir=tmp_path, claim_auto_click=True,
            claim_policy=replace(config, incoming_shadow_enabled=enabled),
            tap_executor=Tap(), read_discards=False)
    baseline_loop = make(False)
    analysis = baseline_loop._default_analyze(raw, path)
    original_analysis = copy.deepcopy(analysis)
    original_pixels = image.tobytes()
    real_import = builtins.__import__
    def forbid_shadow_import(name, *args, **kwargs):
        assert name != "incoming_shadow", "default-off hook imported shadow"
        return real_import(name, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", forbid_shadow_import)
        baseline = baseline_loop._claim_policy_event(image, path, analysis)
    assert baseline["claim"]["action"] == ACTION_PASS
    assert len(attempts) == 1
    assert "shadow" not in baseline["claim"]["incoming_resolution"]
    expected_attempt = attempts[-1]

    real_observe = incoming_shadow.observe_incoming_shadow
    def observe(*args, **kwargs):
        observed.append(kwargs)
        return real_observe(*args, **kwargs)
    monkeypatch.setattr(incoming_shadow, "observe_incoming_shadow", observe)
    enabled = make(True)._claim_policy_event(image, path, analysis)
    shadow = enabled["claim"]["incoming_resolution"].pop("shadow")
    assert "context_missing" in shadow["blockers"]
    assert enabled == baseline
    assert attempts[-1] == expected_attempt
    assert observed[-1]["context"] is None
    assert observed[-1]["actual_runtime_decision"] == ACTION_PASS

    def fail_only_inside_observer(*args, **kwargs):
        raise RuntimeError("injected isolated observer failure")
    monkeypatch.setattr(incoming_shadow, "_observe_incoming_shadow", fail_only_inside_observer)
    failed = make(True)._claim_policy_event(image, path, analysis)
    error = failed["claim"]["incoming_resolution"].pop("shadow")
    assert error["execution_eligible"] is False
    assert "shadow_error:RuntimeError" in error["blockers"]
    assert failed == baseline
    assert attempts == [expected_attempt] * 3
    assert len(observed) == 2
    assert analysis == original_analysis and image.tobytes() == original_pixels


def test_structured_fresh_no_click_keeps_intent_but_old_click_lock_only_observes(tmp_path):
    from test_assist_full_auto import _window
    from round_transition import TransitionResult
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_resume_mode = "full_auto"
    window._round_transition_future = None
    executor = RoundTransitionExecutor(capture=no_gameplay, tap=no_gameplay)
    window._round_transition = executor
    window.round_no = 4
    # A real executor lock result must not re-enter startup with round_no=None.
    executor._clicked_rounds.add(4)
    locked_result = executor.attempt(round_no=4)
    assert locked_result.status == "no_click" and not locked_result.clicked
    calls = []
    submission = _TransitionSubmission(0, "full_auto", 4, Event(), executor)
    window.round_no = 5  # UI bookkeeping changed after round 4 was submitted.
    window._begin_accepted_round_observation = lambda **kwargs: calls.append("read_only")
    window._on_round_transition_result(locked_result, submission=submission)
    assert calls == ["read_only"]
    assert window._full_auto_wanted and window._auto_resume_pending
    assert window.root.scheduled == []

    executor._clicked_rounds.clear()
    window._on_round_transition_result(TransitionResult(
        "no_click", "fresh scene changed before the only tap", clicked=False),
        submission=submission)
    assert len(window.root.scheduled) == 1
    assert window._full_auto_wanted and window._auto_resume_pending


class DeferredTransitionPool:
    """Real Futures without a thread or a capture/action callback."""
    def __init__(self):
        self.calls = []
        self.futures = []

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future
        future = Future()
        self.calls.append((fn, args, kwargs))
        self.futures.append(future)
        return future


@pytest.mark.parametrize("late", ["startup_not_settlement", "advanced", "error"])
@pytest.mark.parametrize("new_mode", ["full_auto", "follow"])
def test_revoked_completed_future_is_discarded_before_one_fresh_restart(tmp_path, late, new_mode):
    from test_assist_full_auto import _window
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_future = None
    window.auto_executor = pool = DeferredTransitionPool()
    executor = RoundTransitionExecutor(capture=no_gameplay, tap=no_gameplay)
    window._ensure_round_transition_executor = lambda: executor
    delivered = []
    window._on_round_transition_result = lambda result, **kwargs: delivered.append(result)
    window._begin_startup_round_transition("full_auto")
    old_future = pool.futures[0]
    old_context = window._round_transition_submission
    if late == "error":
        old_future.set_exception(RuntimeError("revoked worker failure"))
    else:
        old_future.set_result(SimpleNamespace(status=late, reason="old result"))
    # Stop arrives after the worker finished but before its UI poll.
    window._revoke_round_transition_authority()
    window._full_auto_wanted = False
    window._auto_resume_pending = False
    window._transition_follow_pending = False
    window._round_transition_resume_mode = None
    assert old_context.cancel_event.is_set()

    window._full_auto_wanted = new_mode == "full_auto"
    window._auto_resume_pending = new_mode == "full_auto"
    window._begin_startup_round_transition(new_mode)
    window._begin_startup_round_transition(new_mode)
    assert len(pool.calls) == 1 and window._round_transition_future is old_future
    window._poll_round_transition()
    assert delivered == []  # Neither old advice nor an old exception is adopted.
    assert len(pool.calls) == 2
    assert window._round_transition_future is pool.futures[1]
    current = window._round_transition_submission
    assert current.generation == old_context.generation + 1
    assert current.cancel_event is not old_context.cancel_event
    assert not current.cancel_event.is_set() and current.mode == new_mode
    assert window._round_transition_authorized()
    assert window._full_auto_wanted is (new_mode == "full_auto")
    window._poll_round_transition()  # New task is pending; polling cannot resubmit.
    assert len(pool.calls) == 2


def test_second_stop_cancels_pending_restart_before_old_future_is_collected(tmp_path):
    from test_assist_full_auto import _window
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_future = None
    window.auto_executor = pool = DeferredTransitionPool()
    window._ensure_round_transition_executor = lambda: object()
    window._begin_startup_round_transition("full_auto")
    pool.futures[0].set_result(SimpleNamespace(status="advanced", reason="old"))
    window._revoke_round_transition_authority()
    window._begin_startup_round_transition("full_auto")
    assert window._round_transition_restart_mode == "full_auto"
    window._revoke_round_transition_authority()
    window._full_auto_wanted = False
    window._auto_resume_pending = False
    window._round_transition_resume_mode = None
    window._poll_round_transition()
    assert len(pool.calls) == 1
    assert window._round_transition_future is None


def test_no_click_poll_and_next_observation_keep_submitted_round_and_executor(tmp_path):
    from test_assist_full_auto import _window
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_future = None
    window.auto_executor = pool = DeferredTransitionPool()
    original = RoundTransitionExecutor(capture=no_gameplay, tap=no_gameplay)
    original._clicked_rounds.add(4)
    window._round_transition = original
    window._ensure_round_transition_executor = lambda: original
    window.round_no = 4
    window._begin_round_transition({"stop_gate": "gate_turn"})
    submitted = window._round_transition_submission
    assert submitted.round_no == 4 and submitted.executor is original
    pool.futures[0].set_result(original.attempt(round_no=4))

    # Both mutable UI attributes now describe something else; neither may
    # release round 4's recorded click or choose a different observer target.
    window.round_no = 5
    window._round_transition = RoundTransitionExecutor(capture=no_gameplay, tap=no_gameplay)
    window._poll_round_transition()
    observer = window._round_transition_submission
    assert len(pool.calls) == 2
    assert window._accepted_round_observing is True
    assert observer.round_no == 4 and observer.executor is original
    assert original._clicked_rounds == {4}
    assert all(call[0] != window._recover_startup_round_transition for call in pool.calls)

    # A bounded observer segment can time out. Its next segment retains the
    # original round/executor too, even though the UI counter remains 5.
    pool.futures[1].set_result(SimpleNamespace(
        status="accepted_waiting_timeout", reason="still waiting", clicked=False))
    window._poll_round_transition()
    assert window._accepted_round_observe_timer is not None
    window.root.callback()
    assert len(pool.calls) == 3
    assert window._round_transition_submission.round_no == 4
    assert window._round_transition_submission.executor is original
    assert original._clicked_rounds == {4}
