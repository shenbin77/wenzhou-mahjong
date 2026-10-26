# -*- coding: utf-8 -*-
"""Close-box semantics of the assist window: X collapses, only quit exits.

The product decision is that the main window's close box (X) must *not* end
the process: pressing it collapses the window into the right-bottom floating
bar while the follow loop, the log tailers, the full-auto thread and the
200 ms ``_tick`` cadence keep running.  Really exiting is an explicit act
(floating-bar "×" control, floating-bar context menu, 更多 menu).

These tests stay offline: they either use ``TeaAssistWindow.__new__`` with
recording doubles for the widgets they touch, or they verify the data-driven
menu/labels that the Tk widgets are built from.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from assist_window import (  # noqa: E402
    CLOSE_ACTION_COLLAPSE,
    CLOSE_ACTION_QUIT,
    FLOATING_BAR_CLOSE_SIZE,
    FLOATING_BAR_MENU_ITEMS,
    FLOATING_BAR_MENU_QUIT,
    FLOATING_BAR_MENU_RESTORE,
    FLOATING_BAR_SIZE,
    QUIT_MENU_LABEL,
    TeaAssistWindow,
)


# -- doubles -------------------------------------------------------------
class _ProtocolRoot:
    """Records ``protocol()`` registrations instead of creating a WM handler."""

    def __init__(self) -> None:
        self.calls = []

    def protocol(self, name, func):
        self.calls.append((name, func))


class _NoTouchRoot:
    """Fails loudly if an already-collapsed window touches its root."""

    def __getattr__(self, name):
        raise AssertionError(
            f"root.{name} must not be used when the window is already collapsed")


class _ExistingBar:
    """A floating bar that already exists (second X press path)."""

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
    """Minimal ``tk.StringVar`` stand-in."""

    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _TickRefresh:
    """``_tick`` only reads ``last_frame_at`` when no frame has arrived."""

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


class _RecordingMenu:
    """Records the rows a menu double would display."""

    def __init__(self) -> None:
        self.rows = []

    def add_separator(self):
        self.rows.append(("separator", None))

    def add_command(self, label=None, command=None):
        self.rows.append((label, command))


def _bare_window() -> TeaAssistWindow:
    """An AssistWindow with no Tk widgets, as the older unit tests build it."""
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._quitting = False
    window._floating_bar = None
    return window


# -- close box -> collapse ----------------------------------------------
def test_window_close_action_is_collapse_never_quit():
    window = _bare_window()
    assert window.window_close_action() == CLOSE_ACTION_COLLAPSE
    assert window.window_close_action() != CLOSE_ACTION_QUIT


def test_close_box_collapses_once_and_never_quits(monkeypatch):
    """X must collapse exactly once and must not run any quit path."""
    window = _bare_window()
    calls = []
    monkeypatch.setattr(TeaAssistWindow, "collapse_to_floating_bar",
                        lambda self: calls.append("collapse"))
    monkeypatch.setattr(TeaAssistWindow, "quit_app",
                        lambda self: calls.append("quit"))

    assert window.on_window_close() == CLOSE_ACTION_COLLAPSE
    assert calls == ["collapse"], (
        f"close box must collapse only, got {calls!r}")
    assert calls.count("quit") == 0


def test_main_window_protocol_targets_the_collapse_handler():
    window = _bare_window()
    window.root = _ProtocolRoot()

    window._bind_window_close_protocol()

    assert len(window.root.calls) == 1, (
        "the close box must be bound exactly once")
    name, handler = window.root.calls[0]
    assert name == "WM_DELETE_WINDOW"
    assert handler == window.on_window_close
    target = getattr(handler, "__func__", None)
    assert target is TeaAssistWindow.on_window_close
    assert target is not TeaAssistWindow.quit_app
    assert target is not TeaAssistWindow.close


def test_second_close_box_press_reuses_the_existing_bar():
    """Pressing X twice must not withdraw/destroy anything a second time."""
    window = _bare_window()
    bar = _ExistingBar()
    window._floating_bar = bar
    window.root = _NoTouchRoot()

    window.collapse_to_floating_bar()

    assert bar.lifted == 1
    assert bar.destroyed == 0


# -- floating bar: "×" control and context menu -------------------------
def test_floating_close_control_stays_smaller_than_the_bar():
    assert FLOATING_BAR_CLOSE_SIZE[0] < FLOATING_BAR_SIZE[0]
    assert FLOATING_BAR_CLOSE_SIZE[1] < FLOATING_BAR_SIZE[1]


def test_floating_close_control_quits_and_breaks_propagation(monkeypatch):
    """Clicking "×" quits and returns "break" so restore never fires."""
    window = _bare_window()
    calls = []
    monkeypatch.setattr(TeaAssistWindow, "quit_app",
                        lambda self: calls.append("quit"))
    monkeypatch.setattr(TeaAssistWindow, "restore_from_floating_bar",
                        lambda self, _event=None: calls.append("restore"))

    result = window._on_floating_close_click(None)

    assert result == "break"
    assert calls == ["quit"]


def test_break_result_stops_the_toplevel_restore_binding(monkeypatch):
    """Emulate Tk bindtag order: widget binding -> toplevel binding.

    ``close.bind("<Button-1>", ...)`` is a *widget* binding; the bar's
    "click anywhere to restore" is a *toplevel* binding that Tk dispatches
    afterwards.  Returning ``"break"`` from the widget binding stops that
    later tag, which is exactly why a click on "×" cannot restore the window.
    """
    window = _bare_window()
    log = []
    monkeypatch.setattr(TeaAssistWindow, "quit_app",
                        lambda self: log.append("quit"))

    def toplevel_binding(_event):
        log.append("restore")

    for handler in (window._on_floating_close_click, toplevel_binding):
        if handler(None) == "break":
            break

    assert log == ["quit"], "the toplevel restore binding must not run"
    assert "restore" not in log


def test_floating_menu_lists_restore_then_quit():
    labels = [label for label, _action in FLOATING_BAR_MENU_ITEMS]
    assert labels == ["恢复窗口", "退出程序"]


def test_floating_menu_quit_command_is_quit_app():
    window = _bare_window()
    quit_command = window._floating_menu_command(FLOATING_BAR_MENU_QUIT)
    restore_command = window._floating_menu_command(FLOATING_BAR_MENU_RESTORE)
    assert getattr(quit_command, "__func__", None) is TeaAssistWindow.quit_app
    assert (getattr(restore_command, "__func__", None)
            is TeaAssistWindow.restore_from_floating_bar)


def test_destroy_floating_bar_drops_stale_close_control():
    window = _bare_window()
    bar = _ExistingBar()
    window._floating_bar = bar
    window._floating_close_label = object()

    window._destroy_floating_bar()

    assert bar.destroyed == 1
    assert window._floating_bar is None
    assert window._floating_close_label is None, (
        "a stale × reference would be reused by the next collapse")


# -- 更多 menu ----------------------------------------------------------
def test_more_menu_appends_an_explicit_quit_entry():
    window = _bare_window()
    menu = _RecordingMenu()

    window._append_quit_menu_entry(menu)

    assert menu.rows[0][0] == "separator"
    label, command = menu.rows[1]
    assert label == QUIT_MENU_LABEL == "退出程序"
    assert getattr(command, "__func__", None) is TeaAssistWindow.quit_app


# -- explicit quit keeps the full teardown ------------------------------
def _quit_ready_window(tmp_path) -> TeaAssistWindow:
    window = _bare_window()
    window.root = _TearDownRoot()
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


def test_quit_app_keeps_the_full_teardown(tmp_path):
    window = _quit_ready_window(tmp_path)

    window.quit_app()

    assert window.stopped == 1
    assert window.video_stopped == 1
    assert window.remembered == 1
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
    assert window.root.destroyed == 1


def test_close_alias_still_quits(tmp_path):
    """Existing callers/tests use ``close()``; it must stay an explicit quit."""
    window = _quit_ready_window(tmp_path)

    window.close()

    assert window.root.destroyed == 1


# -- regression: no new work inside _tick() -----------------------------
def test_tick_without_floating_bar_skips_floating_refresh():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.display_source = None
    window.refresh = _TickRefresh()
    window.fresh = _Value()
    window.period = _Value("周期 200 ms")
    window.root = _TickRoot()
    window.ticker = None
    refreshes = []
    window._refresh_floating_status = lambda: refreshes.append("refresh")

    window._tick()

    assert refreshes == [], (
        "window test doubles bind _tick alone; the floating refresh must stay "
        "guarded by an existing bar")
    assert window.ticker == "tick-token"


def test_tick_refreshes_floating_text_only_while_collapsed():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.display_source = None
    window.refresh = _TickRefresh()
    window.fresh = _Value()
    window.period = _Value("周期 200 ms")
    window.root = _TickRoot()
    window.ticker = None
    refreshes = []
    window._refresh_floating_status = lambda: refreshes.append("refresh")

    window._floating_bar = object()
    window._tick()

    assert refreshes == ["refresh"]
