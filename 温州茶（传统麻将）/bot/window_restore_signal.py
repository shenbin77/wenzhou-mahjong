"""Per-device, acknowledgement-backed control for an assistant window.

The manager and the Tk window run in different processes. Requests are written
atomically and contain only UI lifecycle commands. The Tk process consumes a
command on its own thread, performs it, and writes an ACK; the manager must not
claim success before that ACK arrives.

``request_restore`` / ``consume_restore`` remain compatible with the original
one-word restore file. New callers should use ``request_control`` and
``wait_for_ack`` so failure or an unresponsive Tk loop is visible.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from instance_profile import safe_serial


CONTROL_DIR = Path(__file__).resolve().parents[2] / "_control"
CONTROL_VERSION = 1
CONTROL_COMMANDS = frozenset({"restore", "quit"})
CONTROL_REQUEST_TTL_S = 15.0
_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class ControlRequest:
    serial: str
    command: str
    request_id: str
    created_at: float
    path: Path
    ack_path: Path | None


@dataclass(frozen=True)
class ControlAck:
    serial: str
    command: str
    request_id: str
    ok: bool
    detail: str


def _serial_parts(serial: str) -> tuple[str, str, str]:
    value = safe_serial(serial)
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", value)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return value, stem, digest


def restore_path(serial: str, root: Path | None = None) -> Path:
    _value, stem, digest = _serial_parts(serial)
    return (root if root is not None else CONTROL_DIR) / f"ui_restore_{stem}_{digest}.cmd"


def ack_path(serial: str, request_id: str, root: Path | None = None) -> Path:
    """Return the unique ACK path; malformed request ids are rejected."""
    if not _REQUEST_ID_RE.fullmatch(str(request_id or "")):
        raise ValueError("invalid UI control request id")
    _value, stem, digest = _serial_parts(serial)
    return ((root if root is not None else CONTROL_DIR)
            / f"ui_ack_{stem}_{digest}_{request_id}.json")


def _atomic_write(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(data, encoding=encoding)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _exclusive_write(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Create one request without overwriting an unconsumed command.

    The control path is deliberately a one-slot mailbox.  Replacing its
    contents would allow a second restore request to erase an earlier quit (or
    vice versa) before the Tk process consumes it.  Exclusive creation makes
    that race visible to the caller instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def request_restore(serial: str, root: Path | None = None) -> Path:
    """Legacy fire-and-forget restore request.

    Kept for repeat-click compatibility. The launcher manager uses
    :func:`request_control` instead and therefore waits for a real ACK.
    """
    path = restore_path(serial, root)
    try:
        _exclusive_write(path, "restore", encoding="ascii")
    except FileExistsError:
        # Repeated desktop activation is idempotent.  More importantly, never
        # overwrite a structured quit/restore request already waiting here.
        pass
    return path


def request_control(serial: str, command: str,
                    root: Path | None = None) -> ControlRequest:
    """Write one structured request and return its unique ACK ticket."""
    value = safe_serial(serial)
    action = str(command or "").strip().lower()
    if action not in CONTROL_COMMANDS:
        raise ValueError(f"unsupported UI control command: {action!r}")
    request_id = uuid.uuid4().hex
    created_at = time.time()
    path = restore_path(value, root)
    reply = ack_path(value, request_id, root)
    try:
        reply.unlink()
    except OSError:
        pass
    payload = {
        "version": CONTROL_VERSION,
        "serial": value,
        "command": action,
        "request_id": request_id,
        "created_at": created_at,
    }
    _discard_stale_mailbox(value, path, now=created_at)
    try:
        _exclusive_write(path, json.dumps(
            payload, ensure_ascii=True, separators=(",", ":")))
    except FileExistsError as exc:
        raise RuntimeError(
            f"{value} 已有未处理的窗口控制请求；请等待本次操作完成") from exc
    return ControlRequest(value, action, request_id, created_at, path, reply)


def _discard_stale_mailbox(serial: str, path: Path, *, now: float) -> bool:
    """Remove only a provably expired one-slot request before a new write."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        modified_at = path.stat().st_mtime
    except (OSError, UnicodeError):
        return False
    created_at = modified_at
    if raw != "restore":
        try:
            payload = json.loads(raw)
            if safe_serial(str(payload.get("serial"))) != serial:
                return False
            created_at = float(payload.get("created_at"))
        except (TypeError, ValueError, json.JSONDecodeError):
            created_at = modified_at
    age = now - created_at
    if age <= CONTROL_REQUEST_TTL_S:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def consume_control(serial: str,
                    root: Path | None = None) -> ControlRequest | None:
    """Consume once; malformed or cross-device data fails closed."""
    value = safe_serial(serial)
    path = restore_path(value, root)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    try:
        path.unlink()
    except OSError:
        return None
    if raw == "restore":
        return ControlRequest(value, "restore", "", 0.0, path, None)
    try:
        payload = json.loads(raw)
        command = str(payload["command"]).strip().lower()
        request_id = str(payload["request_id"])
        request_serial = safe_serial(str(payload["serial"]))
        created_at = float(payload["created_at"])
        version = int(payload["version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (version != CONTROL_VERSION or request_serial != value
            or command not in CONTROL_COMMANDS
            or not _REQUEST_ID_RE.fullmatch(request_id)):
        return None
    age = time.time() - created_at
    if age > CONTROL_REQUEST_TTL_S or age < -CONTROL_REQUEST_TTL_S:
        return None
    return ControlRequest(
        value, command, request_id, created_at, path,
        ack_path(value, request_id, root))


def acknowledge_control(request: ControlRequest, *, ok: bool,
                        detail: str = "") -> Path | None:
    """Publish the result of a structured request after the action ran."""
    if request.ack_path is None or not request.request_id:
        return None
    payload = {
        "version": CONTROL_VERSION,
        "serial": request.serial,
        "command": request.command,
        "request_id": request.request_id,
        "ok": bool(ok),
        "detail": str(detail),
        "acknowledged_at": time.time(),
    }
    _atomic_write(
        request.ack_path,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return request.ack_path


def read_ack(request: ControlRequest, *, consume: bool = True) -> ControlAck | None:
    """Read only the ACK matching ``request``; stale ACKs cannot satisfy it."""
    path = request.ack_path
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    try:
        ack = ControlAck(
            serial=safe_serial(str(payload["serial"])),
            command=str(payload["command"]).strip().lower(),
            request_id=str(payload["request_id"]),
            ok=payload["ok"] is True,
            detail=str(payload.get("detail") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (ack.serial != request.serial or ack.command != request.command
            or ack.request_id != request.request_id):
        return None
    if consume:
        try:
            path.unlink()
        except OSError:
            return None
    return ack


def wait_for_ack(request: ControlRequest, *, timeout: float = 3.0,
                 poll_interval: float = 0.05) -> ControlAck | None:
    """Wait a bounded time for the matching ACK (safe from worker threads)."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        ack = read_ack(request)
        if ack is not None:
            return ack
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(max(0.005, float(poll_interval)), remaining))


def consume_restore(serial: str, root: Path | None = None) -> bool:
    """Backward-compatible one-shot restore consumer.

    A structured ``quit`` is deliberately consumed but returns ``False`` when
    an old window has not yet adopted :func:`consume_control`; the requester
    then times out and reports failure instead of claiming or forcing success.
    """
    request = consume_control(serial, root)
    return bool(request is not None and request.command == "restore")


__all__ = [
    "CONTROL_COMMANDS",
    "CONTROL_REQUEST_TTL_S",
    "ControlAck",
    "ControlRequest",
    "ack_path",
    "acknowledge_control",
    "consume_control",
    "consume_restore",
    "read_ack",
    "request_control",
    "request_restore",
    "restore_path",
    "wait_for_ack",
]
