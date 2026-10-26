"""The manager must label local identity honestly and never duplicate a UI."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import launcher_window as manager
from instance_profile import profile_paths


def test_collect_device_rows_keeps_each_local_binding_separate(tmp_path, monkeypatch):
    serials = ["emulator-5554", "emulator-5556"]
    for serial, name in zip(serials, ["玉姐", "烘熊瞎"], strict=True):
        path = profile_paths(tmp_path, serial).binding
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"serial": serial, "account": name, "mode": "tea_2p"}),
                        encoding="utf-8")
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: serials)
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})

    rows = manager.collect_device_rows(base=tmp_path)

    assert rows == [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None),
    ]


def test_open_existing_device_restores_without_starting(monkeypatch):
    requested = []
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: ["emulator-5554"])
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda serial, command: requested.append((serial, command)))
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: pytest.fail("duplicate assistant started"))

    assert manager.open_or_restore("emulator-5554") == ("restored", 1200)
    assert requested == [("emulator-5554", "restore")]


def test_open_offline_device_refuses_start(monkeypatch):
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: [])
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: pytest.fail("offline assistant started"))

    with pytest.raises(RuntimeError, match="已离线"):
        manager.open_or_restore("emulator-5554")


def test_open_free_online_device_starts_one_ui(monkeypatch):
    launched = []
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: ["emulator-5556"])
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "start_window",
                        lambda serial: launched.append(serial) or SimpleNamespace(pid=1300))

    assert manager.open_or_restore("emulator-5556") == ("started", 1300)
    assert launched == ["emulator-5556"]


def test_restore_timeout_is_reported_not_claimed(monkeypatch):
    ticket = object()
    monkeypatch.setattr(manager, "request_control",
                        lambda serial, command: ticket)
    monkeypatch.setattr(manager, "wait_for_ack",
                        lambda request, timeout: None)

    with pytest.raises(RuntimeError, match="没有收到辅助器 ACK"):
        manager._request_confirmed("emulator-5556", "restore", timeout=0.01)


def test_negative_ack_surfaces_assistant_detail(monkeypatch):
    ticket = object()
    monkeypatch.setattr(manager, "request_control",
                        lambda serial, command: ticket)
    monkeypatch.setattr(
        manager, "wait_for_ack",
        lambda request, timeout: SimpleNamespace(ok=False, detail="动作仍在验证"))

    with pytest.raises(RuntimeError, match="动作仍在验证"):
        manager._request_confirmed("emulator-5556", "quit", timeout=0.01)


def test_quit_waits_for_ack_and_real_process_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda serial, command: calls.append((serial, command)))
    monkeypatch.setattr(manager, "_wait_for_window_exit",
                        lambda pid: calls.append(("wait", pid)) or True)

    assert manager.quit_assistant("emulator-5554") == ("stopped", 1200)
    assert calls == [("emulator-5554", "quit"), ("wait", 1200)]


def test_restart_never_starts_second_ui_before_old_pid_exits(monkeypatch):
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5554"])
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda _serial, _command: "")
    monkeypatch.setattr(manager, "_wait_for_window_exit", lambda _pid: False)
    monkeypatch.setattr(
        manager, "start_window",
        lambda _serial: pytest.fail("new UI started before old PID exited"))

    with pytest.raises(RuntimeError, match="未按时退出"):
        manager.restart_assistant("emulator-5554")


def test_restart_starts_same_serial_after_confirmed_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5554"])
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda serial, command: calls.append((serial, command)))
    monkeypatch.setattr(manager, "_wait_for_window_exit",
                        lambda pid: calls.append(("wait", pid)) or True)
    monkeypatch.setattr(
        manager, "start_window",
        lambda serial: calls.append(("start", serial)) or SimpleNamespace(pid=1300))

    assert manager.restart_assistant("emulator-5554") == ("restarted", 1300)
    assert calls == [
        ("emulator-5554", "quit"), ("wait", 1200),
        ("start", "emulator-5554"),
    ]


def test_offline_restart_keeps_existing_ui(monkeypatch):
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: [])
    monkeypatch.setattr(
        manager, "_request_confirmed",
        lambda *_args: pytest.fail("offline existing UI was told to quit"))

    with pytest.raises(RuntimeError, match="保留现有辅助器"):
        manager.restart_assistant("emulator-5554")


def test_desktop_activation_prefers_first_free_serial():
    rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None),
    ]

    assert manager.choose_activation_target(rows) == rows[1]


def test_desktop_activation_restores_hidden_window_when_all_open(monkeypatch):
    rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", 1300),
    ]
    monkeypatch.setattr(
        manager, "choose_existing_for_restore",
        lambda devices, occupied: ("emulator-5556", 1300))

    assert manager.choose_activation_target(rows) == rows[1]


def test_desktop_activation_with_no_device_waits():
    assert manager.choose_activation_target([]) is None


def test_duplicate_device_operation_is_not_submitted_twice():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._closed = False
    launcher._busy_serials = {"emulator-5554"}
    launcher._buttons = {}
    messages = []
    launcher.status_var = SimpleNamespace(set=messages.append)
    launcher._submit = lambda *_args: pytest.fail("duplicate operation submitted")

    launcher._operate_device("emulator-5554", "open")

    assert messages == ["emulator-5554 正在处理中；不会重复提交操作。"]


def test_pending_desktop_activation_opens_exactly_one_free_serial():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._activation_pending = True
    launcher._closed = False
    launcher._busy_serials = set()
    launcher._rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None),
    ]
    launcher._cancel_empty_refresh = lambda: None
    opened = []
    launcher._operate_device = lambda serial, operation: opened.append(
        (serial, operation))

    launcher._run_pending_activation()
    launcher._run_pending_activation()

    assert launcher._activation_pending is False
    assert opened == [("emulator-5556", "open")]


def test_no_device_keeps_activation_pending_and_schedules_retry():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._activation_pending = True
    launcher._closed = False
    launcher._busy_serials = set()
    launcher._rows = []
    messages = []
    launcher.status_var = SimpleNamespace(set=messages.append)
    scheduled = []
    launcher._schedule_empty_refresh = lambda: scheduled.append(True)

    launcher._run_pending_activation()

    assert launcher._activation_pending is True
    assert scheduled == [True]
    assert "自动等待" in messages[-1]


def test_incident_watchdog_same_serial_set_is_single_instance():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_serials = ("emulator-5554",)
    launcher._watchdog_thread = SimpleNamespace(is_alive=lambda: True)
    launcher._watchdog_generation = 7
    launcher._closed = False

    launcher._ensure_incident_watchdog([
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200)])

    assert launcher._watchdog_generation == 7


def test_incident_watchdog_failure_is_visible_but_not_launcher_fatal():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_generation = 3
    launcher._watchdog_retry_at = 0.0
    messages = []
    launcher.watchdog_var = SimpleNamespace(set=messages.append)

    launcher._apply_watchdog_event({
        "generation": 3,
        "state": "failed",
        "serials": ("emulator-5554",),
    }, "RuntimeError: unavailable")

    assert "启动失败" in messages[-1]
    assert "辅助器仍可正常使用" in messages[-1]
    assert launcher._watchdog_retry_at > 0.0


def test_incident_watchdog_exit_stops_and_joins_owned_thread():
    stops = []
    joins = []
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_generation = 2
    launcher._watchdog = SimpleNamespace(stop=lambda: stops.append(True))
    launcher._watchdog_thread = SimpleNamespace(
        join=lambda timeout: joins.append(timeout), is_alive=lambda: False)
    launcher._watchdog_config_thread = None

    assert launcher._stop_incident_watchdog() is True

    assert stops == [True]
    assert joins == [3.0]
    assert launcher._watchdog is None
    assert launcher._watchdog_thread is None


def test_watchdog_join_timeout_never_starts_second_instance(monkeypatch):
    class _BlockingThread:
        def __init__(self) -> None:
            self.joins = []

        def join(self, timeout: float) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return True

    stops = []
    old_watcher = SimpleNamespace(stop=lambda: stops.append(True))
    old_thread = _BlockingThread()
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_lock = manager.threading.Lock()
    launcher._watchdog = old_watcher
    launcher._watchdog_thread = old_thread
    launcher._watchdog_serials = ("emulator-5554",)
    launcher._watchdog_generation = 5
    launcher._closed = False
    launcher._events = manager.Queue()
    monkeypatch.setattr(
        manager, "build_incident_watchdog",
        lambda _serials: pytest.fail("second watchdog created before old exit"))

    launcher._replace_watchdog(5, ("emulator-5556",))

    assert stops == [True]
    assert old_thread.joins == [3.0]
    assert launcher._watchdog is old_watcher
    assert launcher._watchdog_thread is old_thread
    assert launcher._watchdog_serials == ()
    tag, result, error = launcher._events.get_nowait()
    assert tag == "watchdog"
    assert result["state"] == "stopping"
    assert error is None


def test_watchdog_replacement_starts_after_old_thread_really_exits(monkeypatch):
    starts = []
    old = SimpleNamespace(stop=lambda: None)
    old_thread = SimpleNamespace(
        join=lambda timeout: None, is_alive=lambda: False)
    new = SimpleNamespace(run=lambda: None, stop=lambda: None)

    class _NewThread:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            starts.append(self.kwargs["name"])

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            pass

    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_lock = manager.threading.Lock()
    launcher._watchdog = old
    launcher._watchdog_thread = old_thread
    launcher._watchdog_generation = 6
    launcher._closed = False
    launcher._events = manager.Queue()
    monkeypatch.setattr(manager, "build_incident_watchdog",
                        lambda serials: new)
    monkeypatch.setattr(manager.threading, "Thread", _NewThread)

    launcher._replace_watchdog(6, ("emulator-5556",))

    assert launcher._watchdog is new
    assert launcher._watchdog_thread is not old_thread
    assert starts == ["mahjong-incident-watchdog"]


def test_manager_exit_waits_for_slow_watchdog_before_destroying():
    class _SlowThread:
        def __init__(self) -> None:
            self.alive = True
            self.joins = []

        def join(self, timeout: float) -> None:
            self.joins.append(timeout)

        def is_alive(self) -> bool:
            return self.alive

    stopped = []
    scheduled = []
    destroyed = []
    messages = []
    run_thread = _SlowThread()
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._closed = False
    launcher._empty_refresh_after = None
    launcher._watchdog_generation = 1
    launcher._watchdog = SimpleNamespace(stop=lambda: stopped.append(True))
    launcher._watchdog_thread = run_thread
    launcher._watchdog_config_thread = None
    launcher.watchdog_var = SimpleNamespace(set=messages.append)
    launcher.root = SimpleNamespace(
        after=lambda delay, callback: scheduled.append((delay, callback)),
        destroy=lambda: destroyed.append(True),
    )

    launcher.close()

    assert stopped == [True]
    assert destroyed == []
    assert "正在安全停止" in messages[-1]
    assert scheduled[0][0] == 250

    run_thread.alive = False
    scheduled.pop(0)[1]()
    assert destroyed == [True]
    assert launcher._watchdog is None


def test_incident_watchdog_running_status_names_device_count():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._watchdog_generation = 4
    messages = []
    launcher.watchdog_var = SimpleNamespace(set=messages.append)

    launcher._apply_watchdog_event({
        "generation": 4,
        "state": "running",
        "serials": ("emulator-5554", "emulator-5556"),
    }, None)

    assert messages == ["自动故障包：正在守护 2 台雷电"]


def test_manager_main_signals_existing_instance_without_opening_tk(monkeypatch):
    monkeypatch.setattr(manager, "acquire_manager_instance", lambda: None)
    signals = []
    monkeypatch.setattr(manager, "request_restore", signals.append)
    monkeypatch.setattr(manager.tk, "Tk",
                        lambda: pytest.fail("second manager opened"))

    manager.main()

    assert signals == [manager.MANAGER_SIGNAL]


def test_manager_main_honors_pre_mutex_migration_instance(monkeypatch):
    closed = []
    guard = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(manager, "acquire_manager_instance", lambda: guard)
    monkeypatch.setattr(manager, "existing_manager_pid", lambda: 48772)
    signals = []
    monkeypatch.setattr(manager, "request_restore", signals.append)
    monkeypatch.setattr(manager.tk, "Tk",
                        lambda: pytest.fail("migration duplicate manager opened"))

    manager.main()

    assert closed == [True]
    assert signals == [manager.MANAGER_SIGNAL]
