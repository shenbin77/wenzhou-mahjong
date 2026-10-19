# -*- coding: utf-8 -*-
"""平台派牌时序结构检验测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dealing_structure_check import analyze, _autocorr, _run_lengths, _binary_outcomes


def test_insufficient_data():
    recs = [{"winner": 0}] * 10
    rep = analyze(recs, min_rounds=100)
    assert rep["verdict"] == "insufficient_data"


def test_iid_like_no_signs():
    import random
    random.seed(1)
    seq_win = [1 if random.random() < 0.25 else 0 for _ in range(300)]
    recs = [{"winner": 0 if w == 0 else 1} for w in seq_win]
    rep = analyze(recs, min_rounds=100)
    assert rep["verdict"] == "analyzed"
    assert abs(rep["autocorr_lag1"]) < 0.2


def test_compensation_detected():
    # 0,1,0,1,... 强补偿:输后必赢
    seq_win = [1 if i % 2 == 1 else 0 for i in range(300)]
    recs = [{"winner": 0 if w == 0 else 1} for w in seq_win]
    rep = analyze(recs, min_rounds=100)
    assert rep["verdict"] == "analyzed"
    assert rep["p_win_after_loss"] is not None and rep["p_win_after_loss"] > 0.9
    assert any("补偿" in s for s in rep["non_stationary_signs"])


def test_run_lengths_basic():
    assert _run_lengths([1, 1, 0, 0, 0, 1]) == [2, 3, 1]
    assert _autocorr([1, 0, 1, 0, 1, 0], 1) < -0.5  # 强负自相关
