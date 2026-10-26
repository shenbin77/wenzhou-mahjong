from __future__ import annotations

import subprocess

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
