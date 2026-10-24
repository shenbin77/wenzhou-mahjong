# -*- coding: utf-8 -*-
"""Unit tests for read-only follow HUD formatters."""

from __future__ import annotations

from action_policy import CLAIM_TAP_CHANNEL_READY
from assist_hud import (
    UNKNOWN,
    build_follow_hud,
    format_countdown,
    format_river_summary,
    format_turn_action,
    format_wall_cards,
)


def test_claim_stays_false():
    assert CLAIM_TAP_CHANNEL_READY is False


def test_format_wall_cards_unknown_and_value():
    assert format_wall_cards(None) == f"余牌：{UNKNOWN}"
    assert format_wall_cards("x") == f"余牌：{UNKNOWN}"
    assert format_wall_cards(84) == "余牌：84"
    assert format_wall_cards(0) == "余牌：0"


def test_format_countdown_fail_closed():
    assert format_countdown("absent") == f"倒计时：{UNKNOWN}"
    assert format_countdown("unreadable") == f"倒计时：{UNKNOWN}"
    assert format_countdown(None) == f"倒计时：{UNKNOWN}"
    assert format_countdown("present", 9) == "倒计时：9秒"
    assert format_countdown("low", 1) == "倒计时：1秒"
    assert format_countdown("expired", 0) == "倒计时：0秒"
    assert format_countdown("present", None) == f"倒计时：{UNKNOWN}"


def test_format_turn_never_implies_opponent():
    assert format_turn_action(status="play", actionable=True) == "当前行动：我方可出"
    assert format_turn_action(status="wait", actionable=False) == "当前行动：等待"
    assert format_turn_action(status="abstain", actionable=False) == f"当前行动：{UNKNOWN}"
    assert format_turn_action(status="play", actionable=False) == f"当前行动：{UNKNOWN}"
    text = format_turn_action(status="wait", actionable=False)
    assert "对方" not in text
    assert "不是出牌" not in text


def test_format_river_summary_from_result_only():
    assert format_river_summary({}) == f"牌河：{UNKNOWN}"
    assert "对家刚出" in format_river_summary(
        {"incoming_source": "opponent", "incoming_tile": 12}
    )
    assert UNKNOWN in format_river_summary({"incoming_source": "self"})


def test_build_follow_hud_without_image_is_fail_closed():
    hud = build_follow_hud(
        {"status": "abstain"},
        status="abstain",
        actionable=False,
        run_countdown=True,
    )
    assert hud.wall == f"余牌：{UNKNOWN}"
    assert hud.countdown == f"倒计时：{UNKNOWN}"
    assert hud.turn == f"当前行动：{UNKNOWN}"
    assert hud.river == f"牌河：{UNKNOWN}"


def test_build_follow_hud_uses_result_wall_and_play():
    hud = build_follow_hud(
        {
            "status": "play",
            "wall_cards": 72,
            "countdown_status": "present",
            "countdown_seconds": 5,
            "incoming_source": "opponent",
            "incoming_tile": 3,
            "opponent_river": [1, 2, 3],
            "own_river": [4],
        },
        status="play",
        actionable=True,
        run_countdown=False,
    )
    assert hud.wall == "余牌：72"
    assert hud.countdown == "倒计时：5秒"
    assert hud.turn == "当前行动：我方可出"
    assert "牌河：" in hud.river


def test_format_river_summary_empty_lists_are_unknown():
    """Empty rivers must render 未识别, never 河0/0."""
    assert "0" not in format_river_summary({"own_river": [], "opponent_river": []})
    assert UNKNOWN in format_river_summary({"own_river": [], "opponent_river": []})
    text = format_river_summary({"own_river": [], "opponent_river": [], "discards": {"own_river": [], "opponent_river": []}})
    assert "河0" not in text
    assert "0/0" not in text
    assert UNKNOWN in text
