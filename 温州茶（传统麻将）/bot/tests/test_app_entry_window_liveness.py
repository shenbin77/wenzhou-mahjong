"""A recorded window PID is only a hint: it must be proven alive.

The launcher refuses to open a second window for a serial it believes is
already covered.  Killing a window leaves its PID behind in whatever the
caller remembers, and Windows both recycles PIDs and keeps exited processes
listed while a handle stays open, so the "已打开" judgement must re-open and
re-read every candidate instead of trusting the record.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from app_entry import existing_window_pids, pid_owns_assist_window

CHILD_SOURCE = "import time\ntime.sleep(60)\n"


def _unused_pid() -> int:
    import psutil

    candidate = max(psutil.pids() or [1000]) + 997
    for _ in range(2000):
        if not psutil.pid_exists(candidate):
            return candidate
        candidate += 1
    pytest.skip("找不到可确认未被占用的 PID")


class _FakeScanRow:
    """One raw process-table row: a PID plus an untrustworthy command line."""

    def __init__(self, pid: int, cmdline=()):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": list(cmdline)}


def _spawn(tmp_path, *args):
    return subprocess.Popen([sys.executable, "-u", *args],
                            cwd=str(tmp_path))


def _reap(child) -> None:
    child.kill()
    try:
        child.wait(timeout=15)
    finally:
        # Windows reaps asynchronously; the assertion below must see it gone.
        import psutil

        deadline = time.time() + 15
        while time.time() < deadline and psutil.pid_exists(child.pid):
            time.sleep(0.05)


def test_stale_pid_record_is_not_an_open_window(monkeypatch, tmp_path):
    """A record pointing at a PID that is not alive means no open window."""
    phantom = _unused_pid()
    record = tmp_path / "window_pids.json"
    record.write_text(json.dumps({"emulator-5556": phantom}), encoding="utf-8")
    saved = json.loads(record.read_text(encoding="utf-8"))["emulator-5556"]

    assert saved == phantom
    assert pid_owns_assist_window(saved) is False

    # Even if the process table still hands back that PID with a matching
    # command line, the judgement must not call it an open window.
    row = _FakeScanRow(saved, [sys.executable, "assist_window.py",
                              "--serial", "emulator-5556"])
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: [row])
    assert existing_window_pids("emulator-5556") == {}


def test_killed_window_pid_is_not_an_open_window(tmp_path):
    """A window killed by the operator stops counting as "已打开"."""
    script = tmp_path / "assist_window.py"
    script.write_text(CHILD_SOURCE, encoding="utf-8")
    child = _spawn(tmp_path, str(script), "--serial", "emulator-5556")
    try:
        assert pid_owns_assist_window(child.pid) is True
    finally:
        _reap(child)
    assert pid_owns_assist_window(child.pid) is False


def test_foreign_live_process_is_not_an_open_window(tmp_path):
    """An unrelated live process must never block a window."""
    child = _spawn(tmp_path, "-c", CHILD_SOURCE)
    try:
        assert pid_owns_assist_window(child.pid) is False
    finally:
        _reap(child)


def test_shell_wrapper_carrying_the_launch_line_is_not_a_window():
    """A shell holds the launch command line without ever opening a window.

    Launching from a terminal leaves ``cmd.exe``/``bash``/``env`` alive with
    ``… python.exe -u assist_window.py --serial emulator-5556`` as its own
    command line.  Counting it as an open window is what made every later
    launch answer "现有辅助器 PID … 已打开；不重复运行" while no window ran.
    """
    if sys.platform != "win32":
        pytest.skip("外壳包装进程只在 Windows 上出现")
    child = subprocess.Popen(
        ["cmd.exe", "/c", "rem", "assist_window.py", "--serial", "emulator-5599",
         "&", "timeout", "/t", "20", "/nobreak", ">nul"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert pid_owns_assist_window(child.pid) is False
        assert child.pid not in existing_window_pids("emulator-5556").values()
    finally:
        child.kill()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def test_live_window_child_is_counted(tmp_path):
    """A really running assist window still maps to its own serial."""
    script = tmp_path / "assist_window.py"
    script.write_text(CHILD_SOURCE, encoding="utf-8")
    child = _spawn(tmp_path, str(script), "--serial", "emulator-5599")
    try:
        assert pid_owns_assist_window(child.pid) is True
        assert existing_window_pids("emulator-5556").get("emulator-5599") == child.pid
    finally:
        _reap(child)
