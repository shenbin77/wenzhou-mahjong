# -*- coding: utf-8 -*-
"""Unified follow pipeline: TableState + SessionLogger + decision-fact abstain."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_policy import CLAIM_TAP_CHANNEL_READY  # noqa: E402
from follow_pipeline import (  # noqa: E402
    DECISION_FACTS,
    DISPLAY_FACTS,
    FollowPipeline,
    build_frame_facts,
    format_frame_facts_display,
    missing_decision_facts,
)
from table_state import build_table_state  # noqa: E402


def test_claim_tap_stays_false():
    assert CLAIM_TAP_CHANNEL_READY is True


def test_decision_and_display_fact_names():
    assert "mode" in DECISION_FACTS
    assert "caishen" in DECISION_FACTS
    assert "concealed" in DECISION_FACTS
    for name in (
        "mode", "room_round", "caishen", "concealed", "melds",
        "river", "last_discard", "turn", "countdown", "wall_cards",
    ):
        assert name in DISPLAY_FACTS


def _advise(**overrides):
    base = {
        "status": "advise",
        "mode": "tea_2p",
        "caishen": "w1",
        "hand_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8],
        "melds": [],
        "meld_count": 0,
        "suggestion": "4筒",
        "suggestion_id": 4,
        "suggested_boxes": [[90, 780, 170, 888]],
        "wall_cards": 40,
        "incoming_tile": 9,
        "incoming_source": "opponent",
        "own_river": [1, 2],
        "opponent_river": [3],
    }
    base.update(overrides)
    return base


def test_missing_decision_facts_fail_closed():
    assert missing_decision_facts({}) == ["mode", "caishen", "concealed"]
    assert "caishen" in missing_decision_facts({"mode": "tea_2p", "hand_ids": [1]})
    assert missing_decision_facts(_advise()) == []


def test_observe_writes_follow_session_and_abstains(tmp_path):
    pipe = FollowPipeline(tmp_path / "sess", mode="tea_2p")
    path = pipe.start({"account": "test"})
    assert path is not None and path.is_file()

    blank = np.zeros((900, 1600, 3), dtype=np.uint8)
    outcome = pipe.observe(
        {"status": "advise", "mode": "tea_2p", "suggestion_id": 4,
         "suggestion": "x", "hand_ids": [], "caishen": None},
        blank,
        room_id="869684",
        round_no=3,
        turn_actionable=True,
    )
    assert outcome.abstain is True
    assert outcome.event["status"] == "abstain"
    assert outcome.event["suggestion_id"] is None
    assert outcome.event["claim_tap_channel_ready"] is True
    assert "table_state" in outcome.event
    assert "frame_facts" in outcome.event
    for key in DISPLAY_FACTS:
        assert key in outcome.display

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["kind"] == "session_header"
    assert rows[0]["claim_tap_channel_ready"] is True
    assert rows[-1]["decision"]["claim_tap_channel_ready"] is True
    assert rows[-1]["vision"]["status"] == "abstain"


def test_observe_advise_when_facts_complete(tmp_path):
    pipe = FollowPipeline(tmp_path / "sess2", mode="tea_2p")
    pipe.start()
    outcome = pipe.observe(
        _advise(),
        np.zeros((900, 1600, 3), dtype=np.uint8),
        room_id="R1",
        round_no=1,
        turn_status="advise",
        turn_actionable=True,
    )
    assert outcome.abstain is False
    assert outcome.event["event"] == "recommend"
    assert outcome.event["suggestion_id"] == 4
    assert "玩法" in outcome.display["mode"]
    assert "财神" in outcome.display["caishen"]
    assert "暗手" in outcome.display["concealed"]
    assert "副露" in outcome.display["melds"]
    assert "牌河" in outcome.display["river"]
    assert "最近出牌" in outcome.display["last_discard"]
    assert "轮到" in outcome.display["turn"]
    assert "倒计时" in outcome.display["countdown"]
    assert "余牌" in outcome.display["wall_cards"]
    assert "房号" in outcome.display["room_round"]


def test_format_display_never_invents_values():
    table = build_table_state({"status": "abstain"})
    facts = build_frame_facts({}, table)
    display = format_frame_facts_display(facts)
    for value in display.values():
        assert "未识别" in value or value  # every label present
    assert display["caishen"].endswith("未识别")
    assert "未识别" in display["wall_cards"]


def test_window_start_attaches_pipeline(tmp_path, monkeypatch):
    """开始跟随 wires FollowPipeline onto TeaAssistWindow without Tk mainloop."""
    import assist_window

    class _Var:
        def __init__(self, value=""):
            self._v = value
        def get(self):
            return self._v
        def set(self, value):
            self._v = value
        def strip(self):
            return str(self._v).strip()

    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.session_dir = tmp_path / "live"
    window.session_dir.mkdir()
    window.account = _Var("tester")
    window.room_id = _Var("123")
    window.serial = _Var("emu")
    window.round_no = 2
    window._mode_key = lambda: "tea_2p"
    window.continuous = False
    window._follow_pipeline = None
    window.embedded_follow_log = None
    window.follow_session_log = None
    window.hud_mode = _Var()
    window.hud_room_round = _Var()
    window.hud_caishen = _Var()
    window.hud_concealed = _Var()
    window.hud_melds = _Var()
    window.hud_wall = _Var()
    window.hud_countdown = _Var()
    window.hud_turn = _Var()
    window.hud_river = _Var()
    window.hud_last_discard = _Var()

    assist_window.TeaAssistWindow._attach_follow_pipeline(window)
    assert window._follow_pipeline is not None
    assert window._follow_pipeline.follow_log_path.is_file()
    assert CLAIM_TAP_CHANNEL_READY is True

    class _State:
        status = "advise"
        actionable = True

    result = _advise()
    display = assist_window.TeaAssistWindow._observe_follow_frame(
        window, result, _State())
    assert isinstance(display, dict)
    assert result.get("table_state")
    assert result.get("frame_facts")
    rows = window._follow_pipeline.logger.read_all()
    assert any(r.get("event") == "recommend" for r in rows if r.get("kind") != "session_header")

    # Missing facts → abstain on the same path
    weak = {"status": "advise", "mode": "tea_2p", "suggestion_id": 1, "hand_ids": []}
    assist_window.TeaAssistWindow._observe_follow_frame(window, weak, _State())
    assert weak["status"] == "abstain"
    assert weak.get("suggestion_id") is None

def test_garbage_caishen_label_is_unknown():
    """Codex must-fix: only valid caishen_id counts as known."""
    table = build_table_state({
        "mode": "tea_2p",
        "status": "advise",
        "caishen": "garbage_label",
        "hand_ids": list(range(1, 18)),
    })
    assert table.caishen_id is None
    assert table.caishen_known is False
    assert "caishen" in missing_decision_facts(
        {"mode": "tea_2p", "caishen": "garbage_label", "hand_ids": [1]},
        table,
    )
    # Fallback path without table must also reject garbage labels.
    assert "caishen" in missing_decision_facts(
        {"mode": "tea_2p", "caishen": "garbage_label", "hand_ids": [1]},
        None,
    )


def test_extra_event_cannot_overwrite_safety_fields(tmp_path):
    """Codex must-fix: extra_event must not clobber status/suggestion/safety."""
    pipe = FollowPipeline(tmp_path / "sess_extra", mode="tea_2p")
    pipe.start()
    outcome = pipe.observe(
        _advise(),
        np.zeros((900, 1600, 3), dtype=np.uint8),
        room_id="R1",
        round_no=1,
        turn_status="advise",
        turn_actionable=True,
        extra_event={
            "status": "advise",
            "suggestion": "HACKED",
            "suggestion_id": 999,
            "safety": "opened",
            "claim_tap_channel_ready": True,
            "pipeline": "evil",
            "note_from_caller": "ok-extra",
        },
    )
    assert outcome.event["suggestion"] != "HACKED"
    assert outcome.event["suggestion_id"] == 4
    assert outcome.event["safety"] != "opened"
    assert outcome.event["claim_tap_channel_ready"] is True
    assert outcome.event["pipeline"] == "follow_pipeline/v1"
    assert outcome.event["note_from_caller"] == "ok-extra"


def test_show_missing_facts_hides_discard_suggestion(tmp_path):
    """Codex must-fix: gates run before refresh; missing facts => no '打 X'."""
    import types

    import assist_window
    from live_state import LiveRefreshState

    class _Var:
        def __init__(self, value=""):
            self._v = value

        def get(self):
            return self._v

        def set(self, value):
            self._v = value

        def strip(self):
            return str(self._v).strip()

    class _Choice:
        def __init__(self, var):
            self._var = var
            self._values = ()

        def get(self):
            return self._var.get()

        def __setitem__(self, key, value):
            if key == "values":
                self._values = value

    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.session_dir = tmp_path / "show_gate"
    window.session_dir.mkdir()
    window.account = _Var("tester")
    window.room_id = _Var("123")
    window.serial = _Var("emu")
    window.round_no = 1
    window._mode_key = lambda: "tea_2p"
    window.continuous = True
    window._follow_pipeline = None
    window.embedded_follow_log = None
    window.follow_session_log = None
    window.refresh = LiveRefreshState()
    window.display_source = "live"
    window._result_source = staticmethod(assist_window.frame_result_source)
    window.hand = _Var()
    window.suggestion = _Var("旧建议 打 4筒")
    window.reason = _Var("旧理由")
    window.caishen_title = _Var()
    window.actual_tile = _Var("x")
    window.actual_choice = _Choice(window.actual_tile)
    window.hud_mode = _Var()
    window.hud_room_round = _Var()
    window.hud_caishen = _Var()
    window.hud_concealed = _Var()
    window.hud_melds = _Var()
    window.hud_wall = _Var()
    window.hud_countdown = _Var()
    window.hud_turn = _Var()
    window.hud_river = _Var()
    window.hud_last_discard = _Var()
    window._render_live_frame = lambda *_a, **_k: None
    window._set_offline_notice = lambda: None
    window._update_latency_bar = lambda: None
    window._refresh_view = lambda *_a, **_k: None
    window._caishen_title = lambda state: "财神：识别中"
    window._advice_facts = lambda state: ""
    window._attach_follow_pipeline = (
        assist_window.TeaAssistWindow._attach_follow_pipeline.__get__(window)
    )
    window._observe_follow_frame = (
        assist_window.TeaAssistWindow._observe_follow_frame.__get__(window)
    )
    window._update_follow_hud = (
        assist_window.TeaAssistWindow._update_follow_hud.__get__(window)
    )

    weak = {
        "status": "advise",
        "mode": "tea_2p",
        "suggestion": "4筒",
        "suggestion_id": 4,
        "suggested_boxes": [[90, 780, 170, 888]],
        "hand_ids": [],  # missing concealed
        "caishen": None,  # missing caishen
        "audit_event": {"source": "live", "frame": str(tmp_path / "nope.png"),
                        "flags": [], "mode": "tea_2p", "at": "", "diagnostics": []},
    }
    assist_window.TeaAssistWindow._show(window, weak, 1.0)
    shown = window.suggestion.get()
    assert not str(shown).startswith("打 "), f"main UI must not show discard, got {shown!r}"
    assert weak.get("status") == "abstain"
    assert weak.get("suggestion_id") is None


def test_update_follow_hud_fail_closed_on_pipeline_error(tmp_path):
    """Codex must-fix: pipeline exceptions clear suggestion and show reason."""
    import assist_window

    class _Var:
        def __init__(self, value=""):
            self._v = value

        def get(self):
            return self._v

        def set(self, value):
            self._v = value

    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window.continuous = True
    window.suggestion = _Var("打 4筒")
    window.reason = _Var("旧理由")
    window.hud_mode = _Var()
    window.hud_room_round = _Var()
    window.hud_caishen = _Var()
    window.hud_concealed = _Var()
    window.hud_melds = _Var()
    window.hud_wall = _Var()
    window.hud_countdown = _Var()
    window.hud_turn = _Var()
    window.hud_river = _Var()
    window.hud_last_discard = _Var()
    window.embedded_follow_log = None
    window._follow_pipeline = None

    def boom(_result, _state):
        raise RuntimeError("synthetic pipeline boom")

    window._observe_follow_frame = boom

    class _State:
        status = "advise"
        actionable = True

    result = _advise()
    assist_window.TeaAssistWindow._update_follow_hud(window, result, _State())
    assert window.suggestion.get() == "识别中"
    assert "synthetic pipeline boom" in window.reason.get()
    assert result.get("suggestion") is None
    assert result.get("status") == "abstain"
    assert "管线异常" in window.hud_turn.get()

