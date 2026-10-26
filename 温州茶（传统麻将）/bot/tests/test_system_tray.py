"""Tray lifecycle stays single-instance even when Windows exits slowly."""
from __future__ import annotations

from types import SimpleNamespace

import system_tray


class _Thread:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.joins = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float) -> None:
        self.joins.append(timeout)


def _icon(tmp_path):
    path = tmp_path / "pet.ico"
    path.write_bytes(b"ico")
    return system_tray.WindowsTrayIcon(path, "麻将", lambda _action: None)


def test_start_refuses_duplicate_when_previous_thread_is_still_exiting(tmp_path):
    icon = _icon(tmp_path)
    old = _Thread(alive=True)
    icon._thread = old
    icon._registered = False
    icon._hwnd = None

    assert icon.start(timeout=0) is False
    assert icon._thread is old
    assert "未创建重复图标" in icon.last_error


def test_stop_keeps_live_thread_reference_after_timeout(tmp_path, monkeypatch):
    icon = _icon(tmp_path)
    old = _Thread(alive=True)
    icon._thread = old
    icon._hwnd = 77
    fake_gui = SimpleNamespace(PostMessage=lambda *_args: None)
    fake_con = SimpleNamespace(WM_CLOSE=16)
    monkeypatch.setattr(system_tray, "win32gui", fake_gui)
    monkeypatch.setattr(system_tray, "win32con", fake_con)

    icon.stop(timeout=0.01)

    assert old.joins == [0.01]
    assert icon._thread is old
    assert "保留线程引用" in icon.last_error


def test_stop_clears_reference_after_thread_exits(tmp_path):
    icon = _icon(tmp_path)
    old = _Thread(alive=False)
    icon._thread = old

    icon.stop(timeout=0.01)

    assert old.joins == [0.01]
    assert icon._thread is None
