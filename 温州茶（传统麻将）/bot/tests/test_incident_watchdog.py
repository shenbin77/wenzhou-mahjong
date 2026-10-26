from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import incident_watchdog
from human_input_monitor import ClickTarget
from incident_watchdog import IncidentWatchdog, find_latest_session


def _session(root: Path, name: str, serial: str, stamp: int) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    ledger = folder / "actions.jsonl"
    ledger.write_text(json.dumps({
        "started_at": "2026-10-26T18:00:00+08:00",
        "serial": serial, "mode": "tea_2p",
    }) + "\n", encoding="utf-8")
    os.utime(ledger, (stamp, stamp))
    return folder


def test_default_window_mapping_refresh_is_not_a_one_second_process_loop(
        tmp_path: Path) -> None:
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe")
    assert watcher.input_resolve_interval_s >= 5.0


def test_find_latest_session_uses_verified_header_serial(tmp_path: Path) -> None:
    old = _session(tmp_path, "old", "emulator-5554", 100)
    latest = _session(tmp_path, "latest", "emulator-5554", 200)
    _session(tmp_path, "other", "emulator-5556", 300)
    assert find_latest_session(tmp_path, "emulator-5554") == latest
    assert find_latest_session(tmp_path, "emulator-5556").name == "other"
    assert old != latest


def test_process_once_only_reviews_changed_ledger(monkeypatch, tmp_path: Path) -> None:
    session = _session(tmp_path / "logs", "current", "emulator-5554", 10)
    calls = []

    def review(path, **kwargs):
        calls.append((path, kwargs))
        return {"issue_count": 1, "incident": {"status": "open"}}

    monkeypatch.setattr(incident_watchdog, "review_session", review)
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe")
    first = watcher.process_once()
    second = watcher.process_once()
    assert first[0]["status"] == "reviewed"
    assert second[0]["status"] == "unchanged"
    assert len(calls) == 1
    assert calls[0][0] == session
    status = json.loads((tmp_path / "inbox" / "watchdog_status.json")
                        .read_text(encoding="utf-8"))
    assert status["outcomes"][0]["status"] == "unchanged"


def test_failed_review_does_not_ack_signature_and_retries(monkeypatch,
                                                           tmp_path: Path) -> None:
    _session(tmp_path / "logs", "current", "emulator-5554", 10)
    calls = []

    def review(path, **kwargs):
        calls.append((path, kwargs))
        if len(calls) == 1:
            raise ValueError("transient partial jsonl")
        return {"issue_count": 0, "incident": None}

    monkeypatch.setattr(incident_watchdog, "review_session", review)
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe")

    first = watcher.process_once()
    second = watcher.process_once()
    third = watcher.process_once()

    assert first[0]["status"] == "review_failed"
    assert first[0]["error_type"] == "ValueError"
    assert second[0]["status"] == "reviewed"
    assert third[0]["status"] == "unchanged"
    assert len(calls) == 2


def test_incident_write_failure_does_not_ack_signature(monkeypatch,
                                                        tmp_path: Path) -> None:
    _session(tmp_path / "logs", "current", "emulator-5554", 10)
    calls = []

    def review(path, **kwargs):
        calls.append((path, kwargs))
        status = "write_failed" if len(calls) == 1 else "open"
        incident = {"status": status}
        return {"issue_count": 1, "incident": incident,
                "incidents": [incident]}

    monkeypatch.setattr(incident_watchdog, "review_session", review)
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe")

    first = watcher.process_once()
    second = watcher.process_once()
    third = watcher.process_once()

    assert first[0] == {
        "serial": "emulator-5554", "status": "review_failed",
        "error_type": "IncidentWriteFailed"}
    assert second[0]["status"] == "reviewed"
    assert third[0]["status"] == "unchanged"
    assert len(calls) == 2


def test_input_monitor_rebinds_after_hwnd_is_destroyed(monkeypatch,
                                                        tmp_path: Path) -> None:
    rebound = threading.Event()
    resolve_calls = []

    def resolve(serial, _ldconsole):
        resolve_calls.append(serial)
        hwnd = 100 if len(resolve_calls) == 1 else 200
        return ClickTarget(hwnd, serial, True, "test_mapping")

    class FakeMonitor:
        poll_s = 0.001

        def __init__(self, target, _output):
            self.target = target

        def target_valid(self):
            return self.target.hwnd != 100

        def poll_once(self):
            if self.target.hwnd == 200:
                rebound.set()

    monkeypatch.setattr(incident_watchdog, "resolve_ldplayer_target", resolve)
    monkeypatch.setattr(incident_watchdog, "HumanInputMonitor", FakeMonitor)
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe", input_resolve_interval_s=0.1)

    watcher.start_input_monitors()
    assert rebound.wait(1.0)
    assert watcher._monitor_status["emulator-5554"] == {
        "status": "observing", "hwnd": 200,
        "mapping_basis": "test_mapping"}
    watcher.stop()
    assert all(not thread.is_alive() for thread in watcher._monitor_threads)


def test_input_monitor_survives_write_failure_and_later_records(
        monkeypatch, tmp_path: Path) -> None:
    recorded = threading.Event()
    instances = []
    output = tmp_path / "inbox" / "human-input" / "emulator-5554.jsonl"

    monkeypatch.setattr(
        incident_watchdog, "resolve_ldplayer_target",
        lambda serial, _ldconsole: ClickTarget(
            100, serial, True, "test_mapping"))

    class FakeMonitor:
        poll_s = 0.001

        def __init__(self, target, path):
            self.target = target
            self.path = Path(path)
            self.instance = len(instances)
            instances.append(self)

        def target_valid(self):
            return True

        def poll_once(self):
            if self.instance == 0:
                raise PermissionError("temporary sharing violation")
            if not recorded.is_set():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    "recorded-after-recovery\n", encoding="utf-8")
                recorded.set()

    monkeypatch.setattr(incident_watchdog, "HumanInputMonitor", FakeMonitor)
    watcher = IncidentWatchdog(
        tmp_path / "logs", tmp_path / "inbox", ["emulator-5554"],
        ldconsole="ldconsole.exe", input_resolve_interval_s=0.1)

    watcher.start_input_monitors()
    assert recorded.wait(1.0)
    assert len(instances) >= 2
    assert output.read_text(encoding="utf-8") == "recorded-after-recovery\n"
    assert watcher._monitor_status["emulator-5554"]["status"] == "observing"
    assert any(thread.is_alive() for thread in watcher._monitor_threads)
    watcher.stop()
    assert all(not thread.is_alive() for thread in watcher._monitor_threads)

