# -*- coding: utf-8 -*-
"""原子生成脱敏故障包，供本机巡检/Codex 后续消费。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "create_incident",
    "mark_incident_resolved",
    "read_repo_version",
    "sanitize_event",
]

_EVENT_KEYS = (
    "ts", "at", "event", "status", "gate", "reason", "round_seq", "tick",
    "frame", "after_frame", "device_serial", "serial", "mode", "verified",
    "action_id", "action_source", "source",
)
_BLOCKED_KEY_TOKENS = (
    "token", "cookie", "password", "passwd", "secret", "authorization",
    "api_key", "apikey", "chat", "prompt",
)
_DROPPED_INCIDENT_KEYS = {"window_title", "screen_xy"}


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _safe_scalar(value: Any, limit: int = 500) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + "…"
        return value
    return None


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    scalar = _safe_scalar(value)
    if scalar is not None or value is None:
        return scalar
    if depth >= 3:
        return None
    if isinstance(value, Mapping):
        result = {}
        for key, part in list(value.items())[:40]:
            name = str(key)
            if (name in _DROPPED_INCIDENT_KEYS
                    or any(token in name.lower()
                           for token in _BLOCKED_KEY_TOKENS)):
                continue
            safe = _safe_value(part, depth=depth + 1)
            if safe is not None:
                result[name] = safe
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for part in list(value)[:40]:
            safe = _safe_value(part, depth=depth + 1)
            if safe is not None:
                result.append(safe)
        return result
    return None


def sanitize_event(item: Mapping[str, Any]) -> dict:
    """只留故障定位白名单；不递归复制任意 payload。"""
    result: dict[str, Any] = {}
    for key in _EVENT_KEYS:
        if any(token in key.lower() for token in _BLOCKED_KEY_TOKENS):
            continue
        if key in item:
            value = _safe_scalar(item.get(key))
            if value is not None:
                result[key] = value
    facts = item.get("frame_facts")
    if isinstance(facts, Mapping):
        result["frame_facts"] = {
            key: _safe_scalar(facts.get(key), 200)
            for key in ("round_seq", "turn_status", "turn_actionable",
                        "countdown_status", "caishen", "concealed_count")
            if _safe_scalar(facts.get(key), 200) is not None
        }
    return result


def read_repo_version(project_root: str | Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(project_root), check=True, capture_output=True,
            text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _issue_fingerprint(issue: Mapping[str, Any], session_dir: Path) -> str:
    excerpt = issue.get("excerpt")
    excerpt = excerpt if isinstance(excerpt, Mapping) else {}
    material = {
        "session": str(session_dir.resolve()),
        "category": issue.get("category"),
        "line": issue.get("line"),
        "line_end": issue.get("line_end"),
        "gate": excerpt.get("gate"),
        "stall_class": excerpt.get("stall_class"),
        "ts_start": excerpt.get("ts_start") or issue.get("ts"),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _session_reference(session_dir: Path) -> str:
    digest = hashlib.sha256(
        str(session_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"session/{digest}"


def _safe_path_part(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._-]+", value or ""):
        return value
    suffix = Path(value).suffix
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"part-{digest}{suffix if re.fullmatch(r'[.A-Za-z0-9_-]+', suffix) else ''}"


def _evidence_reference(value: Any, session_dir: Path) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if re.fullmatch(r"session/[0-9a-f]{12}(?:/[A-Za-z0-9._/-]+)?", value):
        return value
    candidate = Path(value)
    try:
        relative = (candidate.resolve().relative_to(session_dir.resolve())
                    if candidate.is_absolute() else candidate)
    except (OSError, ValueError):
        relative = Path(candidate.name)
    safe_parts = [
        _safe_path_part(part) for part in relative.parts
        if part not in ("", ".", "..", relative.anchor)]
    if not safe_parts:
        return _session_reference(session_dir)
    return "/".join([_session_reference(session_dir), *safe_parts])


def _sanitize_excerpt(value: Mapping[str, Any], session_dir: Path) -> dict:
    result: dict[str, Any] = {}
    for key, part in value.items():
        name = str(key)
        if (name in _DROPPED_INCIDENT_KEYS
                or any(token in name.lower()
                       for token in _BLOCKED_KEY_TOKENS)):
            continue
        if name in {"frame", "after_frame"}:
            safe = _evidence_reference(part, session_dir)
        else:
            safe = _safe_value(part)
        if safe is not None:
            result[name] = safe
    return result


def _sanitize_failure_record(
    issue: Mapping[str, Any],
    session_dir: Path,
) -> dict:
    result = {
        key: _safe_scalar(issue.get(key))
        for key in ("category", "class", "source", "line", "line_end",
                    "ts", "reason")
        if _safe_scalar(issue.get(key)) is not None
    }
    frame_reference = _evidence_reference(issue.get("frame"), session_dir)
    if frame_reference is not None:
        result["frame"] = frame_reference
    excerpt = issue.get("excerpt")
    result["excerpt"] = _sanitize_excerpt(
        excerpt if isinstance(excerpt, Mapping) else {}, session_dir)
    return result


def _evidence_paths(
    issue: Mapping[str, Any],
    recent: Sequence[Mapping[str, Any]],
    session_dir: Path,
) -> list[str]:
    paths: list[str] = []
    for value in [issue.get("frame"), *[
            item.get("frame") or item.get("after_frame") for item in recent]]:
        reference = _evidence_reference(value, session_dir)
        if reference is not None and reference not in paths:
            paths.append(reference)
    return paths[-20:]


def _render_readme(payload: Mapping[str, Any]) -> str:
    issue = payload.get("first_failure") or {}
    return "\n".join([
        f"# Incident {payload.get('incident_id')}",
        "",
        f"- 状态：{payload.get('status')}",
        f"- 分类：{issue.get('category')}",
        f"- 设备：{payload.get('serial') or 'unknown'}",
        f"- 版本：{payload.get('repo_version') or 'unknown'}",
        f"- 首次发现：{payload.get('created_at')}",
        f"- 最近发现：{payload.get('last_seen_at')}",
        f"- 原因：{issue.get('reason') or issue.get('excerpt', {}).get('reason') or ''}",
        "",
        "此包只含白名单结构化事件和证据路径，不复制密钥、Cookie、聊天或原始无关日志。",
        "",
    ])


def _read_json(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _update_index(root: Path, payload: Mapping[str, Any]) -> None:
    path = root / "index.json"
    current = _read_json(path) or {"version": 1, "incidents": []}
    entries = [entry for entry in current.get("incidents", [])
               if isinstance(entry, dict)
               and entry.get("incident_id") != payload.get("incident_id")]
    entries.append({
        "incident_id": payload.get("incident_id"),
        "status": payload.get("status"),
        "category": (payload.get("first_failure") or {}).get("category"),
        "serial": payload.get("serial"),
        "created_at": payload.get("created_at"),
        "last_seen_at": payload.get("last_seen_at"),
        "path": str(root / str(payload.get("incident_id"))),
    })
    entries.sort(key=lambda entry: str(entry.get("created_at") or ""))
    _atomic_text(path, json.dumps(
        {"version": 1, "incidents": entries}, ensure_ascii=False,
        indent=2, sort_keys=True) + "\n")


def create_incident(
    issue: Mapping[str, Any],
    session_dir: str | Path,
    recent_events: Sequence[Mapping[str, Any]],
    *,
    incident_root: str | Path,
    serial: Optional[str] = None,
    repo_version: Optional[str] = None,
    process_info: Optional[Mapping[str, Any]] = None,
) -> dict:
    """幂等创建/更新一个 open incident；返回完整脱敏 payload。"""
    session = Path(session_dir)
    root = Path(incident_root)
    incident_id = _issue_fingerprint(issue, session)
    folder = root / incident_id
    old = _read_json(folder / "incident.json")
    now = _now()
    sanitized_recent = [sanitize_event(item) for item in recent_events[-30:]]
    sanitized_recent = [item for item in sanitized_recent if item]
    for item in sanitized_recent:
        for key in ("frame", "after_frame"):
            if key in item:
                item[key] = _evidence_reference(item[key], session)
    safe_process = {}
    if isinstance(process_info, Mapping):
        for key in ("pid", "started_at", "serial", "mode", "enabled"):
            value = _safe_scalar(process_info.get(key), 200)
            if value is not None:
                safe_process[key] = value
    first = _sanitize_failure_record(issue, session)
    old_first = old.get("first_failure") if old else None
    if isinstance(old_first, Mapping):
        old_first = _sanitize_failure_record(old_first, session)
    payload = {
        "schema": 1,
        "incident_id": incident_id,
        "status": "open" if not old else old.get("status", "open"),
        "created_at": old.get("created_at") if old else now,
        "last_seen_at": now,
        "occurrences": int(old.get("occurrences", 0)) + 1 if old else 1,
        "session_dir": _session_reference(session),
        "serial": serial,
        "repo_version": repo_version,
        "process": safe_process,
        "first_failure": old_first if old_first is not None else first,
        "latest_failure": first,
        "recent_events": sanitized_recent,
        "evidence_paths": _evidence_paths(issue, recent_events, session),
        "privacy": {
            "raw_logs_copied": False,
            "screenshots_copied": False,
            "secrets_allowed": False,
        },
    }
    if old and old.get("status") == "resolved":
        payload["resolved_at"] = _safe_scalar(old.get("resolved_at"), 100)
        payload["resolution"] = _safe_scalar(old.get("resolution"), 1000)
    _atomic_text(folder / "incident.json", json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _atomic_text(folder / "README.md", _render_readme(payload))
    _update_index(root, payload)
    return payload


def mark_incident_resolved(
    incident_root: str | Path,
    incident_id: str,
    *,
    resolution: str,
) -> dict:
    root = Path(incident_root)
    path = root / incident_id / "incident.json"
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    payload["status"] = "resolved"
    payload["resolved_at"] = _now()
    payload["resolution"] = str(resolution)[:1000]
    _atomic_text(path, json.dumps(payload, ensure_ascii=False,
                                  indent=2, sort_keys=True) + "\n")
    _atomic_text(path.parent / "README.md", _render_readme(payload))
    _update_index(root, payload)
    return payload
