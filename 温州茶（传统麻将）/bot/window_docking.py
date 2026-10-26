"""Safe magnetic docking beside an LDPlayer window.

The assistant remains an independent top-level window.  This module never
reparents or embeds LDPlayer (``SetParent`` is intentionally absent), so the
emulator's renderer, input routing, pop-ups and ADB screenshot coordinates stay
untouched.

All geometry comes from the same Win32 coordinate space.  Windows therefore
applies the process' DPI virtualization consistently to the emulator rect, the
assistant rect and the monitor work area; no guessed scale factor is applied.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

try:  # pragma: no cover - import availability is exercised through helpers
    import psutil
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:  # keep non-Windows/offline imports fail-closed
    psutil = None
    win32api = None
    win32con = None
    win32gui = None
    win32process = None


DEFAULT_GAP = 8
MANUAL_DETACH_TOLERANCE = 24


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass(frozen=True)
class DockTarget:
    hwnd: int
    rect: Rect
    work_area: Rect
    minimized: bool = False
    pid: int = 0


@dataclass(frozen=True)
class DockPlacement:
    x: int
    y: int
    side: str


def serial_to_ldplayer_index(serial: str) -> int | None:
    """Map LDPlayer's standard ADB serial to its multi-instance index."""
    match = re.fullmatch(r"emulator-(\d+)", str(serial or "").strip())
    if match is None:
        return None
    port = int(match.group(1))
    if port < 5554 or (port - 5554) % 2:
        return None
    return (port - 5554) // 2


def _command_index(command: list[str] | tuple[str, ...]) -> int | None:
    text = " ".join(str(part) for part in command)
    match = re.search(r"(?:^|\s)index=(\d+)(?:\||\s|$)", text)
    return int(match.group(1)) if match else None


def _clamp_axis(value: int, size: int, low: int, high: int) -> int:
    return min(max(int(value), int(low)), max(int(low), int(high) - int(size)))


def choose_dock_placement(
        emulator: Rect, assistant_size: tuple[int, int], work_area: Rect,
        *, gap: int = DEFAULT_GAP) -> DockPlacement | None:
    """Place right, below, left, then above; never overlap.

    ``None`` is returned if neither side can contain the whole assistant.  A
    missing placement is safer than covering the game or pushing controls off
    screen.
    """
    width, height = (max(1, int(assistant_size[0])),
                     max(1, int(assistant_size[1])))
    gap = max(0, int(gap))

    right_x = emulator.right + gap
    if (right_x + width <= work_area.right
            and height <= work_area.height):
        y = _clamp_axis(emulator.top, height, work_area.top, work_area.bottom)
        return DockPlacement(right_x, y, "right")

    bottom_y = emulator.bottom + gap
    if (bottom_y + height <= work_area.bottom
            and width <= work_area.width):
        x = _clamp_axis(emulator.left, width, work_area.left, work_area.right)
        return DockPlacement(x, bottom_y, "bottom")

    left_x = emulator.left - gap - width
    if left_x >= work_area.left and height <= work_area.height:
        y = _clamp_axis(emulator.top, height, work_area.top, work_area.bottom)
        return DockPlacement(left_x, y, "left")

    top_y = emulator.top - gap - height
    if top_y >= work_area.top and width <= work_area.width:
        x = _clamp_axis(emulator.left, width, work_area.left, work_area.right)
        return DockPlacement(x, top_y, "top")
    return None


def find_ldplayer_window(serial: str) -> DockTarget | None:
    """Find the visible top-level ``dnplayer.exe`` window for ``serial``."""
    wanted = serial_to_ldplayer_index(serial)
    if (wanted is None or psutil is None or win32gui is None
            or win32process is None or win32api is None or win32con is None):
        return None

    matches: list[tuple[int, int, int]] = []

    def collect(hwnd, _unused) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            if process.name().lower() != "dnplayer.exe":
                return True
            if _command_index(process.cmdline()) != wanted:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
            if area > 10_000:
                matches.append((area, int(hwnd), int(pid)))
        except Exception:  # noqa: BLE001 - one inaccessible window is skipped
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001 - optional UX must fail closed
        return None
    if not matches:
        return None
    _area, hwnd, pid = max(matches)
    try:
        rect = Rect(*map(int, win32gui.GetWindowRect(hwnd)))
        monitor = win32api.MonitorFromWindow(
            hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        work_area = Rect(*map(int, info["Work"]))
        return DockTarget(
            hwnd=hwnd, rect=rect, work_area=work_area,
            minimized=bool(win32gui.IsIconic(hwnd)),
            pid=pid,
        )
    except Exception:  # noqa: BLE001 - disappearing emulator is normal
        return None


def _refresh_cached_target(target: DockTarget) -> DockTarget | None:
    """Refresh geometry only; never read a process command line on Tk."""
    if (win32gui is None or win32process is None or win32api is None
            or win32con is None):
        return None
    try:
        if not win32gui.IsWindow(target.hwnd):
            return None
        _thread_id, pid = win32process.GetWindowThreadProcessId(target.hwnd)
        if target.pid and int(pid) != target.pid:
            return None
        rect = Rect(*map(int, win32gui.GetWindowRect(target.hwnd)))
        monitor = win32api.MonitorFromWindow(
            target.hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        return DockTarget(
            hwnd=target.hwnd,
            rect=rect,
            work_area=Rect(*map(int, info["Work"])),
            minimized=bool(win32gui.IsIconic(target.hwnd)),
            pid=int(pid),
        )
    except Exception:  # noqa: BLE001 - disappearing window is normal
        return None


class AsyncDockTargetReader:
    """Resolve serial→HWND off the Tk thread, then read geometry cheaply."""

    def __init__(
        self,
        *,
        resolver: Callable[[str], DockTarget | None] = find_ldplayer_window,
        refresher: Callable[[DockTarget], DockTarget | None] =
            _refresh_cached_target,
        retry_s: float = 1.0,
    ) -> None:
        self.resolver = resolver
        self.refresher = refresher
        self.retry_s = max(0.1, float(retry_s))
        self._lock = threading.Lock()
        self._serial: str | None = None
        self._target: DockTarget | None = None
        self._resolving = False
        self._retry_at = 0.0

    def _resolve(self, serial: str) -> None:
        try:
            target = self.resolver(serial)
        except Exception:  # noqa: BLE001 - optional UX remains fail-closed
            target = None
        with self._lock:
            if self._serial == serial:
                self._target = target
                self._retry_at = (0.0 if target is not None
                                  else time.monotonic() + self.retry_s)
            self._resolving = False

    def __call__(self, serial: str) -> DockTarget | None:
        value = str(serial)
        with self._lock:
            if self._serial != value:
                self._serial = value
                self._target = None
                self._retry_at = 0.0
            cached = self._target
        if cached is not None:
            refreshed = self.refresher(cached)
            if refreshed is not None:
                with self._lock:
                    if self._serial == value:
                        self._target = refreshed
                return refreshed
            with self._lock:
                if self._serial == value:
                    self._target = None

        with self._lock:
            now = time.monotonic()
            if self._resolving or now < self._retry_at:
                return None
            self._resolving = True
        threading.Thread(
            target=self._resolve, args=(value,), daemon=True,
            name=f"mahjong-dock-resolve-{value}").start()
        return None


class DockHost(Protocol):
    def state(self) -> str: ...
    def update_idletasks(self) -> None: ...
    def winfo_width(self) -> int: ...
    def winfo_height(self) -> int: ...
    def winfo_x(self) -> int: ...
    def winfo_y(self) -> int: ...
    def geometry(self, value: str) -> None: ...
    def iconify(self) -> None: ...
    def deiconify(self) -> None: ...


class DockFollower:
    """Small state machine that follows one emulator without fighting users."""

    def __init__(
            self, host: DockHost, serial: str, *,
            target_reader: Callable[[str], DockTarget | None] | None = None,
            on_manual_detach: Callable[[], None] | None = None,
            gap: int = DEFAULT_GAP,
            tolerance: int = MANUAL_DETACH_TOLERANCE):
        self.host = host
        self.serial = serial
        self.target_reader = target_reader or AsyncDockTargetReader()
        self.on_manual_detach = on_manual_detach
        self.gap = max(0, int(gap))
        self.tolerance = max(1, int(tolerance))
        self.enabled = False
        self._expected: tuple[int, int] | None = None
        self._last_target: Rect | None = None
        self._auto_iconified = False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self._expected = None
        self._last_target = None
        if not self.enabled:
            self._auto_iconified = False

    @staticmethod
    def _geometry(width: int, height: int, x: int, y: int) -> str:
        return f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}"

    def tick(self) -> str:
        if not self.enabled:
            return "disabled"
        target = self.target_reader(self.serial)
        if target is None:
            return "target_missing"

        state = str(self.host.state())
        if state == "withdrawn":
            return "assistant_withdrawn"
        if target.minimized:
            if state not in {"iconic", "withdrawn"}:
                self.host.iconify()
                self._auto_iconified = True
            return "target_minimized"
        if self._auto_iconified:
            if state == "iconic":
                self.host.deiconify()
            self._auto_iconified = False
            state = str(self.host.state())
        elif state == "iconic":
            # The user minimized the assistant independently; do not undo it.
            return "assistant_minimized"
        if state not in {"normal", "zoomed"}:
            return "assistant_unavailable"

        self.host.update_idletasks()
        width = max(1, int(self.host.winfo_width()))
        height = max(1, int(self.host.winfo_height()))
        current = (int(self.host.winfo_x()), int(self.host.winfo_y()))

        if self._expected is not None:
            moved = max(abs(current[0] - self._expected[0]),
                        abs(current[1] - self._expected[1]))
            if moved > self.tolerance:
                self.enabled = False
                self._expected = None
                self._last_target = None
                if self.on_manual_detach is not None:
                    self.on_manual_detach()
                return "manual_detach"

        placement = choose_dock_placement(
            target.rect, (width, height), target.work_area, gap=self.gap)
        self._last_target = target.rect
        if placement is None:
            return "no_space"
        desired = (placement.x, placement.y)
        if current != desired:
            self.host.geometry(self._geometry(
                width, height, placement.x, placement.y))
        self._expected = desired
        return f"docked_{placement.side}"

