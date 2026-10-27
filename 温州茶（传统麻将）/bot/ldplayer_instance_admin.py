"""Fail-closed administration for explicit LDPlayer copy/remove actions.

The manager may expose these only behind a user gesture and confirmation.
Every operation resolves one local ``emulator-<even port>`` to exactly one
``ldconsole list2`` row immediately before mutation.  The module never edits
assistant bindings, lifecycle intent, follow state, or auto-play authority.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection

from human_input_monitor import parse_ldplayer_list2
from ldplayer_control import serial_to_ldplayer_index


_SAFE_COPY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_FIRST_LDPLAYER_PORT = 5554
MUTATION_OUTCOME_UNKNOWN_MARKER = "[INSTANCE_ADMIN_OUTCOME_UNKNOWN]"


class MutationOutcomeUnknown(RuntimeError):
    """A mutation may have been accepted; callers must block manual retry."""


def _unknown_outcome(message: str) -> MutationOutcomeUnknown:
    return MutationOutcomeUnknown(
        f"{MUTATION_OUTCOME_UNKNOWN_MARKER}{message}；状态未知，勿重复操作，"
        "请重启管理器或在官方多开器人工核验")


@dataclass(frozen=True)
class LdPlayerInstance:
    index: int
    title: str
    serial: str
    running: bool


@dataclass(frozen=True)
class LdPlayerCopyResult:
    source: LdPlayerInstance
    created: LdPlayerInstance


@dataclass(frozen=True)
class LdPlayerRemoveResult:
    removed: LdPlayerInstance


def _serial_for_index(index: int) -> str:
    return f"emulator-{_FIRST_LDPLAYER_PORT + 2 * int(index)}"


def _run_console(runner, argv: list[str], *, timeout_s: float):
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": max(0.1, float(timeout_s)),
        "check": False,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return runner(argv, **kwargs)


def _decode_list2_output(output: str | bytes) -> str:
    if isinstance(output, str):
        return output
    raw = bytes(output or b"")
    for encoding in ("utf-8", "mbcs", "gb18030"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _list_instances(*, ldconsole: str | Path, runner) -> list[LdPlayerInstance]:
    console = str(ldconsole or "").strip()
    if not console:
        raise RuntimeError("未配置 ldconsole，已拒绝实例操作")
    result = _run_console(runner, [console, "list2"], timeout_s=8.0)
    if int(getattr(result, "returncode", 1)) != 0:
        raise RuntimeError("ldconsole list2 失败，未执行实例操作")
    return [
        LdPlayerInstance(
            index=int(row["index"]),
            title=str(row.get("title") or ""),
            serial=_serial_for_index(int(row["index"])),
            running=bool(row.get("running")),
        )
        for row in parse_ldplayer_list2(
            _decode_list2_output(getattr(result, "stdout", b"")))
    ]


def _validated_index(serial: str) -> tuple[str, int]:
    value = str(serial or "").strip()
    index = serial_to_ldplayer_index(value)
    if index is None:
        raise RuntimeError("设备编号不是可安全映射的本机雷电 serial")
    return value, index


def _target_from_rows(serial: str,
                      rows: Collection[LdPlayerInstance]) -> LdPlayerInstance:
    value, index = _validated_index(serial)
    matches = [row for row in rows if row.index == index]
    if len(matches) != 1:
        raise RuntimeError(f"雷电实例 index {index} 不存在或不唯一")
    target = matches[0]
    if target.serial != value:
        raise RuntimeError("雷电实例与目标 serial 映射不一致")
    if not target.title.strip():
        raise RuntimeError("雷电实例名称为空，无法做明确确认")
    return target


def _target(serial: str, *, ldconsole: str | Path, runner) -> LdPlayerInstance:
    _validated_index(serial)
    return _target_from_rows(
        serial, _list_instances(ldconsole=ldconsole, runner=runner))


def _require_expected_identity(expected: LdPlayerInstance,
                               current: LdPlayerInstance) -> None:
    if not isinstance(expected, LdPlayerInstance):
        raise RuntimeError("缺少删除确认时的完整实例身份")
    if current != expected:
        raise RuntimeError(
            "雷电实例身份或运行状态在确认后发生变化，已拒绝删除")


def _stale_reserved_serials(
        rows: Collection[LdPlayerInstance],
        reserved_serials: Collection[str]) -> set[str]:
    visible = {row.serial for row in rows}
    return {str(serial) for serial in reserved_serials} - visible


def describe_ldplayer_instance(
        serial: str, *, ldconsole: str | Path, runner=None,
) -> LdPlayerInstance:
    """Read the exact list2 identity used by the destructive confirmation."""
    return _target(
        serial, ldconsole=ldconsole, runner=runner or subprocess.run)


def _validated_copy_name(name: str) -> str:
    value = str(name or "").strip()
    if not _SAFE_COPY_NAME.fullmatch(value):
        raise ValueError("复制名称只允许1至32位英文字母、数字、点、横线和下划线")
    return value


def copy_ldplayer_instance(
        serial: str,
        new_name: str,
        *,
        ldconsole: str | Path,
        online_serials: Callable[[], Collection[str]],
        assistant_serials: Callable[[], Collection[str]],
        reserved_serials: Callable[[], Collection[str]],
        timeout_s: float = 45.0,
        poll_interval_s: float = 0.5,
        runner=None,
        sleep=None,
        monotonic=None,
) -> LdPlayerCopyResult:
    """Copy one exact local instance and wait for its new list2 identity.

    A successful copy deliberately creates no assistant profile.  The caller
    must tell the user to start/bind the new instance explicitly.
    """
    run = runner or subprocess.run
    pause = sleep or time.sleep
    now = monotonic or time.monotonic
    name = _validated_copy_name(new_name)
    _validated_index(serial)
    before = _list_instances(ldconsole=ldconsole, runner=run)
    source = _target_from_rows(serial, before)
    if source.running:
        raise RuntimeError("源雷电实例仍在运行，必须先在官方多开器关闭")
    if any(row.title == name for row in before):
        raise RuntimeError(f"雷电实例名称 {name!r} 已存在")
    stale = _stale_reserved_serials(before, reserved_serials())
    if stale:
        detail = "、".join(sorted(stale))
        raise RuntimeError(
            f"存在不在 ldconsole list2 中的旧辅助器配置：{detail}；"
            "无法证明新 serial 不会复用旧状态，已拒绝复制")

    online = {str(value) for value in online_serials()}
    assistants = {str(value) for value in assistant_serials()}
    fresh = _list_instances(ldconsole=ldconsole, runner=run)
    fresh_source = _target_from_rows(serial, fresh)
    if fresh_source != source:
        raise RuntimeError("源雷电实例身份或运行状态已变化，拒绝复制")
    if fresh_source.running or fresh_source.serial in online:
        raise RuntimeError("源雷电实例仍在运行或 ADB 在线，拒绝复制")
    if fresh_source.serial in assistants:
        raise RuntimeError("源设备仍有辅助器窗口，必须先明确退出")
    stale = _stale_reserved_serials(fresh, reserved_serials())
    if stale:
        detail = "、".join(sorted(stale))
        raise RuntimeError(
            f"复制前出现脱离 list2 的旧辅助器配置：{detail}；已拒绝复制")
    if any(row.title == name for row in fresh):
        raise RuntimeError(f"雷电实例名称 {name!r} 已存在")

    console = str(ldconsole)
    try:
        copied = _run_console(
            run,
            [console, "copy", "--name", name,
             "--from", str(fresh_source.index)],
            timeout_s=30.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise _unknown_outcome("复制命令执行超时") from exc
    if int(getattr(copied, "returncode", 1)) != 0:
        raise RuntimeError("ldconsole copy 失败；未创建辅助器绑定")

    deadline = now() + max(0.0, float(timeout_s))
    while True:
        try:
            rows = _list_instances(ldconsole=ldconsole, runner=run)
        except Exception as exc:
            raise _unknown_outcome("复制命令已发送，但官方列表复核失败") from exc
        matches = [row for row in rows
                   if row.title == name and row.index != source.index]
        if len(matches) == 1:
            if matches[0].serial in {str(value) for value in reserved_serials()}:
                raise _unknown_outcome(
                    "新实例 serial 与已有辅助器配置发生冲突")
            return LdPlayerCopyResult(source=source, created=matches[0])
        if len(matches) > 1:
            raise _unknown_outcome("复制结果名称不唯一")
        if now() >= deadline:
            raise _unknown_outcome("新实例未在时限内唯一出现")
        pause(max(0.01, float(poll_interval_s)))


def remove_ldplayer_instance(
        expected: LdPlayerInstance,
        *,
        ldconsole: str | Path,
        online_serials: Callable[[], Collection[str]],
        assistant_serials: Callable[[], Collection[str]],
        timeout_s: float = 20.0,
        poll_interval_s: float = 0.5,
        runner=None,
        sleep=None,
        monotonic=None,
) -> LdPlayerRemoveResult:
    """Remove one stopped exact instance; preserve all assistant user data."""
    run = runner or subprocess.run
    pause = sleep or time.sleep
    now = monotonic or time.monotonic
    if not isinstance(expected, LdPlayerInstance):
        raise RuntimeError("缺少删除确认时的完整实例身份")
    serial = expected.serial
    value, index = _validated_index(serial)
    if value != expected.serial or index != expected.index:
        raise RuntimeError("删除确认身份的 serial/index 映射不一致")

    first = _target(serial, ldconsole=ldconsole, runner=run)
    _require_expected_identity(expected, first)
    if first.running:
        raise RuntimeError("雷电实例仍在运行，必须先在官方多开器关闭")
    online = {str(value) for value in online_serials()}
    assistants = {str(value) for value in assistant_serials()}
    target = _target(serial, ldconsole=ldconsole, runner=run)
    _require_expected_identity(expected, target)
    if target != first:
        raise RuntimeError("雷电实例在删除复核期间发生变化，已拒绝删除")
    if target.running or target.serial in online:
        raise RuntimeError("雷电实例仍在运行或 ADB 在线，必须先在官方多开器关闭")
    if target.serial in assistants:
        raise RuntimeError("该设备仍有辅助器窗口，必须先从辅助器明确退出")

    console = str(ldconsole)
    try:
        removed = _run_console(
            run, [console, "remove", "--index", str(target.index)],
            timeout_s=20.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise _unknown_outcome("删除命令执行超时") from exc
    if int(getattr(removed, "returncode", 1)) != 0:
        raise RuntimeError("ldconsole remove 失败；实例和辅助器设置均未改动")

    deadline = now() + max(0.0, float(timeout_s))
    while True:
        try:
            rows = _list_instances(ldconsole=ldconsole, runner=run)
        except Exception as exc:
            raise _unknown_outcome("删除命令已发送，但官方列表复核失败") from exc
        matches = [row for row in rows if row.index == target.index]
        if not matches:
            return LdPlayerRemoveResult(removed=target)
        if len(matches) > 1:
            raise _unknown_outcome("删除后同 index 出现多个实例")
        if now() >= deadline:
            raise _unknown_outcome("删除后实例仍在官方列表中")
        pause(max(0.01, float(poll_interval_s)))
