"""Cross-process simulator authority and last-mile tap revocation (zero ADB)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from serial_lease import SerialBusy, guard_live_run, hold_serial
from tap_executor import TapAborted, TapExecutor


def test_single_process_reentrant_operator_is_rejected():
    with hold_serial("emulator-test-lease"):
        with pytest.raises(SerialBusy):
            with hold_serial("emulator-test-lease"):
                pass
    with hold_serial("emulator-test-lease"):
        pass


def test_cross_process_conflict_has_no_device_side_effects():
    child_code = (
        "from serial_lease import hold_serial,SerialBusy\n"
        "try:\n"
        " with hold_serial('emulator-test-cross'):\n"
        "  print('UNEXPECTED_ACCESS')\n"
        "except SerialBusy:\n"
        " print('BUSY')\n"
    )
    with hold_serial("emulator-test-cross"):
        child = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "BUSY"


def test_dry_run_has_no_live_lease():
    class Dummy:
        enabled = False
        serial = "emulator-test-decorator"

        @guard_live_run
        def run(self):
            return "dry"

    with hold_serial("emulator-test-decorator"):
        assert Dummy().run() == "dry"


def test_live_run_locks_only_during_call():
    class Dummy:
        enabled = True
        serial = "emulator-test-decorator-live"

        @guard_live_run
        def run(self):
            with pytest.raises(SerialBusy):
                with hold_serial(self.serial):
                    pass
            return "ok"

    assert Dummy().run() == "ok"
    with hold_serial(Dummy.serial):
        pass


def test_last_mile_revocation_refuses_every_click(monkeypatch):
    tap = TapExecutor(enabled=True, serial="emulator-test")
    tap.authorization_check = lambda: False
    monkeypatch.setattr(tap, "_adb", lambda *args, **kwargs: pytest.fail("ADB tap must not execute"))
    with pytest.raises(TapAborted, match="权限已撤销"):
        tap._tap(1000, 820)


def test_broken_authority_checker_fails_closed(monkeypatch):
    tap = TapExecutor(enabled=True, serial="emulator-test")
    tap.authorization_check = lambda: 1 / 0
    monkeypatch.setattr(tap, "_adb", lambda *args, **kwargs: pytest.fail("ADB must not execute"))
    with pytest.raises(TapAborted, match="授权检查失败"):
        tap._tap(1000, 820)
