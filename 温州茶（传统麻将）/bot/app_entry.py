"""Choose a free LDPlayer device when the *same* assist app is reopened.

Never opens game tables or sends emulator taps. Can focus an existing UI.
"""
from __future__ import annotations

import os
from pathlib import Path

from instance_profile import safe_serial

ASSIST_SCRIPT_NAME = "assist_window.py"


def _runs_assist_window(argv: object) -> bool:
    """Whether one command line really launches this assist app."""
    try:
        args = list(argv or ())[1:]
    except TypeError:
        return False
    return any(Path(str(arg)).name.lower() == ASSIST_SCRIPT_NAME for arg in args)


def _is_python_interpreter(name: str) -> bool:
    """Only an interpreter runs this app; shells merely carry its name.

    A ``cmd.exe``/``bash``/``env`` wrapper keeps the whole launch command
    (``… python.exe -u assist_window.py --serial emulator-5556``) in its own
    command line, so matching on the command line alone reports a window that
    was never opened and blocks every later launch with "已打开".  The window
    itself is ``python.exe`` or ``pythonw.exe`` — never judged by name alone,
    only as the interpreter that is executing the script.
    """
    return Path(str(name or "").strip().strip('"')).name.lower().startswith("python")


def verified_window_argv(pid: object) -> list[str] | None:
    """Return ``pid``'s command line, but only for a live assist window.

    A recorded PID is a hint, never proof.  Windows recycles PIDs, and an
    exited process stays listed as long as some handle is still open, so a
    stale record would otherwise keep reporting "已打开" forever and block
    every new window.  ``os.kill(pid, 0)`` is deliberately not used: its
    Windows semantics do not mean "this process is alive".
    """
    import psutil
    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return None
    if candidate <= 0 or not psutil.pid_exists(candidate):
        return None
    try:
        proc = psutil.Process(candidate)
        with proc.oneshot():
            # An exited process with an open handle is not an operator window.
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            name = proc.name()
            argv = [str(arg) for arg in (proc.cmdline() or ())]
    except (psutil.Error, OSError, ValueError):
        # Gone between the two calls, or not ours to inspect: treat as absent.
        return None
    if not _is_python_interpreter(name):
        return None
    return argv if _runs_assist_window(argv) else None


def pid_owns_assist_window(pid: object) -> bool:
    """True only when ``pid`` is alive *and* runs this app right now."""
    return verified_window_argv(pid) is not None


def _argv_serial(argv: list[str], default_serial: str) -> str:
    if "--serial" in argv and argv.index("--serial") + 1 < len(argv):
        return safe_serial(argv[argv.index("--serial") + 1])
    return safe_serial(default_serial)


def existing_window_pids(default_serial: str, *, exclude_pid: int | None = None) -> dict[str, int]:
    """Map each serial to one *verified* live assist window's PID.

    The process table only supplies raw PID candidates; every candidate is
    re-opened and re-read before it is trusted, so dead PIDs and unrelated
    processes that merely mention this script are dropped instead of blocking
    a new window.
    """
    import psutil
    self_pid = os.getpid()
    owned: dict[str, int] = {}
    for proc in psutil.process_iter(["pid"]):
        try:
            pid = int(proc.info.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == self_pid or pid == exclude_pid:
            continue
        argv = verified_window_argv(pid)
        if argv is None:
            continue
        try:
            owned[_argv_serial(argv, default_serial)] = pid
        except (ValueError, IndexError):
            continue
    return owned


def choose_device(devices: list[str], occupied: dict[str, int],
                  requested: str | None = None) -> tuple[str | None, int | None]:
    if requested:
        serial = safe_serial(requested)
        return serial, occupied.get(serial)
    for candidate in devices:
        if candidate not in occupied:
            return candidate, None
    return (None, occupied.get(devices[0])) if devices else (None, None)


def activate_window(pid: int) -> bool:
    """Best-effort Win32 focus; does not interfere with other programs."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, unused):
        id_ = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(id_))
        if id_.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return False
    user32.ShowWindow(found[0], 9)  # Restore a minimized existing window.
    user32.SetForegroundWindow(found[0])
    return True
