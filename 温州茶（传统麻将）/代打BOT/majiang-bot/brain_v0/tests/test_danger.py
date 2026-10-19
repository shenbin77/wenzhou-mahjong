# -*- coding: utf-8 -*-
"""DangerModel 单元测试：clamp 上限 / 单调性 / 阈值边界 / 晚巡 boost。"""

import pytest

from engine.danger import DangerModel


class TestRateClamp:
    def test_clamped_at_100(self):
        # 极晚巡 + 完全未见的中张，理论可达上界但必须被夹在 <=100。
        m = DangerModel()
        for tile in (1, 25, 45):  # 万/筒/条 中张(num 3-7)
            assert m.rate(tile, {tile: 0}, turn_no=999) <= 100.0
        # 明确不溢出
        assert m.rate(25, {25: 0}, turn_no=999) <= 100.0

    def test_all_played_is_exactly_zero(self):
        m = DangerModel()
        assert m.rate(25, {25: 4}, turn_no=1) == 0.0
        assert m.rate(25, {25: 4}, turn_no=18) == 0.0


class TestMonotonicity:
    def test_more_played_is_less_dangerous(self):
        m = DangerModel()
        # 固定中张 25，随已出张数增加危险度单调不增（晚巡 boost 同时存在）。
        prev = None
        for played in range(5):
            d = m.rate(25, {25: played}, turn_no=18)
            if prev is not None:
                assert d <= prev
            prev = d
        # 末态为 0
        assert prev == 0.0

    def test_monotonic_within_same_played_count(self):
        m = DangerModel()
        # 同已出数、不同巡目：危险度应随晚巡 boost 仅增不减（played<=1 时）。
        assert m.rate(25, {25: 0}, turn_no=18) >= m.rate(25, {25: 0}, turn_no=1)


class TestLevelBoundaries:
    def test_exact_thresholds(self):
        m = DangerModel()
        assert m.level(75) == "EXTREME"
        assert m.level(74) == "HIGH"
        assert m.level(50) == "HIGH"
        assert m.level(49) == "MID"
        assert m.level(25) == "MID"
        assert m.level(24) == "LOW"
        assert m.level(0) == "LOW"

    def test_boundary_crossings_are_consistent(self):
        m = DangerModel()
        # 相邻分数不应跨越两个档位以上，保证阈值单调。
        levels = [m.level(s) for s in range(0, 101, 25)]
        ranks = {"LOW": 0, "MID": 1, "HIGH": 2, "EXTREME": 3}
        for a, b in zip(levels, levels[1:]):
            assert ranks[b] - ranks[a] in (0, 1)


class TestLateGameBoost:
    def test_boost_only_for_live_tiles(self):
        m = DangerModel()
        # played<=1 的活牌：晚巡 > 早巡。
        early = m.rate(25, {25: 0}, turn_no=1)
        late = m.rate(25, {25: 0}, turn_no=18)
        assert late > early
        # played=0 与 played=1 都享受 boost，但 played=1 基数更低。
        assert m.rate(25, {25: 1}, turn_no=18) > m.rate(25, {25: 1}, turn_no=1)

    def test_no_boost_for_dead_tiles(self):
        m = DangerModel()
        # played>=2 的牌不再享受 late_game_boost，巡目变化不改变危险度。
        assert m.rate(25, {25: 2}, turn_no=1) == m.rate(25, {25: 2}, turn_no=18)
        assert m.rate(25, {25: 3}, turn_no=1) == m.rate(25, {25: 3}, turn_no=18)

    def test_boost_magnitude_equals_late_game_boost(self):
        m = DangerModel(late_game_boost=15.0)
        base = m.rate(25, {25: 0}, turn_no=1)  # progress~0 → 无 boost
        late = m.rate(25, {25: 0}, turn_no=999)  # progress 夹顶为 1.0
        # 晚巡比早巡恰好多出 late_game_boost（progress 满时）。
        assert late - base == pytest.approx(15.0)
