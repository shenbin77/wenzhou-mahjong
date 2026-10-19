# -*- coding: utf-8 -*-
"""engine/play_style.py 测试：用合成结局数据验证「少点炮多自摸」可计算。"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine import storage, play_style  # noqa: E402


def _tmp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "mahjong.db")


def _add_round(db, win_type, is_dealer=0):
    # 结局字段通过 outcome 参数落库（与 tracker.set_outcome 一致的语义）
    storage.save_round(
        {"result": win_type, "rounds_played": 1, "note": "t"},
        [],
        db_path=db,
        outcome={"is_zimo": 1 if win_type == "zimo" else 0,
                 "win_type": win_type, "is_dealer": is_dealer,
                 "winner_seat": "bottom", "losing_seat": None},
    )


def test_empty_db_reports_insufficient():
    db = _tmp_db()
    res = play_style.analyze_play_style(db)
    assert res["total"] == 0
    assert "样本" in res["verdict"]


def test_zimo_heavy_profile_detected():
    db = _tmp_db()
    for _ in range(6):
        _add_round(db, "zimo")
    for _ in range(3):
        _add_round(db, "point")
    for _ in range(1):
        _add_round(db, "lose")
    res = play_style.analyze_play_style(db)
    assert res["total"] == 10
    assert res["zimo"] == 6
    assert res["zimo_rate"] == pytest.approx(round(6 / 9, 3), abs=1e-6)
    assert res["deal_rate"] == pytest.approx(0.1, abs=1e-6)
    assert "少点炮" in res["verdict"]


def test_deal_heavy_profile_detected():
    db = _tmp_db()
    for _ in range(8):
        _add_round(db, "lose")
    res = play_style.analyze_play_style(db)
    assert res["deal_rate"] > 0.4
    assert "点炮偏多" in res["verdict"]


def test_classify_falls_back_to_is_win():
    db = _tmp_db()
    # win_type 缺失，仅靠 is_win/is_zimo 启发式
    storage.save_round({"is_win": 1, "rounds_played": 1},
                       [], db_path=db,
                       outcome={"is_zimo": 0})
    res = play_style.analyze_play_style(db)
    assert res["point"] == 1


def test_win_rate_convenience_returns_headline_metric():
    """win_rate() 只返回胜率这一件事（用户只要胜率）。"""
    db = _tmp_db()
    for _ in range(7):
        _add_round(db, "zimo")
    for _ in range(3):
        _add_round(db, "point")
    for _ in range(2):
        _add_round(db, "lose")
    wr = play_style.win_rate(db)
    assert wr["total"] == 12
    assert wr["win_count"] == 10
    assert wr["win_rate"] == pytest.approx(round(10 / 12, 3), abs=1e-6)
    assert wr["insufficient"] is False

    # 空库 → 样本不足
    empty = play_style.win_rate(_tmp_db())
    assert empty["insufficient"] is True
    assert empty["win_rate"] == 0.0
