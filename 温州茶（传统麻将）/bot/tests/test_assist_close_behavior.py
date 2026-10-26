# -*- coding: utf-8 -*-
"""Close-box, mascot-orb and notification-area lifecycle tests."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

BOT = Path(__file__).resolve().parent.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import assist_window as assist_module  # noqa: E402
from assist_window import (  # noqa: E402
    CLOSE_ACTION_COLLAPSE,
    CLOSE_ACTION_QUIT,
    FLOATING_ORB_MENU_ITEMS,
    FLOATING_ORB_MENU_QUIT,
    FLOATING_ORB_MENU_RESTORE,
    QUIT_MENU_LABEL,
    TRAY_ACTION_QUIT,
    TRAY_ACTION_RESTORE,
    TeaAssistWindow,
)


class _ProtocolRoot:
    def __init__(self) -> None:
        self.calls = []

    def protocol(self, name, func):
        self.calls.append((name, func))


class _NoTouchRoot:
    def __getattr__(self, name):
        raise AssertionError(
            f"root.{name} must not be used when already collapsed")


class _ExistingOrb:
    def __init__(self) -> None:
        self.lifted = 0
        self.destroyed = 0

    def winfo_exists(self):
        return True

    def lift(self):
        self.lifted += 1

    def destroy(self):
        self.destroyed += 1


class _Value:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _TickRefresh:
    last_frame_at = None


class _TickRoot:
    def __init__(self) -> None:
        self.delayed = []

    def after(self, delay, callback):
        self.delayed.append((delay, callback))
        return "tick-token"


class _TearDownRoot:
    def __init__(self) -> None:
        self.cancelled = []
        self.destroyed = 0

    def after_cancel(self, handle):
        self.cancelled.append(handle)

    def destroy(self):
        self.destroyed += 1


class _Executor:
    def __init__(self) -> None:
        self.shutdowns = []

    def shutdown(self, **kwargs):
        self.shutdowns.append(kwargs)


class _Closable:
    def __init__(self) -> None:
        self.stopped = 0
        self.closed = 0

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1


class _Tray:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _RecordingMenu:
    def __init__(self) -> None:
        self.rows = []

    def add_separator(self):
        self.rows.append(("separator", None))

    def add_command(self, label=None, command=None):
        self.rows.append((label, command))


def _bare_window() -> TeaAssistWindow:
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._quitting = False
    window._floating_orb = None
    return window


def test_window_close_action_is_collapse_never_quit():
    window = _bare_window()
    assert window.window_close_action() == CLOSE_ACTION_COLLAPSE
    assert window.window_close_action() != CLOSE_ACTION_QUIT


def test_close_box_collapses_to_orb_and_never_quits(monkeypatch):
    window = _bare_window()
    calls = []
    monkeypatch.setattr(TeaAssistWindow, "collapse_to_floating_orb",
                        lambda self: calls.append("collapse"))
    monkeypatch.setattr(TeaAssistWindow, "quit_app",
                        lambda self: calls.append("quit") or True)

    assert window.on_window_close() == CLOSE_ACTION_COLLAPSE
    assert calls == ["collapse"]


def test_main_window_protocol_targets_the_collapse_handler():
    window = _bare_window()
    window.root = _ProtocolRoot()
    window._bind_window_close_protocol()
    assert len(window.root.calls) == 1
    name, handler = window.root.calls[0]
    assert name == "WM_DELETE_WINDOW"
    assert handler == window.on_window_close
    assert getattr(handler, "__func__", None) is TeaAssistWindow.on_window_close


def test_second_close_box_press_reuses_existing_orb():
    window = _bare_window()
    orb = _ExistingOrb()
    window._floating_orb = orb
    window.root = _NoTouchRoot()

    window.collapse_to_floating_orb()

    assert orb.lifted == 1
    assert orb.destroyed == 0


def test_floating_orb_menu_lists_open_then_quit():
    labels = [label for label, _action in FLOATING_ORB_MENU_ITEMS]
    assert labels == ["打开主窗口", "退出程序"]


def test_floating_orb_menu_commands_are_explicit():
    window = _bare_window()
    quit_command = window._floating_menu_command(FLOATING_ORB_MENU_QUIT)
    restore_command = window._floating_menu_command(
        FLOATING_ORB_MENU_RESTORE)
    assert getattr(quit_command, "__func__", None) is TeaAssistWindow.quit_app
    assert (getattr(restore_command, "__func__", None)
            is TeaAssistWindow.restore_from_floating_orb)


def test_destroy_floating_orb_drops_image_and_widget_references():
    window = _bare_window()
    orb = _ExistingOrb()
    window._floating_orb = orb
    window._floating_canvas = object()
    window._floating_image = object()
    window._floating_status_dot = 1

    window._destroy_floating_orb()

    assert orb.destroyed == 1
    assert window._floating_orb is None
    assert window._floating_canvas is None
    assert window._floating_image is None
    assert window._floating_status_dot is None


def test_more_menu_appends_an_explicit_quit_entry():
    window = _bare_window()
    menu = _RecordingMenu()
    window._append_quit_menu_entry(menu)
    assert menu.rows[0][0] == "separator"
    label, command = menu.rows[1]
    assert label == QUIT_MENU_LABEL == "退出程序"
    assert getattr(command, "__func__", None) is TeaAssistWindow.quit_app


def test_tray_restore_runs_only_when_tk_thread_drains_queue(monkeypatch):
    window = _bare_window()
    window._tray_actions = Queue()
    calls = []
    monkeypatch.setattr(TeaAssistWindow, "restore_from_floating_orb",
                        lambda self: calls.append("restore"))
    window._tray_actions.put(TRAY_ACTION_RESTORE)
    assert calls == []
    assert window._drain_system_tray_actions() is True
    assert calls == ["restore"]


def test_tray_quit_stops_tick_after_tk_thread_drains_queue(monkeypatch):
    window = _bare_window()
    window._tray_actions = Queue()
    calls = []
    monkeypatch.setattr(TeaAssistWindow, "quit_app",
                        lambda self: calls.append("quit") or True)
    window._tray_actions.put(TRAY_ACTION_QUIT)
    assert window._drain_system_tray_actions() is False
    assert calls == ["quit"]


def test_structured_restore_acknowledges_after_window_is_restored(monkeypatch):
    window = _bare_window()
    window.serial = _Value("emulator-5556")
    request = SimpleNamespace(command="restore")
    events = []
    monkeypatch.setattr(
        assist_module, "consume_control", lambda serial: request)
    monkeypatch.setattr(
        assist_module, "acknowledge_control",
        lambda received, *, ok, detail="": events.append(
            ("ack", received, ok, detail)))
    window.restore_from_floating_orb = lambda: events.append(("restore",))

    assert window._process_window_control() is True
    assert events[0] == ("restore",)
    assert events[1][0:3] == ("ack", request, True)


def test_structured_quit_acknowledges_acceptance_before_teardown(monkeypatch):
    window = _bare_window()
    window.serial = _Value("emulator-5556")
    request = SimpleNamespace(command="quit")
    events = []
    monkeypatch.setattr(
        assist_module, "consume_control", lambda serial: request)
    monkeypatch.setattr(
        assist_module, "acknowledge_control",
        lambda received, *, ok, detail="": events.append(
            ("ack", received, ok, detail)))
    window._persist_quit_intent = lambda: (True, "")
    window.quit_app = lambda **_kwargs: events.append(("quit",)) or True

    assert window._process_window_control() is False
    assert events[0][0:3] == ("ack", request, True)
    assert events[1] == ("quit",)


def test_structured_control_exception_is_acknowledged_as_failure(monkeypatch):
    window = _bare_window()
    window.serial = _Value("emulator-5556")
    request = SimpleNamespace(command="restore")
    acknowledgements = []
    monkeypatch.setattr(
        assist_module, "consume_control", lambda serial: request)
    monkeypatch.setattr(
        assist_module, "acknowledge_control",
        lambda received, *, ok, detail="": acknowledgements.append(
            (received, ok, detail)))

    def fail_restore():
        raise RuntimeError("restore failed")

    window.restore_from_floating_orb = fail_restore

    assert window._process_window_control() is True
    assert acknowledgements[0][0] is request
    assert acknowledgements[0][1] is False
    assert "restore failed" in acknowledgements[0][2]


def _quit_ready_window(tmp_path) -> TeaAssistWindow:
    window = _bare_window()
    window.root = _TearDownRoot()
    window._system_tray = _Tray()
    window._adb_diagnostic_timer = "adb"
    window._auto_poll_timer = "auto"
    window._check_poll_timer = "check"
    window._review_poll_timer = "review"
    window.recorder_timer = "recorder"
    window.ticker = "ticker"
    window.latency_log = [{"action": "follow", "ms": 12}]
    window.session_dir = tmp_path / "session"
    window.executor = _Executor()
    window.auto_executor = _Executor()
    window._check_executor = _Executor()
    window.embedded_follow_log = _Closable()
    window.follow_session_log = _Closable()
    window.stopped = 0
    window.video_stopped = 0
    window.remembered = 0

    def stop():
        window.stopped += 1

    def stop_video_review():
        window.video_stopped += 1

    def remember_geometry():
        window.remembered += 1

    window.stop = stop
    window.stop_video_review = stop_video_review
    window._remember_geometry = remember_geometry
    return window


def test_quit_app_keeps_full_teardown_and_removes_tray(tmp_path):
    window = _quit_ready_window(tmp_path)
    window.quit_app()
    assert window.stopped == 1
    assert window.video_stopped == 1
    assert window.remembered == 1
    assert window._system_tray.stopped == 1
    assert window._suppress_review is True
    assert window.embedded_follow_log.stopped == 1
    assert window.follow_session_log.closed == 1
    assert window.recorder_timer is None and window.ticker is None
    assert window.root.cancelled == [
        "adb", "auto", "check", "review", "recorder", "ticker"]
    for executor in (window.executor, window.auto_executor,
                     window._check_executor):
        assert executor.shutdowns == [{"wait": False, "cancel_futures": True}]
    assert window.root.destroyed == 1
    latency = window.session_dir / "live_latency.jsonl"
    rows = [json.loads(line) for line in
            latency.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows == [{"action": "follow", "ms": 12}]


def test_quit_app_is_idempotent(tmp_path):
    window = _quit_ready_window(tmp_path)
    window.quit_app()
    window.quit_app()
    assert window.stopped == 1
    assert window._system_tray.stopped == 1
    assert window.root.destroyed == 1


def test_explicit_quit_persists_window_autorecovery_off(tmp_path, monkeypatch):
    window = _quit_ready_window(tmp_path)
    window._profile_paths = SimpleNamespace(serial="emulator-5554")
    writes = []
    monkeypatch.setattr(
        assist_module, "set_assistant_desired_open",
        lambda base, serial, desired: writes.append((base, serial, desired)))

    window.quit_app()

    assert len(writes) == 1
    assert writes[0][1:] == ("emulator-5554", False)


def test_explicit_quit_write_failure_cancels_exit_and_restores_window(
        tmp_path, monkeypatch):
    window = _quit_ready_window(tmp_path)
    window._profile_paths = SimpleNamespace(serial="emulator-5554")
    window.status = _Value()
    restored = []
    window.restore_from_floating_orb = lambda: restored.append(True)
    monkeypatch.setattr(
        assist_module, "set_assistant_desired_open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert window.quit_app() is False
    assert window.root.destroyed == 0
    assert window.stopped == 0
    assert restored == [True]
    assert "已取消退出" in window.status.get()


def test_close_alias_still_quits(tmp_path):
    window = _quit_ready_window(tmp_path)
    window.close()
    assert window.root.destroyed == 1


def test_tick_without_companion_skips_status_refresh():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.display_source = None
    window.refresh = _TickRefresh()
    window.fresh = _Value()
    window.period = _Value("周期 200 ms")
    window.root = _TickRoot()
    window.ticker = None
    refreshes = []
    window._refresh_companion_status = lambda: refreshes.append("refresh")
    window._tick()
    assert refreshes == []
    assert window.ticker == "tick-token"


def test_tick_refreshes_status_while_orb_is_visible():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.display_source = None
    window.refresh = _TickRefresh()
    window.fresh = _Value()
    window.period = _Value("周期 200 ms")
    window.root = _TickRoot()
    window.ticker = None
    window._floating_orb = object()
    refreshes = []
    window._refresh_companion_status = lambda: refreshes.append("refresh")
    window._tick()
    assert refreshes == ["refresh"]


def test_companion_signal_requires_fresh_current_account_match():
    window = _bare_window()
    window.account = _Value("玉姐")
    window.serial = _Value("emulator-5556")
    assert window._companion_nickname() == "玉姐"
    assert window._companion_identity_text() == "账号待核验"
    window._identity_state = "match"
    window._identity_key = ("玉姐", "emulator-5556")
    window._identity_verified_at = time.perf_counter()
    assert window._companion_identity_text() == "当前画面已核验"
    assert window._companion_status_color() == "#00c853"
    window.account.set("另一个玩家")
    assert window._companion_identity_text() == "账号待核验"
    assert window._companion_status_color() == "#ffb300"
