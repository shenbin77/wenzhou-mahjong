# -*- coding: utf-8 -*-
"""deltas 落库 + 净 EV 统计测试（P0-1）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import storage


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def test_save_round_stores_deltas():
    db = _tmp_db()
    try:
        storage.save_round({"rounds_played": 1}, [], db_path=db,
                           outcome={"win_type": "zimo", "winner_seat": "bottom"},
                           deltas={"bottom": 3.0, "top": -1.0, "left": -1.0, "right": -1.0})
        rows = storage.get_recent_rounds(limit=10, db_path=db)
        assert len(rows) == 1
        assert rows[0]["deltas"] == {"bottom": 3.0, "top": -1.0, "left": -1.0, "right": -1.0}
    finally:
        os.remove(db)


def test_save_round_autoderives_deltas_from_win_type():
    db = _tmp_db()
    try:
        # 不传 deltas → 由 win_type 推导代理
        storage.save_round({"rounds_played": 1}, [], db_path=db,
                           outcome={"win_type": "lose", "winner_seat": None,
                                    "losing_seat": "bottom"})
        rows = storage.get_recent_rounds(limit=10, db_path=db)
        assert rows[0]["deltas"]["bottom"] == -1.0
    finally:
        os.remove(db)


def test_get_net_ev_focus():
    db = _tmp_db()
    try:
        for wt, seat in [("zimo", "bottom"), ("lose", None), ("point", "bottom")]:
            storage.save_round({"rounds_played": 1}, [], db_path=db,
                               outcome={"win_type": wt, "winner_seat": seat})
        ev = storage.get_net_ev(db)
        # zimo+3, lose-1, point+1 => net +3
        assert ev["focus_net"] == 3.0
        assert abs(ev["net_ev_per_round"] - 1.0) < 1e-6
        assert ev["rounds"] == 3
    finally:
        os.remove(db)


def test_outcome_to_deltas_proxy():
    assert storage.outcome_to_deltas("zimo")["bottom"] == 3.0
    assert storage.outcome_to_deltas("point")["bottom"] == 1.0
    assert storage.outcome_to_deltas("lose")["bottom"] == -1.0
    assert storage.outcome_to_deltas("draw")["bottom"] == 0.0
