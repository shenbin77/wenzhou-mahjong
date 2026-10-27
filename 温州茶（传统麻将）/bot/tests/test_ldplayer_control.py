"""Offline-only safety tests for one-click LDPlayer startup."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from ldplayer_control import (serial_to_ldplayer_index,
                              start_and_wait_online)


def _done(stdout=b"", returncode=0, stderr=b""):
    return SimpleNamespace(stdout=stdout, stderr=stderr,
                           returncode=returncode)


def _list2(*, index=1, running=0):
    return (f"{index},LDPlayer-{index},0,0,{running},0,0,1600,900,240\r\n"
            .encode())


@pytest.mark.parametrize(("serial", "expected"), [
    ("emulator-5554", 0),
    ("emulator-5556", 1),
    ("emulator-5558", 2),
    ("emulator-5560", 3),
    ("emulator-5555", None),
    ("emulator-5552", None),
    ("127.0.0.1:5556", None),
    ("emulator-5556-extra", None),
    ("", None),
])
def test_serial_mapping_is_exact(serial, expected):
    assert serial_to_ldplayer_index(serial) == expected


def test_already_online_still_requires_list2_but_never_launches():
    commands = []

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return _done(_list2(index=1, running=1))

    result = start_and_wait_online(
        "emulator-5556", ldconsole="ldconsole.exe",
        online_serials=lambda: ["emulator-5556"], runner=runner)

    assert result.status == "already_online"
    assert result.ok and result.index == 1
    assert result.command_sent is False
    assert [call[0] for call in commands] == [["ldconsole.exe", "list2"]]


def test_online_adb_cannot_bypass_missing_list2_index():
    commands = []

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return _done(_list2(index=0, running=1))

    result = start_and_wait_online(
        "emulator-5558", ldconsole="ldconsole.exe",
        online_serials=lambda: ["emulator-5558"], runner=runner)

    assert result.status == "error"
    assert "不存在或不唯一" in result.detail
    assert result.command_sent is False
    assert [call[0] for call in commands] == [["ldconsole.exe", "list2"]]


def test_offline_instance_is_verified_launched_and_waited():
    commands = []
    online_checks = iter([[], [], ["emulator-5556"]])

    def online():
        return next(online_checks, ["emulator-5556"])

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[1] == "list2":
            return _done(_list2(index=1, running=0))
        return _done()

    result = start_and_wait_online(
        "emulator-5556", ldconsole="ldconsole.exe",
        online_serials=online, runner=runner, sleep=lambda _s: None)

    assert result.status == "started" and result.ok
    assert result.command_sent is True and result.index == 1
    assert [call[0] for call in commands] == [
        ["ldconsole.exe", "list2"],
        ["ldconsole.exe", "launch", "--index", "1"],
        ["ldconsole.exe", "adb", "--index", "1", "--command",
         "get-state"],
    ]
    assert all(call[1]["shell"] is False for call in commands)
    if os.name == "nt":
        assert all(call[1]["creationflags"] == getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000)
                   for call in commands)


def test_running_instance_waits_for_adb_without_duplicate_launch():
    commands = []
    online_checks = iter([[], ["emulator-5558"]])

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return _done(_list2(index=2, running=1))

    result = start_and_wait_online(
        "emulator-5558", ldconsole="ldconsole.exe",
        online_serials=lambda: next(online_checks, ["emulator-5558"]),
        runner=runner, sleep=lambda _s: None)

    assert result.status == "started"
    assert result.command_sent is False
    assert [call[0] for call in commands] == [["ldconsole.exe", "list2"]]


def test_exact_instance_adb_probe_registers_expected_serial():
    commands = []
    online = set()

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[1] == "list2":
            return _done(_list2(index=0, running=1))
        if argv[1] == "adb":
            online.add("emulator-5554")
            return _done(stdout=b"device\r\n")
        pytest.fail(f"unexpected command: {argv}")

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: online, runner=runner,
        sleep=lambda _s: None)

    assert result.status == "started"
    assert result.command_sent is False
    assert [call[0] for call in commands] == [
        ["ldconsole.exe", "list2"],
        ["ldconsole.exe", "adb", "--index", "0", "--command",
         "get-state"],
    ]
    assert all(call[1]["shell"] is False for call in commands)


def test_probe_device_output_cannot_bypass_exact_global_serial_gate():
    commands = []
    clock = iter([0.0, 0.0, 0.0, 1.0, 1.0])

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "list2":
            return _done(_list2(index=0, running=1))
        return _done(stdout=b"device\r\n")

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: ["emulator-5556"], timeout_s=0.5,
        runner=runner, sleep=lambda _s: None,
        monotonic=lambda: next(clock, 1.0))

    assert result.status == "error"
    assert "stdout=device" in result.detail
    assert commands.count(
        ["ldconsole.exe", "adb", "--index", "0", "--command",
         "get-state"]) == 1
    assert not any(command[1] == "launch" for command in commands)


def test_early_probe_failure_is_reported_if_global_serial_never_arrives():
    clock = iter([0.0, 0.0, 0.0, 1.0, 1.0])

    def runner(argv, **kwargs):
        if argv[1] == "list2":
            return _done(_list2(index=0, running=1))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], timeout_s=0.5,
        runner=runner, sleep=lambda _s: None,
        monotonic=lambda: next(clock, 1.0))

    assert result.status == "error"
    assert "TimeoutExpired" in result.detail
    assert "探测失败1次" in result.detail
    assert "最近一次官方ADB探测" in result.detail


def test_earlier_probe_failure_survives_later_device_probe_on_timeout():
    commands = []
    clock = iter([0.0, 0.0, 0.0, 0.2, 0.31, 0.31, 1.0, 1.0])

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "list2":
            return _done(_list2(index=0, running=1))
        probe_number = sum(command[1] == "adb" for command in commands)
        if probe_number == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return _done(stdout=b"device\r\n")

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], timeout_s=0.8,
        adb_probe_interval_s=0.1, runner=runner,
        sleep=lambda _s: None, monotonic=lambda: next(clock, 1.0))

    assert result.status == "error"
    assert "最近一次官方ADB探测：exit=0, stdout=device" in result.detail
    assert "探测失败1次" in result.detail
    assert "TimeoutExpired" in result.detail


@pytest.mark.parametrize("serial", [
    "emulator-5555", "127.0.0.1:5556", "unknown", "",
])
def test_unmappable_serial_fails_closed_without_subprocess(serial):
    result = start_and_wait_online(
        serial, ldconsole="ldconsole.exe", online_serials=lambda: [],
        runner=lambda *_a, **_k: pytest.fail("subprocess must not run"))
    assert result.status == "error"
    assert result.command_sent is False


def test_missing_list2_index_never_launches():
    commands = []

    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return _done(_list2(index=0, running=0))

    result = start_and_wait_online(
        "emulator-5558", ldconsole="ldconsole.exe",
        online_serials=lambda: [], runner=runner)

    assert result.status == "error"
    assert "不存在或不唯一" in result.detail
    assert [call[0] for call in commands] == [["ldconsole.exe", "list2"]]


def test_duplicate_list2_index_fails_closed():
    raw = _list2(index=1, running=0) + _list2(index=1, running=0)
    result = start_and_wait_online(
        "emulator-5556", ldconsole="ldconsole.exe",
        online_serials=lambda: [],
        runner=lambda *_a, **_k: _done(raw))
    assert result.status == "error"
    assert "不唯一" in result.detail


def test_list2_failure_never_launches():
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        return _done(returncode=7, stderr=b"failed")

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], runner=runner)
    assert result.status == "error"
    assert commands == [["ldconsole.exe", "list2"]]


def test_launch_failure_is_reported_without_waiting():
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "list2":
            return _done(_list2(index=0, running=0))
        return _done(returncode=9)

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], runner=runner)
    assert result.status == "error"
    assert result.command_sent is True
    assert len(commands) == 2


def test_wait_timeout_does_not_send_second_launch():
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "list2":
            return _done(_list2(index=0, running=0))
        return _done()

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], timeout_s=0,
        runner=runner, sleep=lambda _s: None)

    assert result.status == "error"
    assert result.command_sent is True
    assert "不会重复启动" in result.detail
    assert commands.count(["ldconsole.exe", "launch", "--index", "0"]) == 1
    assert not any(command[1] == "adb" for command in commands)


def test_subprocess_timeout_is_returned_as_error():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = start_and_wait_online(
        "emulator-5554", ldconsole="ldconsole.exe",
        online_serials=lambda: [], runner=runner)
    assert result.status == "error"
    assert "TimeoutExpired" in result.detail
