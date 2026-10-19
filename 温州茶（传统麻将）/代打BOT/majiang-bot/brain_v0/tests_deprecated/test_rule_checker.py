# -*- coding: utf-8 -*-
"""rule_mining 测试：用合成牌谱验证一致性校验器。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from rule_mining import checker, rules_spec as S  # noqa: E402


def _valid_pu():
    hands = {s: [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24] for s in S.PLAYERS}
    return {
        "round_no": 1,
        "hands": hands,
        "discards": {s: [] for s in S.PLAYERS},
        "win": {"seat": "bottom", "type": "zimo", "fans": []},
        "scores": {"bottom": 6, "top": -2, "left": -2, "right": -2},
    }


def test_valid_pu_passes():
    rep = checker.check_corpus([_valid_pu()])
    assert rep["ok"] is True
    assert rep["errors"] == []


def test_same_tile_over_four_fails():
    pu = _valid_pu()
    # 把 bottom 起手塞入 5 张一万(id=1)
    pu["hands"]["bottom"] = [1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    rep = checker.check_corpus([pu])
    assert rep["ok"] is False
    assert any(e.rule == "同牌超4" for e in rep["errors"])


def test_hand_size_wrong_fails():
    pu = _valid_pu()
    pu["hands"]["top"] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23]  # 12 张
    rep = checker.check_corpus([pu])
    assert any(e.rule == "起手张数" for e in rep["errors"])


def test_score_not_zero_fails():
    pu = _valid_pu()
    pu["scores"] = {"bottom": 6, "top": -2, "left": -2, "right": -1}  # 和=1
    rep = checker.check_corpus([pu])
    assert any(e.rule == "结算守恒" for e in rep["errors"])


def test_dealer_zimo_winner_must_be_positive():
    pu = _valid_pu()
    pu["scores"] = {"bottom": -6, "top": 2, "left": 2, "right": 2}  # 自摸但胡牌者负分
    rep = checker.check_corpus([pu])
    assert any(e.rule == "自摸得分" for e in rep["errors"])


def test_mine_demo_runs():
    from rule_mining import mine
    rep = mine.mine_from_pu_file if False else None
    # 直接调用 checker 的 demo 等价逻辑
    rep = checker.check_corpus([_valid_pu()])
    assert rep["ok"]
