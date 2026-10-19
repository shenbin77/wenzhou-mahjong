# -*- coding: utf-8 -*-
"""engine/opponent_model.py 测试：层A座位分布 + 庄家加权 + 听牌范围。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.opponent_model import OpponentModel  # noqa: E402


def test_remaining_per_tile():
    m = OpponentModel()
    assert m.remaining_per_tile({1: 0}, 1) == 4
    assert m.remaining_per_tile({1: 2}, 1) == 2
    assert m.remaining_per_tile({1: 4}, 1) == 0


def test_seat_distribution_uniform_when_no_unseen():
    m = OpponentModel()
    dist = m.seat_distribution(1, {1: 0})
    assert dist == {"top": pytest.approx(1 / 3), "left": pytest.approx(1 / 3), "right": pytest.approx(1 / 3)}


def test_seat_distribution_weighted_by_unseen():
    m = OpponentModel()
    dist = m.seat_distribution(1, {1: 0}, seat_unseen={"top": 10, "left": 5, "right": 5})
    assert dist["top"] == pytest.approx(0.5)
    assert dist["left"] == pytest.approx(0.25)
    assert dist["right"] == pytest.approx(0.25)


def test_dealer_gets_higher_danger():
    m = OpponentModel(dealer_seat="top", dealer_bonus=0.25)
    played = {1: 0}  # 万1 一张未出 → 剩余4，危险度高
    unseen = {"top": 10, "left": 10, "right": 10}
    d_top = m.danger_to_seat(1, played, 1, "top", unseen)
    d_left = m.danger_to_seat(1, played, 1, "left", unseen)
    assert d_top > d_left  # 庄家加权后更高


def test_listen_candidates_excludes_seen_and_discarded():
    m = OpponentModel()
    played = {1: 4}        # 万1 已见完
    discards = [5]         # 该座位打过万5
    cands = m.listen_candidates(discards, played)
    assert 1 not in cands           # 已见完
    assert 5 not in cands           # 已打出
    assert len(cands) > 0
