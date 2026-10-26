"""按设备的全自动控制文件：不点界面也能让运行中的 assist_window 恢复/关闭全自动。

文件：``D:\\温州麻将\\_control\\full_auto_<serial>-<hash>.cmd``，内容 ``resume`` 或 ``stop``。
assist_window 每 500ms 读取一次并立刻删除（消费一次）。``resume`` 只是
“申请”，仍要走 _start_full_auto 的全部现有门禁（通道/玩法/昵称/跟随）。

命令行：``python full_auto_control.py resume emulator-5556``
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from instance_profile import safe_serial

CONTROL_DIR = Path(__file__).resolve().parents[2] / "_control"
COMMANDS = ("resume", "stop")
COMMAND_LOCK_TIMEOUT_S = 0.5


def _safe(serial: str) -> str:
    value = safe_serial(serial)
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", value)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}"


def _legacy_control_path(serial: str, root: Path | None = None) -> Path | None:
    """Return only legacy names that cannot collide with another valid serial."""
    value = safe_serial(serial)
    if re.fullmatch(r"emulator-[0-9]+", value) is None:
        return None
    return Path(root or CONTROL_DIR) / f"full_auto_{value}.cmd"


def control_path(serial: str, root: Path | None = None) -> Path:
    return Path(root or CONTROL_DIR) / f"full_auto_{_safe(serial)}.cmd"


def _command_paths(serial: str, root: Path | None = None) -> tuple[Path, ...]:
    current = control_path(serial, root)
    legacy = _legacy_control_path(serial, root)
    return (current,) if legacy is None or legacy == current else (current, legacy)


@contextmanager
def _command_lock(serial: str, root: Path | None = None):
    """Serialize one command slot across manager/assistant processes."""
    lock_path = control_path(serial, root).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    backend = "windows" if os.name == "nt" else "posix"
    try:
        if backend == "windows":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + COMMAND_LOCK_TIMEOUT_S
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("全自动控制槽正忙，已安全放弃")
                    time.sleep(0.01)
        else:
            import fcntl

            deadline = time.monotonic() + COMMAND_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("全自动控制槽正忙，已安全放弃")
                    time.sleep(0.01)
        yield
    finally:
        if acquired:
            try:
                if backend == "windows":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _read_commands_unlocked(
        serial: str, root: Path | None = None,
) -> tuple[list[tuple[Path, str]], list[Path]]:
    observed: list[tuple[Path, str]] = []
    unreadable: list[Path] = []
    for path in _command_paths(serial, root):
        if not path.is_file():
            continue
        try:
            command = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            unreadable.append(path)
        else:
            observed.append((path, command))
    return observed, unreadable


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _atomic_write_command(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(command)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _revoke_resume_files_unlocked(
        serial: str, root: Path | None = None) -> bool:
    """Delete inherited resume authority while preserving every stop command."""
    for path in _command_paths(serial, root):
        if not path.is_file():
            continue
        try:
            command = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return False
        if command == "stop":
            continue
        if command != "resume":
            return False
        try:
            path.unlink()
        except OSError:
            return False
    return True


def write_command(serial: str, command: str, root: Path | None = None) -> Path:
    command = command.strip().lower()
    if command not in COMMANDS:
        raise ValueError(f"未知命令 {command!r}，只支持 {COMMANDS}")
    with _command_lock(serial, root):
        if command == "resume":
            observed, unreadable = _read_commands_unlocked(serial, root)
            if unreadable:
                raise OSError("已有全自动控制命令无法读取，拒绝覆盖")
            commands = [value for _path, value in observed]
            if "stop" in commands:
                raise RuntimeError("已有停手命令待消费，拒绝写入 resume")
            if any(value != "resume" for value in commands):
                raise RuntimeError("已有未知控制命令，拒绝写入 resume")
        path = control_path(serial, root)
        _atomic_write_command(path, command)
    return path


def consume_command(serial: str, root: Path | None = None) -> Optional[str]:
    """Atomically consume one command; stop always dominates resume."""
    if not serial:
        return None
    try:
        with _command_lock(serial, root):
            observed, unreadable = _read_commands_unlocked(serial, root)
            if not observed and not unreadable:
                return None
            stop_paths = [path for path, command in observed
                          if command == "stop"]
            if stop_paths:
                cleanup_ok = not unreadable
                for path, command in observed:
                    if command == "stop":
                        continue
                    if command != "resume" or not _unlink(path):
                        cleanup_ok = False
                # Never remove the durable stop until every competing resume
                # is gone.  If cleanup fails, repeated stop is safe.
                if cleanup_ok:
                    for path in stop_paths:
                        _unlink(path)
                return "stop"
            if unreadable or any(
                    command != "resume" for _path, command in observed):
                return None
            for path, _command in observed:
                if not _unlink(path):
                    return None
            return "resume" if observed else None
    except (OSError, TimeoutError):
        return None


def revoke_pending_command(serial: str, root: Path | None = None) -> bool:
    """Remove stale resume authority before starting a UI window.

    Missing is success. ``stop`` is deliberately preserved so a duplicate
    startup can never steal a pending stop from the already-running window.
    Any read/deletion failure is fail-closed.
    """
    try:
        with _command_lock(serial, root):
            return _revoke_resume_files_unlocked(serial, root)
    except (OSError, TimeoutError):
        return False


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("用法: python full_auto_control.py resume|stop <serial>")
        return 2
    print(write_command(args[1], args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
