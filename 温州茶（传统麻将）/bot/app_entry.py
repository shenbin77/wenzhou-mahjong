"""Choose a free LDPlayer device when the *same* assist app is reopened.

Never opens game tables or sends emulator taps. Can focus an existing UI.
"""
from __future__ import annotations

import os
from pathlib import Path

from instance_profile import safe_serial


def existing_window_pids(default_serial: str, *, exclude_pid: int | None = None) -> dict[str, int]:
    import psutil
    owned: dict[str, int] = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if int(proc.pid) == (exclude_pid or os.getpid()):
                continue
            argv = proc.info.get("cmdline") or []
            if not any(Path(str(a)).name.lower() == "assist_window.py" for a in argv[1:]):
                continue
            if "--serial" in argv and argv.index("--serial") + 1 < len(argv):
                serial = safe_serial(argv[argv.index("--serial") + 1])
            else:
                serial = default_serial
            owned[serial] = int(proc.pid)
        except (ValueError, IndexError, OSError, psutil.Error):
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
