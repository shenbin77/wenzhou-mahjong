# -*- coding: utf-8 -*-
"""Regression tests for the user's real competition replay route."""
from pathlib import Path

from tools.competition_route import (
    COMPETITION_REPLAY_STEPS,
    LIVE_OBSERVED_ROUTE_STEPS,
    build_manifest,
    extract_touch_downs,
    load_record,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "操作录制" / "浙江游戏大厅操作录制_20260821-2024.record"


def test_historical_record_has_expected_resolution_and_touch_trace():
    data = load_record(SOURCE)
    downs = extract_touch_downs(data)
    assert data["recordInfo"]["resolutionWidth"] == 1600
    assert data["recordInfo"]["resolutionHeight"] == 900
    assert len(downs) >= 20
    assert {"timing_ms": 3785, "x": 373.0, "y": 573.0, "pointer_id": 1} in downs
    assert {"timing_ms": 24103, "x": 1509.0, "y": 258.0, "pointer_id": 1} in downs


def test_route_steps_are_replay_only_and_not_live_table_actions():
    ids = [step["id"] for step in COMPETITION_REPLAY_STEPS]
    assert ids[0] == "competition_entry"
    assert ids[-1] == "record_area_b"
    assert all("table" not in step["id"] for step in COMPETITION_REPLAY_STEPS)
    assert all(0 <= step["x"] <= 1600 and 0 <= step["y"] <= 900
               for step in COMPETITION_REPLAY_STEPS)


def test_manifest_explicitly_does_not_promote_ordinary_game_capture():
    manifest = build_manifest(SOURCE)
    assert manifest["route"]["target"] == "飞翔的比赛场/比赛战绩/查看回放"
    assert manifest["verification"]["historical_trace_recovered"] is True
    assert manifest["verification"]["live_replay_capture_verified"] is False
    assert manifest["verification"]["ordinary_game_capture_is_not_substitute"] is True


def test_live_observed_route_ends_at_replay_without_table_action():
    ids = [step["id"] for step in LIVE_OBSERVED_ROUTE_STEPS]
    assert ids == [
        "competition_entry", "enter_flyxiang", "competition_records",
        "select_date", "detail", "load_replays", "round_replay",
    ]
    assert "start_game" not in ids and "discard" not in ids
    assert all(0 <= step["x"] <= 1600 and 0 <= step["y"] <= 900
               for step in LIVE_OBSERVED_ROUTE_STEPS)
