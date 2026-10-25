"""Launch one copy of the SAME assist_window.py per connected LDPlayer.

Examples:
    python launch_multi.py --list
    python launch_multi.py --all
    python launch_multi.py --serial emulator-5556
Only starts UI windows; never presses any game control or enables auto-play.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from instance_profile import safe_serial
from harvest_tea import DEFAULT_ADB

HERE = Path(__file__).resolve().parent


def existing_ui_processes() -> tuple[list[int], set[str]]:
    """Avoid spawning another UI while an old global-binding window is open."""
    try:
        import psutil
    except ImportError:
        return [], set()  # Running new UIs still have their OS-level mutex.
    legacy: list[int] = []
    profiled: set[str] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            argv = proc.info.get("cmdline") or []
            if not any(Path(str(arg)).name.lower() == "assist_window.py"
                       for arg in argv[1:]):
                continue
            if "--serial" in argv and argv.index("--serial") + 1 < len(argv):
                profiled.add(safe_serial(argv[argv.index("--serial") + 1]))
            else:
                legacy.append(int(proc.pid))
        except (OSError, ValueError, IndexError, psutil.Error):
            continue
    return legacy, profiled


def connected_devices(adb_path: str = DEFAULT_ADB) -> list[str]:
    proc = subprocess.run([str(adb_path), "devices", "-l"], check=True,
                          capture_output=True, text=True, timeout=12)
    devices: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            try:
                devices.append(safe_serial(fields[0]))
            except ValueError:
                continue
    return sorted(set(devices))


def start_window(serial: str, *, detached: bool = True):
    """Spawn an independent UI process, never another BOT implementation."""
    serial = safe_serial(serial)
    exe = Path(sys.executable)
    if detached and exe.name.lower() == "python.exe":
        ui_exe = exe.with_name("pythonw.exe")
        if ui_exe.is_file():
            exe = ui_exe
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if detached else 0
    return subprocess.Popen(
        [str(exe), str(HERE / "assist_window.py"), "--serial", serial],
        cwd=str(HERE), creationflags=flags,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="仅显示在线模拟器")
    mode.add_argument("--all", action="store_true", help="为每个在线模拟器开辅助器窗口")
    mode.add_argument("--serial", help="为指定雷电窗口开辅助器")
    args = parser.parse_args()
    devices = connected_devices()
    if args.list:
        print("\n".join(devices) if devices else "当前没有可用的ADB设备")
        return 0
    selection = devices if args.all else [safe_serial(args.serial)]
    if not selection:
        print("没有可用设备，未启动任何窗口")
        return 1
    invalid = sorted(set(selection) - set(devices))
    if invalid:
        parser.error(f"设备不在线：{', '.join(invalid)}")
    legacy, open_serials = existing_ui_processes()
    if legacy:
        print(f"检测到旧版未隔离辅助器进程 {legacy}；请先关闭旧辅助器窗口，再使用多开启动器。")
        print("没有修改或结束现有进程；雷电模拟器不受影响。")
        return 2
    for serial in selection:
        if serial in open_serials:
            print(f"{serial}: 已有辅助器窗口，跳过重复启动")
            continue
        proc = start_window(serial)
        print(f"{serial}: UI PID={proc.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
