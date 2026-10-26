"""The manager must label local identity honestly and never duplicate a UI."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import launcher_window as manager
from instance_profile import profile_paths

WAIT_FOR_STARTED_WINDOW = manager._wait_for_started_window


@pytest.fixture(autouse=True)
def _isolate_lifecycle_state(monkeypatch):
    """Launcher unit tests must never touch the operator's real profile state."""
    monkeypatch.setattr(
        manager, "set_assistant_desired_open", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        manager, "_wait_for_started_window", lambda *_args, **_kwargs: None)


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


def test_collect_device_rows_keeps_saved_binding_visible_while_offline(
        tmp_path, monkeypatch):
    offline = profile_paths(tmp_path, "emulator-5556").binding
    offline.parent.mkdir(parents=True)
    offline.write_text(json.dumps({
        "serial": "emulator-5556", "account": "烘熊瞎", "mode": "tea_2p",
    }), encoding="utf-8")
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5554"])
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})

    rows = manager.collect_device_rows(base=tmp_path)

    assert rows == [
        manager.DeviceRow("emulator-5554", None, None, None,
                          online=True, desired_open=False),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None,
                          online=False, desired_open=True),
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
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: pytest.fail("offline assistant started"))

    with pytest.raises(RuntimeError, match="已离线"):
        manager.open_or_restore("emulator-5554")


def test_existing_assistant_can_be_restored_while_emulator_is_offline(monkeypatch):
    requested = []
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: [])
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5554": 1200})
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda serial, command: requested.append((serial, command)))

    assert manager.open_or_restore("emulator-5554") == ("restored", 1200)
    assert requested == [("emulator-5554", "restore")]


def test_open_free_online_device_starts_one_ui(monkeypatch):
    launched = []
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: ["emulator-5556"])
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "start_window",
                        lambda serial: launched.append(serial) or SimpleNamespace(pid=1300))

    assert manager.open_or_restore("emulator-5556") == ("started", 1300)
    assert launched == ["emulator-5556"]


def test_window_only_recovery_revokes_stale_authority_and_waits_for_ack(
        monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "discover_profile_bindings",
                        lambda _base: {"emulator-5556": {"account": "烘熊瞎"}})
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5556"])
    monkeypatch.setattr(
        manager, "start_window",
        lambda serial: calls.append(("start", serial)) or SimpleNamespace(pid=1300))
    monkeypatch.setattr(manager, "_wait_for_started_window",
                        lambda serial, process: calls.append(("ack", serial, process.pid)))
    import full_auto_control
    monkeypatch.setattr(full_auto_control, "revoke_pending_command",
                        lambda serial: calls.append(("revoke", serial)) or True)

    assert manager.recover_window_only("emulator-5556") == ("started", 1300)
    assert calls == [
        ("revoke", "emulator-5556"),
        ("start", "emulator-5556"),
        ("ack", "emulator-5556", 1300),
    ]


def test_window_only_recovery_refuses_spawn_if_stale_authority_cannot_be_revoked(
        monkeypatch):
    monkeypatch.setattr(manager, "discover_profile_bindings",
                        lambda _base: {"emulator-5556": {"account": "烘熊瞎"}})
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5556"])
    import full_auto_control
    monkeypatch.setattr(full_auto_control, "revoke_pending_command",
                        lambda _serial: False)
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: pytest.fail("window spawned with stale resume"))

    with pytest.raises(RuntimeError, match="遗留全自动命令"):
        manager.recover_window_only("emulator-5556")


def test_window_only_recovery_existing_window_requires_readiness_ack(monkeypatch):
    monkeypatch.setattr(manager, "discover_profile_bindings",
                        lambda _base: {"emulator-5556": {"account": "烘熊瞎"}})
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "existing_window_pids",
                        lambda _default: {"emulator-5556": 1300})
    requests = []
    monkeypatch.setattr(manager, "_request_confirmed",
                        lambda serial, command: requests.append((serial, command)))
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: pytest.fail("existing window scanned ADB"))

    assert manager.recover_window_only("emulator-5556") == (
        "already_running", 1300)
    assert requests == [("emulator-5556", "ping")]


def test_window_only_recovery_offline_is_nonfailure_and_does_not_wait(
        monkeypatch):
    monkeypatch.setattr(manager, "discover_profile_bindings",
                        lambda _base: {"emulator-5556": {"account": "烘熊瞎"}})
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices", lambda _adb: [])
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: pytest.fail("offline window spawned"))

    assert manager.recover_window_only("emulator-5556") == ("offline", 0)


def test_window_only_recovery_does_not_overwrite_exit_during_startup(
        monkeypatch):
    intents = iter([True, False])
    monkeypatch.setattr(manager, "discover_profile_bindings",
                        lambda _base: {"emulator-5556": {"account": "烘熊瞎"}})
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: next(intents))
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(manager, "connected_devices",
                        lambda _adb: ["emulator-5556"])
    monkeypatch.setattr(manager, "start_window",
                        lambda _serial: SimpleNamespace(pid=1300))
    monkeypatch.setattr(manager, "_wait_for_started_window",
                        lambda _serial, _process: None)
    monkeypatch.setattr(manager, "set_assistant_desired_open",
                        lambda *_args: pytest.fail("recovery overwrote exit intent"))
    import full_auto_control
    monkeypatch.setattr(full_auto_control, "revoke_pending_command",
                        lambda _serial: True)

    assert manager.recover_window_only("emulator-5556") == (
        "desired_false", 0)


def test_started_window_requires_matching_pid_and_control_ack(monkeypatch):
    process = SimpleNamespace(pid=1300, poll=lambda: None)
    scans = iter([{}, {"emulator-5554": 1300}])
    requested = []
    monkeypatch.setattr(
        manager, "_wait_for_started_window",
        WAIT_FOR_STARTED_WINDOW)
    monkeypatch.setattr(
        manager, "existing_window_pids", lambda _default: next(scans))
    monkeypatch.setattr(
        manager, "_request_confirmed",
        lambda serial, command, timeout: requested.append(
            (serial, command, timeout)))
    monkeypatch.setattr(manager.time, "sleep", lambda _seconds: None)

    manager._wait_for_started_window("emulator-5554", process, timeout=1.0)

    assert requested and requested[0][:2] == ("emulator-5554", "restore")


def test_started_window_early_exit_is_reported(monkeypatch):
    process = SimpleNamespace(pid=1300, poll=lambda: 2)
    monkeypatch.setattr(
        manager, "_wait_for_started_window",
        WAIT_FOR_STARTED_WINDOW)
    monkeypatch.setattr(manager, "existing_window_pids", lambda _default: {})

    with pytest.raises(RuntimeError, match="窗口就绪前退出"):
        manager._wait_for_started_window(
            "emulator-5554", process, timeout=0.1)


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


def test_desktop_activation_never_selects_offline_saved_profile():
    rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None,
                          online=False),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None,
                          online=True),
    ]

    assert manager.choose_activation_target(rows) == rows[1]


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


def test_no_device_finishes_activation_without_background_retry():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._activation_pending = True
    launcher._closed = False
    launcher._busy_serials = set()
    launcher._rows = []
    messages = []
    launcher.status_var = SimpleNamespace(set=messages.append)

    launcher._run_pending_activation()

    assert launcher._activation_pending is False
    assert "启动雷电＋辅助器" in messages[-1]


def test_launcher_has_no_periodic_device_retry_but_pid_exit_recovery():
    assert not hasattr(manager.MahjongLauncher, "_schedule_empty_refresh")
    assert not hasattr(manager.MahjongLauncher, "_recover_bound_assistants")
    assert hasattr(manager.MahjongLauncher, "_arm_liveness_watch")


def _recovery_launcher(row):
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._closed = False
    launcher._rows = [row]
    launcher._rendered_rows = tuple([row])
    launcher._buttons = {}
    launcher._busy_serials = set()
    launcher._recovery_inflight = set()
    launcher._recovery_failures = {}
    launcher._recovery_scheduled = set()
    launcher._recovery_tokens = {}
    launcher._intentional_shutdowns = set()
    launcher._liveness_watches = set()
    launcher._events = manager.Queue()
    launcher.status_var = SimpleNamespace(set=lambda _value: None)
    launcher._render = lambda _rows: None
    launcher.root = SimpleNamespace(after=lambda _delay, callback: callback())
    return launcher


def test_pid_exit_fresh_false_intent_never_recovers(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200,
                            online=True, desired_open=True)
    launcher = _recovery_launcher(row)
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: False)

    launcher._handle_assistant_exit("emulator-5554", 1200)

    assert launcher._rows[0].pid is None
    assert launcher._rows[0].desired_open is False
    assert submitted == []


def test_pid_exit_true_intent_submits_one_window_only_recovery(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200,
                            online=True, desired_open=True)
    launcher = _recovery_launcher(row)
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)

    launcher._handle_assistant_exit("emulator-5554", 1200)
    launcher._handle_assistant_exit("emulator-5554", 1200)

    assert submitted == [(
        "recover:emulator-5554", manager.recover_window_only,
        "emulator-5554")]
    assert launcher._recovery_inflight == {"emulator-5554"}


def test_liveness_watch_is_deduplicated_and_uses_no_device_scan(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200)
    launcher = _recovery_launcher(row)
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs["name"])

    monkeypatch.setattr(manager.threading, "Thread", FakeThread)
    monkeypatch.setattr(manager, "connected_devices",
                        lambda *_args: pytest.fail("periodic device scan"))

    launcher._arm_liveness_watch(row)
    launcher._arm_liveness_watch(row)

    assert started == ["mahjong-assistant-liveness-emulator-5554"]
    assert launcher._liveness_watches == {("emulator-5554", 1200)}


def test_liveness_probe_errors_become_exit_event(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200)
    launcher = _recovery_launcher(row)
    monkeypatch.setattr(
        manager, "pid_owns_assist_window",
        lambda _pid: (_ for _ in ()).throw(OSError("probe failed")))
    monkeypatch.setattr(manager.time, "sleep", lambda _delay: None)

    launcher._arm_liveness_watch(row)
    deadline = manager.time.monotonic() + 1.0
    event = None
    while manager.time.monotonic() < deadline:
        try:
            event = launcher._events.get_nowait()
            break
        except manager.Empty:
            manager.time.sleep(0.001)

    assert event == (
        "assistant-exit", {"serial": "emulator-5554", "pid": 1200}, None)


def test_stale_recovery_timer_is_invalidated_by_manual_reset():
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None)
    launcher = _recovery_launcher(row)
    callbacks = []
    launcher.root = SimpleNamespace(
        after=lambda _delay, callback: callbacks.append(callback))
    started = []
    launcher._start_recovery = lambda serial: started.append(serial)

    launcher._schedule_recovery("emulator-5554", 5.0)
    launcher._reset_recovery_state("emulator-5554")
    callbacks[0]()

    assert started == []


def test_reconcile_recovers_all_known_desired_windows_serially(monkeypatch):
    first = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None)
    second = manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None)
    launcher = _recovery_launcher(first)
    launcher._rows = [first, second]
    launcher._rendered_rows = tuple(launcher._rows)
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)
    launcher._arm_liveness_watch = lambda _row: None
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: True)

    launcher._reconcile_known_desired_windows()
    assert submitted == [(
        "recover:emulator-5554", manager.recover_window_only,
        "emulator-5554")]

    launcher._finish_assistant_recovery(
        "emulator-5554", ("started", 1300), None)
    assert submitted == [
        ("recover:emulator-5554", manager.recover_window_only,
         "emulator-5554"),
        ("recover:emulator-5556", manager.recover_window_only,
         "emulator-5556"),
    ]


def test_reconcile_skips_false_offline_and_fused_rows(monkeypatch):
    rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None,
                          desired_open=False),
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None,
                          online=False),
        manager.DeviceRow("emulator-5558", "拾刘", "tea_2p", None),
    ]
    launcher = _recovery_launcher(rows[0])
    launcher._rows = rows
    launcher._rendered_rows = tuple(rows)
    launcher._recovery_failures["emulator-5558"] = (3, float("inf"))
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: False)

    launcher._reconcile_known_desired_windows()

    assert submitted == []


def test_recovery_finish_honours_fresh_exit_intent(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None)
    launcher = _recovery_launcher(row)
    launcher._recovery_inflight.add("emulator-5554")
    launcher._busy_serials.add("emulator-5554")
    armed = []
    launcher._arm_liveness_watch = lambda current: armed.append(current)
    monkeypatch.setattr(manager, "read_assistant_desired_open",
                        lambda *_args, **_kwargs: False)

    launcher._finish_assistant_recovery(
        "emulator-5554", ("started", 1300), None)

    assert launcher._rows[0].pid is None
    assert launcher._rows[0].desired_open is False
    assert armed == []


def test_recovery_failures_back_off_then_fuse_without_refresh(monkeypatch):
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None)
    launcher = _recovery_launcher(row)
    scheduled = []
    launcher.root = SimpleNamespace(
        after=lambda delay, callback: scheduled.append((delay, callback)))
    launcher.refresh = lambda **_kwargs: pytest.fail("background refresh")
    monkeypatch.setattr(manager.time, "monotonic", lambda: 100.0)

    launcher._recovery_inflight.add("emulator-5554")
    launcher._busy_serials.add("emulator-5554")
    launcher._finish_assistant_recovery("emulator-5554", None, "boom-1")
    assert launcher._recovery_failures["emulator-5554"] == (1, 105.0)
    assert scheduled[-1][0] == 5000

    launcher._recovery_scheduled.clear()
    launcher._recovery_inflight.add("emulator-5554")
    launcher._busy_serials.add("emulator-5554")
    launcher._finish_assistant_recovery("emulator-5554", None, "boom-2")
    assert launcher._recovery_failures["emulator-5554"] == (2, 130.0)
    assert scheduled[-1][0] == 30000

    launcher._recovery_scheduled.clear()
    launcher._recovery_inflight.add("emulator-5554")
    launcher._busy_serials.add("emulator-5554")
    launcher._finish_assistant_recovery("emulator-5554", None, "boom-3")
    assert launcher._recovery_failures["emulator-5554"] == (3, float("inf"))
    assert len(scheduled) == 2


def test_manual_open_clears_recovery_fuse():
    row = manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None)
    launcher = _recovery_launcher(row)
    launcher._recovery_failures["emulator-5554"] = (3, float("inf"))
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)

    launcher._operate_device("emulator-5554", "open")

    assert "emulator-5554" not in launcher._recovery_failures
    assert submitted == [(
        "open:emulator-5554", manager.open_or_restore, "emulator-5554")]


@pytest.mark.parametrize(("row", "expected"), [
    (manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None,
                       online=False), True),
    (manager.DeviceRow("emulator-5554", None, None, None,
                       online=False), False),
    (manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None,
                       online=True), False),
    (manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", 1200,
                       online=False), True),
])
def test_offline_start_button_requires_saved_offline_free_profile(row, expected):
    assert manager.can_start_offline_device(row) is expected


def test_start_ldplayer_then_opens_same_serial_assistant(monkeypatch):
    calls = []

    def fake_start(serial, *, ldconsole, online_serials):
        calls.append((serial, ldconsole, tuple(online_serials())))
        return SimpleNamespace(ok=True, status="started", detail="")

    monkeypatch.setitem(
        sys.modules, "ldplayer_control",
        SimpleNamespace(start_and_wait_online=fake_start))
    monkeypatch.setattr(manager, "_adb_path", lambda: "adb")
    monkeypatch.setattr(
        manager, "connected_devices", lambda _adb: ["emulator-5556"])
    monkeypatch.setattr(
        manager, "open_or_restore",
        lambda serial: calls.append(("open", serial)) or ("started", 1300))

    result = manager.start_ldplayer_and_assistant("emulator-5556")

    assert result == ("started", "started", 1300)
    assert calls == [
        ("emulator-5556", manager.LDCONSOLE_PATH, ("emulator-5556",)),
        ("open", "emulator-5556"),
    ]


def test_ldplayer_error_never_opens_assistant(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "ldplayer_control",
        SimpleNamespace(start_and_wait_online=lambda *_args, **_kwargs:
                        SimpleNamespace(ok=False, status="error",
                                        detail="实例不存在")))
    monkeypatch.setattr(
        manager, "open_or_restore",
        lambda _serial: pytest.fail("assistant opened after LDPlayer failure"))

    with pytest.raises(RuntimeError, match="实例不存在"):
        manager.start_ldplayer_and_assistant("emulator-5558")


def test_offline_row_start_is_one_background_operation():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._closed = False
    launcher._busy_serials = set()
    launcher._rows = [
        manager.DeviceRow("emulator-5556", "烘熊瞎", "tea_2p", None,
                          online=False, desired_open=True),
    ]
    messages = []
    launcher.status_var = SimpleNamespace(set=messages.append)
    button_states = []
    launcher._buttons = {"emulator-5556": [
        SimpleNamespace(configure=lambda **kwargs: button_states.append(kwargs))]}
    submitted = []
    launcher._submit = lambda *args: submitted.append(args)

    launcher._start_offline_device("emulator-5556")
    launcher._start_offline_device("emulator-5556")

    assert submitted == [(
        "start-ld:emulator-5556", manager.start_ldplayer_and_assistant,
        "emulator-5556")]
    assert button_states == [{"state": "disabled"}]
    assert "正在处理中" in messages[-1]


def test_online_or_unknown_row_never_submits_ldplayer_start():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._closed = False
    launcher._busy_serials = set()
    launcher._rows = [
        manager.DeviceRow("emulator-5554", "玉姐", "tea_2p", None,
                          online=True),
    ]
    launcher._buttons = {}
    launcher.status_var = SimpleNamespace(set=lambda _value: None)
    launcher._submit = lambda *_args: pytest.fail("ineligible LDPlayer started")

    launcher._start_offline_device("emulator-5554")
    launcher._start_offline_device("emulator-5558")


def test_identical_refresh_rows_do_not_rebuild_widgets():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._rendered_rows = None
    rendered = []
    launcher._render = lambda rows: rendered.append(tuple(rows))
    first = [manager.DeviceRow(
        "emulator-5554", "玉姐", "tea_2p", 1200, online=True)]
    changed = [manager.DeviceRow(
        "emulator-5554", "玉姐", "tea_2p", None, online=True)]

    assert launcher._render_if_changed(first) is True
    assert launcher._render_if_changed(list(first)) is False
    assert launcher._render_if_changed(changed) is True
    assert rendered == [tuple(first), tuple(changed)]


def test_start_failure_is_reported_and_refreshes_once():
    launcher = manager.MahjongLauncher.__new__(manager.MahjongLauncher)
    launcher._busy_serials = {"emulator-5558"}
    states = []
    launcher._buttons = {"emulator-5558": [
        SimpleNamespace(configure=lambda **kwargs: states.append(kwargs))]}
    messages = []
    launcher.status_var = SimpleNamespace(set=messages.append)
    refreshes = []
    launcher.refresh = lambda **kwargs: refreshes.append(kwargs)

    launcher._finish_ldplayer_start(
        "emulator-5558", None, "实例 index 2 不存在")

    assert launcher._busy_serials == set()
    assert states == [{"state": "normal"}]
    assert refreshes == [{
        "status_after": "emulator-5558：启动失败：实例 index 2 不存在"}]


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
