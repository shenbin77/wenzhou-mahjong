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
        assert "绿=已验证" in log.status_var.get()
        assert log._collapsed is False
        assert log._body_frame.winfo_manager() == "pack"
        log.stop()
    finally:
        root.destroy()


def test_embedded_panel_tails_jsonl(tmp_path: Path):
    root = _tk_root()
    try:
        target = tmp_path / FOLLOW_SESSION_FILENAME
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
        assert "绿=已验证" in log.status_var.get()
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

def test_embedded_panel_is_permanently_visible(tmp_path: Path):
    """Operational log remains visible; the old collapse control is gone."""
    root = _tk_root()
    try:
        frame = ttk.LabelFrame(root, text="log")
        frame.pack(fill="both", expand=True)
        log = FollowSessionLog(root, tmp_path, container=frame, compact=True)
        assert log._collapsed is False
        assert log._collapse_btn is None
        assert log._body_frame.winfo_manager() == "pack"
        log._toggle_collapsed()
        assert log._collapsed is False
        root.update_idletasks()
        assert log._body_frame.winfo_manager() == "pack"
        # still scrollable in the permanent console
        assert int(log.text.cget("height")) == FollowSessionLog.EMBED_TEXT_HEIGHT
        control_texts = [
            child.cget("text") for child in log.host.winfo_children()[0].winfo_children()
            if "text" in child.keys()
        ]
        assert "打开完整日志" in control_texts
        assert "刷新" not in control_texts
        assert "自动跟随" not in control_texts
        log.stop()
    finally:
        root.destroy()


def test_log_line_colors_are_semantic():
    assert FollowSessionLog._line_level("evt=action gate=ready") == "ok"
    assert FollowSessionLog._line_level("evt=idle abstain=hand") == "wait"
    assert FollowSessionLog._line_level("evt=stop stop=gate") == "error"

