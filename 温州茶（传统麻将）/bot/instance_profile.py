"""Stable per-emulator UI profile paths. One product, any number of windows.

No emulator activity or network calls occur in this module.
"""
from __future__ import annotations

import re
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

SERIAL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,127}$")
ASSISTANT_STATE_SCHEMA = "assistant_state/v1"


def safe_serial(serial: str) -> str:
    value = str(serial or "").strip()
    if not SERIAL_RE.fullmatch(value) or value in (".", ".."):
        raise ValueError("设备 serial 格式不合法，不能创建绑定配置")
    return value


@dataclass(frozen=True)
class ProfilePaths:
    serial: str
    binding: Path
    account: Path
    geometry: Path
    assistant_state: Path


def profile_paths(base: Path, serial: str) -> ProfilePaths:
    safe = safe_serial(serial)
    # TCP ADB serials contain ':', illegal in a Windows folder name.
    # Include a hash so normalization cannot merge distinct serials.
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", safe)
    folder = stem + "-" + hashlib.sha256(safe.encode("utf-8")).hexdigest()[:10]
    profile = Path(base) / "profiles" / folder
    return ProfilePaths(safe, profile / "binding.json",
                        profile / "last_account.txt",
                        profile / "window_state.json",
                        profile / "assistant_state.json")


def read_binding_for_serial(path: Path, serial: str):
    """Legacy migration guard: never restore another device's binding."""
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("serial") != safe_serial(serial):
        return None
    if not str(data.get("account") or "").strip():
        return None
    return data


def atomic_write_json(path: Path, payload: dict) -> None:
    """Replace one small profile JSON atomically, preserving the old file on crash."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def discover_profile_bindings(base: Path) -> dict[str, dict]:
    """Return valid saved bindings, rejecting misplaced/cross-serial files."""
    root = Path(base)
    found: dict[str, dict] = {}
    profiles = root / "profiles"
    if not profiles.is_dir():
        return found
    for candidate in profiles.glob("*/binding.json"):
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            serial = safe_serial(raw.get("serial") if isinstance(raw, dict) else "")
            expected = profile_paths(root, serial).binding
            if candidate.resolve() != expected.resolve():
                continue
            binding = read_binding_for_serial(candidate, serial)
        except (OSError, ValueError, TypeError):
            continue
        if binding is not None:
            found[serial] = binding
    return found


def discover_reserved_profile_serials(base: Path) -> set[str]:
    """Return every serial reserved by binding or assistant lifecycle state.

    Instance-copy safety uses this stricter inventory instead of the forgiving
    binding reader.  If a persisted file cannot be mapped back to its exact
    serial-specific path, callers cannot prove a future LDPlayer index is free
    of old assistant state, so the inventory fails closed without deleting it.
    """
    profiles = Path(base) / "profiles"
    if not profiles.is_dir():
        return set()
    reserved: set[str] = set()
    for filename, attribute in (
            ("binding.json", "binding"),
            ("assistant_state.json", "assistant_state")):
        for candidate in profiles.glob(f"*/{filename}"):
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                serial = safe_serial(
                    raw.get("serial") if isinstance(raw, dict) else "")
                expected = getattr(profile_paths(Path(base), serial), attribute)
                if candidate.resolve() != expected.resolve():
                    raise ValueError("profile path does not match serial")
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"存在无法核验的辅助器配置 {candidate}；拒绝复制雷电实例"
                ) from exc
            reserved.add(serial)
    return reserved


def read_assistant_desired_open(base: Path, serial: str, *,
                                default: bool = False) -> bool:
    """Read validated lifecycle intent; only a missing legacy file uses default."""
    safe = safe_serial(serial)
    path = profile_paths(base, safe).assistant_state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return bool(default)
    except (OSError, UnicodeError, ValueError):
        return False
    if (not isinstance(data, dict)
            or data.get("schema") != ASSISTANT_STATE_SCHEMA
            or data.get("serial") != safe
            or not isinstance(data.get("desired_open"), bool)):
        return False
    return bool(data["desired_open"])


def set_assistant_desired_open(base: Path, serial: str, desired: bool) -> None:
    """Persist only UI lifecycle intent; follow/auto-play authorization is excluded."""
    safe = safe_serial(serial)
    atomic_write_json(profile_paths(base, safe).assistant_state, {
        "schema": ASSISTANT_STATE_SCHEMA,
        "serial": safe,
        "desired_open": bool(desired),
    })
