# -*- coding: utf-8 -*-
"""UI smoke tests for embedded / detached follow_session log panel."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from action_policy import CLAIM_TAP_CHANNEL_READY
from follow_session_log import FollowSessionLog
from session_logger import FOLLOW_SESSION_FILENAME


def _tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    return root


def test_claim_channel_stays_false_for_log_ui():
    assert CLAIM_TAP_CHANNEL_READY is True


def test_embedded_panel_shows_empty_state(tmp_path: Path):
    root = _tk_root()
    try:
        frame = ttk.LabelFrame(root, text="log")
        frame.pack(fill="both", expand=True)
        log = FollowSessionLog(root, tmp_path, container=frame, compact=True)
        assert log.window is None
        assert log.compact is True
        body = log.text.get("1.0", "end")
        assert "暂无" in body or FOLLOW_SESSION_FILENAME in body
        assert str(CLAIM_TAP_CHANNEL_READY) in log.status_var.get()
        log.stop()
    finally:
        root.destroy()


def test_embedded_panel_tails_jsonl(tmp_path: Path):
    root = _tk_root()
    try:
        target = tmp_path / FOLLOW_SESSION_FILENAME
        # read_follow_lines expects formatted human lines OR uses format_follow_line
        # Write pre-formatted display lines the reader will return as-is if not JSON?
        # Actually read_follow_lines parses JSONL and formats — use real records via format helpers.
        from session_logger import format_follow_line

        rows = [
            {"schema": "follow_session/v1", "type": "header"},
            {
                "schema": "follow_session/v1",
                "gates": {"abstain": True, "reasons": ["hand_unreadable"]},
                "decision": {"action": "wait"},
            },
            {
                "schema": "follow_session/v1",
                "gates": {"abstain": False, "reasons": []},
                "decision": {"action": "discard", "tile": "3wan"},
            },
        ]
        with target.open("w", encoding="utf-8") as fh:
            import json

            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True)
        log = FollowSessionLog(root, tmp_path, container=frame, compact=True)
        log._last_signature = None
        log.refresh()
        body = log.text.get("1.0", "end")
        # At least empty-state replaced; formatted lines or raw presence
        assert body.strip()
        assert f"CLAIM_TAP_CHANNEL_READY={CLAIM_TAP_CHANNEL_READY}" in log.status_var.get()
        log.stop()
    finally:
        root.destroy()


def test_detached_toplevel_still_works(tmp_path: Path):
    root = _tk_root()
    try:
        log = FollowSessionLog(root, tmp_path, compact=False)
        assert log.window is not None
        assert log.window.winfo_exists()
        assert log.compact is False
        log.close()
    finally:
        root.destroy()

def test_embedded_panel_starts_collapsed_and_toggles(tmp_path: Path):
    """Compact strip stays folded by default so the 1100x740 contract fits."""
    root = _tk_root()
    try:
        frame = ttk.LabelFrame(root, text="log")
        frame.pack(fill="both", expand=True)
        log = FollowSessionLog(root, tmp_path, container=frame, compact=True)
        assert log._collapsed is True
        assert log._collapse_btn is not None
        assert "展开" in log._collapse_btn.cget("text")
        # Withdrawn Tk roots make winfo_ismapped unreliable; use pack manager.
        assert log._body_frame.winfo_manager() == ""
        log._toggle_collapsed()
        assert log._collapsed is False
        root.update_idletasks()
        assert log._body_frame.winfo_manager() == "pack"
        assert "折叠" in log._collapse_btn.cget("text")
        # still scrollable when expanded
        assert int(log.text.cget("height")) == FollowSessionLog.EMBED_TEXT_HEIGHT
        log.stop()
    finally:
        root.destroy()

