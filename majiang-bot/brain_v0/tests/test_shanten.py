# -*- coding: utf-8 -*-
"""向听/听牌/胡牌判定的回归测试。牌编码:1-9万 21-29筒 41-49条 55红中。"""

import pytest

from engine.shanten import (
    _suit_decompositions,
    best_discard_by_efficiency,
    calc_shanten,
    calc_ting_tiles,
    discard_analysis,
    is_win,
)


class TestStandardShanten:
    def test_complete_win_14(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 24]
        assert is_win(hand) is True

    def test_listening_13(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24]
        assert calc_shanten(hand) == 0

    def test_dandiao_ting(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
        assert calc_shanten(hand) == 0
        assert calc_ting_tiles(hand) == [25, 55]

    def test_two_away(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 23, 25, 27]
        assert calc_shanten(hand) == 1

    def test_thirteen_orphans_like_far_hand(self):
        hand = [1, 4, 7, 21, 24, 27, 41, 44, 47, 2, 5, 8, 25]
        assert calc_shanten(hand) >= 3

    def test_suit_boundary_no_cross_straight(self):
        with pytest.raises(ValueError):
            is_win([7, 8, 9, 10])
        assert calc_shanten([8, 9, 21]) >= 2

    def test_kong_shape_triplet(self):
        hand14 = [1, 1, 1, 2, 3, 4, 5, 6, 7, 21, 21, 22, 22, 22]
        assert is_win(hand14) is True


class TestQidui:
    def test_qidui_win_with_joker(self):
        hand = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, CAISHEN := 55, 55]
        assert is_win(hand) is True

    def test_qidui_listening_all_with_joker(self):
        hand13 = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, 55]
        ting = calc_ting_tiles(hand13)
        assert len(ting) > 0

    def test_qidui_four_same_counts_as_two_pairs(self):
        hand = [1, 1, 1, 1, 21, 21, 22, 22, 23, 23, 24, 24, 25, 25]
        assert is_win(hand) is True


class TestJoker:
    def test_joker_completes_meld_and_pair(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 21, 21, 55, 55]
        assert is_win(hand) is True

    def test_single_joker_win(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 55]
        assert is_win(hand) is True

    def test_joker_reduces_shanten_by_one_each(self):
        base = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 23, 25]
        s0 = calc_shanten(base)
        s1 = calc_shanten(base + [55])
        s2 = calc_shanten(base + [55, 55])
        assert s1 == s0 - 1
        assert s2 <= s1 - 1

    def test_all_jokers_is_win_at_14(self):
        with pytest.raises(ValueError):
            is_win([55] * 14)

    def test_four_jokers_does_not_explode(self):
        # 4 张孤财神：枚举 4 张替代牌时最优是把 4 张都赋成同一牌，
        # 等价于 4 张同牌 → 七对视角 2 个对子，向听 = 6 - 2 = 4；
        # 标准型视角 8 - 2*1(刻子) = 6，取小值仍是 4。该值确定且可复现。
        result = calc_shanten([55] * 4)
        assert result == 4
        assert isinstance(result, int)
        # 同一输入重复调用（含 lru_cache 命中路径）结果必须一致。
        assert calc_shanten([55] * 4) == result
        # 七对视角 k 张孤财神向听 = 6 - k//2，锁死 1~4 张全财神手牌，
        # 防止枚举提前 break 退化成魔法数（5 张以上被 validate_tiles 拒绝）。
        assert [calc_shanten([55] * k) for k in (1, 2, 3, 4)] == [6, 5, 5, 4]


class TestBestDiscard:
    def test_prefers_keeps_efficiency(self):
        # 14 张：三个完整面子(万1-9) + 21,22,23 顺子外的 25(筒5)/47(条7) 两个孤张。
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
        scores = best_discard_by_efficiency(hand)
        assert len(scores) == len(set(t for _, t in scores))

        rows = discard_analysis(hand)
        by_tile = {row["tile"]: row for row in rows}
        max_u = max(row["ukeire_count"] for row in rows)
        best_shanten = min(row["shanten_after"] for row in rows)

        # 并列最优集合必须恰为两个孤张，不接受析取式放宽。
        tied = {
            tile for tile, row in by_tile.items()
            if row["shanten_after"] == best_shanten
            and row["ukeire_count"] == max_u
        }
        assert tied == {25, 47}
        assert best_shanten == 0
        assert max_u == 7
        # 精确进张明细：打 25 听 47+财神(3+4=7)，打 47 听 25+财神(3+4=7)。
        assert by_tile[25]["ukeire"] == {47: 3, 55: 4}
        assert by_tile[47]["ukeire"] == {25: 3, 55: 4}
        for tile in (25, 47):
            assert by_tile[tile]["shanten_after"] == 0
            assert by_tile[tile]["ukeire_count"] == 7
            assert by_tile[tile]["eff_score"] == -1000 * 0 + 7
        # 其余 12 张弃后向听变 1 且无进张，不可能成为最优。
        others = set(by_tile) - tied
        assert len(others) == 12
        assert all(by_tile[t]["shanten_after"] == 1 for t in others)
        assert all(by_tile[t]["ukeire_count"] == 0 for t in others)

        # 排序契约：先向听、再进张降序、最后牌码升序 tie-break → 首选恰为 25。
        assert rows[0]["tile"] == 25
        assert rows[1]["tile"] == 47
        assert scores[0] == (7, 25)
        assert scores[1] == (7, 47)

    def test_requires_14_tiles(self):
        with pytest.raises(ValueError):
            best_discard_by_efficiency([1, 2, 3])


class TestSuitDecompositions:
    """_suit_decompositions 单花色分解独立测试。"""

    def test_known_decomposition_structure(self):
        # 111 222 333 44 (万, suit-local 1-9) → 至少包含 (3,0) 和 (2,1) 等分解
        result = _suit_decompositions({1: 3, 2: 3, 3: 3, 4: 2})
        # 3 面子 0 搭子 + 对子 (三刻+一对)
        assert (3, 0) in result
        assert result[(3, 0)] == 1  # pair_used=1
        # 3 面子 1 搭子 (三刻+44为搭子)
        assert (3, 1) in result
        assert result[(3, 1)] == 0  # pair_used=0
        # 2 面子 1 搭子 + 对子 (222+333+111为搭子? 或类似)
        assert (2, 1) in result
        # 返回 dict 类型
        assert isinstance(result, dict)

    def test_all_isolated_singles(self):
        # 1 2 3 4 5 (4 孤张 + 1 对子? 不对, 5 张各一张)
        result = _suit_decompositions({1: 1, 2: 1, 3: 1, 4: 1, 5: 1})
        # 没有对子,部分难组合,但至少返回结构
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_single_triplet(self):
        # 111 → 1 面子, pair_used=0
        result = _suit_decompositions({1: 3})
        assert (1, 0) in result
        assert result[(1, 0)] == 0

    def test_pair_only(self):
        # 11 → 两种分解二选一：作对子 (0,0,pair_used=True) 或作搭子 (0,1,pair_used=False)
        result = _suit_decompositions({1: 2})
        assert (0, 0) in result
        assert result[(0, 0)] == 1  # pair_used=1
        assert (0, 1) in result
        assert result[(0, 1)] == 0  # 作搭子时 pair_used=0


class TestDiscardAnalysis:
    def test_discard_analysis_requires_14_tiles(self):
        with pytest.raises(ValueError, match="14"):
            discard_analysis([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25])


class TestInputGuards:
    """CODE_REVIEW 2026-08-27 第1/2项边界契约。"""

    def test_ting_tiles_rejects_14_tiles_with_chinese_error(self):
        hand14 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 24]
        with pytest.raises(ValueError, match="仅接受13张手牌"):
            calc_ting_tiles(hand14)

    def test_ting_tiles_rejects_short_hand(self):
        with pytest.raises(ValueError, match="仅接受13张手牌"):
            calc_ting_tiles([1, 2, 3])

    def test_ting_tiles_works_on_13(self):
        assert calc_ting_tiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]) == [25, 55]

    def test_validate_tiles_rejects_more_than_14(self):
        with pytest.raises(ValueError, match="不能超过14张"):
            calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 25, 26])

    def test_empty_hand_returns_deterministic_zero_not_magic(self):
        # 空输入曾返回魔法数 99；现在固定返回 0（保守下界，与 is_win([]) is False 自洽）。
        assert calc_shanten([]) == 0
        assert is_win([]) is False
        assert 99 not in {calc_shanten([]), calc_shanten([55]), calc_shanten([1])}
