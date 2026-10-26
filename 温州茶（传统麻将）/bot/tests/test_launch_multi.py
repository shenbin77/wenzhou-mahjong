from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import launch_multi


def test_connected_devices_hides_background_console_on_windows(monkeypatch) -> None:
    captured = {}

    class Done:
        stdout = "List of devices attached\nemulator-5554 device product:x\n"

    def run(*args, **kwargs):
        captured.update(kwargs)
        return Done()

    monkeypatch.setattr(launch_multi.os, "name", "nt")
    monkeypatch.setattr(launch_multi.subprocess, "run", run)

    assert launch_multi.connected_devices("adb.exe") == ["emulator-5554"]
    assert captured["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_start_window_revokes_stale_authority_before_spawn(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        launch_multi, "revoke_pending_command",
        lambda serial: calls.append(("revoke", serial)) or True)
    monkeypatch.setattr(
        launch_multi.subprocess, "Popen",
        lambda *args, **kwargs: calls.append(("spawn", args, kwargs))
        or SimpleNamespace(pid=1300))

    process = launch_multi.start_window("emulator-5556", detached=False)

    assert process.pid == 1300
    assert calls[0] == ("revoke", "emulator-5556")
    assert calls[1][0] == "spawn"


def test_start_window_refuses_spawn_when_revoke_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        launch_multi, "revoke_pending_command", lambda _serial: False)
    monkeypatch.setattr(
        launch_multi.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("spawned with stale resume"))

    with pytest.raises(RuntimeError, match="遗留全自动命令"):
        launch_multi.start_window("emulator-5556", detached=False)
