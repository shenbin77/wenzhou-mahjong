"""Fail-closed LDPlayer startup for one saved ADB serial.

This module only starts an LDPlayer instance.  It never opens an assistant,
restores follow/auto-play authority, or interacts with the game.  The visible
manager owns those later steps after this module has observed the exact ADB
serial online.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Literal

from human_input_monitor import parse_ldplayer_list2


_EMULATOR_SERIAL = re.compile(r"^emulator-(\d+)$")
_FIRST_LDPLAYER_PORT = 5554
_DEFAULT_START_TIMEOUT_S = 120.0
_ADB_PROBE_INTERVAL_S = 2.0
_ADB_PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class LdPlayerStartResult:
    """One atomic, user-facing result for a single requested serial."""

    status: Literal["started", "already_online", "error"]
    serial: str
    index: int | None
    command_sent: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != "error"


def serial_to_ldplayer_index(serial: str) -> int | None:
    """Map LDPlayer's default even ADB ports to instance indices.

    TCP serials, odd ports, ports below 5554, and any decorated value are
    deliberately rejected instead of guessing which local instance to start.
    The derived index is still not trusted until ``ldconsole list2`` contains
    exactly one matching row.
    """

    value = str(serial or "").strip()
    match = _EMULATOR_SERIAL.fullmatch(value)
    if match is None:
        return None
    port = int(match.group(1))
    delta = port - _FIRST_LDPLAYER_PORT
    if delta < 0 or delta % 2:
        return None
    return delta // 2


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


def _online_now(online_serials: Callable[[], Collection[str]],
                serial: str) -> bool:
    return serial in {str(value) for value in online_serials()}


def _probe_instance_adb(runner, console: str, index: int) -> tuple[bool, str]:
    """Ask LDPlayer's exact instance to expose ADB; never treats it as proof.

    ``ldconsole adb ... get-state`` is read-only, but on a cold boot it also
    makes LDPlayer register the instance with the shared ADB server.  The
    caller must still require the expected ``emulator-*`` serial from its
    independent global detector before declaring success.
    """

    command = [
        console, "adb", "--index", str(index), "--command", "get-state",
    ]
    try:
        completed = _run_console(
            runner, command, timeout_s=_ADB_PROBE_TIMEOUT_S)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    def snippet(value) -> str:
        if isinstance(value, bytes):
            text = value.decode(errors="replace")
        else:
            text = str(value or "")
        return " ".join(text.split())[:160] or "-"

    return (int(getattr(completed, "returncode", 1)) == 0
            and snippet(getattr(completed, "stdout", b"")).lower() == "device"), (
        f"exit={int(getattr(completed, 'returncode', 1))}, "
        f"stdout={snippet(getattr(completed, 'stdout', b''))}, "
        f"stderr={snippet(getattr(completed, 'stderr', b''))}"
    )


def start_and_wait_online(
        serial: str,
        *,
        ldconsole: str | Path,
        online_serials: Callable[[], Collection[str]],
        timeout_s: float = _DEFAULT_START_TIMEOUT_S,
        poll_interval_s: float = 0.5,
        adb_probe_interval_s: float = _ADB_PROBE_INTERVAL_S,
        runner=None,
        sleep=None,
        monotonic=None,
) -> LdPlayerStartResult:
    """Start exactly one LDPlayer and wait until its ADB serial is online.

    ``online_serials`` must be a read-only detector supplied by the manager.
    All process and clock dependencies are injectable so tests never start a
    real emulator.  A running LDPlayer row is never sent a duplicate launch;
    the function only waits for its ADB endpoint to become ready.
    """

    value = str(serial or "").strip()
    index = serial_to_ldplayer_index(value)
    if index is None:
        return LdPlayerStartResult(
            "error", value, None, False,
            "设备编号不是可安全映射的本机雷电 serial，已拒绝启动",
        )
    console = str(ldconsole or "").strip()
    if not console:
        return LdPlayerStartResult(
            "error", value, index, False, "未配置 ldconsole，已拒绝启动")

    run = runner or subprocess.run
    pause = sleep or time.sleep
    now = monotonic or time.monotonic
    command_sent = False
    try:
        listed = _run_console(run, [console, "list2"], timeout_s=8.0)
        if int(getattr(listed, "returncode", 1)) != 0:
            return LdPlayerStartResult(
                "error", value, index, False,
                "ldconsole list2 失败，未发送启动命令",
            )
        matches = [row for row in parse_ldplayer_list2(
            getattr(listed, "stdout", b""))
                   if row["index"] == index]
        if len(matches) != 1:
            return LdPlayerStartResult(
                "error", value, index, False,
                f"雷电实例 index {index} 不存在或不唯一，未发送启动命令",
            )

        # ADB presence is not proof that this saved local LDPlayer identity is
        # still valid.  Always establish the unique list2 index first so a
        # stale/foreign emulator endpoint cannot bypass instance validation.
        if _online_now(online_serials, value):
            return LdPlayerStartResult(
                "already_online", value, index, False,
                "雷电实例已核验且 ADB 在线，未重复发送启动命令",
            )

        if not matches[0]["running"]:
            # Mark before invoking: a timeout cannot prove that ldconsole did
            # not already accept the command.  The UI must not blindly retry.
            command_sent = True
            launched = _run_console(
                run,
                [console, "launch", "--index", str(index)],
                timeout_s=15.0,
            )
            if int(getattr(launched, "returncode", 1)) != 0:
                return LdPlayerStartResult(
                    "error", value, index, True,
                    f"雷电实例 index {index} 启动命令失败",
                )

        deadline = now() + max(0.0, float(timeout_s))
        next_adb_probe = now()
        last_adb_probe = "尚未执行"
        adb_probe_failures = 0
        last_adb_probe_failure = "无"
        while True:
            if _online_now(online_serials, value):
                detail = ("雷电已启动且 ADB 已上线" if command_sent else
                          "雷电进程已存在，ADB 已上线")
                return LdPlayerStartResult(
                    "started", value, index, command_sent, detail)
            current = now()
            if current >= deadline:
                action = "启动命令已发送" if command_sent else "雷电进程已存在"
                return LdPlayerStartResult(
                    "error", value, index, command_sent,
                    f"{action}，但等待 ADB 上线超时；不会重复启动；"
                    f"最近一次官方ADB探测：{last_adb_probe}；"
                    f"探测失败{adb_probe_failures}次，最近失败："
                    f"{last_adb_probe_failure}",
                )
            if current >= next_adb_probe:
                probe_ok, last_adb_probe = _probe_instance_adb(
                    run, console, index)
                if not probe_ok:
                    adb_probe_failures += 1
                    last_adb_probe_failure = last_adb_probe
                # The official probe is only a bounded readiness nudge.  It
                # cannot substitute for the exact global serial gate.
                if _online_now(online_serials, value):
                    detail = ("雷电已启动且 ADB 已上线" if command_sent else
                              "雷电进程已存在，ADB 已上线")
                    return LdPlayerStartResult(
                        "started", value, index, command_sent, detail)
                current = now()
                next_adb_probe = current + max(
                    0.1, float(adb_probe_interval_s))
                if current >= deadline:
                    continue
            remaining = max(0.01, deadline - current)
            pause(min(max(0.01, float(poll_interval_s)), remaining))
    except Exception as exc:
        return LdPlayerStartResult(
            "error", value, index, command_sent,
            f"启动雷电失败：{type(exc).__name__}: {exc}",
        )
