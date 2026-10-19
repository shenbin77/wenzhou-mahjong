# -*- coding: utf-8 -*-
"""规则引擎 ↔ hand-record/v0 对账：假结算用例贴近 schema。

输入结构使用 seats 枚举 bottom|right|top|left、tile_id int TARGET_IDS、
joker=55；seat 名映射到 scoring 的 0..3 整型座位（bottom=0,right=1,top=2,left=3）。
"""
from __future__ import annotations

from engine.wenzhou_rules import (
    JOKER_ID,
    fans_for_win,
    hand_is_win,
    score_dianpao,
    score_zimo,
)

# hand-record/v0 seat enum → scoring seat index
SEAT_INDEX = {"bottom": 0, "right": 1, "top": 2, "left": 3}
INDEX_SEAT = {v: k for k, v in SEAT_INDEX.items()}


def _settle_from_hand_record(case: dict) -> dict[str, int]:
    """Minimal adapter: hand_record-style settle case → named seat deltas."""
    win_type = case["win_type"]  # fangpao | zimo
    winner = SEAT_INDEX[case["winner_seat"]]
    dealer = SEAT_INDEX[case["dealer_seat"]]
    fan_types = list(case.get("fan_types") or [])
    if win_type == "fangpao":
        loser = SEAT_INDEX[case["loser_seat"]]
        deltas = score_dianpao(
            winner_seat=winner,
            dealer_seat=dealer,
            loser_seat=loser,
            fan_types=fan_types,
        )
    elif win_type == "zimo":
        deltas = score_zimo(
            winner_seat=winner,
            dealer_seat=dealer,
            fan_types=fan_types,
        )
    else:
        raise ValueError(win_type)
    return {INDEX_SEAT[i]: int(deltas[i]) for i in range(4)}


class TestHandRecordSettleAdapter:
    def test_dianpao_basic(self):
        # 闲家 bottom 胡，left 点炮，庄=top
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "fangpao",
            "winner_seat": "bottom",
            "loser_seat": "left",
            "dealer_seat": "top",
            "fan_types": [],
            "hand": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24],
            "win_tile": 24,
            "joker": 55,
            "expected_deltas": {"bottom": 1, "right": 0, "top": 0, "left": -1},
        }
        assert hand_is_win(case["hand"] + [case["win_tile"]])
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]
        assert sum(got.values()) == 0

    def test_zimo_xian(self):
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "zimo",
            "winner_seat": "right",
            "dealer_seat": "bottom",
            "fan_types": [],
            "hand": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24],
            "win_tile": 24,
            "joker": 55,
            "expected_deltas": {"bottom": -2, "right": 6, "top": -2, "left": -2},
        }
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]
        assert sum(got.values()) == 0

    def test_zhuang_dianpao_double(self):
        # 庄 bottom 胡，right 点炮 → 单位 2
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "fangpao",
            "winner_seat": "bottom",
            "loser_seat": "right",
            "dealer_seat": "bottom",
            "fan_types": [],
            "expected_deltas": {"bottom": 2, "right": -2, "top": 0, "left": 0},
        }
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]

    def test_zhuang_zimo(self):
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "zimo",
            "winner_seat": "bottom",
            "dealer_seat": "bottom",
            "fan_types": [],
            "expected_deltas": {"bottom": 9, "right": -3, "top": -3, "left": -3},
        }
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]

    def test_hongzhong_laizi_hu(self):
        # 红中赖子补对：标准型胡 + 点炮对账
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, JOKER_ID]
        win_tile = 24
        assert hand_is_win(hand13 + [win_tile])
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "fangpao",
            "winner_seat": "left",
            "loser_seat": "top",
            "dealer_seat": "right",
            "fan_types": [],
            "hand": hand13,
            "win_tile": win_tile,
            "joker": 55,
            "seats": [
                {"seat": "bottom", "tiles": [], "unknown_count": 13},
                {"seat": "right", "tiles": [], "unknown_count": 13},
                {"seat": "top", "tiles": [], "unknown_count": 13},
                {"seat": "left", "tiles": hand13, "unknown_count": 0},
            ],
            "expected_deltas": {"bottom": 0, "right": 0, "top": -1, "left": 1},
        }
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]

    def test_qidui_dianpao(self):
        hand13 = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, JOKER_ID]
        win_tile = JOKER_ID
        assert hand_is_win(hand13 + [win_tile])
        fans = fans_for_win(hand13, win_tile)
        assert "qidui" in fans
        case = {
            "schema": "hand-record/v0-settle-fixture",
            "win_type": "fangpao",
            "winner_seat": "bottom",
            "loser_seat": "right",
            "dealer_seat": "top",
            "fan_types": ["qidui"],
            "hand": hand13,
            "win_tile": win_tile,
            "joker": 55,
            "expected_deltas": {"bottom": 2, "right": -2, "top": 0, "left": 0},
        }
        got = _settle_from_hand_record(case)
        assert got == case["expected_deltas"]
        assert sum(got.values()) == 0
