# -*- coding: utf-8 -*-
"""模拟器与牌谱统计测试。"""

import os
import tempfile

from engine.advisor import local_strategy, random_strategy
from engine.game_log import (
    GameLogger,
    new_round_record,
    rng_fairness_report,
    stats_from_records,
)
from engine.simulator import Simulator


class TestSimulator:
    def test_single_round_deterministic(self):
        s = Simulator(seed=42)
        res = s.play_round(dealer=0, focus_seat=0)
        assert "deltas" in res
        assert sum(res["deltas"].values()) == 0
        assert res["draws"] > 0

    def test_run_stats_sane(self):
        s = Simulator(seed=7)
        stats = s.run(n_rounds=3, focus_strategy=local_strategy)
        assert stats["rounds"] == 3
        assert abs(stats["focus_net"] + stats["opponent_net"]) < 1e-6
        assert 0 <= stats["focus_win_rate"] <= 1
        assert 0 <= stats["focus_fangpao_rate"] <= 1

    def test_seed_reproducibility(self):
        a = Simulator(seed=123).run(n_rounds=2)
        b = Simulator(seed=123).run(n_rounds=2)
        assert a["focus_net"] == b["focus_net"]
        assert a["focus_wins"] == b["focus_wins"]

    def test_local_vs_random_smoke(self):
        s = Simulator(seed=9)
        stats_local = s.run(n_rounds=2, focus_strategy=local_strategy)
        s2 = Simulator(seed=9)
        stats_rand = s2.run(n_rounds=2, focus_strategy=random_strategy)
        assert "focus_avg_score" in stats_local
        assert "focus_avg_score" in stats_rand


class TestGameLogger:
    def test_append_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "log.jsonl")
            g = GameLogger(path)
            rec = new_round_record(
                room="170075", round_no=1, dealer=0, winner=1,
                fangpao_by=2, win_type="fangpao", fans=["qidui"],
                deltas={"0": -4, "1": 4, "2": 0, "3": 0},
            )
            g.append(rec)
            g.append(new_round_record(room="170075", round_no=2))
            rows = g.load_all()
            assert len(rows) == 2
            assert rows[0]["fans"] == ["qidui"]
            assert rows[1]["round_no"] == 2

    def test_empty_log(self):
        with tempfile.TemporaryDirectory() as td:
            g = GameLogger(os.path.join(td, "none.jsonl"))
            assert g.load_all() == []


class TestStats:
    def _records(self):
        mk = lambda **kw: new_round_record(**kw)
        return [
            mk(room="A", winner=0, deltas={"0": 4, "1": -2, "2": -2, "3": 0}),
            mk(room="A", fangpao_by=0, deltas={"0": -4, "1": 4}),
            mk(room="B", winner=0, deltas={"0": 6, "1": -2, "2": -2, "3": -2}),
        ]

    def test_basic_stats(self):
        s = stats_from_records(self._records(), focus_seat=0)
        assert s["rounds"] == 3
        assert s["focus_hu"] == 2
        assert s["focus_fangpao"] == 1
        assert s["focus_net"] == 6
        assert len(s["sessions"]) == 2

    def test_empty_records(self):
        assert stats_from_records([])["rounds"] == 0

    def test_session_win20_flag(self):
        records = self._records()
        records += [new_round_record(room="A", deltas={"0": 20})]
        s = stats_from_records(records, focus_seat=0)
        sess_a = next(x for x in s["sessions"] if x["room"] == "A")
        assert sess_a["net"] == 20
        assert sess_a["win20_walk"] is True

    def test_rng_report_insufficient(self):
        rep = rng_fairness_report(self._records(), min_rounds=100)
        assert rep["verdict"] == "insufficient_data"

    def test_rng_report_normal(self):
        import random

        rng = random.Random(1)
        records = []
        for i in range(400):
            win = rng.random() < 0.25
            records.append(new_round_record(
                room=f"R{i//16}", winner=0 if win else None,
                deltas={"0": 4} if win else {"0": 0},
            ))
        rep = rng_fairness_report(records, min_rounds=100)
        assert rep["verdict"] in ("normal", "watch")
        assert rep["rounds"] == 400
