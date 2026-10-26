from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import human_input_monitor
from human_input_monitor import (
    ClickTarget,
    HumanInputMonitor,
    associate_human_click,
    build_human_click_event,
    load_human_clicks,
    parse_ldplayer_list2,
    resolve_ldplayer_target,
)


def test_build_event_is_strictly_target_window_scoped() -> None:
    target = ClickTarget(100, "emulator-5554", True)
    assert build_human_click_event(
        target, clicked_root_hwnd=999, screen_xy=(10, 20),
        client_xy=(1, 2), window_title="其它") is None
    event = build_human_click_event(
        target, clicked_root_hwnd=100, screen_xy=(110, 220),
        client_xy=(10, 20), window_title="雷电模拟器",
        ts="2026-10-26T18:00:00+08:00")
    assert event == {
        "event": "human_click_observed",
        "ts": "2026-10-26T18:00:00+08:00",
        "hwnd": 100,
        "window_title": "雷电模拟器",
        "screen_xy": [110, 220],
        "client_xy": [10, 20],
        "serial": "emulator-5554",
        "mapping_status": "verified",
        "mapping_basis": "caller_unverified",
        "scope": "target_window_only",
        "input": "mouse_left_down",
    }


def test_unverified_mapping_stays_unknown_even_with_serial_text() -> None:
    event = build_human_click_event(
        ClickTarget(100, "emulator-5554", False), clicked_root_hwnd=100,
        screen_xy=(1, 2), client_xy=(1, 2), window_title="雷电")
    assert event["serial"] is None
    assert event["mapping_status"] == "unknown"


def test_associate_requires_verified_same_serial_and_time_window() -> None:
    clicks = [
        {"event": "human_click_observed", "mapping_status": "unknown",
         "serial": None, "ts": "2026-10-26T18:00:09+08:00"},
        {"event": "human_click_observed", "mapping_status": "verified",
         "serial": "emulator-5556", "ts": "2026-10-26T18:00:09+08:00"},
        {"event": "human_click_observed", "mapping_status": "verified",
         "serial": "emulator-5554", "ts": "2026-10-26T18:00:09+08:00",
         "hwnd": 100, "screen_xy": [11, 22], "client_xy": [1, 2]},
    ]
    hit = associate_human_click(
        "2026-10-26T18:00:10+08:00", clicks, "emulator-5554")
    assert hit["serial"] == "emulator-5554"
    assert hit["delta_to_progress_s"] == 1.0
    assert associate_human_click(
        "2026-10-26T18:00:20+08:00", clicks, "emulator-5554") is None


class _Api:
    def __init__(self):
        self.down = False
        self.root = 100
        self.valid = True

    def left_down(self):
        return self.down

    def cursor(self):
        return 110, 220

    def root_at(self, _point):
        return self.root

    def client(self, _hwnd, _point):
        return 10, 20

    def title(self, _hwnd):
        return "雷电模拟器"

    def is_window(self, _hwnd):
        return self.valid


def test_monitor_records_rising_edge_once_and_never_other_window(tmp_path: Path) -> None:
    api = _Api()
    path = tmp_path / "human_input.jsonl"
    monitor = HumanInputMonitor(
        ClickTarget(100, "emulator-5554", True), path, api=api)
    assert monitor.poll_once() is None
    api.down = True
    assert monitor.poll_once()["serial"] == "emulator-5554"
    assert monitor.poll_once() is None  # held button is not another click
    api.down = False
    assert monitor.poll_once() is None
    api.root = 999
    api.down = True
    assert monitor.poll_once() is None
    rows = load_human_clicks(path)
    assert len(rows) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["scope"] == "target_window_only"


def test_monitor_stops_observing_destroyed_target(tmp_path: Path) -> None:
    api = _Api()
    monitor = HumanInputMonitor(
        ClickTarget(100, "emulator-5554", True),
        tmp_path / "human_input.jsonl", api=api)
    api.down = True
    api.valid = False
    assert monitor.target_valid() is False
    assert monitor.poll_once() is None
    assert not monitor.output.exists()


def test_append_retries_transient_open_failure_once(monkeypatch,
                                                     tmp_path: Path) -> None:
    api = _Api()
    api.down = True
    output = tmp_path / "human_input.jsonl"
    real_open = os.open
    attempts = []

    def flaky_open(path, flags, mode=0o777):
        attempts.append(Path(path))
        if len(attempts) == 1:
            raise PermissionError("temporary sharing violation")
        return real_open(path, flags, mode)

    monkeypatch.setattr(human_input_monitor.os, "open", flaky_open)
    monitor = HumanInputMonitor(
        ClickTarget(100, "emulator-5554", True), output, api=api,
        append_retry_s=0)

    event = monitor.poll_once()

    assert event["serial"] == "emulator-5554"
    assert len(attempts) == 2
    assert len(load_human_clicks(output)) == 1


def test_ldconsole_mapping_requires_exact_default_port_formula(monkeypatch) -> None:
    raw = (b"0,LDPlayer,331994,331996,1,25972,1844,1600,900,240\r\n"
           b"1,LDPlayer-1,331880,266436,1,2824,20700,1600,900,240\r\n")
    rows = parse_ldplayer_list2(raw)
    assert rows[1]["index"] == 1 and rows[1]["hwnd"] == 331880

    class Done:
        stdout = raw

    monkeypatch.setattr("human_input_monitor.subprocess.run",
                        lambda *_args, **_kwargs: Done())
    target = resolve_ldplayer_target("emulator-5556", "ldconsole.exe")
    assert target == ClickTarget(
        331880, "emulator-5556", True, "ldconsole_list2_index_port")
    assert resolve_ldplayer_target("127.0.0.1:5556", "ldconsole.exe") is None
    assert resolve_ldplayer_target("emulator-5555", "ldconsole.exe") is None


def test_ldconsole_mapping_hides_background_console_on_windows(monkeypatch) -> None:
    captured = {}

    class Done:
        stdout = b"0,LDPlayer,331994,331996,1,25972,1844,1600,900,240\r\n"

    def run(*args, **kwargs):
        captured.update(kwargs)
        return Done()

    monkeypatch.setattr("human_input_monitor.os.name", "nt")
    monkeypatch.setattr("human_input_monitor.subprocess.run", run)
    resolve_ldplayer_target("emulator-5554", "ldconsole.exe")

    assert captured["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000)

