# -*- coding: utf-8 -*-
"""UI hand-off contract: recommendation first, explicit full auto second."""

from __future__ import annotations

from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import action_policy
import auto_play_loop
import full_auto_control
from assist_window import (AUTOPLAY_UNKNOWN_TILE, TeaAssistWindow,
                           autoplay_hand_display)


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_autoplay_underread_display_keeps_physical_count_without_guessing():
    analysis = {
        "status": "abstain",
        "reason": "识牌不完整，停止决策",
        "vision_diagnostics": [
            {"accepted": True, "label": "w2"} for _ in range(8)
        ],
    }
    event = {
        "event": "idle",
        "turn_gate": {"evidence": {
            "physical_boxes": 17,
            "accepted_tiles": 8,
        }},
        "bright_subset_cut": {"hit": True, "confirmed": False, "streak": 1},
    }

    display = autoplay_hand_display(event, analysis)

    assert len(display["display_hand_labels"]) == 17
    assert display["display_hand_labels"].count(AUTOPLAY_UNKNOWN_TILE) == 9
    assert "物理牌框 17/17" in display["display_hand_text"]
    assert "牌面直读 8/17" in display["display_hand_text"]
    assert "不会按残缺手牌算牌" in display["display_reason"]
    assert analysis.get("hand") is None and analysis.get("hand_ids") is None


def test_autoplay_complete_read_needs_no_display_override():
    event = {"turn_gate": {"evidence": {
        "physical_boxes": 17,
        "accepted_tiles": 17,
    }}}
    assert autoplay_hand_display(event, {"status": "advise"}) == {}


class Button:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


class Root:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.scheduled.append((delay, callback))
        return "timer"

    def after_cancel(self, token):
        self.scheduled.append(("cancel", token))


class PendingFuture:
    def done(self):
        return False


class Executor:
    def submit(self, fn):
        self.fn = fn
        return PendingFuture()


class ImmediateExecutor:
    def __init__(self):
        self.submissions = 0

    def submit(self, fn):
        self.submissions += 1
        return SimpleNamespace(done=lambda: True, result=fn)


class ImmediateAutoExecutor:
    def submit(self, fn):
        outcome = fn()
        return SimpleNamespace(done=lambda: True, result=lambda: outcome)


def _window(tmp_path: Path):
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._mode_key = lambda: "tea_2p"
    window.account = Var("玉姐")
    window.serial = Var("emulator-5556")
    window.full_auto_status = Var()
    window.status = Var()
    window.live_info = Var()
    window.start_button = Button()
    window.auto_button = Button()
    window._auto_events = Queue()
    window.auto_executor = Executor()
    window.root = Root()
    window.session_dir = tmp_path / "assist"
    window.embedded_follow_log = None
    window.follow_session_log = None
    window.full_auto_running = False
    window._auto_loop = None
    window._auto_future = None
    window._auto_poll_timer = None
    window._ready = lambda: True
    window._identity_gate_ok = lambda **_kwargs: True
    window._is_bound = lambda: True
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window._set_operational_status = lambda *args, **kwargs: None
    window._refresh_binding_views = lambda: None
    window.stop = lambda: setattr(window, "continuous", False)
    return window


def test_full_auto_starts_follow_flow_before_recommendation(tmp_path):
    window = _window(tmp_path)
    window.continuous = False
    window._lobby_active = False
    started = []
    window.start = lambda: started.append("follow")

    window._start_full_auto()

    assert window.full_auto_running is False
    assert started == ["follow"]
    assert window._full_auto_wanted is True
    assert window._auto_resume_pending is True
    assert "自动找位" in window.full_auto_status.get()


def test_full_auto_handoff_enables_guarded_loop(monkeypatch, tmp_path):
    created = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"

        def run(self):
            return {"actions": 0, "stopped": True, "stop_reason": "test"}

        def request_stop(self, reason):
            self.stop_reason = reason

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs),
    )
    window = _window(tmp_path)
    window.continuous = True
    window.auto_executor = ImmediateAutoExecutor()

    window._start_full_auto()

    assert window.full_auto_running is True
    assert created["enabled"] is True
    assert created["claim_auto_click"] is True
    assert created["claim_policy"][1]["include_chi"] is True
    assert created["player_name"] == "玉姐"
    assert created["allow_return_lobby"] is True
    assert window.start_button.config["text"] == "停止全部"
    assert window.auto_button.config["text"] == "关闭全自动（恢复只读推荐）"
    assert window.root.delay == 150


def test_full_auto_does_not_report_running_while_worker_is_queued(
        monkeypatch, tmp_path):
    class FakeLoop:
        def __init__(self, **kwargs):
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"

        def run(self):
            raise AssertionError("queued worker must not run in this test")

        def request_stop(self, _reason):
            return None

    class BlockingExecutor:
        def submit(self, fn):
            self.fn = fn
            return PendingFuture()

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs))
    window = _window(tmp_path)
    window.continuous = True
    window.auto_executor = BlockingExecutor()

    started = window._start_full_auto()

    assert started is False
    assert window.full_auto_running is False
    assert window._auto_start_pending is True
    assert window._auto_resume_pending is True
    assert window._auto_future is not None
    assert "已排队" in window.full_auto_status.get()
    assert "全自动跟随中" not in window.full_auto_status.get()
    assert window.root.delay == 50


def test_full_auto_submit_failure_keeps_pending_and_never_reports_running(
        monkeypatch, tmp_path):
    class FakeLoop:
        def __init__(self, **kwargs):
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"

        def run(self):
            return None

        def request_stop(self, _reason):
            return None

    class FailingExecutor:
        @staticmethod
        def submit(_fn):
            raise RuntimeError("executor unavailable")

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs))
    window = _window(tmp_path)
    window.continuous = True
    window.auto_executor = FailingExecutor()
    retries = []
    window.start = lambda: retries.append("follow_gates")

    started = window._start_full_auto()

    assert started is False
    assert window.full_auto_running is False
    assert window._auto_start_pending is False
    assert window._auto_future is None
    assert window._full_auto_wanted is True
    assert window._auto_resume_pending is True
    assert retries == ["follow_gates"]
    assert "提交失败" in window.full_auto_status.get()
    assert "全自动跟随中" not in window.full_auto_status.get()


def test_second_full_auto_click_cancels_the_single_queued_worker(
        monkeypatch, tmp_path):
    loops = []

    class FakeLoop:
        def __init__(self, **kwargs):
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"
            self.stop_reasons = []
            loops.append(self)

        def run(self):
            raise AssertionError("queued worker must not run")

        def request_stop(self, reason):
            self.stop_reasons.append(reason)

    class CancellableFuture(PendingFuture):
        def __init__(self):
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1
            return True

    class BlockingExecutor:
        def __init__(self):
            self.submissions = 0
            self.future = CancellableFuture()

        def submit(self, fn):
            self.submissions += 1
            self.fn = fn
            return self.future

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs))
    window = _window(tmp_path)
    window.continuous = True
    window.auto_executor = BlockingExecutor()

    window._toggle_full_auto()
    window._toggle_full_auto()

    assert window.auto_executor.submissions == 1
    assert len(loops) == 1
    assert len(loops[0].stop_reasons) == 1
    assert window.auto_executor.future.cancel_calls == 1
    assert window._auto_start_pending is False
    assert window._full_auto_wanted is False
    assert window._auto_resume_pending is False
    assert "取消" in window.full_auto_status.get()


def test_uncancellable_start_keeps_worker_identity_until_future_finishes(
        monkeypatch, tmp_path):
    loops = []

    class FakeLoop:
        def __init__(self, **kwargs):
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"
            self.stop_reasons = []
            loops.append(self)

        def run(self):
            return {"actions": 0, "stop_reason": "授权已撤销"}

        def request_stop(self, reason):
            self.stop_reasons.append(reason)

    class RunningFuture:
        def __init__(self):
            self.finished = False
            self.cancel_calls = 0
            self.result_calls = 0

        def cancel(self):
            self.cancel_calls += 1
            return False

        def done(self):
            return self.finished

        def result(self):
            self.result_calls += 1
            return {"actions": 0, "stop_reason": "授权已撤销"}

    class RunningExecutor:
        def __init__(self):
            self.submissions = 0
            self.future = RunningFuture()

        def submit(self, fn):
            self.submissions += 1
            self.fn = fn
            return self.future

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs))
    window = _window(tmp_path)
    window.continuous = True
    window.auto_executor = RunningExecutor()
    reviews = []
    window._schedule_session_review = lambda path: reviews.append(Path(path))

    window._toggle_full_auto()
    old_future = window._auto_future
    old_loop = window._auto_loop
    window._auto_start_event.set()  # worker crossed the start handshake
    window._toggle_full_auto()

    assert window._auto_start_cancel_pending is True
    assert window._auto_start_pending is True
    assert window._auto_future is old_future
    assert window._auto_loop is old_loop
    assert old_loop.stop_reasons
    assert window.auto_executor.submissions == 1

    # A new request cannot queue a second worker behind the untracked old one.
    assert window._start_full_auto() is False
    assert window.auto_executor.submissions == 1

    old_future.finished = True
    window._poll_full_auto_start()

    assert window._auto_start_pending is False
    assert window._auto_start_cancel_pending is False
    assert window._auto_future is None
    assert window._auto_loop is None
    assert old_future.result_calls == 1
    assert reviews == [Path(window.session_dir)]


def test_repeated_resume_is_idempotent_and_stop_targets_the_only_loop(
        monkeypatch, tmp_path):
    loops = []

    class FakeLoop:
        def __init__(self, **kwargs):
            self.session_dir = Path(kwargs["session_dir"])
            self.follow_log_path = self.session_dir / "follow_session.jsonl"
            self.ledger_path = self.session_dir / "actions.jsonl"
            self.stop_reasons = []
            loops.append(self)

        def run(self):
            raise AssertionError("queued worker must not run")

        def request_stop(self, reason):
            self.stop_reasons.append(reason)

    class CancellableFuture(PendingFuture):
        def __init__(self):
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1
            return True

    class BlockingExecutor:
        def __init__(self):
            self.submissions = 0
            self.future = CancellableFuture()

        def submit(self, fn):
            self.submissions += 1
            self.fn = fn
            return self.future

    monkeypatch.setattr(auto_play_loop, "AutoPlayLoop", FakeLoop)
    monkeypatch.setattr(
        action_policy, "recommended_runner_config",
        lambda **kwargs: ("safe-policy", kwargs))
    commands = iter(["resume", "resume", "stop"])
    monkeypatch.setattr(
        full_auto_control, "consume_command", lambda serial: next(commands))
    window = _window(tmp_path)
    window.continuous = True
    window.continuous_caishen = "1w"
    window.auto_executor = BlockingExecutor()

    window._poll_full_auto_control()
    first_loop = window._auto_loop
    window._poll_full_auto_control()

    assert window.auto_executor.submissions == 1
    assert window._auto_loop is first_loop
    assert len(loops) == 1
    assert "不重复提交" in window.full_auto_status.get()

    window._poll_full_auto_control()

    assert window.auto_executor.submissions == 1
    assert len(loops[0].stop_reasons) == 1
    assert window.auto_executor.future.cancel_calls == 1
    assert window._auto_start_pending is False
    assert window._full_auto_wanted is False
    assert window._auto_resume_pending is False


def test_full_auto_click_progresses_lobby_identity_mode_to_takeover(
        monkeypatch, tmp_path):
    """One click remains armed through every asynchronous start gate."""
    window = _window(tmp_path)
    window.continuous = False
    window._lobby_active = False
    phases = []

    window.start = lambda: phases.append("lobby")
    window._start_full_auto()
    assert phases == ["lobby"]
    assert window._full_auto_wanted is True
    assert window._auto_resume_pending is True

    window._lobby_active = True
    window._lobby_in_game_verified = False
    window._lobby_timer = None
    window._lobby_future = SimpleNamespace(
        done=lambda: True,
        result=lambda: SimpleNamespace(
            state="in_game", message="已进入牌局",
            room_cards=8, room_card_status="ok",
            seated=True, terminal=False),
    )

    def wait_for_identity():
        phases.append("identity")
        window._identity_state = "checking"
        window._identity_pending = "follow"

    window.start = wait_for_identity
    window._poll_lobby_probe()
    assert phases == ["lobby", "identity"]
    assert window._lobby_in_game_verified is True
    assert window._auto_resume_pending is True

    window._identity_generation = 1
    window._identity_events = Queue()
    window._identity_events.put((
        1, ("玉姐", "emulator-5556"),
        SimpleNamespace(
            status="match", detail="左下本人昵称一致",
            detected="玉姐", matched=True),
        12,
    ))

    def wait_for_mode():
        phases.append("mode")
        window._check_state = "checking"
        window._check_pending = "follow"

    window.start = wait_for_mode
    window._poll_identity_check()
    assert phases == ["lobby", "identity", "mode"]
    assert window._identity_state == "match"
    assert window._auto_resume_pending is True

    window._check_events = Queue()
    window._check_events.put((
        "tea_2p", "tea_2p", "规则文字确认温州茶2人"))

    def start_follow():
        phases.append("follow")
        window.continuous = True
        window.continuous_caishen = "1w"

    window.start = start_follow
    window._poll_mode_check()
    assert phases == ["lobby", "identity", "mode", "follow"]
    assert window._check_state == "match"
    assert window._auto_resume_pending is True

    monkeypatch.setattr(
        full_auto_control, "consume_command", lambda serial: None)
    def take_over():
        phases.append("takeover")
        window.full_auto_running = True
        window._auto_resume_pending = False

    window._start_full_auto = take_over
    window._poll_full_auto_control()

    assert phases == [
        "lobby", "identity", "mode", "follow", "takeover"]
    assert window._auto_resume_pending is False


def test_pending_handoff_survives_a_temporarily_invalid_binding(
        monkeypatch, tmp_path):
    window = _window(tmp_path)
    window.continuous = True
    window.continuous_caishen = "1w"
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._is_bound = lambda: False
    monkeypatch.setattr(
        full_auto_control, "consume_command", lambda serial: None)

    window._poll_full_auto_control()

    assert window.full_auto_running is False
    assert window._full_auto_wanted is True
    assert window._auto_resume_pending is True
    assert "未绑定" in window.full_auto_status.get()


def test_accepted_waiting_observes_without_second_click_then_reenters_gates(
        tmp_path):
    """Accepted click waits read-only; proof advances bookkeeping once."""
    window = _window(tmp_path)
    window.continuous = False
    window._full_auto_wanted = True
    window._auto_resume_pending = False
    window._round_transition_future = None
    window._lobby_in_game_verified = False
    calls = []
    probes = iter([(False, "transition_table"),
                   (True, "table_dealt")])

    class TransitionExecutor:
        clicks = 1

        @staticmethod
        def _capture():
            calls.append("capture")
            return object()

        @staticmethod
        def _probe(_frame):
            return next(probes)

        @staticmethod
        def _sleep(_seconds):
            calls.append("sleep")

        @staticmethod
        def _report(result, round_no):
            calls.append(("report", result.status, round_no))
            return result

    transition = TransitionExecutor()
    window._round_transition = transition
    window.auto_executor = ImmediateExecutor()
    window.round_no = 4
    window.next_round = lambda: calls.append("next_round")
    window.start = lambda: calls.append("start_gates")

    window._on_round_transition_result(SimpleNamespace(
        status="accepted_waiting",
        reason="下一局按钮与旧结算页已持续消失"))

    assert window._full_auto_wanted is True
    assert window._auto_resume_pending is True
    assert "next_round" not in calls
    assert "start_gates" not in calls
    assert transition.clicks == 1
    assert window.root.delay == 200

    window.root.callback()

    assert calls.count("capture") == 2
    assert calls.count("next_round") == 1
    assert calls.count("start_gates") == 1
    assert transition.clicks == 1
    assert window._lobby_in_game_verified is True
    assert window._auto_resume_pending is True


def test_accepted_waiting_observer_is_bounded_and_does_not_advance():
    captures = []
    result = TeaAssistWindow._observe_accepted_round_transition(
        lambda: captures.append("capture") or object(),
        lambda _frame: (False, "still_transitioning"),
        lambda _seconds: None,
        max_polls=3,
        interval_s=0.05,
    )

    assert result.status == "accepted_waiting_timeout"
    assert result.clicked is False
    assert len(captures) == 3
    assert len(result.evidence["accepted_waiting_observe"]) == 3


def test_accepted_waiting_timeout_schedules_one_next_read_only_segment(
        tmp_path):
    window = _window(tmp_path)
    window.continuous = False
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_future = None
    window._accepted_round_observing = False
    window._accepted_round_observe_timer = None
    window._quitting = False
    window.round_no = 4
    next_round_calls = []
    observed_segments = []

    class TransitionExecutor:
        clicks = 1

        @staticmethod
        def _capture():
            raise AssertionError("the segment double supplies its own result")

        @staticmethod
        def _probe(_frame):
            raise AssertionError("the segment double supplies its own result")

        @staticmethod
        def _sleep(_seconds):
            return None

    transition = TransitionExecutor()
    window._round_transition = transition
    window.auto_executor = ImmediateExecutor()
    window.next_round = lambda: next_round_calls.append("next")

    def observe_segment(*_args, **_kwargs):
        observed_segments.append("segment")
        return SimpleNamespace(
            status="accepted_waiting_timeout",
            reason="本段仍未取得新局正证据")

    window._observe_accepted_round_transition = observe_segment
    window._begin_accepted_round_observation()
    window._begin_accepted_round_observation()

    assert observed_segments == []
    assert window.auto_executor.submissions == 1
    assert window._accepted_round_observing is True

    window.root.callback()  # collect segment timeout, queue exactly one timer

    assert observed_segments == ["segment"]
    assert window._accepted_round_observing is False
    assert window._round_transition_future is None
    assert window._accepted_round_observe_timer == "timer"
    assert window.auto_executor.submissions == 1
    assert next_round_calls == []
    assert transition.clicks == 1
    scheduled_before_duplicate = len(window.root.scheduled)
    window._schedule_accepted_round_observation()
    assert len(window.root.scheduled) == scheduled_before_duplicate

    _delay, resume = window.root.scheduled[-1]
    resume()

    assert window.auto_executor.submissions == 2
    assert window._accepted_round_observing is True
    assert window._accepted_round_observe_timer is None
    assert next_round_calls == []
    assert transition.clicks == 1

    window.root.callback()  # the second segment really runs and captures again

    assert observed_segments == ["segment", "segment"]
    assert window.auto_executor.submissions == 2
    assert window._accepted_round_observing is False
    assert window._accepted_round_observe_timer == "timer"
    assert next_round_calls == []
    assert transition.clicks == 1


def test_stop_revokes_waiting_authority_when_running_future_cannot_cancel(
        monkeypatch, tmp_path):
    window = _window(tmp_path)
    window.continuous = False
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._round_transition_future = None
    window._accepted_round_observing = False
    window._accepted_round_observe_timer = None
    window._accepted_round_observe_generation = 0
    window._accepted_round_future_generation = None
    window._accepted_round_cancel_event = None
    window._quitting = False
    window.round_no = 4
    calls = []

    class UncancellableFuture:
        def __init__(self, fn):
            self.fn = fn
            self.cancel_calls = 0
            self.completed = False
            self.outcome = None

        def done(self):
            return self.completed

        def cancel(self):
            self.cancel_calls += 1
            return False

        def run(self):
            self.outcome = self.fn()
            self.completed = True
            return self.outcome

        def result(self):
            return self.outcome

    class RunningExecutor:
        def submit(self, fn):
            self.future = UncancellableFuture(fn)
            return self.future

    class TransitionExecutor:
        clicks = 1

        @staticmethod
        def _capture():
            calls.append("capture")
            return object()

        @staticmethod
        def _probe(_frame):
            calls.append("positive_evidence")
            return True, "table_dealt"

        @staticmethod
        def _sleep(_seconds):
            calls.append("sleep")

    window._round_transition = TransitionExecutor()
    window.auto_executor = RunningExecutor()
    window.next_round = lambda: calls.append("next_round")
    window.start = lambda: calls.append("start_gates")
    window._start_full_auto = lambda: calls.append("start_bot")
    window._begin_accepted_round_observation()
    future = window.auto_executor.future
    cancel_event = window._accepted_round_cancel_event
    scheduled_poll = window.root.callback
    monkeypatch.setattr(
        full_auto_control, "consume_command", lambda serial: "stop")

    window._poll_full_auto_control()

    assert future.cancel_calls == 1
    assert cancel_event.is_set()
    assert window._full_auto_wanted is False
    assert window._auto_resume_pending is False
    assert window._accepted_round_observing is True
    assert window._round_transition_future is future
    assert window.full_auto_running is False
    assert "全自动跟随中" not in window.full_auto_status.get()
    assert "授权已撤销" in window.full_auto_status.get()

    late_result = future.run()
    assert late_result.status == "accepted_waiting_cancelled"
    scheduled_poll()

    assert calls == []
    assert window._accepted_round_observing is False
    assert window._round_transition_future is None
    assert window._round_transition.clicks == 1
    assert window.full_auto_running is False


def test_stop_cancels_a_queued_waiting_segment(monkeypatch, tmp_path):
    window = _window(tmp_path)
    window._full_auto_wanted = True
    window._auto_resume_pending = True
    window._accepted_round_observing = False
    window._round_transition_future = None
    window._accepted_round_observe_timer = "waiting-timer"
    window._accepted_round_observe_generation = 3
    window._accepted_round_cancel_event = None
    window._quitting = False
    monkeypatch.setattr(
        full_auto_control, "consume_command", lambda serial: "stop")

    window._poll_full_auto_control()

    assert window._accepted_round_observe_timer is None
    assert ("cancel", "waiting-timer") in window.root.scheduled
    assert window._full_auto_wanted is False
    assert window._auto_resume_pending is False
