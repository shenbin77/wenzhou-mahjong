# -*- coding: utf-8 -*-
"""只观察指定雷电窗口内的鼠标按下，供离线归因使用。

安全边界：
- 不装键盘钩子，不记录其它窗口，不发送输入；
- HWND 必须由调用方提供，serial 只有在调用方声明映射已核验时才落盘；
- 单次鼠标观察不是游戏动作真值，必须再与可信牌河推进交叉确认。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ClickTarget",
    "HumanInputMonitor",
    "associate_human_click",
    "build_human_click_event",
    "load_human_clicks",
    "parse_ldplayer_list2",
    "resolve_ldplayer_target",
]

VK_LBUTTON = 0x01
GA_ROOT = 2


@dataclass(frozen=True)
class ClickTarget:
    hwnd: int
    serial: Optional[str] = None
    mapping_verified: bool = False
    mapping_basis: str = "caller_unverified"


def _ts_seconds(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for parser in (
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp(),
        lambda raw: datetime.strptime(raw, "%Y/%m/%d %H:%M:%S").timestamp(),
    ):
        try:
            return parser(text)
        except ValueError:
            continue
    return None


def build_human_click_event(
    target: ClickTarget,
    *,
    clicked_root_hwnd: int,
    screen_xy: Sequence[int],
    client_xy: Sequence[int],
    window_title: str,
    ts: Optional[str] = None,
) -> Optional[dict]:
    """只为目标根窗口构造事件；其它窗口返回 None。"""
    if int(clicked_root_hwnd) != int(target.hwnd):
        return None
    verified = bool(target.mapping_verified and target.serial)
    return {
        "event": "human_click_observed",
        "ts": ts or datetime.now().astimezone().isoformat(),
        "hwnd": int(target.hwnd),
        "window_title": str(window_title or ""),
        "screen_xy": [int(screen_xy[0]), int(screen_xy[1])],
        "client_xy": [int(client_xy[0]), int(client_xy[1])],
        "serial": str(target.serial) if verified else None,
        "mapping_status": "verified" if verified else "unknown",
        "mapping_basis": target.mapping_basis if verified else "unknown",
        "scope": "target_window_only",
        "input": "mouse_left_down",
    }


def load_human_clicks(path: str | Path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    result: list[dict] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("event") == "human_click_observed":
            result.append(item)
    return result


def associate_human_click(
    progress_ts: Any,
    clicks: Sequence[Mapping[str, Any]],
    serial: Optional[str],
    *,
    max_before_s: float = 2.5,
    max_after_s: float = 0.5,
) -> Optional[dict]:
    """把可信推进与附近已核验同 serial 点击关联；未知映射绝不猜。"""
    progress_s = _ts_seconds(progress_ts)
    if progress_s is None or not serial:
        return None
    candidates: list[tuple[float, Mapping[str, Any], float]] = []
    for click in clicks:
        if (click.get("event") != "human_click_observed"
                or click.get("mapping_status") != "verified"
                or click.get("serial") != serial):
            continue
        click_s = _ts_seconds(click.get("ts"))
        if click_s is None:
            continue
        delta = progress_s - click_s
        if -float(max_after_s) <= delta <= float(max_before_s):
            candidates.append((abs(delta), click, delta))
    if not candidates:
        return None
    _distance, click, delta = min(candidates, key=lambda part: part[0])
    return {
        "ts": click.get("ts"),
        "serial": serial,
        "hwnd": click.get("hwnd"),
        "window_title": click.get("window_title"),
        "screen_xy": click.get("screen_xy"),
        "client_xy": click.get("client_xy"),
        "delta_to_progress_s": round(float(delta), 3),
        "mapping_status": "verified",
    }


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Win32Api:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("human input observation is Windows-only")
        self.user32 = ctypes.windll.user32
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
        self.user32.GetCursorPos.restype = ctypes.c_bool
        self.user32.WindowFromPoint.argtypes = [_Point]
        self.user32.WindowFromPoint.restype = ctypes.c_void_p
        self.user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.user32.GetAncestor.restype = ctypes.c_void_p
        self.user32.ScreenToClient.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_Point)]
        self.user32.ScreenToClient.restype = ctypes.c_bool
        self.user32.IsWindow.argtypes = [ctypes.c_void_p]
        self.user32.IsWindow.restype = ctypes.c_bool
        self.user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int

    def left_down(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)

    def cursor(self) -> tuple[int, int]:
        point = _Point()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        return int(point.x), int(point.y)

    def root_at(self, screen_xy: tuple[int, int]) -> int:
        hwnd = self.user32.WindowFromPoint(_Point(*screen_xy))
        return int(self.user32.GetAncestor(hwnd, GA_ROOT) or hwnd or 0)

    def client(self, hwnd: int, screen_xy: tuple[int, int]) -> tuple[int, int]:
        point = _Point(*screen_xy)
        if not self.user32.ScreenToClient(int(hwnd), ctypes.byref(point)):
            raise OSError("ScreenToClient failed")
        return int(point.x), int(point.y)

    def title(self, hwnd: int) -> str:
        length = int(self.user32.GetWindowTextLengthW(int(hwnd)))
        buf = ctypes.create_unicode_buffer(max(1, length + 1))
        self.user32.GetWindowTextW(int(hwnd), buf, len(buf))
        return buf.value

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(int(hwnd)))


class HumanInputMonitor:
    """轮询左键边沿；只有命中明确 target HWND 才写事件。"""

    def __init__(self, target: ClickTarget, output: str | Path, *,
                 poll_s: float = 0.02, api=None,
                 append_attempts: int = 3,
                 append_retry_s: float = 0.05) -> None:
        if int(target.hwnd) <= 0:
            raise ValueError("target hwnd must be positive")
        self.target = target
        self.output = Path(output)
        self.poll_s = max(0.01, float(poll_s))
        self.api = api or _Win32Api()
        self.append_attempts = max(1, int(append_attempts))
        self.append_retry_s = max(0.0, float(append_retry_s))
        self._was_down = False

    def target_valid(self) -> bool:
        """Return whether the exact HWND still exists.

        The watchdog also periodically re-runs the serial-to-process mapping;
        this local check closes the gap between those slower resolver calls.
        """
        checker = getattr(self.api, "is_window", None)
        return bool(checker(self.target.hwnd)) if callable(checker) else True

    def poll_once(self) -> Optional[dict]:
        if not self.target_valid():
            self._was_down = False
            return None
        down = bool(self.api.left_down())
        if not down or self._was_down:
            self._was_down = down
            return None
        self._was_down = True
        try:
            screen = self.api.cursor()
            root = self.api.root_at(screen)
            if int(root) != int(self.target.hwnd):
                return None
            client = self.api.client(self.target.hwnd, screen)
            event = build_human_click_event(
                self.target, clicked_root_hwnd=root, screen_xy=screen,
                client_xy=client, window_title=self.api.title(root))
        except (OSError, ValueError):
            return None
        if event is not None:
            self._append(event)
        return event

    def _append(self, event: Mapping[str, Any]) -> None:
        payload = (json.dumps(dict(event), ensure_ascii=False) + "\n").encode(
            "utf-8")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        last_error: Optional[OSError] = None
        for attempt in range(self.append_attempts):
            fd: Optional[int] = None
            wrote_full_line = False
            try:
                self.output.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.output, flags, 0o600)
                written = os.write(fd, payload)
                if written != len(payload):
                    raise OSError(
                        f"short append: wrote {written}/{len(payload)} bytes")
                wrote_full_line = True
                os.fsync(fd)
                return
            except OSError as exc:
                last_error = exc
                # A complete O_APPEND write may already be durable even when
                # fsync/close reports an error.  Never retry that line and risk
                # recording one physical click twice.
                if wrote_full_line or attempt + 1 >= self.append_attempts:
                    raise
                time.sleep(self.append_retry_s)
            finally:
                if fd is not None:
                    os.close(fd)
        if last_error is not None:  # pragma: no cover - loop always returns/raises
            raise last_error

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll_s):
            self.poll_once()


def parse_ldplayer_list2(output: str | bytes) -> list[dict]:
    """解析官方 ``ldconsole list2`` 数字字段；标题乱码不影响映射。"""
    text = (output.decode("utf-8", errors="replace")
            if isinstance(output, bytes) else str(output))
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 7:
            continue
        try:
            index = int(parts[0])
            hwnd = int(parts[2])
            renderer_hwnd = int(parts[3])
            running = int(parts[4]) == 1
            pid = int(parts[5])
        except ValueError:
            continue
        rows.append({
            "index": index, "title": parts[1], "hwnd": hwnd,
            "renderer_hwnd": renderer_hwnd, "running": running, "pid": pid,
        })
    return rows


def resolve_ldplayer_target(
    serial: str,
    ldconsole_path: str | Path,
) -> Optional[ClickTarget]:
    """用官方实例 index 与默认 emulator 端口公式做双向精确映射。"""
    match = re.fullmatch(r"emulator-(\d+)", str(serial or ""))
    if not match:
        return None
    port = int(match.group(1))
    if port < 5554 or (port - 5554) % 2:
        return None
    expected_index = (port - 5554) // 2
    try:
        completed = subprocess.run(
            [str(ldconsole_path), "list2"], check=True,
            capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for row in parse_ldplayer_list2(completed.stdout):
        if (row["index"] == expected_index and row["running"]
                and row["hwnd"] > 0 and row["pid"] > 0):
            return ClickTarget(
                row["hwnd"], serial, True,
                "ldconsole_list2_index_port")
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="只记录指定雷电 HWND 内的鼠标左键按下")
    parser.add_argument("--hwnd", type=int)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--mapping-verified", action="store_true")
    parser.add_argument("--ldconsole", default=r"D:\leidian\LDPlayer14\ldconsole.exe")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.hwnd:
        target = ClickTarget(
            args.hwnd, args.serial or None, bool(args.mapping_verified),
            "explicit_hwnd" if args.mapping_verified else "caller_unverified")
    else:
        target = resolve_ldplayer_target(args.serial, args.ldconsole)
        if target is None:
            print("无法把 serial 精确映射到正在运行的雷电窗口；拒绝猜测")
            return 2
    stop = threading.Event()
    try:
        HumanInputMonitor(target, args.output).run(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
