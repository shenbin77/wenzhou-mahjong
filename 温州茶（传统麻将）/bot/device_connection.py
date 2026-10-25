"""Read-only ADB connection verification for a pinned LDPlayer instance.

Being connected proves ADB reachability and a decodable live screenshot,
NOT the player's in-game identity, current mode, or safe autoplay readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
import subprocess

import cv2
import numpy as np

from instance_profile import safe_serial


@dataclass(frozen=True)
class DeviceConnection:
    success: bool
    serial: str
    detail: str
    width: int = 0
    height: int = 0


def verify_device(serial: str, adb_path: str) -> DeviceConnection:
    serial = safe_serial(serial)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        found = subprocess.run(
            [str(adb_path), "devices", "-l"],
            capture_output=True, text=True, timeout=8, check=True,
            creationflags=flags,
        )
        states = {}
        for line in found.stdout.splitlines()[1:]:
            words = line.split()
            if len(words) >= 2:
                states[words[0]] = words[1]
        if states.get(serial) != "device":
            state = states.get(serial, "未找到")
            return DeviceConnection(False, serial, f"ADB设备不可用：{serial}（{state}）")
        shot = subprocess.run(
            [str(adb_path), "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=14, check=True,
            creationflags=flags,
        )
        buf = np.frombuffer(shot.stdout, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
        if image is None or image.ndim != 3:
            return DeviceConnection(False, serial, "ADB在线但截图无效，请检查雷电画面")
        height, width = image.shape[:2]
        if (width, height) != (1600, 900):
            return DeviceConnection(
                False, serial,
                f"画面尺寸为{width}×{height}，BOT要求1600×900；已连接ADB但尚不允许跟随",
                width, height)
        return DeviceConnection(True, serial, f"连接成功 · {serial} · 画面1600×900",
                                width, height)
    except (subprocess.SubprocessError, OSError) as exc:
        return DeviceConnection(False, serial, f"无法连接ADB/读取画面：{exc}")
