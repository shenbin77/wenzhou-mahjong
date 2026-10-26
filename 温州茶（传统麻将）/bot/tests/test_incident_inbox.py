from __future__ import annotations

import json
from pathlib import Path

from incident_inbox import (
    create_incident,
    mark_incident_resolved,
    sanitize_event,
)


def _issue() -> dict:
    return {
        "category": "stuck_suspected", "class": "decision",
        "source": "actions.jsonl", "line": 10, "line_end": 13,
        "ts": "2026-10-26T18:00:00+08:00", "frame": "frames/a.png",
        "excerpt": {"stall_class": "actionable", "threshold_s": 5.0,
                    "reason": "same state", "token": "must-not-copy",
                    "state_signature": {"gate": "bright", "cookie": "no"}},
    }


def test_sanitize_event_is_allowlist_not_recursive_dump() -> None:
    safe = sanitize_event({
        "ts": "x", "event": "idle", "frame": "a.png",
        "token": "secret", "cookie": "secret", "chat": "private",
        "random_blob": {"password": "secret"},
        "frame_facts": {"turn_actionable": True, "caishen": "w1",
                        "private": "no"},
    })
    assert safe["event"] == "idle"
    assert safe["frame_facts"] == {"turn_actionable": True, "caishen": "w1"}
    assert "token" not in safe and "random_blob" not in safe


def test_incident_is_atomic_idempotent_and_resolvable(tmp_path: Path) -> None:
    session = tmp_path / "ui_20261026_emulator5554_玩家昵称"
    session.mkdir()
    root = tmp_path / "inbox"
    recent = [{"ts": "x", "event": "idle", "frame": "frames/a.png",
               "cookie": "never"}]
    first = create_incident(
        _issue(), session, recent, incident_root=root,
        serial="emulator-5554", repo_version="abc123",
        process_info={"pid": 10, "token": "never"})
    second = create_incident(
        _issue(), session, recent, incident_root=root,
        serial="emulator-5554", repo_version="abc123")
    assert first["incident_id"] == second["incident_id"]
    assert second["occurrences"] == 2
    folder = root / first["incident_id"]
    payload = json.loads((folder / "incident.json").read_text(encoding="utf-8"))
    assert payload["privacy"]["secrets_allowed"] is False
    assert "token" not in json.dumps(payload)
    assert payload["first_failure"]["excerpt"]["state_signature"] == {
        "gate": "bright"}
    assert not list(folder.glob("*.tmp"))
    resolved = mark_incident_resolved(
        root, first["incident_id"], resolution="fixed and regressed")
    assert resolved["status"] == "resolved"
    rescanned = create_incident(
        _issue(), session, recent, incident_root=root,
        serial="emulator-5554", repo_version="abc123")
    assert rescanned["status"] == "resolved"
    assert rescanned["resolved_at"] == resolved["resolved_at"]
    assert rescanned["resolution"] == "fixed and regressed"
    assert "reopened_at" not in rescanned
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert index["incidents"][0]["status"] == "resolved"


def test_incident_redacts_absolute_paths_nickname_and_screen_metadata(
        tmp_path: Path) -> None:
    nickname = "敏感玩家昵称"
    session = tmp_path / f"ui_20261026_emulator5554_{nickname}"
    session.mkdir()
    frame = session / "frames" / "before.png"
    issue = _issue()
    issue["frame"] = str(frame)
    issue["excerpt"] = {
        "frame": str(frame),
        "human_click": {
            "ts": "2026-10-26T18:00:00+08:00",
            "serial": "emulator-5554",
            "client_xy": [10, 20],
            "screen_xy": [110, 220],
            "window_title": f"雷电模拟器 - {nickname}",
            "delta_to_progress_s": 1.0,
        },
    }
    payload = create_incident(
        issue, session, [{"event": "idle", "frame": str(frame)}],
        incident_root=tmp_path / "inbox", serial="emulator-5554")
    encoded = json.dumps(payload, ensure_ascii=False)
    click = payload["first_failure"]["excerpt"]["human_click"]

    assert str(tmp_path) not in encoded
    assert nickname not in encoded
    assert "window_title" not in encoded
    assert "screen_xy" not in encoded
    assert click["client_xy"] == [10, 20]
    assert click["serial"] == "emulator-5554"
    assert payload["session_dir"].startswith("session/")
    assert payload["evidence_paths"] == [
        f"{payload['session_dir']}/frames/before.png"]


def test_rescan_sanitizes_legacy_first_failure(tmp_path: Path) -> None:
    nickname = "旧包玩家昵称"
    session = tmp_path / f"ui_{nickname}"
    session.mkdir()
    root = tmp_path / "inbox"
    payload = create_incident(
        _issue(), session, [], incident_root=root,
        serial="emulator-5554")
    path = root / payload["incident_id"] / "incident.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["first_failure"]["frame"] = str(session / "frames" / "x.png")
    legacy["first_failure"]["excerpt"]["human_click"] = {
        "window_title": f"雷电-{nickname}", "screen_xy": [1, 2],
        "client_xy": [3, 4], "serial": "emulator-5554"}
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    rescanned = create_incident(
        _issue(), session, [], incident_root=root,
        serial="emulator-5554")
    encoded = json.dumps(rescanned, ensure_ascii=False)

    assert str(tmp_path) not in encoded
    assert nickname not in encoded
    assert "window_title" not in encoded
    assert "screen_xy" not in encoded
    assert rescanned["first_failure"]["excerpt"]["human_click"][
        "client_xy"] == [3, 4]

