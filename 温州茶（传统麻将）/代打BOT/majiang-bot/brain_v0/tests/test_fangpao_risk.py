# -*- coding: utf-8 -*-
"""放炮风险 API 单测：已出4→0、中张>边张、后期抬升、庄家加权、红中财神。"""
from __future__ import annotations

import pytest

from engine.fangpao_risk import fangpao_risk, rank_discards_by_fangpao_risk
from engine.tiles import CAISHEN_ID


def test_all_four_played_risk_zero():
    r = fangpao_risk(25, {25: 4}, turn_no=10)
    assert r["fangpao_risk"] == 0
    assert r["level"] == "LOW"
    assert r["components"]["played"] == 4


def test_mid_tile_unseen_riskier_than_edge():
    # 5筒(25) 中张 vs 1筒(21)/9筒(29) 边张；同未出、同巡
    mid = fangpao_risk(25, {}, turn_no=1)
    edge1 = fangpao_risk(21, {}, turn_no=1)
    edge9 = fangpao_risk(29, {}, turn_no=1)
    assert mid["fangpao_risk"] > edge1["fangpao_risk"]
    assert mid["fangpao_risk"] > edge9["fangpao_risk"]


def test_late_game_raises_risk():
    early = fangpao_risk(25, {25: 0}, turn_no=1)
    late = fangpao_risk(25, {25: 0}, turn_no=18)
    assert late["fangpao_risk"] > early["fangpao_risk"]


def test_dealer_seat_weighting():
    # 庄家 top：dealer_weighted_max 应体现庄家座位更高危险
    no_dealer = fangpao_risk(5, {5: 0}, turn_no=8, dealer_seat=None)
    with_dealer = fangpao_risk(5, {5: 0}, turn_no=8, dealer_seat="top")
    assert with_dealer["components"]["by_seat"]["top"] > no_dealer["components"]["by_seat"]["top"]
    assert with_dealer["fangpao_risk"] >= no_dealer["fangpao_risk"]


def test_hongzhong_joker_definition():
    # 已出4 → 0
    assert fangpao_risk(CAISHEN_ID, {CAISHEN_ID: 4}, turn_no=5)["fangpao_risk"] == 0
    r = fangpao_risk(CAISHEN_ID, {CAISHEN_ID: 0}, turn_no=5)
    assert r["components"]["is_joker"] is True
    assert r["components"]["joker_note"]
    assert 0 <= r["fangpao_risk"] <= 100
    # 红中不享受中张 mid boost 路径下仍有基底危险
    assert r["fangpao_risk"] > 0


def test_rank_discards_ascending_safe_first():
    hand = [21, 25, 29, 5]  # 边、中、边、中万
    rows = rank_discards_by_fangpao_risk(hand, {25: 3, 21: 0}, turn_no=10)
    assert len(rows) == 4
    risks = [r["fangpao_risk"] for r in rows]
    assert risks == sorted(risks)
    # 已出3的中张应相对更安全（靠近队首）
    by_tile = {r["tile"]: r["fangpao_risk"] for r in rows}
    assert by_tile[25] < by_tile[21]


def test_structured_output_keys():
    r = fangpao_risk(8, {}, 1)
    assert set(r) >= {"tile", "fangpao_risk", "level", "components"}
    assert "base_danger" in r["components"]
