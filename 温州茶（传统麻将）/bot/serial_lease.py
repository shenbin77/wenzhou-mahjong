"""Fail-closed cross-process lease for one simulator's real-click operator.

The OS owns the lock's lifetime: process death cannot leave a stale PID lock.
An abandoned Windows mutex deliberately does *not* grant click authority.
Read-only/dry-run sessions do not need to acquire this lease.
"""
from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from functools import wraps

_HELD: set[str] = set()
_GUARD = threading.Lock()


class SerialBusy(RuntimeError):
    """Another real-click operator already owns this emulator."""


@contextmanager
def hold_serial(serial: str):
    """Hold one serial lease until the run loop and its post-click checks exit."""
    if not serial or any(ch.isspace() for ch in serial):
        raise ValueError("模拟器序列号无效")
    token = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:24]
    with _GUARD:
        if token in _HELD:
            raise SerialBusy(f"设备 {serial} 已被当前进程中的另一个代打实例占用")
        _HELD.add(token)
    handle = None
    acquired = False
    try:
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel.ReleaseMutex.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.CreateMutexW(None, 0, "Local\\WZMJ_AUTOPLAY_" + token)
            if not handle:
                raise SerialBusy("无法创建设备互斥锁")
            status = kernel.WaitForSingleObject(handle, 0)
            if status == 0x0:
                acquired = True
            elif status == 0x80:
                # A previous process died while controlling this serial.
                kernel.ReleaseMutex(handle)
                raise SerialBusy("上次操控进程异常终止；设备需人工确认后重试")
            else:
                raise SerialBusy(f"设备 {serial} 已由其他进程接管")
        else:
            import fcntl
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "wzmj_autoplay_" + token + ".lock")
            handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SerialBusy(f"设备 {serial} 已由其他进程接管") from exc
            acquired = True
        yield
    finally:
        try:
            if handle is not None:
                if os.name == "nt":
                    if acquired:
                        kernel.ReleaseMutex(handle)
                    kernel.CloseHandle(handle)
                else:
                    if acquired:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    os.close(handle)
        finally:
            with _GUARD:
                _HELD.discard(token)


def guard_live_run(func):
    """Protect the entire live run with a per-serial OS mutex.

    Injected dry-run test loops remain concurrently executable.
    """
    @wraps(func)
    def guarded(self, *args, **kwargs):
        if not self.enabled:
            return func(self, *args, **kwargs)
        with hold_serial(self.serial):
            return func(self, *args, **kwargs)
    return guarded
