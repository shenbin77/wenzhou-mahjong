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


def test_packed_windows_11_callback_keeps_low_word_mouse_event(monkeypatch):
    fake_con = SimpleNamespace(
        WM_LBUTTONUP=0x0202,
        WM_LBUTTONDBLCLK=0x0203,
        WM_RBUTTONUP=0x0205,
        WM_CONTEXTMENU=0x007B,
    )
    monkeypatch.setattr(system_tray, "win32con", fake_con)

    packed_right_click = (7 << 16) | fake_con.WM_CONTEXTMENU
    packed_double_click = (7 << 16) | fake_con.WM_LBUTTONDBLCLK

    assert system_tray.tray_mouse_event(packed_right_click) == 0x007B
    assert system_tray.tray_message_opens_menu(packed_right_click) is True
    assert (system_tray.tray_action_for_mouse_message(packed_double_click)
            == system_tray.TRAY_ACTION_RESTORE)


def test_legacy_right_button_up_still_opens_menu(monkeypatch):
    fake_con = SimpleNamespace(
        WM_LBUTTONUP=0x0202,
        WM_LBUTTONDBLCLK=0x0203,
        WM_RBUTTONUP=0x0205,
        WM_CONTEXTMENU=0x007B,
    )
    monkeypatch.setattr(system_tray, "win32con", fake_con)

    assert system_tray.tray_message_opens_menu(fake_con.WM_RBUTTONUP) is True


def test_v4_context_menu_anchor_decodes_signed_words():
    packed = ((200 & 0xFFFF) << 16) | (150 & 0xFFFF)
    assert system_tray.tray_menu_anchor(packed) == (150, 200)
    assert system_tray.tray_menu_anchor(0xFFFFFFFF) is None


def test_add_icon_sets_v4_after_every_registration(tmp_path, monkeypatch):
    icon = _icon(tmp_path)
    calls = []
    icon._hwnd = 77
    icon._hicon = 88
    fake_gui = SimpleNamespace(
        NIF_ICON=1, NIF_MESSAGE=2, NIF_TIP=4, NIM_ADD=0,
        Shell_NotifyIcon=lambda operation, data: calls.append(
            ("add", operation, data)),
    )
    monkeypatch.setattr(system_tray, "win32gui", fake_gui)
    monkeypatch.setattr(
        system_tray, "win32con", SimpleNamespace())
    monkeypatch.setattr(
        system_tray, "_set_notify_version_4",
        lambda hwnd, icon_id: calls.append(("v4", hwnd, icon_id)))

    icon._add_icon()

    assert calls[0][0] == "add"
    assert calls[1] == ("v4", 77, icon._ICON_ID)
    assert icon._version4 is True
