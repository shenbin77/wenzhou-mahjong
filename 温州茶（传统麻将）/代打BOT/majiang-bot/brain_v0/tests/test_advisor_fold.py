# -*- coding: utf-8 -*-
"""舅舅规则#2(早拆转守)决策测试。

注意: 这些用例直接构造 advise_result(含 shanten/options.ukeire_count),
不依赖引擎算牌, 只测『攻守转换触发逻辑』本身。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.advisor import Advisor


def test_uncle_fold_bad_hand_after_window():
    # 2 轮后(early_fold_turn=3) 仍高向听 + 薄进张 → 拆牌
    adv = {"shanten": 3, "options": [{"ukeire_count": 2}]}
    folds, reason = Advisor().should_fold_uncle(adv, turn_no=4)
    assert folds and reason == "uncle_early_fold"


def test_uncle_no_fold_before_window():
    # 窗口未到(摸牌<2轮) → 不触发早拆
    adv = {"shanten": 3, "options": [{"ukeire_count": 2}]}
    folds, _ = Advisor().should_fold_uncle(adv, turn_no=2)
    assert not folds


def test_uncle_no_fold_when_ting():
    # 听牌(向听=0) 即有胡路(接炮/自摸可能) → 不拆
    adv = {"shanten": 0, "options": []}
    folds, _ = Advisor().should_fold_uncle(adv, turn_no=10)
    assert not folds


def test_uncle_no_fold_good_ukeire():
    # 进张厚(胡路仍在) → 不拆, 继续发展
    adv = {"shanten": 2, "options": [{"ukeire_count": 14}]}
    folds, _ = Advisor().should_fold_uncle(adv, turn_no=4)
    assert not folds


def test_uncle_late_backstop():
    # 早巡 + 有进张信息但胡路差 → 触发早拆
    adv = {"shanten": 3, "options": [{"ukeire_count": 2}]}
    folds, reason = Advisor().should_fold_uncle(adv, turn_no=14)
    assert folds and reason in ("uncle_early_fold", "late_game_backstop")


def test_uncle_late_backstop_no_ukeire():
    # 早巡无进张信息(13 张情境): 不到 late 不触发, 到 late 兜底
    adv = {"shanten": 3, "options": []}
    folds, _ = Advisor().should_fold_uncle(adv, turn_no=4)
    assert not folds                     # 早巡, 无 options → 不触发
    folds, reason = Advisor().should_fold_uncle(adv, turn_no=14)
    assert folds and reason == "late_game_backstop"


def test_default_should_fold_unchanged():
    # 原 late-game 兜底逻辑不被早拆改动影响
    assert Advisor().should_fold({"shanten": 3}, turn_no=14) is True
    assert Advisor().should_fold({"shanten": 3}, turn_no=4) is False
