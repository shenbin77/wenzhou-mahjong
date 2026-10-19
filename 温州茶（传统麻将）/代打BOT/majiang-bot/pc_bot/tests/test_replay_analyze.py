# -*- coding: utf-8 -*-
"""replay_analyze helpers (no emulator)."""
import json
from pathlib import Path

import pytest

# tools/ is under pc_bot/
import sys
PC_BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PC_BOT))

from tools import replay_analyze as ra  # noqa: E402


def test_discover_frames_empty(tmp_path):
    assert ra.discover_frames(tmp_path) == []


def test_discover_frames_recursive(tmp_path):
    d = tmp_path / "day1" / "game"
    d.mkdir(parents=True)
    (d / "a.png").write_bytes(b"x")
    (d / "b.JPG").write_bytes(b"x")
    (d / "c.txt").write_text("no")
    found = ra.discover_frames(tmp_path)
    assert len(found) == 2
    assert {p.suffix.lower() for p in found} == {".png", ".jpg"}


def test_compute_stats_empty_notes():
    stats = ra.compute_stats([], [], dry_run=True, note="empty")
    assert stats["n_frames"] == 0
    assert stats["win_rate"] is None
    assert "settle" in (stats["win_rate_note"] or "").lower() or "结算" in (stats["win_rate_note"] or "")
    assert stats["dry_run"] is True


def test_segment_rounds_basic():
    frames = [
        {"ok": True, "gameplay": True, "name": "f1", "concealed": list(range(1, 15)),
         "shanten": 3, "hongzhong": 1, "is_hand14": True, "settle_score": None},
        {"ok": True, "gameplay": True, "name": "f2", "concealed": list(range(1, 14)) + [21],
         "shanten": 2, "hongzhong": 1, "is_hand14": True, "settle_score": None},
        {"ok": True, "gameplay": False, "name": "settle", "concealed": [],
         "settle_score": 500, "shanten": None, "hongzhong": 0},
    ]
    rounds = ra.segment_rounds(frames)
    assert len(rounds) == 1
    assert rounds[0]["settle_score"] == 500
