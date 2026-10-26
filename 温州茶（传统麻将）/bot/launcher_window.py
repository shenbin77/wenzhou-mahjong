"""One visible entry point for the per-emulator Mahjong assistants.

This window only discovers connected devices and opens or restores one UI per
serial.  It never starts following, sends game taps, or infers that a locally
saved nickname proves the account currently logged into the emulator.
"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from tkinter import ttk

from adb_runtime import resolve_adb_path_cached
from app_entry import (choose_existing_for_restore, existing_window_pids,
                       pid_owns_assist_window)
from harvest_tea import DEFAULT_SERIAL
from instance_profile import profile_paths, read_binding_for_serial, safe_serial
from launch_multi import connected_devices, start_window
from window_restore_signal import (consume_restore, request_control,
                                   request_restore, wait_for_ack)


HERE = Path(__file__).resolve().parent
PROFILE_BASE = HERE.parent / "采集" / "assist_sessions"
ICON_PATH = HERE / "assets" / "温州麻将精灵.ico"
MASCOT_PATH = HERE / "assets" / "温州麻将精灵.png"
MANAGER_SIGNAL = "manager"
CONTROL_ACK_TIMEOUT_S = 4.0
PROCESS_EXIT_TIMEOUT_S = 6.0
EMPTY_DEVICE_REFRESH_MS = 3000
MANAGER_MUTEX_NAME = "Local\\WenzhouMahjongAssistantManager"
LOGS_ROOT = HERE.parent / "采集" / "dev_auto_play"
INCIDENT_ROOT = HERE.parents[1] / "_incidents" / "tea2"
LDCONSOLE_PATH = Path(r"D:\leidian\LDPlayer14\ldconsole.exe")

BG = "#171b25"
SURFACE = "#252b38"
SURFACE_LIGHT = "#303849"
TEXT = "#f4f2e9"
MUTED = "#a9b2c2"
GOLD = "#f2bc54"
GREEN = "#56d7a0"


@dataclass(frozen=True)
class DeviceRow:
    serial: str
    account: str | None
    mode: str | None
    pid: int | None


class ManagerInstanceGuard:
    """Lifetime guard for the one visible multi-instance manager."""

    def __init__(self, handle=None) -> None:
        self.handle = handle

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle and os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(handle)


def acquire_manager_instance() -> ManagerInstanceGuard | None:
    """Atomically elect one manager; a second desktop launch only signals it."""
    if os.name != "nt":
        return None if existing_manager_pid() is not None else ManagerInstanceGuard()
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MANAGER_MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建辅助器管理器单例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return ManagerInstanceGuard(handle)


def choose_activation_target(rows: list[DeviceRow]) -> DeviceRow | None:
    """A desktop activation opens a free serial, else restores one window."""
    for row in rows:
        if row.pid is None:
            return row
    if not rows:
        return None
    serial, _pid = choose_existing_for_restore(
        [row.serial for row in rows],
        {row.serial: row.pid for row in rows if row.pid is not None},
    )
    return next((row for row in rows if row.serial == serial), rows[0])


def build_incident_watchdog(serials: tuple[str, ...]):
    """Create the read-only incident watcher lazily so launcher stays usable."""
    from incident_watchdog import IncidentWatchdog

    return IncidentWatchdog(
        LOGS_ROOT,
        INCIDENT_ROOT,
        serials,
        ldconsole=LDCONSOLE_PATH,
        interval_s=2.0,
    )


def _adb_path() -> str:
    path = resolve_adb_path_cached()
    if not path:
        raise RuntimeError("未找到可用 adb，请检查雷电是否安装并已启动")
    return path


def collect_device_rows(*, base: Path = PROFILE_BASE) -> list[DeviceRow]:
    """Read one device/process/profile snapshot without touching game state."""
    devices = connected_devices(_adb_path())
    occupied = existing_window_pids(DEFAULT_SERIAL)
    rows: list[DeviceRow] = []
    for serial in devices:
        profile = profile_paths(base, serial)
        binding = read_binding_for_serial(profile.binding, serial)
        rows.append(DeviceRow(
            serial=serial,
            account=str(binding["account"]).strip() if binding else None,
            mode=str(binding.get("mode") or "") if binding else None,
            pid=occupied.get(serial),
        ))
    return rows


def open_or_restore(serial: str) -> tuple[str, int]:
    """Recheck online state and ownership immediately before acting."""
    serial = safe_serial(serial)
    if serial not in connected_devices(_adb_path()):
        raise RuntimeError(f"{serial} 已离线，未启动辅助器")
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is not None:
        _request_confirmed(serial, "restore")
        return "restored", pid
    process = start_window(serial)
    return "started", int(process.pid)


def _request_confirmed(serial: str, command: str, *,
                       timeout: float = CONTROL_ACK_TIMEOUT_S) -> str:
    """Send one lifecycle command and require the matching Tk-process ACK."""
    ticket = request_control(serial, command)
    ack = wait_for_ack(ticket, timeout=timeout)
    if ack is None:
        raise RuntimeError(
            f"{serial} 未确认{command}：{timeout:g} 秒内没有收到辅助器 ACK；"
            "窗口可能仍在运行，请勿重复操作")
    if not ack.ok:
        detail = ack.detail or "辅助器拒绝了请求"
        raise RuntimeError(f"{serial} {command} 失败：{detail}")
    return ack.detail


def _wait_for_window_exit(pid: int, *,
                          timeout: float = PROCESS_EXIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while pid_owns_assist_window(pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def quit_assistant(serial: str) -> tuple[str, int]:
    """Ask one UI to exit cleanly; never terminate a process directly."""
    serial = safe_serial(serial)
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is None:
        raise RuntimeError(f"{serial} 辅助器未运行，无需退出")
    _request_confirmed(serial, "quit")
    if not _wait_for_window_exit(pid):
        raise RuntimeError(
            f"{serial} 已确认退出请求，但 PID {pid} 在 "
            f"{PROCESS_EXIT_TIMEOUT_S:g} 秒内仍未退出；未强制结束")
    return "stopped", pid


def restart_assistant(serial: str) -> tuple[str, int]:
    """Cleanly exit one UI, verify process death, then start that serial."""
    serial = safe_serial(serial)
    if serial not in connected_devices(_adb_path()):
        raise RuntimeError(f"{serial} 已离线，保留现有辅助器且不重启")
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is None:
        process = start_window(serial)
        return "started", int(process.pid)
    _request_confirmed(serial, "quit")
    if not _wait_for_window_exit(pid):
        raise RuntimeError(
            f"{serial} 旧窗口 PID {pid} 未按时退出；为避免双实例，未启动新窗口")
    process = start_window(serial)
    return "restarted", int(process.pid)


def existing_manager_pid() -> int | None:
    """Identify a live manager process, ignoring wrappers and stale PIDs."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        found: list[int] = []
        current = os.getpid()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _unused):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value != "温州麻将辅助器 · 多开管理":
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value > 0 and owner.value != current:
                found.append(int(owner.value))
                return False
            return True

        user32.EnumWindows(callback, 0)
        return found[0] if found else None

    import psutil

    current = os.getpid()
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = process.info
            pid = int(info["pid"])
            if pid == current or pid <= 0:
                continue
            name = Path(str(info.get("name") or "")).name.lower()
            if name not in {"python.exe", "pythonw.exe"}:
                continue
            argv = [str(arg) for arg in (info.get("cmdline") or ())]
            if any(Path(arg).name.lower() == "launcher_window.py" for arg in argv[1:]):
                if process.status() != psutil.STATUS_ZOMBIE:
                    return pid
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
    return None


class MahjongLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._events: Queue[tuple] = Queue()
        self._refreshing = False
        self._closed = False
        self._rows: list[DeviceRow] = []
        self._buttons: dict[str, list[tk.Button]] = {}
        self._busy_serials: set[str] = set()
        self._activation_pending = True
        self._empty_refresh_after = None
        self._watchdog = None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_config_thread: threading.Thread | None = None
        self._watchdog_serials: tuple[str, ...] = ()
        self._watchdog_generation = 0
        self._watchdog_retry_at = 0.0
        self._watchdog_lock = threading.Lock()
        self._mascot = None

        root.title("温州麻将辅助器 · 多开管理")
        root.configure(bg=BG)
        root.geometry("800x490")
        root.minsize(680, 370)
        if ICON_PATH.is_file():
            try:
                root.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self._poll)
        self.refresh()

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(18, 12))
        if MASCOT_PATH.is_file():
            try:
                from PIL import Image, ImageTk

                source = Image.open(MASCOT_PATH).convert("RGBA")
                source.thumbnail((58, 58))
                self._mascot = ImageTk.PhotoImage(source)
                tk.Label(header, image=self._mascot, bg=BG).pack(side="left", padx=(0, 12))
            except (OSError, RuntimeError, tk.TclError):
                pass
        words = tk.Frame(header, bg=BG)
        words.pack(side="left", fill="x", expand=True)
        tk.Label(words, text="温州麻将辅助器", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        tk.Label(words, text="为每台在线雷电打开一个独立辅助器", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(2, 0))

        self.refresh_button = tk.Button(
            header, text="刷新设备", command=self.refresh,
            bg=SURFACE_LIGHT, fg=TEXT, activebackground="#465168",
            activeforeground=TEXT, relief="flat", padx=14, pady=7)
        self.refresh_button.pack(side="right", padx=(8, 0))
        tk.Button(header, text="退出管理器", command=self.close,
                  bg=BG, fg=MUTED, activebackground=SURFACE,
                  activeforeground=TEXT, relief="flat", padx=8, pady=7).pack(side="right")

        self.count_var = tk.StringVar(value="正在检查在线雷电…")
        tk.Label(self.root, textvariable=self.count_var, bg=BG, fg=GOLD,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(
                     anchor="w", padx=26, pady=(0, 10))

        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True, padx=22)
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self._list_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda _event: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(
            self._list_window, width=event.width))

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=24, pady=(10, 16))
        self.status_var = tk.StringVar(value="本地绑定昵称仅作标记；身份以游戏画面核验结果为准。")
        tk.Label(footer, textvariable=self.status_var, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x")
        self.watchdog_var = tk.StringVar(value="自动故障包：等待在线雷电")
        tk.Label(footer, textvariable=self.watchdog_var, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(
                     fill="x", pady=(4, 0))

    def _submit(self, tag: str, function, *args) -> None:
        def worker() -> None:
            try:
                result = function(*args)
                self._events.put((tag, result, None))
            except Exception as exc:  # noqa: BLE001 - show a concrete error in UI
                self._events.put((tag, None, str(exc)))

        threading.Thread(target=worker, daemon=True,
                         name=f"mahjong-launcher-{tag}").start()

    def refresh(self) -> None:
        if self._refreshing or self._closed:
            return
        self._refreshing = True
        self.refresh_button.configure(state="disabled")
        self.status_var.set("正在读取在线雷电与辅助器状态…")
        self._submit("refresh", collect_device_rows)

    def _open_device(self, serial: str) -> None:
        self._operate_device(serial, "open")

    def _operate_device(self, serial: str, operation: str) -> None:
        if self._closed:
            return
        if serial in self._busy_serials:
            self.status_var.set(f"{serial} 正在处理中；不会重复提交操作。")
            return
        self._busy_serials.add(serial)
        for button in self._buttons.get(serial, []):
            button.configure(state="disabled")
        labels = {"open": "打开/恢复", "quit": "退出", "restart": "重启"}
        self.status_var.set(f"正在{labels[operation]} {serial}…")
        functions = {
            "open": open_or_restore,
            "quit": quit_assistant,
            "restart": restart_assistant,
        }
        self._submit(f"{operation}:{serial}", functions[operation], serial)

    def _cancel_empty_refresh(self) -> None:
        after_id, self._empty_refresh_after = self._empty_refresh_after, None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

    def _schedule_empty_refresh(self) -> None:
        if self._closed or self._empty_refresh_after is not None:
            return

        def retry() -> None:
            self._empty_refresh_after = None
            if not self._closed and self._activation_pending:
                self.refresh()

        self._empty_refresh_after = self.root.after(EMPTY_DEVICE_REFRESH_MS, retry)

    def _run_pending_activation(self) -> None:
        if not self._activation_pending or self._closed:
            return
        if self._busy_serials:
            self.status_var.set("上一台辅助器正在处理；完成后继续响应本次桌面启动。")
            return
        target = choose_activation_target(self._rows)
        if target is None:
            self.status_var.set(
                "没有在线雷电；本窗口会自动等待，启动雷电后将打开辅助器。")
            self._schedule_empty_refresh()
            return
        self._cancel_empty_refresh()
        self._activation_pending = False
        self._operate_device(target.serial, "open")

    def _run_watchdog(self, watcher, generation: int,
                      serials: tuple[str, ...]) -> None:
        try:
            watcher.run()
        except Exception as exc:  # noqa: BLE001 - surface without blocking UI
            self._events.put(("watchdog", {
                "generation": generation,
                "state": "failed",
                "serials": serials,
            }, f"{type(exc).__name__}: {exc}"))
        else:
            self._events.put(("watchdog", {
                "generation": generation,
                "state": "stopped",
                "serials": serials,
            }, None))

    def _replace_watchdog(self, generation: int,
                          serials: tuple[str, ...]) -> None:
        with self._watchdog_lock:
            previous = self._watchdog
            previous_thread = self._watchdog_thread
            if previous is not None:
                try:
                    previous.stop()
                except Exception as exc:  # noqa: BLE001 - do not duplicate it
                    self._watchdog_serials = ()
                    self._events.put(("watchdog", {
                        "generation": generation,
                        "state": "failed",
                        "serials": serials,
                    }, f"停止旧守护失败：{type(exc).__name__}: {exc}"))
                    return
            if (previous_thread is not None
                    and previous_thread is not threading.current_thread()):
                previous_thread.join(timeout=3.0)
            if previous_thread is not None and previous_thread.is_alive():
                # A review may still be finishing an atomic incident bundle.
                # Keep ownership of that exact instance and wait; starting a
                # second watcher here would duplicate mouse monitors and race
                # writes to the shared incident index.
                self._watchdog_serials = ()
                self._events.put(("watchdog", {
                    "generation": generation,
                    "state": "stopping",
                    "serials": serials,
                }, None))
                return
            self._watchdog = None
            self._watchdog_thread = None
            if (self._closed or generation != self._watchdog_generation
                    or not serials):
                return
            try:
                watcher = build_incident_watchdog(serials)
            except Exception as exc:  # noqa: BLE001 - manager must remain usable
                self._events.put(("watchdog", {
                    "generation": generation,
                    "state": "failed",
                    "serials": serials,
                }, f"{type(exc).__name__}: {exc}"))
                return
            thread = threading.Thread(
                target=self._run_watchdog,
                args=(watcher, generation, serials),
                daemon=True,
                name="mahjong-incident-watchdog",
            )
            self._watchdog = watcher
            self._watchdog_thread = thread
            thread.start()
            self._events.put(("watchdog", {
                "generation": generation,
                "state": "running",
                "serials": serials,
            }, None))

    def _ensure_incident_watchdog(self, rows: list[DeviceRow]) -> None:
        serials = tuple(sorted(row.serial for row in rows))
        thread = self._watchdog_thread
        if (serials == self._watchdog_serials and thread is not None
                and thread.is_alive()):
            return
        if (not serials and self._watchdog is None
                and (thread is None or not thread.is_alive())):
            self._watchdog_serials = ()
            self.watchdog_var.set("自动故障包：等待在线雷电")
            return
        self._watchdog_serials = serials
        self._watchdog_generation += 1
        generation = self._watchdog_generation
        if not serials:
            self.watchdog_var.set("自动故障包：等待在线雷电")
        else:
            self.watchdog_var.set(
                f"自动故障包：正在连接 {len(serials)} 台雷电…")
        config_thread = threading.Thread(
            target=self._replace_watchdog,
            args=(generation, serials),
            daemon=True,
            name="mahjong-watchdog-config",
        )
        self._watchdog_config_thread = config_thread
        config_thread.start()

    def _apply_watchdog_event(self, result: dict, error: str | None) -> None:
        if int(result.get("generation", -1)) != self._watchdog_generation:
            return
        serials = tuple(result.get("serials") or ())
        state = result.get("state")
        if error or state == "failed":
            detail = error or "未知错误"
            self._watchdog_retry_at = time.monotonic() + 5.0
            self.watchdog_var.set(
                f"自动故障包：启动失败（{detail}）；辅助器仍可正常使用")
        elif state == "running":
            self._watchdog_retry_at = 0.0
            self.watchdog_var.set(
                f"自动故障包：正在守护 {len(serials)} 台雷电")
        elif state == "stopping":
            self._watchdog_retry_at = time.monotonic() + 1.0
            self.watchdog_var.set(
                "自动故障包：旧守护正在安全停止；停止后自动切换")
        elif state == "stopped" and not self._closed:
            self._watchdog_retry_at = time.monotonic() + 5.0
            self.watchdog_var.set("自动故障包：已停止，等待下次设备刷新重启")

    def _retry_incident_watchdog_if_needed(self) -> None:
        if self._closed or not self._rows:
            return
        run_thread = self._watchdog_thread
        config_thread = self._watchdog_config_thread
        if run_thread is not None and run_thread.is_alive():
            return
        if config_thread is not None and config_thread.is_alive():
            return
        if time.monotonic() < self._watchdog_retry_at:
            return
        self._watchdog_retry_at = time.monotonic() + 5.0
        self._ensure_incident_watchdog(self._rows)

    def _stop_incident_watchdog(self) -> bool:
        self._watchdog_generation += 1
        watcher = self._watchdog
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:  # noqa: BLE001 - manager exit must continue
                pass
        config_thread = self._watchdog_config_thread
        if (config_thread is not None
                and config_thread is not threading.current_thread()):
            config_thread.join(timeout=3.0)
        latest = self._watchdog
        if latest is not None and latest is not watcher:
            try:
                latest.stop()
            except Exception:  # noqa: BLE001 - manager exit must continue
                pass
        run_thread = self._watchdog_thread
        if run_thread is not None and run_thread is not threading.current_thread():
            run_thread.join(timeout=3.0)
        config_alive = bool(
            config_thread is not None and config_thread.is_alive())
        run_alive = bool(run_thread is not None and run_thread.is_alive())
        if config_alive or run_alive:
            return False
        self._watchdog = None
        self._watchdog_thread = None
        self._watchdog_config_thread = None
        return True

    def _finish_close_when_watchdog_stops(self) -> None:
        config_thread = self._watchdog_config_thread
        run_thread = self._watchdog_thread
        if ((config_thread is not None and config_thread.is_alive())
                or (run_thread is not None and run_thread.is_alive())):
            self.root.after(250, self._finish_close_when_watchdog_stops)
            return
        self._watchdog = None
        self._watchdog_thread = None
        self._watchdog_config_thread = None
        self.root.destroy()

    def _render(self, rows: list[DeviceRow]) -> None:
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        self._buttons.clear()
        self.count_var.set(f"在线雷电 {len(rows)} 台 · 辅助器已开 {sum(r.pid is not None for r in rows)} 台")
        if not rows:
            tk.Label(self.list_frame,
                     text="没有在线雷电。管理器会自动等待；启动雷电即可。",
                     bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 11),
                     pady=36).pack(fill="x")
            return
        for index, row in enumerate(rows, start=1):
            panel = tk.Frame(self.list_frame, bg=SURFACE, padx=16, pady=13)
            panel.pack(fill="x", pady=(0, 9))
            number = tk.Label(panel, text=f"{index:02d}", bg=SURFACE, fg=GOLD,
                              font=("Microsoft YaHei UI", 16, "bold"))
            number.pack(side="left", padx=(0, 18))
            details = tk.Frame(panel, bg=SURFACE)
            details.pack(side="left", fill="x", expand=True)
            tk.Label(details, text=row.serial, bg=SURFACE, fg=TEXT,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
            nickname = row.account or "未设置昵称"
            tk.Label(details, text=f"本地绑定：{nickname} · 当前登录待核验",
                     bg=SURFACE, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
            state = (f"辅助器运行中 · PID {row.pid}" if row.pid is not None
                     else "辅助器未打开")
            tk.Label(panel, text=state, bg=SURFACE,
                     fg=GREEN if row.pid is not None else MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=12)
            controls = tk.Frame(panel, bg=SURFACE)
            controls.pack(side="right")
            buttons: list[tk.Button] = []
            if row.pid is not None:
                for text, operation, background, foreground in (
                    ("恢复窗口", "open", GOLD, "#22252e"),
                    ("重启", "restart", SURFACE_LIGHT, TEXT),
                    ("退出", "quit", BG, MUTED),
                ):
                    button = tk.Button(
                        controls, text=text,
                        command=lambda serial=row.serial, op=operation:
                            self._operate_device(serial, op),
                        bg=background, fg=foreground,
                        activebackground="#465168", activeforeground=TEXT,
                        relief="flat", padx=10, pady=8,
                        font=("Microsoft YaHei UI", 9, "bold"))
                    button.pack(side="left", padx=(5, 0))
                    buttons.append(button)
            else:
                button = tk.Button(
                    controls, text="打开辅助器",
                    command=lambda serial=row.serial: self._open_device(serial),
                    bg=GOLD, fg="#22252e", activebackground="#ffd989",
                    activeforeground="#22252e", relief="flat", padx=12,
                    pady=8, font=("Microsoft YaHei UI", 9, "bold"))
                button.pack(side="left")
                buttons.append(button)
            self._buttons[row.serial] = buttons

    def _poll(self) -> None:
        if self._closed:
            return
        if consume_restore(MANAGER_SIGNAL):
            self._activation_pending = True
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            if not self._refreshing:
                self.refresh()
        while True:
            try:
                tag, result, error = self._events.get_nowait()
            except Empty:
                break
            if tag == "refresh":
                self._refreshing = False
                self.refresh_button.configure(state="normal")
                if error:
                    self.status_var.set(f"设备检查失败：{error}")
                    if self._activation_pending:
                        self._schedule_empty_refresh()
                else:
                    self._rows = result
                    self._render(result)
                    self._ensure_incident_watchdog(result)
                    self.status_var.set("选择对应雷电打开或恢复辅助器。")
            elif any(tag.startswith(f"{operation}:")
                     for operation in ("open", "quit", "restart")):
                operation, serial = tag.split(":", 1)
                self._busy_serials.discard(serial)
                if error:
                    self.status_var.set(f"{serial}：{error}")
                    for button in self._buttons.get(serial, []):
                        button.configure(state="normal")
                    if self._activation_pending:
                        self.refresh()
                else:
                    action, pid = result
                    verbs = {
                        "restored": "已确认恢复",
                        "started": "已打开",
                        "stopped": "已确认退出",
                        "restarted": "已重启",
                    }
                    verb = verbs.get(action, action)
                    self.status_var.set(f"{serial}：{verb}辅助器（PID {pid}）")
                    self.refresh()
            if tag == "refresh" and not error:
                if self._rows:
                    self._cancel_empty_refresh()
                self._run_pending_activation()
            elif tag == "watchdog" and result is not None:
                self._apply_watchdog_event(result, error)
        self._retry_incident_watchdog_if_needed()
        self.root.after(150, self._poll)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_empty_refresh()
        if self._stop_incident_watchdog():
            self.root.destroy()
            return
        self.watchdog_var.set("自动故障包：正在安全停止，完成后退出管理器")
        self.root.after(250, self._finish_close_when_watchdog_stops)


def main() -> None:
    guard = acquire_manager_instance()
    if guard is None:
        request_restore(MANAGER_SIGNAL)
        return
    # One migration release may still be running without the new mutex.  Do
    # not open a second manager beside it; signal it and let the next restart
    # adopt the atomic guard.
    if existing_manager_pid() is not None:
        guard.close()
        request_restore(MANAGER_SIGNAL)
        return
    try:
        root = tk.Tk()
        MahjongLauncher(root)
        root.mainloop()
    finally:
        guard.close()


if __name__ == "__main__":
    main()
