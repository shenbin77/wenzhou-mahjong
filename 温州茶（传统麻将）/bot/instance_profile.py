"""Stable per-emulator UI profile paths. One product, any number of windows.

No emulator activity or network calls occur in this module.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from pathlib import Path

SERIAL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,127}$")


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


def profile_paths(base: Path, serial: str) -> ProfilePaths:
    safe = safe_serial(serial)
    # TCP ADB serials contain ':', illegal in a Windows folder name.
    # Include a hash so normalization cannot merge distinct serials.
    stem = re.sub(r"[^a-zA-Z0-9_.-]", "_", safe)
    folder = stem + "-" + hashlib.sha256(safe.encode("utf-8")).hexdigest()[:10]
    profile = Path(base) / "profiles" / folder
    return ProfilePaths(safe, profile / "binding.json",
                        profile / "last_account.txt",
                        profile / "window_state.json")


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
