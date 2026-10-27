"""Run the real outer scheduler with offline clocks; never touch an emulator."""
from __future__ import annotations
import sys
import threading
import time

import pytest

from auto_play_loop import AutoPlayLoop


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleep_calls = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        self.now += seconds


class VirtualEvent:
    def __init__(self, clock):
        self.clock = clock
        self.waits = []
        self.signaled = False
    def wait(self, seconds):
        self.waits.append(seconds)
        if not self.signaled:
            self.clock.now += seconds
        return self.signaled
    def set(self):
        self.signaled = True


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("scheduler tests must not perform device/process I/O")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def loop_for(tmp_path, clock, **kwargs):
    loop = AutoPlayLoop(
        enabled=False, adb_path=sys.executable, serial="offline-period",
        session_dir=tmp_path / "session", interval_s=1.2,
        duration_s=kwargs.pop("duration_s", 0), max_actions=kwargs.pop("max_actions", 3),
        clock_fn=clock, sleep_fn=clock.sleep, **kwargs)
    loop.check_gates = lambda: None
    loop._pacing_wakeup = VirtualEvent(clock)
    return loop


def replay(loop, clock, durations, *, verification_wait=0.0):
    starts = []
    source = iter(durations)
    def tick():
        starts.append(clock.now)
        clock.now += next(source)
        if verification_wait:
            loop._sleep(verification_wait)
        loop.actions_done += 1
        return {"event": "offline_replayed_tick"}
    loop.tick = tick
    summary = loop.run()
    return starts, summary


@pytest.mark.parametrize(("durations", "expected", "waits"), [
    ([4.2, 4.2, 4.2], [0.0, 4.2, 8.4], []),
    ([.85, .85, .85], [0.0, 2.0, 4.0], [1.15, 1.15]),
    ([4.2, .85, 4.2], [0.0, 4.2, 6.2], [1.15]),
])
def test_target_period_removes_tail_delay_without_catchup_bursts(
        tmp_path, durations, expected, waits):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=2.0)
    starts, summary = replay(loop, clock, durations)
    assert starts == pytest.approx(expected)
    assert loop._pacing_wakeup.waits == pytest.approx(waits)
    assert clock.sleep_calls == []
    assert summary["stop_gate"] == "max_actions"


@pytest.mark.parametrize(("durations", "expected"), [
    ([4.2, 4.2, 4.2], [0.0, 5.4, 10.8]),
    ([.85, .85, .85], [0.0, 2.05, 4.1]),
])
def test_none_keeps_original_interval_semantics(tmp_path, durations, expected):
    clock = Clock()
    loop = loop_for(tmp_path, clock)
    starts, _ = replay(loop, clock, durations)
    assert starts == pytest.approx(expected)
    assert clock.sleep_calls == [1.2, 1.2]
    assert loop._pacing_wakeup.waits == []


def test_target_pacing_preserves_internal_verification_sleep(tmp_path):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=2.0)
    starts, _ = replay(loop, clock, [1.7, 1.7, 1.7], verification_wait=.5)
    assert starts == pytest.approx([0, 2.2, 4.4])
    assert clock.sleep_calls == [.5, .5, .5]
    assert loop._pacing_wakeup.waits == []


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_invalid_numeric_period_does_not_create_session(tmp_path, value):
    with pytest.raises(ValueError, match="target_period_s"):
        loop_for(tmp_path, Clock(), target_period_s=value)
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("value", [True, False, "2", object()])
def test_non_numeric_period_does_not_create_session(tmp_path, value):
    with pytest.raises(TypeError, match="target_period_s"):
        loop_for(tmp_path, Clock(), target_period_s=value)
    assert not (tmp_path / "session").exists()


def test_preexisting_stop_is_never_cleared(tmp_path):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=2.0)
    loop.request_stop("already revoked")
    starts, summary = replay(loop, clock, [])
    assert starts == []
    assert summary["stop_reason"] == "already revoked"
    assert loop._pacing_wakeup.waits == []


def test_duration_deadline_limits_wait_and_prevents_extra_tick(tmp_path):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=2.0, duration_s=1.5, max_actions=99)
    starts, summary = replay(loop, clock, [.25])
    assert starts == [0.0]
    assert clock.now == pytest.approx(1.5)
    assert loop._pacing_wakeup.waits == [1.25]
    assert summary["stop_gate"] == "duration"


@pytest.mark.parametrize("ending", ["stopped", "requested", "exception"])
def test_terminal_tick_never_enters_pacing_wait(tmp_path, ending):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=2.0)
    def tick():
        if ending == "stopped":
            loop._stop("offline_terminal", "finished")
        elif ending == "requested":
            loop.request_stop("revoked during tick")
        else:
            raise RuntimeError("offline failed tick")
    loop.tick = tick
    if ending == "exception":
        with pytest.raises(RuntimeError, match="offline failed tick"):
            loop.run()
        assert loop.stop_gate == "unhandled_exception"
    else:
        loop.run()
    assert loop._pacing_wakeup.waits == []


def test_request_stop_interrupts_real_event_wait_before_second_tick(tmp_path):
    clock = Clock()
    loop = loop_for(tmp_path, clock, target_period_s=20.0)
    real_event = threading.Event()
    entered = threading.Event()
    class ObservedEvent:
        def wait(self, seconds):
            entered.set()
            return real_event.wait(seconds)
        def set(self):
            real_event.set()
    loop._pacing_wakeup = ObservedEvent()
    ticks = []
    loop.tick = lambda: ticks.append("tick")
    results = []
    errors = []
    def run():
        try:
            results.append(loop.run())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(2.0)
        started = time.monotonic()
        loop.request_stop("test stop while waiting")
        worker.join(1.0)
        assert not worker.is_alive()
        assert time.monotonic() - started < 1.0
        assert ticks == ["tick"]
        assert errors == []
        assert results[0]["stop_gate"] == "user_stop"
    finally:
        real_event.set()
        worker.join(1.0)
