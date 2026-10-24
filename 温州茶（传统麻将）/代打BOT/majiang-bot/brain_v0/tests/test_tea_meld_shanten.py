# -*- coding: utf-8 -*-
"""茶向听引擎副露（meld）回归测试。

覆盖：
- meld_count=0 / melds=None 时与改造前逐值一致（纯手牌行为不变）；
- meld_count=1/2 的向听收敛（5−n 面子 + 1 将，17张制）；
- 杠计数契约（人工规则金标，见 TestKongGoldenRules）：
  * 杠在逻辑牌型里只占一个面子（与吃/碰同价，logical_slot_count=3×组数）；
  * 杠的四张牌全部已被杠消耗，第 4 张不得再参与顺子/对子/财神分配；
  * 杠后补牌使暗手仍按"每组副露减 3 张"：1 组副露时等待态暗手 13 张、
    摸牌态 14 张（2 组：10/11，以此类推）；
  * 杠的第 4 张只进物理可见账（visible_physical_count：吃/碰3、杠4），
    用于"同种≤4张"校验与进张剩余统计。
  画面实测备注：暗杠屏显 3 个牌位（中间位抬高 26px），第 4 张被盖住；
  其牌值由调用方按"3 个连续同 label 框"信号补全后显式传入，引擎不虚构。
- 非法形态拒绝（张数不守恒、面子张数非法、含副露同种超 4 张、
  meld_count 与 melds 矛盾）；
- 听牌/进张/弃牌链路贯通副露。

牌编码：1-9万，21-29筒，41-49条，61东 62南 63西 64北 73白。
"""

import pytest

from engine.tea.shanten import (
    _normalise_melds,
    best_discard_by_efficiency,
    calc_shanten,
    calc_ting_tiles,
    discard_analysis,
    is_win,
    validate_tiles,
)


class TestNoMeldUnchanged:
    """副露参数缺省时必须与改造前完全一致（回归基线）。"""

    def test_full_drawn_hand_is_win(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61, 61]
        assert calc_shanten(hand) == -1
        assert calc_shanten(hand, meld_count=0) == -1
        assert calc_shanten(hand, melds=None) == -1

    def test_waiting_hand_shanten_zero(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61]
        assert calc_shanten(hand) == 0

    def test_melds_empty_list_equals_default(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61, 61]
        assert calc_shanten(hand, melds=[]) == calc_shanten(hand)

    def test_badui_still_evaluated_without_melds(self):
        # 八对：8对+1单（17张）即胡
        hand = [1, 1, 2, 2, 3, 3, 21, 21, 22, 22, 23, 23, 41, 41, 61, 61, 73]
        assert is_win(hand) is True

    def test_discard_analysis_rejects_non_drawn_state(self):
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61, 61]
        assert discard_analysis(hand)
        with pytest.raises(ValueError):
            discard_analysis(hand[:14])


class TestMeldConvergence:
    """n 组副露 = 已成立 n 个面子，闭手向 (5−n) 面子 + 1 将收敛。"""

    MELD1 = [[1, 1, 1]]  # 碰 1万
    # 闭手13(等待态)：4个完整面子 + 单张6条，等成将
    HAND13 = [2, 3, 4, 5, 6, 7, 21, 22, 23, 41, 42, 43, 61]
    # 闭手14(刚摸牌)：上述 + 6条 → 4面子+将+1组副露 = 胡
    HAND14 = HAND13 + [61]

    def test_one_meld_waiting_shanten(self):
        assert calc_shanten(self.HAND13, melds=self.MELD1) == 0

    def test_one_meld_drawn_is_win(self):
        assert is_win(self.HAND14, melds=self.MELD1) is True

    def test_scalar_meld_count_matches_melds(self):
        assert (calc_shanten(self.HAND13, meld_count=1)
                == calc_shanten(self.HAND13, melds=self.MELD1))

    def test_two_melds_complete_hand_is_win(self):
        melds = [[1, 1, 1], [2, 2, 2]]
        hand = [3, 4, 5, 6, 7, 8, 21, 22, 23, 24, 24]  # 3面子+将
        assert is_win(hand, melds=melds) is True

    def test_two_melds_five_melds_plus_taatsu_is_tenpai(self):
        melds = [[1, 1, 1], [2, 2, 2]]
        # 刚摸牌态：副露2组+暗手3面子+1搭子 = 5面子+搭子，缺将，
        # 打一张即进入单钓/搭子听 → 向听0（弃牌后即可胡）
        hand = [3, 4, 5, 6, 7, 21, 22, 23, 41, 42, 43]
        assert calc_shanten(hand, melds=melds) == 0

    def test_extracting_meld_preserves_shanten(self):
        # 把暗手里已成立的刻子拆成副露：账目守恒下向听不变（等价分解）
        hand = [1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 61]
        assert calc_shanten(hand) == 0
        after = calc_shanten([2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 61],
                             melds=[[1, 1, 1]])
        assert after == 0

    def test_counting_meld_never_hurts(self):
        # 同一暗手：计入副露组数后向听只会更低或持平
        concealed = [2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 61]
        assert (calc_shanten(concealed, meld_count=1)
                <= calc_shanten(concealed, meld_count=0))


class TestKongGoldenRules:
    """杠计数契约的人工规则金标（期望值全部由人按麻将规则手算，非模型输出）。

    规则依据（温州茶 17 张制）：
    - 胡牌型 = 5 面子 + 1 将（或八对+1单，仅无副露时评估）。
    - 每组副露 = 已成立 1 个面子：吃/碰/杠同价，杠只占一个面子。
    - 杠的四张牌全部被杠消耗：第 4 张不得再与暗手组成顺子/对子，
      也不得计入财神；它只进物理可见账（同种≤4、进张剩余）。
    - 杠后补牌：暗手张数仍按"每组副露减 3 张"守恒——
      1 组副露：等待态暗手 16−3=13、摸牌态 17−3=14；
      2 组副露：等待态 10、摸牌态 11。
    """

    KONG_BEI = [[64, 64, 64, 64]]   # 杠北（字牌）
    KONG_BAI = [[73, 73, 73, 73]]   # 杠白
    PENG_1W = [[1, 1, 1]]           # 碰 1万
    KONG_1W = [[1, 1, 1, 1]]        # 杠 1万

    # ---- 碰（对照组，确立"1 组副露=1 面子"基准） ----
    # 暗手13(等待) = 234万/567万/123筒/123条 4面子 + 单张东。
    # 手算：总面子 4+1(碰)=5 已齐，缺将 → 向听 0，单钓东。
    PENG_WAIT13 = [2, 3, 4, 5, 6, 7, 21, 22, 23, 41, 42, 43, 61]

    def test_golden_peng_waiting(self):
        assert calc_shanten(self.PENG_WAIT13, melds=self.PENG_1W) == 0
        assert calc_ting_tiles(self.PENG_WAIT13, melds=self.PENG_1W) == [61]

    def test_golden_peng_drawn_is_win(self):
        # 摸牌态 14 张：摸进东成将 → 5面子+将 = 胡（向听 -1）
        assert is_win(self.PENG_WAIT13 + [61], melds=self.PENG_1W) is True

    # ---- 明杠 ----
    # 暗手13(等待) = 123/456/789万 + 123筒 4面子 + 单张4条。
    # 手算：杠=1面子 → 5面子齐、缺将 → 向听 0，单钓4条。
    KONG_WAIT13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41]

    def test_golden_exposed_kong_waiting(self):
        assert calc_shanten(self.KONG_WAIT13, melds=self.KONG_BAI) == 0
        assert calc_ting_tiles(self.KONG_WAIT13, melds=self.KONG_BAI) == [41]

    def test_golden_exposed_kong_drawn_is_win(self):
        # 摸牌态 14 张（17−3，杠后补牌不多扣暗手）：摸进4条成将 → 胡
        assert is_win(self.KONG_WAIT13 + [41], melds=self.KONG_BAI) is True

    def test_golden_exposed_kong_rejects_12_tile_waiting(self):
        # 旧错误契约的"等待态12张(=16−4)"是非法形态：12+3=15 ≠ 16/17
        with pytest.raises(ValueError):
            calc_shanten(self.KONG_WAIT13[:-1], melds=self.KONG_BAI)

    # ---- 暗杠 ----
    # 逻辑账与明杠完全一致（暗杠只是第4张屏显被盖住，牌同样全部被杠消耗）。
    def test_golden_concealed_kong_same_accounting(self):
        assert (calc_shanten(self.KONG_WAIT13, melds=self.KONG_BEI)
                == calc_shanten(self.KONG_WAIT13, melds=self.KONG_BAI) == 0)
        assert calc_ting_tiles(self.KONG_WAIT13, melds=self.KONG_BEI) == [41]
        assert is_win(self.KONG_WAIT13 + [41], melds=self.KONG_BEI) is True

    # ---- 加杠 ----
    # 加杠 = 碰之后补第 4 张，逻辑牌型不变（仍是 1 个面子），
    # 同一副 13 张暗手在"碰 1万"与"加杠 1万"下向听必须相同；
    # 区别只在物理账：碰可见 3 张、加杠可见 4 张。
    def test_golden_added_kong_logical_unchanged(self):
        s_peng = calc_shanten(self.PENG_WAIT13, melds=self.PENG_1W)
        s_kong = calc_shanten(self.PENG_WAIT13, melds=self.KONG_1W)
        assert s_peng == s_kong == 0
        assert (calc_ting_tiles(self.PENG_WAIT13, melds=self.PENG_1W)
                == calc_ting_tiles(self.PENG_WAIT13, melds=self.KONG_1W))

    # ---- 杠后补牌（摸牌态张数账） ----
    def test_golden_kong_supplement_draw_14_tiles(self):
        # 杠后补牌 = 摸牌态：暗手 14 张（17−3）。弃牌分析必须接受 14 张。
        rows = discard_analysis(self.KONG_WAIT13 + [42], melds=self.KONG_BEI)
        assert rows
        # 手算：打掉任一孤张(41/42)后回到 4面子+杠+孤张 → 向听 0
        assert min(r["shanten_after"] for r in rows) == 0

    def test_golden_kong_supplement_rejects_13_as_drawn(self):
        # 13 张+1 组杠 = 16(等待态)，不是摸牌态；旧契约"13=17−4 刚摸牌"是错的
        with pytest.raises(ValueError):
            discard_analysis(self.KONG_WAIT13, melds=self.KONG_BEI)

    # ---- 余牌否定（核心回归：杠的第 4 张不得回灌暗手分解） ----
    # 副露：杠 1万。暗手13(等待) = 2..9万 + 123筒 + 对4条。
    # 手算：万子 2-9 最優拆 234/567 + 89搭（或 23搭 + 456/789）→ 3面子+1搭；
    # 筒 123 第 3 面子… 合计暗手 3面子+1搭+1将，加杠 1 面子 = 4面子+1搭+1将
    # → 向听 = 10−2*4−1−1 = 0；听口 = 4万(234/456/789) 与 7万(789)，
    # 1万四张已全部被杠消耗，不可能做听口。
    # 旧错误逻辑把第 4 张 1万回灌暗手凑成 123万，会错报"已胡(-1)"。
    SPARE_DENY_HAND = [2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41]

    def test_golden_kong_spare_not_reusable(self):
        assert calc_shanten(self.SPARE_DENY_HAND, melds=self.KONG_1W) == 0
        assert is_win(self.SPARE_DENY_HAND, melds=self.KONG_1W) is False
        assert calc_ting_tiles(self.SPARE_DENY_HAND, melds=self.KONG_1W) == [4, 7]

    def test_golden_kong_spare_counts_only_physically(self):
        # 物理账：杠的 4 张 1万全可见 → 进张剩余按 4−4=0；
        # 4万/7万暗手各 1 张 → 各剩 3 张
        from engine.tea.shanten import calc_ukeire
        waits = calc_ukeire(self.SPARE_DENY_HAND, melds=self.KONG_1W)
        assert waits == {4: 3, 7: 3}

    # ---- 两组杠 ----
    # 2 组副露：等待态暗手 16−6=10 张。
    # 暗手10 = 123/456/789万 3面子 + 单张东；加 2 杠 = 5面子、缺将
    # → 向听 0，单钓东。旧契约的"9 张(=16−8)暗手"是非法形态(9+6=15)。
    def test_golden_two_kongs_waiting_10_tiles(self):
        kongs = [self.KONG_BEI[0], self.KONG_BAI[0]]
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 61]
        assert calc_shanten(hand, melds=kongs) == 0
        assert calc_ting_tiles(hand, melds=kongs) == [61]
        with pytest.raises(ValueError):
            calc_shanten(hand[:-1], melds=kongs)

    # ---- 两套计数契约 ----
    def test_golden_two_count_contract(self):
        from engine.tea.shanten import (
            expected_concealed_count,
            logical_slot_count,
            visible_physical_count,
        )
        # 碰：逻辑 3 / 物理 3；杠：逻辑 3 / 物理 4
        assert logical_slot_count(melds=self.PENG_1W) == 3
        assert visible_physical_count(melds=self.PENG_1W) == 3
        assert logical_slot_count(melds=self.KONG_1W) == 3
        assert visible_physical_count(melds=self.KONG_1W) == 4
        # 杠后补牌含在账内：1 组副露 → 等待 13 / 摸牌 14
        assert expected_concealed_count(melds=self.KONG_1W) == 13
        assert expected_concealed_count(melds=self.KONG_1W, drawn=True) == 14
        # 2 组（含杠）→ 等待 10 / 摸牌 11；物理 7
        two = [self.KONG_BEI[0], self.PENG_1W[0]]
        assert logical_slot_count(melds=two) == 6
        assert visible_physical_count(melds=two) == 7
        assert expected_concealed_count(melds=two) == 10

    def test_scalar_meld_count_rejects_bad_accounting(self):
        # 标量接口每组按 1 面子(3 逻辑张)计：12张+1组 → 12+3=15 不守恒，
        # 必须拒绝。杠必须走 melds=[[x,x,x,x]] 显式传 4 张做物理账。
        with pytest.raises(ValueError):
            calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23],
                         meld_count=1)


class TestIllegalShapes:
    """非法形态必须拒绝，宁弃权不算错。"""

    def test_14_tiles_with_two_melds_scalar_rejected(self):
        # 14张+1组副露=17 守恒合法；14张+2组=20 必须拒绝
        assert calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 42],
                            meld_count=1) >= 0
        with pytest.raises(ValueError):
            calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 42],
                         meld_count=2)

    def test_14_tiles_with_two_melds_rejected(self):
        with pytest.raises(ValueError):
            calc_shanten([2, 3, 4, 5, 6, 7, 21, 22, 23, 41, 42, 43, 61, 61],
                         melds=[[1, 1, 1], [2, 2, 2]])

    def test_meld_must_be_3_or_4_tiles(self):
        hand13 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41]
        with pytest.raises(ValueError):
            calc_shanten(hand13, melds=[[61, 61, 61, 61, 61]])
        with pytest.raises(ValueError):
            calc_shanten(hand13 + [42, 43, 61, 61], melds=[[61, 61]])

    def test_same_tile_over_four_across_melds(self):
        # 闭手2张西 + 暗杠西×4 = 6张西，不可能
        with pytest.raises(ValueError):
            validate_tiles([61, 61], melds=[[61, 61, 61, 61]])
        # 闭手2张1万 + 碰1万×3 = 5张，不可能
        with pytest.raises(ValueError):
            validate_tiles([1, 1], melds=[[1, 1, 1]])

    def test_meld_count_conflicts_with_melds(self):
        with pytest.raises(ValueError):
            calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41],
                         meld_count=2, melds=[[64, 64, 64, 64]])

    def test_meld_count_out_of_range(self):
        with pytest.raises(ValueError):
            calc_shanten([1] * 16, meld_count=6)

    def test_meld_tile_must_be_legal(self):
        with pytest.raises(ValueError):
            validate_tiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41],
                           melds=[[99, 99, 99]])


class TestMeldPipelines:
    """听牌/进张/弃牌链路必须贯通副露。"""

    MELD1 = [[1, 1, 1]]
    HAND13 = [2, 3, 4, 5, 6, 7, 21, 22, 23, 41, 42, 43, 61]
    HAND14 = HAND13 + [61]

    def test_ting_tiles_with_meld(self):
        assert calc_ting_tiles(self.HAND13, melds=self.MELD1) == [61]

    def test_ting_tiles_rejects_drawn_state(self):
        with pytest.raises(ValueError):
            calc_ting_tiles(self.HAND14, melds=self.MELD1)

    def test_discard_analysis_with_meld(self):
        rows = discard_analysis(self.HAND14, melds=self.MELD1)
        assert rows, "必须给出弃牌候选"
        assert min(r["shanten_after"] for r in rows) == 0

    def test_best_discard_with_meld(self):
        cands = best_discard_by_efficiency(self.HAND14, melds=self.MELD1)
        assert cands
        # 打多余的一张6条进入单钓将听，必须是候选之一
        assert any(tile == 61 for _, tile in cands)

    def test_ukeire_counts_meld_tiles_as_visible(self):
        from engine.tea.shanten import calc_ukeire
        waits = calc_ukeire(self.HAND13, melds=self.MELD1)
        # 6条闭手已有1张，副露是1万不占6条 → 剩3张
        assert waits.get(61) == 3


class TestNormaliseMelds:
    def test_none_passthrough(self):
        assert _normalise_melds(None) == (0, 0, None)

    def test_empty_list(self):
        assert _normalise_melds([]) == (0, 0, [])

    def test_sizes(self):
        count, total, groups = _normalise_melds([[1, 1, 1], [64, 64, 64, 64]])
        assert (count, total) == (2, 7)
        assert groups == [[1, 1, 1], [64, 64, 64, 64]]
