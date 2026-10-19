# -*- coding: utf-8 -*-
"""建议#4：APP 发牌公平性校验工具冒烟测试。

只验证「工具能跑、能正确返回 insufficient_data（样本不足）」，
不对发牌机制本身下结论——那需要 ≥100 局标注数据（由回放/账号拉取补齐）。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine import storage, game_log  # noqa: E402


def test_rounds_to_records_maps_winner_seat():
    from tools.fairness_check import rounds_to_records, SEAT_IDX
    rows = [
        {"winner_seat": "bottom"},
        {"winner_seat": "top"},
        {"winner_seat": None},
    ]
    recs = rounds_to_records(rows)
    assert recs[0]["winner"] == SEAT_IDX["bottom"]
    assert recs[1]["winner"] == SEAT_IDX["top"]
    assert recs[2]["winner"] is None


def test_fairness_report_insufficient_on_small_sample():
    # 14 局 < 默认 100 -> insufficient_data（与 data/mahjong.db 现状一致）
    rounds = storage.get_recent_rounds(limit=100000)
    recs = [{"winner": 0 if r.get("winner_seat") == "bottom" else None,
             "deltas": {}} for r in rounds]
    rep = game_log.rng_fairness_report(recs, min_rounds=100)
    assert rep["verdict"] == "insufficient_data"
    assert rep["rounds"] == len(rounds)


def test_fairness_check_main_runs():
    from tools.fairness_check import main
    rep = main([])  # 用默认库（data/mahjong.db），当前 14 局 -> insufficient_data
    assert "verdict" in rep
