"""LDPlayer instance administration must be exact and never mutate for tests."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import ldplayer_instance_admin as admin


def _done(stdout: str = "", returncode: int = 0):
    return SimpleNamespace(
        stdout=stdout.encode("utf-8"), stderr=b"", returncode=returncode)


def _row(index: int, title: str, *, running: bool = False) -> str:
    active = 1 if running else 0
    pid = 1000 + index if running else -1
    return f"{index},{title},0,0,{active},{pid},0,1600,900,240\n"


class SequenceRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if not self.replies:
            raise AssertionError(f"unexpected command: {argv!r}")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _clock(values):
    iterator = iter(values)
    return lambda: next(iterator)


def _safe_state(online=(), assistants=(), reserved=()):
    return {
        "online_serials": lambda: online,
        "assistant_serials": lambda: assistants,
        "reserved_serials": lambda: reserved,
    }


def test_describe_rejects_tcp_and_never_calls_console():
    runner = SequenceRunner([])

    with pytest.raises(RuntimeError, match="本机雷电 serial"):
        admin.describe_ldplayer_instance(
            "127.0.0.1:5555", ldconsole="ldconsole", runner=runner)

    assert runner.calls == []


def test_describe_requires_one_exact_list2_row():
    runner = SequenceRunner([_done(_row(0, "base") + _row(0, "duplicate"))])

    with pytest.raises(RuntimeError, match="不存在或不唯一"):
        admin.describe_ldplayer_instance(
            "emulator-5554", ldconsole="ldconsole", runner=runner)


def test_describe_rejects_blank_instance_name():
    runner = SequenceRunner([_done(_row(0, ""))])

    with pytest.raises(RuntimeError, match="名称为空"):
        admin.describe_ldplayer_instance(
            "emulator-5554", ldconsole="ldconsole", runner=runner)


def test_describe_preserves_gb18030_instance_name():
    output = _row(0, "玉姐专用").encode("gb18030")
    runner = SequenceRunner([SimpleNamespace(
        stdout=output, stderr=b"", returncode=0)])

    result = admin.describe_ldplayer_instance(
        "emulator-5554", ldconsole="ldconsole", runner=runner)

    assert result.title == "玉姐专用"


def test_copy_uses_official_exact_index_and_returns_new_serial():
    before = _row(0, "base") + _row(1, "other")
    after = before + _row(2, "wzmj-copy-0")
    runner = SequenceRunner([
        _done(before),  # resolve source and reject duplicate destination name
        _done(before),  # fresh re-check after the live-state safety gates
        _done(),        # official copy command
        _done(after),   # wait for unique new list2 row
    ])

    result = admin.copy_ldplayer_instance(
        "emulator-5554", "wzmj-copy-0", ldconsole="ldconsole",
        runner=runner, sleep=lambda _delay: None,
        monotonic=lambda: 0.0, **_safe_state(),
    )

    assert result.source.index == 0
    assert result.created == admin.LdPlayerInstance(
        index=2, title="wzmj-copy-0", serial="emulator-5558",
        running=False)
    assert runner.calls[2][0] == [
        "ldconsole", "copy", "--name", "wzmj-copy-0", "--from", "0"]
    for _argv, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE


@pytest.mark.parametrize("name", ["", "中文", "a b", "a" * 33])
def test_copy_rejects_unsafe_name_before_list2(name):
    runner = SequenceRunner([])

    with pytest.raises(ValueError, match="复制名称"):
        admin.copy_ldplayer_instance(
            "emulator-5554", name, ldconsole="ldconsole", runner=runner,
            **_safe_state())

    assert runner.calls == []


def test_copy_rejects_existing_title_without_copy_command():
    rows = _row(0, "base") + _row(1, "already")
    runner = SequenceRunner([_done(rows)])

    with pytest.raises(RuntimeError, match="已存在"):
        admin.copy_ldplayer_instance(
            "emulator-5554", "already", ldconsole="ldconsole",
            runner=runner, **_safe_state())

    assert all(call[0][1] == "list2" for call in runner.calls)


def test_copy_failure_or_timeout_never_repeats_mutation():
    rows = _row(0, "base")
    command_failure = SequenceRunner([
        _done(rows), _done(rows), _done(returncode=3),
    ])
    with pytest.raises(RuntimeError, match="copy 失败"):
        admin.copy_ldplayer_instance(
            "emulator-5554", "copy-1", ldconsole="ldconsole",
            runner=command_failure, **_safe_state())
    assert sum(call[0][1] == "copy" for call in command_failure.calls) == 1

    timeout = SequenceRunner([
        _done(rows), _done(rows), _done(), _done(rows), _done(rows),
    ])
    with pytest.raises(RuntimeError, match="勿重复操作"):
        admin.copy_ldplayer_instance(
            "emulator-5554", "copy-2", ldconsole="ldconsole",
            timeout_s=1.0, poll_interval_s=0.1, runner=timeout,
            sleep=lambda _delay: None, monotonic=_clock([0.0, 0.5, 1.0]),
            **_safe_state(),
        )
    assert sum(call[0][1] == "copy" for call in timeout.calls) == 1


@pytest.mark.parametrize(
    ("rows", "online", "assistants", "message", "running"),
    [
        (_row(1, "victim", running=True), (), (), "仍在运行", True),
        (_row(1, "victim"), ("emulator-5556",), (), "ADB 在线", False),
        (_row(1, "victim"), (), ("emulator-5556",), "辅助器窗口", False),
    ],
)
def test_remove_requires_stopped_offline_instance_without_assistant(
        rows, online, assistants, message, running):
    runner = SequenceRunner([_done(rows), _done(rows)])

    with pytest.raises(RuntimeError, match=message):
        admin.remove_ldplayer_instance(
            admin.LdPlayerInstance(
                index=1, title="victim", serial="emulator-5556",
                running=running),
            ldconsole="ldconsole",
            online_serials=lambda: online,
            assistant_serials=lambda: assistants,
            runner=runner,
        )

    assert all(call[0][1] == "list2" for call in runner.calls)


def test_remove_uses_exact_index_then_waits_until_absent():
    rows = _row(0, "base") + _row(2, "victim")
    remaining = _row(0, "base")
    runner = SequenceRunner([_done(rows), _done(rows), _done(),
                             _done(remaining)])

    result = admin.remove_ldplayer_instance(
        admin.LdPlayerInstance(
            index=2, title="victim", serial="emulator-5558", running=False),
        ldconsole="ldconsole",
        online_serials=lambda: (), assistant_serials=lambda: (),
        runner=runner, sleep=lambda _delay: None, monotonic=lambda: 0.0,
    )

    assert result.removed.title == "victim"
    assert runner.calls[2][0] == [
        "ldconsole", "remove", "--index", "2"]
    assert sum(call[0][1] == "remove" for call in runner.calls) == 1


def test_remove_failure_or_timeout_never_repeats_mutation():
    rows = _row(2, "victim")
    command_failure = SequenceRunner([
        _done(rows), _done(rows), _done(returncode=2)])
    with pytest.raises(RuntimeError, match="remove 失败"):
        admin.remove_ldplayer_instance(
            admin.LdPlayerInstance(
                index=2, title="victim", serial="emulator-5558",
                running=False),
            ldconsole="ldconsole",
            online_serials=lambda: (), assistant_serials=lambda: (),
            runner=command_failure)
    assert sum(call[0][1] == "remove" for call in command_failure.calls) == 1

    timeout = SequenceRunner([
        _done(rows), _done(rows), _done(), _done(rows), _done(rows),
    ])
    with pytest.raises(RuntimeError, match="勿重复操作"):
        admin.remove_ldplayer_instance(
            admin.LdPlayerInstance(
                index=2, title="victim", serial="emulator-5558",
                running=False),
            ldconsole="ldconsole",
            online_serials=lambda: (), assistant_serials=lambda: (),
            timeout_s=1.0, poll_interval_s=0.1, runner=timeout,
            sleep=lambda _delay: None, monotonic=_clock([0.0, 0.5, 1.0]),
        )
    assert sum(call[0][1] == "remove" for call in timeout.calls) == 1
