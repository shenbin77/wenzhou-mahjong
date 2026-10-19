# -*- coding: utf-8 -*-
"""红中赖子 + 七对边界：四同两对、财神补单/成对、开杠禁七对、13/14、听牌含红中。"""
from __future__ import annotations

from collections import Counter

import pytest

from engine.scoring import _qidui_shape, detect_fans
from engine.shanten import (
    calc_shanten,
    calc_ting_tiles,
    calc_ukeire,
    is_win,
    validate_tiles,
)
from engine.tiles import CAISHEN_ID


class TestQiduiQuadAndJokerFill:
    def test_four_identical_count_as_two_pairs_shanten_and_fan(self):
        hand = [1, 1, 1, 1, 21, 21, 22, 22, 23, 23, 24, 24, 25, 25]
        assert calc_shanten(hand) == -1
        assert is_win(hand)
        assert is_win(hand, use_qidui=False) is False
        assert _qidui_shape(Counter(hand), 0) is True
        assert detect_fans(hand[:-1], hand[-1]) == ["qidui"]

    def test_jokers_fill_singles_before_forming_pure_joker_pairs(self):
        # 5 对 + 2 单 + 2 财神 → 先补两单 → 7 对
        hand = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 55, 55]
        assert _qidui_shape(Counter(t for t in hand if t != 55), 2) is True
        assert is_win(hand)
        assert "qidui" in detect_fans(hand[:-1], hand[-1])

    def test_two_jokers_form_one_pure_pair_when_no_singles(self):
        hand = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 55, 55]
        assert is_win(hand)
        fans = detect_fans(hand[:-1], hand[-1])
        assert "qidui" in fans

    def test_insufficient_jokers_not_qidui(self):
        hand = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 7, 8, 55]  # 5对+3单+1财
        assert _qidui_shape(Counter(t for t in hand if t != 55), 1) is False
        assert "qidui" not in detect_fans(hand[:-1], hand[-1])


class TestOpenMeldDisablesQidui:
    def test_shanten_ignores_qidui_when_open_meld_count_positive(self):
        # 11 张五对+单：若算七对会很近，但副露后应走标准型
        concealed = [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 27]
        assert len(concealed) == 11
        s_flag_on = calc_shanten(concealed, use_qidui=True, open_meld_count=1)
        s_flag_off = calc_shanten(concealed, use_qidui=False, open_meld_count=1)
        assert s_flag_on == s_flag_off
        assert is_win(concealed, use_qidui=True, open_meld_count=1) is False

    def test_standard_win_with_one_open_meld_still_works(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 21]  # 3 顺 + 雀头，open=1
        assert is_win(hand, open_meld_count=1) is True
        assert calc_shanten(hand, open_meld_count=1) == -1

    def test_pairs_shape_cannot_win_via_qidui_after_open_meld(self):
        # 6 对在 open=0 可胡七对；缩成 11 张 + open=1 不可再走七对
        pairs14 = [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 27, 27, 41, 41]
        assert is_win(pairs14, use_qidui=True, open_meld_count=0)
        short11 = [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 27]
        assert is_win(short11, use_qidui=True, open_meld_count=1) is False


class TestHandSizeAndAllJoker:
    def test_qidui_listening_13_and_win_14(self):
        h13 = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, 24]
        assert calc_shanten(h13) == 0
        ting = calc_ting_tiles(h13)
        assert 24 in ting
        assert CAISHEN_ID in ting
        assert is_win(h13 + [24])
        assert is_win(h13 + [55])

    def test_four_jokers_in_14_qidui_ok(self):
        hand = [1, 1, 4, 4, 7, 7, 21, 21, 24, 27, 55, 55, 55, 55]
        assert _qidui_shape(Counter(t for t in hand if t != 55), 4) is True
        assert is_win(hand)
        assert calc_shanten(hand) == -1

    def test_more_than_four_same_tile_including_joker_rejected(self):
        with pytest.raises(ValueError, match="最多4张"):
            validate_tiles([55] * 5)
        with pytest.raises(ValueError, match="最多4张"):
            is_win([55] * 14)

    def test_pure_joker_short_hand_deterministic(self):
        assert [calc_shanten([55] * k) for k in (1, 2, 3, 4)] == [6, 5, 5, 4]


class TestTingIncludesHongzhong:
    def test_dandiao_ting_includes_caishen(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
        assert calc_ting_tiles(hand) == [25, 55]

    def test_ukeire_falls_back_to_joker_when_wait_tile_exhausted(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
        # 手牌已有 1 张 25，再 played 3 → 场上可见 4，进张只剩红中
        uk = calc_ukeire(hand, {25: 3})
        assert 25 not in uk or uk.get(25, 0) == 0
        assert uk.get(55, 0) == 4

    def test_qidui_ting_includes_hongzhong(self):
        h13 = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, 55]
        ting = calc_ting_tiles(h13)
        assert CAISHEN_ID in ting
        assert len(ting) > 1


class TestOpenMeldHandSizeGuards:
    @pytest.mark.parametrize("open_n,wait,win", [
        (0, 13, 14),
        (1, 10, 11),
        (2, 7, 8),
    ])
    def test_ting_and_win_lengths(self, open_n, wait, win):
        # 构造标准听牌：剩余面子用万条筒拼
        base_melds = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [21, 22, 23]]
        need = 4 - open_n
        tiles = [t for g in base_melds[:need] for t in g]
        if open_n == 0:
            tiles = tiles + [25]  # 13
        elif open_n == 1:
            tiles = tiles + [25]  # 10
        else:
            tiles = tiles + [25]  # 7
        assert len(tiles) == wait
        assert calc_shanten(tiles, open_meld_count=open_n) == 0
        assert 25 in calc_ting_tiles(tiles, open_meld_count=open_n)
        assert 55 in calc_ting_tiles(tiles, open_meld_count=open_n)
        assert is_win(tiles + [25], open_meld_count=open_n)
        assert len(tiles + [25]) == win
