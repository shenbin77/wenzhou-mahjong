"""One visible entry point for the per-emulator Mahjong assistants.

This window discovers devices, explicitly starts one saved LDPlayer on demand,
and opens or restores one UI per serial.  It never starts following, sends game
taps, or infers that a saved nickname proves the account currently logged in.
"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from tkinter import ttk

from adb_runtime import resolve_adb_path_cached
from app_entry import (choose_existing_for_restore, existing_window_pids,
                       pid_owns_assist_window)
from harvest_tea import DEFAULT_SERIAL
from instance_profile import (discover_profile_bindings,
                              read_assistant_desired_open, safe_serial,
                              set_assistant_desired_open)
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
STARTUP_READY_TIMEOUT_S = 10.0
AUTORECOVERY_FAILURE_BUDGET = 3
AUTORECOVERY_BACKOFF_S = (5.0, 30.0)
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
    online: bool = True
    desired_open: bool = True


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
    online_rows = [row for row in rows if row.online]
    for row in online_rows:
        if row.pid is None:
            return row
    restore_rows = online_rows or [row for row in rows if row.pid is not None]
    if not restore_rows:
        return None
    serial, _pid = choose_existing_for_restore(
        [row.serial for row in restore_rows],
        {row.serial: row.pid for row in restore_rows if row.pid is not None},
    )
    return next((row for row in restore_rows if row.serial == serial),
                restore_rows[0])


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
    """Merge online devices with crash-safe saved profiles; never hide settings."""
    devices = set(connected_devices(_adb_path()))
    bindings = discover_profile_bindings(base)
    occupied = existing_window_pids(DEFAULT_SERIAL)
    rows: list[DeviceRow] = []
    serials = sorted(devices | set(bindings),
                     key=lambda value: (value not in devices, value))
    for serial in serials:
        binding = bindings.get(serial)
        rows.append(DeviceRow(
            serial=serial,
            account=str(binding["account"]).strip() if binding else None,
            mode=str(binding.get("mode") or "") if binding else None,
            pid=occupied.get(serial),
            online=serial in devices,
            # Existing bindings predate assistant_state/v1. Preserve the old
            # user expectation that their window should return after a crash.
            desired_open=read_assistant_desired_open(
                base, serial, default=binding is not None),
        ))
    return rows


def can_start_offline_device(row: DeviceRow) -> bool:
    """Only a saved, offline profile may expose the explicit LDPlayer start."""
    return bool(not row.online and row.account)


def start_ldplayer_and_assistant(serial: str) -> tuple[str, str, int]:
    """Explicitly start one LDPlayer, wait for ADB, then open its assistant."""
    from ldplayer_control import start_and_wait_online

    serial = safe_serial(serial)
    result = start_and_wait_online(
        serial,
        ldconsole=LDCONSOLE_PATH,
        online_serials=lambda: connected_devices(_adb_path()),
    )
    if not result.ok:
        raise RuntimeError(result.detail or f"{serial} 雷电启动失败")
    action, pid = open_or_restore(serial)
    return result.status, action, pid


def _revoke_stale_click_authority(serial: str) -> None:
    """Every new UI process starts without inherited follow/click authority."""
    from full_auto_control import revoke_pending_command

    if not revoke_pending_command(serial):
        raise RuntimeError(f"{serial} 无法撤销遗留全自动命令，拒绝启动窗口")


def open_or_restore(serial: str) -> tuple[str, int]:
    """Recheck online state and ownership immediately before acting."""
    serial = safe_serial(serial)
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is not None:
        set_assistant_desired_open(PROFILE_BASE, serial, True)
        _request_confirmed(serial, "restore")
        return "restored", pid
    if serial not in connected_devices(_adb_path()):
        raise RuntimeError(f"{serial} 已离线，设置已保留，未启动辅助器")
    set_assistant_desired_open(PROFILE_BASE, serial, True)
    _revoke_stale_click_authority(serial)
    process = start_window(serial)
    _wait_for_started_window(serial, process)
    return "started", int(process.pid)


def recover_window_only(serial: str) -> tuple[str, int]:
    """Recover one persisted UI intent without restoring click authority."""
    serial = safe_serial(serial)
    bindings = discover_profile_bindings(PROFILE_BASE)
    desired = read_assistant_desired_open(
        PROFILE_BASE, serial, default=serial in bindings)
    if not desired:
        return "desired_false", 0
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is not None:
        _request_confirmed(serial, "ping")
        return "already_running", pid
    if serial not in connected_devices(_adb_path()):
        return "offline", 0
    _revoke_stale_click_authority(serial)
    process = start_window(serial)
    _wait_for_started_window(serial, process)
    # The newly constructed window persists ``True`` itself.  Never write it
    # here: an explicit exit may have changed the intent to ``False`` while
    # this worker was waiting for the startup ACK.
    if not read_assistant_desired_open(PROFILE_BASE, serial, default=False):
        return "desired_false", int(process.pid)
    return "started", int(process.pid)


def _wait_for_started_window(serial: str, process, *,
                             timeout: float = STARTUP_READY_TIMEOUT_S) -> None:
    """Require a real Tk window and responsive control loop after spawning."""
    serial = safe_serial(serial)
    pid = int(process.pid)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if existing_window_pids(DEFAULT_SERIAL).get(serial) == pid:
            remaining = max(0.05, deadline - time.monotonic())
            _request_confirmed(
                serial, "restore", timeout=min(CONTROL_ACK_TIMEOUT_S, remaining))
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            raise RuntimeError(
                f"{serial} 辅助器 PID {pid} 在窗口就绪前退出；已停止自动重试")
        time.sleep(0.05)
    raise RuntimeError(
        f"{serial} 辅助器 PID {pid} 在 {timeout:g} 秒内未完成启动握手")


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
    set_assistant_desired_open(PROFILE_BASE, serial, False)
    return "stopped", pid


def restart_assistant(serial: str) -> tuple[str, int]:
    """Cleanly exit one UI, verify process death, then start that serial."""
    serial = safe_serial(serial)
    if serial not in connected_devices(_adb_path()):
        raise RuntimeError(f"{serial} 已离线，保留现有辅助器且不重启")
    pid = existing_window_pids(DEFAULT_SERIAL).get(serial)
    if pid is None:
        set_assistant_desired_open(PROFILE_BASE, serial, True)
        _revoke_stale_click_authority(serial)
        process = start_window(serial)
        _wait_for_started_window(serial, process)
        return "started", int(process.pid)
    _request_confirmed(serial, "quit")
    if not _wait_for_window_exit(pid):
        raise RuntimeError(
            f"{serial} 旧窗口 PID {pid} 未按时退出；为避免双实例，未启动新窗口")
    set_assistant_desired_open(PROFILE_BASE, serial, True)
    _revoke_stale_click_authority(serial)
    process = start_window(serial)
    _wait_for_started_window(serial, process)
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
        self._rendered_rows: tuple[DeviceRow, ...] | None = None
        self._post_refresh_status: str | None = None
        self._recovery_inflight: set[str] = set()
        self._recovery_failures: dict[str, tuple[int, float]] = {}
        self._recovery_scheduled: set[str] = set()
        self._recovery_tokens: dict[str, int] = {}
        self._intentional_shutdowns: set[str] = set()
        self._liveness_watches: set[tuple[str, int]] = set()
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
        tk.Label(words, text="按设备启动雷电并打开一个独立辅助器", bg=BG, fg=MUTED,
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

    def refresh(self, *, status_after: str | None = None) -> None:
        if status_after is not None:
            self._post_refresh_status = status_after
        if self._refreshing or self._closed:
            return
        self._refreshing = True
        self.refresh_button.configure(state="disabled")
        self.status_var.set("正在读取在线雷电与辅助器状态…")
        self._submit("refresh", collect_device_rows)

    def _open_device(self, serial: str) -> None:
        self._operate_device(serial, "open")

    def _reset_recovery_state(self, serial: str) -> None:
        getattr(self, "_recovery_failures", {}).pop(serial, None)
        getattr(self, "_recovery_scheduled", set()).discard(serial)
        getattr(self, "_recovery_inflight", set()).discard(serial)
        tokens = getattr(self, "_recovery_tokens", None)
        if tokens is not None:
            tokens[serial] = tokens.get(serial, 0) + 1

    def _start_offline_device(self, serial: str) -> None:
        if self._closed:
            return
        row = next((item for item in self._rows if item.serial == serial), None)
        if row is None or not can_start_offline_device(row):
            self.status_var.set(
                f"{serial} 当前不是可启动的离线已保存设备，请先刷新状态。")
            return
        if serial in self._busy_serials:
            self.status_var.set(f"{serial} 正在处理中；不会重复提交操作。")
            return
        self._reset_recovery_state(serial)
        getattr(self, "_intentional_shutdowns", set()).discard(serial)
        self._busy_serials.add(serial)
        for button in self._buttons.get(serial, []):
            button.configure(state="disabled")
        self.status_var.set(f"正在启动 {serial} 雷电并等待 ADB 上线…")
        self._submit(
            f"start-ld:{serial}", start_ldplayer_and_assistant, serial)

    def _operate_device(self, serial: str, operation: str) -> None:
        if self._closed:
            return
        if serial in self._busy_serials:
            self.status_var.set(f"{serial} 正在处理中；不会重复提交操作。")
            return
        automatic = serial in getattr(self, "_recovery_inflight", set())
        if operation == "quit":
            getattr(self, "_intentional_shutdowns", set()).add(serial)
        elif operation in {"open", "restart"} and not automatic:
            self._reset_recovery_state(serial)
            getattr(self, "_intentional_shutdowns", set()).discard(serial)
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

    def _run_pending_activation(self) -> None:
        if not self._activation_pending or self._closed:
            return
        if self._busy_serials:
            self.status_var.set("上一台辅助器正在处理；完成后继续响应本次桌面启动。")
            return
        target = choose_activation_target(self._rows)
        if target is None:
            self.status_var.set(
                "没有在线雷电；请选择已保存设备行的“启动雷电＋辅助器”。")
            self._activation_pending = False
            return
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
        serials = tuple(sorted(row.serial for row in rows if row.online))
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
        if self._closed or not any(row.online for row in self._rows):
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

    def _arm_liveness_watch(self, row: DeviceRow) -> None:
        """Watch one known UI PID; never scan ADB or touch Tk from the thread."""
        if not row.online or row.pid is None or self._closed:
            return
        key = (row.serial, int(row.pid))
        watches = getattr(self, "_liveness_watches", None)
        if watches is None:
            watches = self._liveness_watches = set()
        if key in watches:
            return
        watches.add(key)

        def watch() -> None:
            misses = 0
            try:
                while not self._closed:
                    try:
                        alive = pid_owns_assist_window(key[1])
                    except Exception:  # noqa: BLE001 - a failed probe is a miss
                        alive = False
                    if alive:
                        misses = 0
                    else:
                        misses += 1
                        if misses >= 3:
                            break
                    time.sleep(0.25)
            finally:
                if not self._closed:
                    self._events.put(("assistant-exit", {
                        "serial": key[0], "pid": key[1]}, None))

        threading.Thread(
            target=watch, daemon=True,
            name=f"mahjong-assistant-liveness-{row.serial}").start()

    def _replace_row(self, serial: str, **changes) -> DeviceRow | None:
        updated: list[DeviceRow] = []
        selected = None
        for row in self._rows:
            if row.serial == serial:
                row = replace(row, **changes)
                selected = row
            updated.append(row)
        if selected is not None:
            self._rows = updated
            self._render_if_changed(updated)
        return selected

    def _handle_assistant_exit(self, serial: str, pid: int) -> None:
        getattr(self, "_liveness_watches", set()).discard((serial, pid))
        row = next((item for item in self._rows
                    if item.serial == serial), None)
        if row is None or row.pid != pid:
            return
        desired = read_assistant_desired_open(
            PROFILE_BASE, serial, default=False)
        row = self._replace_row(serial, pid=None, desired_open=desired)
        if row is None or not desired:
            self.status_var.set(f"{serial} 辅助器已退出，保持关闭。")
            return
        if (not row.online or not row.account
                or serial in getattr(self, "_intentional_shutdowns", set())):
            self.status_var.set(
                f"{serial} 辅助器已退出；当前状态不满足自动恢复。")
            return
        self._schedule_recovery(serial, 0.0)

    def _schedule_recovery(self, serial: str, delay: float) -> None:
        scheduled = getattr(self, "_recovery_scheduled", None)
        if scheduled is None:
            scheduled = self._recovery_scheduled = set()
        if self._closed or serial in scheduled:
            return
        scheduled.add(serial)
        tokens = getattr(self, "_recovery_tokens", None)
        if tokens is None:
            tokens = self._recovery_tokens = {}
        token = tokens.get(serial, 0) + 1
        tokens[serial] = token

        def start() -> None:
            if tokens.get(serial) != token:
                return
            scheduled.discard(serial)
            self._start_recovery(serial)

        self.root.after(max(0, int(delay * 1000)), start)

    def _start_recovery(self, serial: str) -> None:
        if self._closed:
            return
        row = next((item for item in self._rows
                    if item.serial == serial), None)
        desired = read_assistant_desired_open(
            PROFILE_BASE, serial, default=False)
        if (row is None or not desired or not row.online or not row.account
                or row.pid is not None or serial in self._busy_serials
                or serial in getattr(self, "_intentional_shutdowns", set())
                or serial in getattr(self, "_recovery_inflight", set())):
            return
        failures, retry_at = getattr(self, "_recovery_failures", {}).get(
            serial, (0, 0.0))
        if failures >= AUTORECOVERY_FAILURE_BUDGET:
            return
        remaining = retry_at - time.monotonic()
        if remaining > 0:
            self._schedule_recovery(serial, remaining)
            return
        self._busy_serials.add(serial)
        self._recovery_inflight.add(serial)
        for button in self._buttons.get(serial, []):
            button.configure(state="disabled")
        self.status_var.set(f"{serial} 辅助器异常退出，正在安全恢复窗口…")
        self._submit(f"recover:{serial}", recover_window_only, serial)

    def _reconcile_known_desired_windows(self) -> None:
        """Serially recover missing windows from the current explicit snapshot."""
        if (self._closed or self._busy_serials
                or getattr(self, "_recovery_inflight", set())):
            return
        scheduled = getattr(self, "_recovery_scheduled", set())
        intentional = getattr(self, "_intentional_shutdowns", set())
        failures = getattr(self, "_recovery_failures", {})
        for row in self._rows:
            if (not row.online or not row.account or row.pid is not None
                    or row.serial in scheduled or row.serial in intentional
                    or failures.get(row.serial, (0, 0.0))[0]
                    >= AUTORECOVERY_FAILURE_BUDGET):
                continue
            desired = read_assistant_desired_open(
                PROFILE_BASE, row.serial, default=False)
            if not desired:
                if row.desired_open:
                    self._replace_row(row.serial, desired_open=False)
                continue
            self._schedule_recovery(row.serial, 0.0)
            return

    def _finish_assistant_recovery(self, serial: str, result,
                                   error: str | None) -> None:
        self._busy_serials.discard(serial)
        self._recovery_inflight.discard(serial)
        for button in self._buttons.get(serial, []):
            button.configure(state="normal")
        if error:
            previous, _retry_at = self._recovery_failures.get(serial, (0, 0.0))
            failures = previous + 1
            if failures >= AUTORECOVERY_FAILURE_BUDGET:
                self._recovery_failures[serial] = (failures, float("inf"))
                self.status_var.set(
                    f"{serial} 自动恢复连续失败 {failures} 次，已熔断；"
                    "请检查后手动打开，不会循环闪退。")
                self._reconcile_known_desired_windows()
                return
            delay = AUTORECOVERY_BACKOFF_S[
                min(failures - 1, len(AUTORECOVERY_BACKOFF_S) - 1)]
            self._recovery_failures[serial] = (
                failures, time.monotonic() + delay)
            self.status_var.set(
                f"{serial} 自动恢复失败：{error}；{delay:g} 秒后再试")
            self._schedule_recovery(serial, delay)
            self._reconcile_known_desired_windows()
            return
        action, pid = result
        if action == "offline":
            detail = "雷电已离线，请手动刷新或启动本行雷电"
            self._replace_row(serial, online=False, pid=None)
            self.status_var.set(f"{serial}：{detail}；不会后台等待。")
            self._reconcile_known_desired_windows()
            return
        if action == "desired_false":
            row = self._replace_row(
                serial, desired_open=False, pid=(pid or None))
            if row is not None and pid:
                self._arm_liveness_watch(row)
                detail = f"退出意图已确认，等待窗口 PID {pid} 结束"
            else:
                detail = "恢复意图已取消"
            self.status_var.set(f"{serial}：{detail}；不会恢复代打。")
            self._reconcile_known_desired_windows()
            return
        if not read_assistant_desired_open(
                PROFILE_BASE, serial, default=False):
            row = self._replace_row(serial, pid=pid, desired_open=False)
            if row is not None and pid:
                self._arm_liveness_watch(row)
            self.status_var.set(
                f"{serial}：恢复期间收到退出意图，等待窗口 PID {pid} 结束；"
                "不会恢复代打。")
            self._reconcile_known_desired_windows()
            return
        self._recovery_failures.pop(serial, None)
        row = self._replace_row(serial, pid=pid, desired_open=True)
        if row is not None:
            self._arm_liveness_watch(row)
        verb = "已发现现有" if action == "already_running" else "已重新打开"
        self.status_var.set(f"{serial}：{verb}辅助器窗口（PID {pid}）")
        self._reconcile_known_desired_windows()

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
        online_count = sum(row.online for row in rows)
        saved_count = sum(row.account is not None for row in rows)
        self.count_var.set(
            f"在线雷电 {online_count} 台 · 已保存设备 {saved_count} 台 · "
            f"辅助器已开 {sum(r.pid is not None for r in rows)} 台")
        if not rows:
            tk.Label(self.list_frame,
                     text="没有设备配置。请先在雷电多开器中创建实例。",
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
            connection = "当前登录待核验" if row.online else "雷电离线，设置已保留"
            tk.Label(details, text=f"本地绑定：{nickname} · {connection}",
                     bg=SURFACE, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))
            state = (f"辅助器运行中 · PID {row.pid}" if row.pid is not None
                     else ("辅助器未打开" if row.online
                           else "雷电离线 · 可手动启动"))
            tk.Label(panel, text=state, bg=SURFACE,
                     fg=GREEN if row.pid is not None else MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=12)
            controls = tk.Frame(panel, bg=SURFACE)
            controls.pack(side="right")
            buttons: list[tk.Button] = []
            if can_start_offline_device(row):
                button = tk.Button(
                    controls, text="启动雷电＋辅助器",
                    command=lambda serial=row.serial:
                        self._start_offline_device(serial),
                    bg=GOLD, fg="#22252e", activebackground="#ffd989",
                    activeforeground="#22252e", relief="flat", padx=12,
                    pady=8, font=("Microsoft YaHei UI", 9, "bold"),
                    state=("disabled" if row.serial in self._busy_serials
                           else "normal"))
                button.pack(side="left")
                buttons.append(button)
            elif row.pid is not None:
                operations = [
                    ("恢复窗口", "open", GOLD, "#22252e"),
                    ("退出", "quit", BG, MUTED),
                ]
                if row.online:
                    operations.insert(
                        1, ("重启", "restart", SURFACE_LIGHT, TEXT))
                for text, operation, background, foreground in operations:
                    button = tk.Button(
                        controls, text=text,
                        command=lambda serial=row.serial, op=operation:
                            self._operate_device(serial, op),
                        bg=background, fg=foreground,
                        activebackground="#465168", activeforeground=TEXT,
                        relief="flat", padx=10, pady=8,
                        font=("Microsoft YaHei UI", 9, "bold"),
                        state=("disabled" if row.serial in self._busy_serials
                               else "normal"))
                    button.pack(side="left", padx=(5, 0))
                    buttons.append(button)
            elif row.online:
                button = tk.Button(
                    controls, text="打开辅助器",
                    command=lambda serial=row.serial: self._open_device(serial),
                    bg=GOLD, fg="#22252e", activebackground="#ffd989",
                    activeforeground="#22252e", relief="flat", padx=12,
                    pady=8, font=("Microsoft YaHei UI", 9, "bold"),
                    state=("disabled" if row.serial in self._busy_serials
                           else "normal"))
                button.pack(side="left")
                buttons.append(button)
            else:
                tk.Label(controls, text="配置保留中", bg=SURFACE, fg=MUTED,
                         font=("Microsoft YaHei UI", 9)).pack(side="left")
            self._buttons[row.serial] = buttons

    def _render_if_changed(self, rows: list[DeviceRow]) -> bool:
        """Avoid destroying Tk rows when a refresh reports identical state."""
        snapshot = tuple(rows)
        if snapshot == self._rendered_rows:
            return False
        self._rendered_rows = snapshot
        self._render(rows)
        return True

    def _finish_ldplayer_start(self, serial: str, result,
                               error: str | None) -> None:
        self._busy_serials.discard(serial)
        for button in self._buttons.get(serial, []):
            button.configure(state="normal")
        if error:
            message = f"{serial}：启动失败：{error}"
        else:
            ld_status, action, pid = result
            ld_text = "雷电已在线" if ld_status == "already_online" else "雷电已启动"
            action_text = "辅助器已恢复" if action == "restored" else "辅助器已打开"
            message = f"{serial}：{ld_text}，{action_text}（PID {pid}）"
        # This is an explicit operation completion, not a background poll.
        self.refresh(status_after=message)

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
                    self._post_refresh_status = None
                    self.status_var.set(f"设备检查失败：{error}")
                else:
                    self._rows = result
                    self._render_if_changed(result)
                    for row in result:
                        self._arm_liveness_watch(row)
                    self._ensure_incident_watchdog(result)
                    message = self._post_refresh_status
                    self._post_refresh_status = None
                    self.status_var.set(
                        message or "选择对应雷电打开或恢复辅助器。")
            elif tag.startswith("start-ld:"):
                serial = tag.split(":", 1)[1]
                self._finish_ldplayer_start(serial, result, error)
            elif tag == "assistant-exit" and result is not None:
                self._handle_assistant_exit(
                    str(result.get("serial") or ""), int(result.get("pid") or 0))
            elif tag.startswith("recover:"):
                serial = tag.split(":", 1)[1]
                self._finish_assistant_recovery(serial, result, error)
            elif any(tag.startswith(f"{operation}:")
                     for operation in ("open", "quit", "restart")):
                operation, serial = tag.split(":", 1)
                self._busy_serials.discard(serial)
                if error:
                    if operation == "quit":
                        getattr(self, "_intentional_shutdowns", set()).discard(serial)
                    message = f"{serial}：{error}"
                    self.status_var.set(message)
                    for button in self._buttons.get(serial, []):
                        button.configure(state="normal")
                    if operation in {"open", "restart"}:
                        # An explicit operation may have partially changed the
                        # PID.  Take one explicit fresh snapshot, then let the
                        # normal serial reconciler decide whether to recover.
                        self.refresh(status_after=message)
                    elif self._activation_pending:
                        self.refresh()
                else:
                    if operation in {"open", "restart", "quit"}:
                        getattr(self, "_intentional_shutdowns", set()).discard(serial)
                    if operation in {"open", "restart"}:
                        self._reset_recovery_state(serial)
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
                self._run_pending_activation()
                self._reconcile_known_desired_windows()
            elif tag == "watchdog" and result is not None:
                self._apply_watchdog_event(result, error)
        self._retry_incident_watchdog_if_needed()
        self.root.after(150, self._poll)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
