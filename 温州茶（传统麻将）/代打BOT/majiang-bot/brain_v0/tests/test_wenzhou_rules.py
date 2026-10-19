# -*- coding: utf-8 -*-
"""温州红中放炮门面：红中赖子胡、七对、点炮、庄闲差分。"""

from engine.wenzhou_rules import (
    JOKER_ID,
    fans_for_win,
    hand_is_win,
    hand_shanten,
    score_dianpao,
    score_zimo,
    ting_tiles,
)


class TestJokerWin:
    def test_hongzhong_as_laizi_completes_pair(self):
        # 标准型：差一对，红中补对子
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, JOKER_ID]
        assert hand_is_win(hand) is True

    def test_hongzhong_as_laizi_in_ting(self):
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, JOKER_ID]
        assert hand_shanten(hand13) == 0
        ting = ting_tiles(hand13)
        assert JOKER_ID in ting or len(ting) > 0


class TestQidui:
    def test_qidui_with_two_jokers(self):
        hand = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, JOKER_ID, JOKER_ID]
        assert hand_is_win(hand) is True
        fans = fans_for_win(hand[:-1], hand[-1])
        assert "qidui" in fans

    def test_qidui_only_hand_requires_qidui_path(self):
        # 七对专用形：关七对后不能按标准型胡
        hand = [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 27, 41, JOKER_ID, JOKER_ID]
        assert hand_is_win(hand) is True
        assert hand_is_win(hand, use_qidui=False) is False
        assert "qidui" in fans_for_win(hand[:-1], hand[-1])


class TestDianpaoScoring:
    def test_basic_dianpao(self):
        d = score_dianpao(winner_seat=1, dealer_seat=0, loser_seat=3)
        assert d[3] == -1 and d[1] == 1
        assert sum(d.values()) == 0

    def test_dealer_loser_pays_double(self):
        d = score_dianpao(winner_seat=1, dealer_seat=3, loser_seat=3)
        assert d[3] == -2 and d[1] == 2

    def test_dealer_winner_gets_double(self):
        d = score_dianpao(winner_seat=0, dealer_seat=0, loser_seat=2)
        assert d[2] == -2 and d[0] == 2

    def test_qidui_dianpao_doubles(self):
        d = score_dianpao(winner_seat=1, dealer_seat=0, loser_seat=3, fan_types=["qidui"])
        assert d[3] == -2 and d[1] == 2


class TestZimoZhuangXian:
    def test_xian_zimo(self):
        d = score_zimo(winner_seat=1, dealer_seat=0)
        assert d == {0: -2, 1: 6, 2: -2, 3: -2}

    def test_dealer_zimo(self):
        d = score_zimo(winner_seat=0, dealer_seat=0)
        assert d == {0: 9, 1: -3, 2: -3, 3: -3}
