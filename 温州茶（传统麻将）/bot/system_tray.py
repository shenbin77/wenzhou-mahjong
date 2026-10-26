"""Small native Windows notification-area icon for the desktop assistant.

The tray window owns no Tk objects.  Its Win32 message loop runs on a daemon
thread and only emits string actions into a thread-safe sink; the Tk thread
decides when to restore or quit.  This keeps every Tk call on the Tk thread.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable


TRAY_ACTION_RESTORE = "restore"
TRAY_ACTION_QUIT = "quit"
TRAY_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("打开主窗口", TRAY_ACTION_RESTORE),
    ("退出程序", TRAY_ACTION_QUIT),
)

try:  # pragma: no cover - availability is exercised through ``available``
    import win32api
    import win32con
    import win32gui
except ImportError:  # A floating orb still provides a restore/quit fallback.
    win32api = None
    win32con = None
    win32gui = None


def tray_action_for_mouse_message(message: int) -> str | None:
    """Map a Win32 tray mouse message to a product action."""
    if win32con is None:
        return None
    if message == win32con.WM_LBUTTONUP:
        return TRAY_ACTION_RESTORE
    return None


class WindowsTrayIcon:
    """Own a notification-area icon without touching Tk from its thread."""

    _ICON_ID = 1
    _COMMAND_RESTORE = 1001
    _COMMAND_QUIT = 1002

    def __init__(
        self,
        icon_path: str | Path,
        tooltip: str,
        action_sink: Callable[[str], None],
    ) -> None:
        self.icon_path = Path(icon_path)
        self._tooltip = str(tooltip)
        self._action_sink = action_sink
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._hicon = None
        self._notify_data = None
        self._registered = False
        self._last_error: str | None = None
        if win32con is not None:
            self._wm_tray = win32con.WM_USER + 20
            self._wm_tooltip = win32con.WM_USER + 21
        else:
            self._wm_tray = 0
            self._wm_tooltip = 0

    @property
    def available(self) -> bool:
        return (
            win32gui is not None
            and win32api is not None
            and win32con is not None
            and self.icon_path.is_file()
        )

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(
            thread is not None and thread.is_alive()
            and self._hwnd and self._registered)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self, timeout: float = 2.0) -> bool:
        """Start the native message loop once and report whether it is ready."""
        if self.running:
            return True
        if self._thread is not None and self._thread.is_alive():
            self._last_error = "上一托盘线程仍在退出，未创建重复图标"
            return False
        if not self.available:
            self._last_error = "系统托盘依赖或图标文件不可用"
            return False
        self._ready.clear()
        self._last_error = None
        self._registered = False
        self._thread = threading.Thread(
            target=self._run,
            name="mahjong-system-tray",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(max(0.0, float(timeout)))
        return self.running

    def update_tooltip(self, tooltip: str) -> None:
        """Update text now or when the tray thread becomes ready."""
        self._tooltip = str(tooltip)
        hwnd = self._hwnd
        if hwnd and win32gui is not None:
            try:
                win32gui.PostMessage(hwnd, self._wm_tooltip, 0, 0)
            except Exception:  # noqa: BLE001 - the icon may be exiting
                pass

    def stop(self, timeout: float = 2.0) -> None:
        """Remove the icon and stop its message loop.  Safe to call twice."""
        hwnd = self._hwnd
        if hwnd and win32gui is not None and win32con is not None:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001 - process teardown must continue
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        if thread is not None and thread.is_alive():
            self._last_error = "托盘线程未在限定时间内退出；保留线程引用以防重复启动"
            return
        self._thread = None

    def _emit(self, action: str) -> None:
        try:
            self._action_sink(action)
        except Exception:  # noqa: BLE001 - a bad callback must not kill tray
            pass

    def _run(self) -> None:  # pragma: no cover - covered by Windows smoke test
        assert win32api is not None
        assert win32con is not None
        assert win32gui is not None
        class_name = (
            f"WenzhouMahjongTray_{os.getpid()}_{id(self):x}"
        )
        try:
            instance = win32api.GetModuleHandle(None)
            taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            message_map = {
                self._wm_tray: self._on_tray_message,
                self._wm_tooltip: self._on_tooltip_message,
                taskbar_created: self._on_taskbar_created,
                win32con.WM_CLOSE: self._on_close,
                win32con.WM_DESTROY: self._on_destroy,
            }
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            window_class.lpfnWndProc = message_map
            win32gui.RegisterClass(window_class)
            self._hwnd = win32gui.CreateWindow(
                class_name,
                class_name,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                instance,
                None,
            )
            self._hicon = win32gui.LoadImage(
                0,
                str(self.icon_path),
                win32con.IMAGE_ICON,
                0,
                0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
            self._add_icon()
            self._registered = True
            self._ready.set()
            win32gui.PumpMessages()
        except Exception as exc:  # noqa: BLE001 - expose failure, keep app alive
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._delete_icon()
            self._registered = False
            hicon = self._hicon
            self._hicon = None
            if hicon is not None:
                try:
                    win32gui.DestroyIcon(hicon)
                except Exception:  # noqa: BLE001
                    pass
            self._hwnd = None
            self._ready.set()

    def _notification_data(self):
        assert win32con is not None
        tooltip = self._tooltip[:127]
        return (
            self._hwnd,
            self._ICON_ID,
            win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
            self._wm_tray,
            self._hicon,
            tooltip,
        )

    def _add_icon(self) -> None:
        assert win32gui is not None
        assert win32con is not None
        self._notify_data = self._notification_data()
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._notify_data)

    def _delete_icon(self) -> None:
        if self._notify_data is None or win32gui is None or win32con is None:
            return
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, self._notify_data)
        except Exception:  # noqa: BLE001 - Explorer may already be gone
            pass
        self._notify_data = None

    def _on_tooltip_message(self, _hwnd, _msg, _wparam, _lparam):
        assert win32gui is not None
        assert win32con is not None
        self._notify_data = self._notification_data()
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._notify_data)
        except Exception:  # noqa: BLE001 - next tick/taskbar restore can retry
            pass
        return 0

    def _on_taskbar_created(self, _hwnd, _msg, _wparam, _lparam):
        self._add_icon()
        return 0

    def _on_tray_message(self, hwnd, _msg, _wparam, lparam):
        assert win32con is not None
        action = tray_action_for_mouse_message(int(lparam))
        if action is not None:
            self._emit(action)
        elif int(lparam) == win32con.WM_RBUTTONUP:
            self._show_menu(hwnd)
        return 0

    def _show_menu(self, hwnd: int) -> None:
        assert win32gui is not None
        assert win32con is not None
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(
                menu, win32con.MF_STRING, self._COMMAND_RESTORE, "打开主窗口")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, None)
            win32gui.AppendMenu(
                menu, win32con.MF_STRING, self._COMMAND_QUIT, "退出程序")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            command = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN
                | win32con.TPM_RIGHTBUTTON
                | win32con.TPM_RETURNCMD,
                x,
                y,
                0,
                hwnd,
                None,
            )
            if command == self._COMMAND_RESTORE:
                self._emit(TRAY_ACTION_RESTORE)
            elif command == self._COMMAND_QUIT:
                self._emit(TRAY_ACTION_QUIT)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _on_close(self, hwnd, _msg, _wparam, _lparam):
        assert win32gui is not None
        win32gui.DestroyWindow(hwnd)
        return 0

    def _on_destroy(self, _hwnd, _msg, _wparam, _lparam):
        assert win32gui is not None
        self._delete_icon()
        win32gui.PostQuitMessage(0)
        return 0


__all__ = [
    "TRAY_ACTION_QUIT",
    "TRAY_ACTION_RESTORE",
    "TRAY_MENU_ITEMS",
    "WindowsTrayIcon",
    "tray_action_for_mouse_message",
]
