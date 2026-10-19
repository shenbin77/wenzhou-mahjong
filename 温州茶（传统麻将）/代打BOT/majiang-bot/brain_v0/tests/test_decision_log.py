# -*- coding: utf-8 -*-
"""hand14 决策日志：GameLogger.append 真正写入字段。"""
import json
import os
import tempfile

from engine.advisor import Advisor
from engine.game_log import GameLogger, new_decision_record


class TestNewDecisionRecord:
    def test_fields(self):
        rec = new_decision_record(
            hand14=[1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47],
            played_counts={25: 1},
            turn_no=3,
            open_meld_count=0,
            suggested_discard=47,
            suggested_name="7条",
            shanten=2,
            options_top=[{"tile": 47, "name": "7条", "final_score": 1.0}],
            source="local",
        )
        assert rec["kind"] == "decision"
        assert rec["hand14"][0] == 1
        assert rec["played_counts"]["25"] == 1
        assert rec["turn_no"] == 3
        assert rec["open_meld_count"] == 0
        assert rec["suggested_discard"] == 47
        assert rec["shanten"] == 2
        assert rec["source"] == "local"


class TestAdvisorDecisionLog:
    def _hand14(self):
        return [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]

    def test_local_advise_appends_jsonl(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        logger = GameLogger(str(path))
        a = Advisor(decision_logger=logger)
        res = a.local_advise(self._hand14(), {25: 2}, turn_no=4, open_meld_count=0)
        assert res["options"]
        assert path.is_file()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["kind"] == "decision"
        assert row["hand14"] == self._hand14()
        assert row["played_counts"] == {"25": 2}
        assert row["turn_no"] == 4
        assert row["open_meld_count"] == 0
        assert row["suggested_discard"] == res["options"][0]["tile"]
        assert row["suggested_name"] == res["options"][0]["name"]
        assert row["shanten"] == res["shanten"]
        assert isinstance(row["options_top"], list)
        assert len(row["options_top"]) >= 1

    def test_hand13_still_logs(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        a = Advisor(decision_logger=GameLogger(str(path)))
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
        res = a.local_advise(hand13, {}, turn_no=2)
        assert res["options"] == []
        row = json.loads(path.read_text(encoding="utf-8").strip())
        assert row["hand14"] == hand13
        assert row["suggested_discard"] is None
        assert row["shanten"] == res["shanten"]

    def test_no_logger_is_noop(self):
        a = Advisor(decision_logger=None)
        res = a.local_advise(self._hand14(), {}, turn_no=1)
        assert res["options"]
