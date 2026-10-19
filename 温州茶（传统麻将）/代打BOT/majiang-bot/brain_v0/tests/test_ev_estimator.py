# -*- coding: utf-8 -*-
"""真·1 步 EV 估算器测试（P1-1）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.danger import EvEstimator, DangerModel
from engine.opponent_model import OpponentModel


HAND = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41]


def test_ev_of_discard_structure_and_sorting():
    est = EvEstimator(danger_model=DangerModel())
    res = est.ev_of_discard(HAND, {}, turn_no=4)
    # 14 张里 41 重复 → 13 个唯一候选
    assert len(res) == 13
    keys = {"tile", "shanten_after", "ukeire_count", "danger", "ev"}
    assert all(keys.issubset(r.keys()) for r in res)
    # 按 EV 降序
    evs = [r["ev"] for r in res]
    assert evs == sorted(evs, reverse=True)


def test_fully_seen_tile_has_zero_danger():
    est = EvEstimator(danger_model=DangerModel())
    played = {1: 4}  # 1万已全见 → 打出无点炮风险
    res = est.ev_of_discard(HAND, played, turn_no=10)
    by_tile = {r["tile"]: r for r in res}
    # 1万 danger 应为 0（剩余 0 张，对手不可能持有）
    assert by_tile[1]["danger"] == 0.0


def test_dealer_tile_more_dangerous():
    # 庄家加权：同一张牌打给庄家危险度更高 → 期望损失更大。
    # 用非对称 seat_unseen（庄家座位未露牌最多），否则对称分布下庄家惩罚
    # 与庄家加成相互抵消，总期望损失与庄家座位无关（看似"未生效"）。
    est = EvEstimator(danger_model=DangerModel())
    played = {9: 0}  # 9万完全未见、孤立
    unseen = {"top": 30, "left": 3, "right": 3}
    res_top = est.ev_of_discard(HAND, played, turn_no=14, seat_unseen=unseen,
                                dealer_seat="top")
    res_left = est.ev_of_discard(HAND, played, turn_no=14, seat_unseen=unseen,
                                 dealer_seat="left")
    by_top = {r["tile"]: r for r in res_top}
    by_left = {r["tile"]: r for r in res_left}
    # 庄家在 top(高未露牌)时打 9万 的期望损失 应高于 庄家在 left 时。
    assert by_top[9]["danger"] > by_left[9]["danger"]


def test_dealer_seat_int_and_string_equivalent():
    est = EvEstimator(danger_model=DangerModel())
    played = {9: 0}
    unseen = {"top": 30, "left": 3, "right": 3}
    res_str = est.ev_of_discard(HAND, played, turn_no=14, seat_unseen=unseen,
                                dealer_seat="top")
    res_int = est.ev_of_discard(HAND, played, turn_no=14, seat_unseen=unseen,
                                dealer_seat=1)
    by_str = {r["tile"]: r for r in res_str}
    by_int = {r["tile"]: r for r in res_int}
    assert by_str[9]["danger"] == by_int[9]["danger"]


def test_ev_advise_in_advisor():
    from engine.advisor import Advisor
    adv = Advisor()
    out = adv.advise(HAND, {}, turn_no=4)
    assert "ev_options" in out
    assert len(out["ev_options"]) == 13
    evs = [o["ev"] for o in out["ev_options"]]
    assert evs == sorted(evs, reverse=True)
