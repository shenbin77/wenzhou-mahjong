# -*- coding: utf-8 -*-
"""危险度模型与建议聚合层测试。"""

from engine.advisor import Advisor, local_strategy, random_strategy
from engine.danger import DangerModel


class TestDangerModel:
    def setup_method(self):
        self.m = DangerModel()

    def test_more_played_less_danger(self):
        counts = {25: 0}
        d0 = self.m.rate(25, counts, turn_no=5)
        counts[25] = 3
        d3 = self.m.rate(25, counts, 5)
        assert d3 < d0

    def test_all_played_is_zero(self):
        assert self.m.rate(25, {25: 4}, 10) == 0.0

    def test_late_game_boosts_live_tiles(self):
        counts = {25: 0}
        early = self.m.rate(25, counts, 1)
        late = self.m.rate(25, counts, 18)
        assert late > early

    def test_clamped_100(self):
        assert self.m.rate(25, {25: 0}, 999) <= 100

    def test_levels(self):
        assert self.m.level(90) == "EXTREME"
        assert self.m.level(60) == "HIGH"
        assert self.m.level(30) == "MID"
        assert self.m.level(5) == "LOW"

    def test_tile_number_parsing(self):
        assert DangerModel._tile_number(1) == 1
        assert DangerModel._tile_number(23) == 3
        assert DangerModel._tile_number(45) == 5


class TestAdvisor:
    def _hand14(self):
        return [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]

    def test_local_advise_structure(self):
        a = Advisor()
        res = a.local_advise(self._hand14(), {}, turn_no=3)
        assert res["source"] == "local"
        assert len(res["options"]) >= 2
        tiles = [o["tile"] for o in res["options"]]
        assert set(tiles) == set(set(self._hand14()))
        scores = [o["final_score"] for o in res["options"]]
        assert scores == sorted(scores, reverse=True)

    def test_safe_tile_preferred_under_threat(self):
        a = Advisor(danger_lambda=2.0)
        counts = {47: 3}
        res = a.local_advise([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 47], counts, 10)
        by_tile = {o["tile"]: o for o in res["options"]}
        assert by_tile[47]["danger"] < by_tile[24]["danger"]

    def test_advise_degrades_without_afa(self):
        class DeadClient:
            enabled = False
            last_error = "disabled"

            def recommend(self, *args, **kwargs):
                return None

        a = Advisor(afa_client=DeadClient())
        res = a.advise(self._hand14(), {}, turn_no=2)
        assert res["engine"] == "local"
        assert any("afa_unavailable" in w for w in res["warnings"])

    def test_advise_merges_afa(self):
        class FakeAfa:
            last_error = None

            def recommend(self, hand14, rivers=(), penggang=()):
                return {
                    "source": "afa",
                    "min_shanten": 0,
                    "win_tiles": [25],
                    "options": [
                        {"tile": 25, "score": 88.0, "danger_rate": 10.0,
                         "shanten_after": 0, "win_tiles_after": [25]},
                        {"tile": 47, "score": 12.0, "danger_rate": 80.0,
                         "shanten_after": 0, "win_tiles_after": []},
                    ],
                    "peng_suggest": [],
                    "gang_suggest": [],
                }

        a = Advisor(afa_client=FakeAfa(), afa_mode="blend")
        res = a.advise(self._hand14(), {}, turn_no=2)
        assert res["engine"] == "afa+local"
        assert res["options"][0]["tile"] == 25
        assert all("afa_score" in o for o in res["options"])

    def test_afa_is_shadow_by_default(self):
        class FakeAfa:
            last_error = None

            def recommend(self, hand14, rivers=(), penggang=()):
                return {"source": "afa", "min_shanten": 0, "win_tiles": [],
                        "options": [{"tile": 47, "score": 99, "danger_rate": 0,
                                     "shanten_after": 0, "win_tiles_after": []}],
                        "peng_suggest": [], "gang_suggest": []}

        res = Advisor(afa_client=FakeAfa()).advise(self._hand14(), {}, 2)
        assert res["engine"] == "local+afa-shadow"
        assert "afa" in res
        assert res["options"][0]["source"] == "local"

    def test_disagreement_warning(self):
        class LyingAfa:
            last_error = None

            def recommend(self, hand14, rivers=(), penggang=()):
                return {
                    "source": "afa",
                    "min_shanten": 0,
                    "win_tiles": [],
                    "options": [
                        {"tile": 47, "score": 50.0, "danger_rate": 0.0,
                         "shanten_after": 0, "win_tiles_after": []},
                    ],
                    "peng_suggest": [],
                    "gang_suggest": [],
                }

        a = Advisor(afa_client=LyingAfa())
        counts = {47: 0}
        res = a.advise([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 21, 22, 22, 47], counts, 15)
        assert any("disagreement" in w for w in res["warnings"])

    def test_afa_shanten_mismatch_and_invalid_option_are_quarantined(self):
        class MismatchedAfa:
            last_error = None

            def recommend(self, hand14, rivers=(), penggang=()):
                return {
                    "source": "afa", "min_shanten": 8, "win_tiles": [],
                    "options": [{"tile": 55, "score": 99.0, "danger_rate": 0,
                                 "shanten_after": 8, "win_tiles_after": []}],
                    "peng_suggest": [], "gang_suggest": [],
                }

        res = Advisor(afa_client=MismatchedAfa()).advise(self._hand14(), {}, 2)
        assert any("afa_shanten_disagreement" in w for w in res["warnings"])
        assert any("afa_invalid_option" in w for w in res["warnings"])
        assert res["engine"] == "local+afa-shadow"
        assert all(o["source"] == "local" for o in res["options"])

    def test_blend_is_blocked_when_shanten_convention_differs(self):
        class MismatchedAfa:
            last_error = None

            def recommend(self, hand14, rivers=(), penggang=()):
                return {
                    "source": "afa", "min_shanten": 7, "win_tiles": [],
                    "options": [{"tile": 47, "score": 99.0, "danger_rate": 0,
                                 "shanten_after": 7, "win_tiles_after": []}],
                    "peng_suggest": [], "gang_suggest": [],
                }

        res = Advisor(afa_client=MismatchedAfa(), afa_mode="blend").advise(
            self._hand14(), {}, 2
        )
        assert res["engine"] == "local+afa-shadow"
        assert any("afa_blend_blocked" in w for w in res["warnings"])
        assert all(o["source"] == "local" for o in res["options"])

    def test_should_fold(self):
        a = Advisor()
        far = {"shanten": 2}
        near = {"shanten": 0}
        assert a.should_fold(far, turn_no=13) is True
        assert a.should_fold(far, turn_no=5) is False
        assert a.should_fold(near, turn_no=16) is False

    def test_strategies_callable(self):
        hand = self._hand14()
        ctx = {"played_counts": {}, "turn_no": 1, "advisor": Advisor()}
        t1 = random_strategy(hand, dict(ctx))
        t2 = local_strategy(hand, dict(ctx))
        assert t1 in hand and t2 in hand
