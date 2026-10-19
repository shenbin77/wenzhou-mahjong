# -*- coding: utf-8 -*-
"""官方计分器测试(依据 02_游戏规则对齐.md)。"""

import pytest

from engine.scoring import ScoreCalculator, detect_fans, settle_hand


class TestZimo:
    def test_xianjia_zimo_basic(self):
        d = settle_hand("zimo", winner_seat=1, dealer_seat=0)
        assert d == {0: -2, 1: 6, 2: -2, 3: -2}

    def test_dealer_zimo(self):
        d = settle_hand("zimo", winner_seat=0, dealer_seat=0)
        assert d == {0: 9, 1: -3, 2: -3, 3: -3}

    def test_zimo_qidui_double(self):
        d = settle_hand("zimo", winner_seat=2, dealer_seat=0, fan_types=["qidui"])
        assert d[2] == 12
        assert all(d[s] == -4 for s in (0, 1, 3))

    def test_dealer_zimo_no_zhuangxian(self):
        """use_zhuangxian=False 时庄家自摸也不翻倍。"""
        d = settle_hand("zimo", winner_seat=0, dealer_seat=0, use_zhuangxian=False)
        assert d == {0: 6, 1: -2, 2: -2, 3: -2}


class TestFangpao:
    def test_basic(self):
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=3)
        assert d[3] == -1 and d[1] == 1

    def test_discarder_is_dealer_pays_2(self):
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=3, loser_seat=3)
        assert d[3] == -2 and d[1] == 2

    def test_winner_is_dealer_gets_2(self):
        d = settle_hand("fangpao", winner_seat=0, dealer_seat=0, loser_seat=2)
        assert d[2] == -2 and d[0] == 2

    def test_qidui_fangpao_doubles(self):
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=3,
                        fan_types=["qingyise", "duidui"], stacking="multiply")
        assert d[3] == -4 and d[1] == 4

    def test_stacking_modes(self):
        calc = ScoreCalculator(stacking="multiply")
        assert calc.hu_multiplier(["qidui", "qingyise"]) == 4
        assert ScoreCalculator(stacking="add").hu_multiplier(["qidui", "qingyise"]) == 5
        assert ScoreCalculator(stacking="max").hu_multiplier(["qidui", "qingyise"]) == 2

    def test_invalid_stacking(self):
        with pytest.raises(ValueError):
            ScoreCalculator(stacking="bogus")

    def test_fangpao_requires_loser(self):
        with pytest.raises(ValueError):
            settle_hand("fangpao", winner_seat=1, dealer_seat=0)

    def test_self_fangpao_rejected(self):
        with pytest.raises(ValueError):
            settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=1)

    def test_dealer_discarder_no_zhuangxian(self):
        """use_zhuangxian=False 时庄家点炮不翻倍(unit=1)。"""
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=3, loser_seat=3,
                        use_zhuangxian=False)
        assert d[3] == -1 and d[1] == 1


class TestGang:
    def test_an_gang(self):
        d = settle_hand("zimo", winner_seat=1, dealer_seat=0,
                        gang_records=[{"type": "an", "seat": 2}])
        assert d[2] == 4 and d[0] == -4 and d[3] == -4

    def test_bu_gang(self):
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=3,
                        gang_records=[{"type": "bu", "seat": 2}])
        assert d[2] == 3

    def test_zhi_gang(self):
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=3,
                        gang_records=[{"type": "zhi", "seat": 2, "from_seat": 0}])
        assert d[2] == 3 and d[0] == -3

    def test_gang_after_draw_still_settled(self):
        """流局仍结算杠分:用无胡场景模拟——直接验证杠分零和。"""
        d = settle_hand("fangpao", winner_seat=1, dealer_seat=0, loser_seat=3,
                        gang_records=[
                            {"type": "an", "seat": 0},
                            {"type": "zhi", "seat": 1, "from_seat": 2},
                        ])
        assert sum(d.values()) == 0

    def test_draw_settles_gang_only(self):
        d = settle_hand("draw", winner_seat=None, dealer_seat=0,
                        gang_records=[{"type": "an", "seat": 2}])
        assert d == {0: -2, 1: -2, 2: 6, 3: -2}

    def test_invalid_gang_type(self):
        with pytest.raises(ValueError):
            settle_hand("zimo", winner_seat=1, dealer_seat=0,
                        gang_records=[{"type": "xxx", "seat": 0}])

    def test_zhi_gang_requires_from(self):
        with pytest.raises(ValueError):
            settle_hand("zimo", winner_seat=1, dealer_seat=0,
                        gang_records=[{"type": "zhi", "seat": 0}])


class TestZeroSum:
    @pytest.mark.parametrize("wt,winner,dealer,loser,fans", [
        ("zimo", 0, 0, None, []),
        ("zimo", 3, 1, None, ["dandiao"]),
        ("fangpao", 2, 2, 0, ["qidui"]),
        ("fangpao", 0, 3, 1, ["qingyise", "gangshanghua"]),
        ("zimo", 1, 2, None, ["duidui", "dandiao", "tianhu"]),
    ])
    def test_always_zero_sum(self, wt, winner, dealer, loser, fans):
        d = settle_hand(wt, winner, dealer, loser_seat=loser, fan_types=fans)
        assert sum(d.values()) == 0
        assert len(d) == 4


class TestDetectFans:
    CAISHEN = 55

    def test_qingyise_duidui(self):
        hand13 = [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5]
        fans = detect_fans(hand13, win_tile=5)
        assert "qingyise" in fans
        assert "duidui" in fans

    def test_qidui_detection(self):
        hand13 = [1, 1, 2, 2, 21, 21, 22, 22, 41, 41, 42, 42, self.CAISHEN]
        fans = detect_fans(hand13, win_tile=43)
        assert "qidui" in fans

    def test_plain_hand_no_fans(self):
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24]
        fans = detect_fans(hand13, win_tile=24)
        assert fans == []

    def test_context_fans_passthrough(self):
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24]
        fans = detect_fans(hand13, win_tile=24, allow_context_fans=["gangshanghua"])
        assert "gangshanghua" in fans

    def test_qingyise_qidui_combo(self):
        """清一色+七对子组合: detect_fans 应同时返回 qingyise 和 qidui。"""
        # 全万: 1,1,2,2,3,3,4,4,5,5,6,6,55(财神) + 7 → 7对
        fans = detect_fans([1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 55], 7)
        assert "qingyise" in fans
        assert "qidui" in fans

    def test_qingyise_qidui_joker_boundary(self):
        """清一色+七对子含财神: 财神补足第7对。"""
        # 1,1,2,2,3,3,4,4,5,5,55,55,55 + 6 → 全万,财神当第6/7对
        fans = detect_fans([1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 55, 55, 55], 6)
        assert "qingyise" in fans
        assert "qidui" in fans
