# -*- coding: utf-8 -*-
"""Tests for structured follow-session JSONL logger."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import action_policy as ap  # noqa: E402
from session_logger import (  # noqa: E402
    FOLLOW_SESSION_FILENAME,
    FOLLOW_SESSION_SCHEMA,
    SessionLogger,
    abstain_reasons_from_event,
    build_follow_record,
)


def test_claim_tap_channel_stays_false():
    assert ap.CLAIM_TAP_CHANNEL_READY is True


def test_build_follow_record_shapes_vision_gates_caishen_decision():
    event = {
        "ts": "2026-10-24T20:00:00+08:00",
        "event": "idle",
        "round_seq": 2,
        "frame": "frames/x.png",
        "status": "abstain",
        "reason": "手牌张数不确定",
        "suggestion": None,
        "suggestion_id": None,
        "caishen": "w1",
        "card_count": 40,
        "turn_gate": {
            "state": "wait",
            "reason": "手牌 16 张，等待摸牌",
            "code": "wait_sixteen",
            "evidence": {"concealed": 16},
        },
        "table_state": {
            "mode": "tea_2p",
            "status": "abstain",
            "caishen": "w1",
            "caishen_known": True,
            "concealed_count": 16,
            "logical_meld_slots": 0,
            "wall_cards": 40,
            "countdown_status": "absent",
            "countdown_seconds": None,
            "incoming_tile": None,
            "incoming_source": None,
            "discards_trusted": True,
            "notes": ["hand_wait_sixteen"],
            "action_discs_seen": False,
            "action_disc_count": 0,
            "hand_ids": list(range(1, 17)),
        },
    }
    record = build_follow_record(event, session="/tmp/s", tick=3)
    assert record["schema"] == FOLLOW_SESSION_SCHEMA
    assert record["claim_tap_channel_ready"] is True
    assert record["safety"] == "claim_channel_ready_gated"
    assert record["tick"] == 3
    assert record["vision"]["status"] == "abstain"
    assert record["vision"]["hand_ids"] == list(range(1, 17))
    assert record["caishen"]["label"] == "w1"
    assert record["caishen"]["known"] is True
    assert "turn_gate:手牌 16 张，等待摸牌" in record["gates"]["abstain_reasons"]
    assert "table:hand_wait_sixteen" in record["gates"]["abstain_reasons"]
    assert record["decision"]["kind"] == "none"
    assert record["table"]["wall_cards"] == 40


def test_claim_decision_block_never_marks_channel_ready():
    event = {
        "event": "idle",
        "guard": "visible",
        "claim": {
            "status": "blocked",
            "action": "peng",
            "blocked_reason": "来牌归属未知",
            "claim_reason": "行动行可见",
            "executable": False,
            "auto_click": {
                "requested": True,
                "executed": False,
                "channel_ready": False,
            },
        },
    }
    record = build_follow_record(event, session="s", tick=1)
    assert record["decision"]["kind"] == "claim"
    assert record["decision"]["claim_tap_channel_ready"] is True
    assert record["claim_tap_channel_ready"] is True
    reasons = abstain_reasons_from_event(event)
    assert any("claim_blocked" in item for item in reasons)


def test_session_logger_writes_jsonl(tmp_path: Path):
    logger = SessionLogger(tmp_path)
    logger.write_header({"mode": "tea_2p"})
    logger.append(
        {
            "ts": "2026-10-24T20:01:00+08:00",
            "event": "dry_run_plan",
            "round_seq": 1,
            "status": "advise",
            "suggestion_id": 3,
            "caishen": "t5",
            "tap": [100, 800],
            "turn_gate": {"state": "go", "reason": "ok", "code": "ok", "evidence": {}},
            "table_state": {
                "caishen": "t5",
                "caishen_known": True,
                "hand_ids": [1, 2, 3],
                "notes": [],
            },
        },
        tick=1,
        analysis={"hand_ids": [1, 2, 3], "shanten": 1, "status": "advise"},
    )
    assert logger.path is not None
    assert logger.path.name == FOLLOW_SESSION_FILENAME
    rows = logger.read_all()
    assert len(rows) == 2
    assert rows[0]["kind"] == "session_header"
    assert rows[0]["claim_tap_channel_ready"] is True
    assert rows[1]["schema"] == FOLLOW_SESSION_SCHEMA
    assert rows[1]["vision"]["hand_ids"] == [1, 2, 3]
    assert rows[1]["decision"]["kind"] == "discard"
    # Round-trip JSONL
    raw = logger.path.read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(line) for line in raw)


def test_disabled_logger_writes_nothing(tmp_path: Path):
    logger = SessionLogger(tmp_path, enabled=False)
    assert logger.append({"event": "idle"}) is None
    assert not (tmp_path / FOLLOW_SESSION_FILENAME).exists()


def test_format_follow_line_gate_abstain_decision():
    from session_logger import format_follow_line, read_follow_lines

    header = {
        "schema": FOLLOW_SESSION_SCHEMA,
        "kind": "session_header",
        "at": "2026-10-24T20:00:00+08:00",
        "claim_tap_channel_ready": False,
    }
    assert format_follow_line(header) is None

    abstain_event = {
        "at": "2026-10-24T20:05:11+08:00",
        "tick": 7,
        "event": "idle",
        "gates": {
            "turn": {"state": "wait", "reason": "手牌 16 张，等待摸牌", "code": "wait_sixteen"},
            "abstain_reasons": ["turn_gate:手牌 16 张，等待摸牌"],
            "stop_kind": None,
        },
        "vision": {"status": "abstain", "reason": "手牌张数不确定"},
        "decision": {"kind": "none", "claim_tap_channel_ready": False},
        "claim_tap_channel_ready": False,
    }
    line = format_follow_line(abstain_event)
    assert line is not None
    assert "20:05:11" in line
    assert "t7" in line
    assert "gate=wait/wait_sixteen" in line
    assert "dec=none" in line
    assert "abstain=" in line
    assert "WARN_CLAIM_CHANNEL_READY" not in line

    decide = {
        "at": "2026-10-24T20:06:00+08:00",
        "tick": 8,
        "event": "dry_run_plan",
        "gates": {
            "turn": {"state": "go", "code": "ok"},
            "abstain_reasons": [],
        },
        "vision": {"status": "advise"},
        "decision": {
            "kind": "discard",
            "suggestion_id": 3,
            "claim_tap_channel_ready": False,
        },
        "claim_tap_channel_ready": False,
    }
    line2 = format_follow_line(decide)
    assert line2 is not None
    assert "dec=discard#3" in line2
    assert "gate=go" in line2


def test_read_follow_lines_tails_file(tmp_path: Path):
    from session_logger import format_follow_line, read_follow_lines

    logger = SessionLogger(tmp_path)
    logger.write_header({"mode": "tea_2p"})
    for tick in range(1, 4):
        logger.append(
            {
                "ts": f"2026-10-24T20:1{tick}:00+08:00",
                "event": "idle",
                "status": "abstain",
                "reason": f"reason-{tick}",
                "turn_gate": {"state": "wait", "reason": "r", "code": "wait_sixteen"},
                "table_state": {"notes": [], "caishen": "w1", "caishen_known": True},
            },
            tick=tick,
        )
    lines = read_follow_lines(tmp_path / FOLLOW_SESSION_FILENAME, limit=2)
    assert len(lines) == 2
    assert all("gate=wait/wait_sixteen" in item for item in lines)
    assert ap.CLAIM_TAP_CHANNEL_READY is True


def test_resolve_follow_session_path_prefers_session(tmp_path: Path):
    from session_logger import resolve_follow_session_path

    session = tmp_path / "sess"
    session.mkdir()
    target = session / FOLLOW_SESSION_FILENAME
    target.write_text("{}\n", encoding="utf-8")
    other_root = tmp_path / "other"
    other_root.mkdir()
    older = other_root / FOLLOW_SESSION_FILENAME
    older.write_text("{}\n", encoding="utf-8")
    found = resolve_follow_session_path(session, search_roots=[other_root])
    assert found == target
    assert resolve_follow_session_path(tmp_path / "missing", search_roots=[other_root]) == older

