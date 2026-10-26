"""按设备的全自动控制文件：不点界面也能让运行中的 assist_window 恢复/关闭全自动。

文件：``D:\\温州麻将\\_control\\full_auto_<serial>.cmd``，内容 ``resume`` 或 ``stop``。
assist_window 每 500ms 读取一次并立刻删除（消费一次）。``resume`` 只是
“申请”，仍要走 _start_full_auto 的全部现有门禁（通道/玩法/昵称/跟随）。

命令行：``python full_auto_control.py resume emulator-5556``
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

CONTROL_DIR = Path(__file__).resolve().parents[2] / "_control"
COMMANDS = ("resume", "stop")


def _safe(serial: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)


def control_path(serial: str, root: Path | None = None) -> Path:
    return Path(root or CONTROL_DIR) / f"full_auto_{_safe(serial)}.cmd"


def write_command(serial: str, command: str, root: Path | None = None) -> Path:
    command = command.strip().lower()
    if command not in COMMANDS:
        raise ValueError(f"未知命令 {command!r}，只支持 {COMMANDS}")
    path = control_path(serial, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(command, encoding="utf-8")
    return path


def consume_command(serial: str, root: Path | None = None) -> Optional[str]:
    """读取并删除控制文件；无文件/内容非法返回 None（非法文件也删除）。"""
    if not serial:
        return None
    path = control_path(serial, root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        return None   # 删不掉就不执行，避免重复消费
    return text if text in COMMANDS else None


def revoke_pending_command(serial: str, root: Path | None = None) -> bool:
    """Remove stale click authority before crash-recovering a UI window.

    Missing is success.  Any deletion failure is fail-closed so a newly opened
    assistant cannot consume an old ``resume`` command.
    """
    path = control_path(serial, root)
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("用法: python full_auto_control.py resume|stop <serial>")
        return 2
    print(write_command(args[1], args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
