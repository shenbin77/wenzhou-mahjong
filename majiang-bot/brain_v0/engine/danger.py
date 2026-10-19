# -*- coding: utf-8 -*-
"""本地危险度模型(启发式兜底层)。

只依据全局已出信息:已出张数 + 局面进度 + 中张属性。
不知道"谁在等什么",精度低于阿法 API,故仅作兜底与交叉校验。
"""

_BASE_BY_PLAYED = {0: 80.0, 1: 55.0, 2: 30.0, 3: 8.0, 4: 0.0}
# 单局预估总巡数：牌山 60 张 / 4 人 ≈ 15 轮，18 为保守值（含庄家多摸等波动）。
# 用于由 turn_no 折算局面进度 progress=(turn_no-1)/EST_TOTAL_TURNS，驱动 late_game_boost 后段抬升。
EST_TOTAL_TURNS = 18


class DangerModel:
    def __init__(self, late_game_boost=15.0, mid_tile_boost=5.0, dealer_seat=None):
        self.late_game_boost = late_game_boost
        self.mid_tile_boost = mid_tile_boost
        # 庄家座位（SEATS 字符串或 0-3 整数或 None），仅作属性透传；
        # 单张危险度 rate() 本身与座位无关，此属性用于 EvEstimator 的
        # 回退链（self.danger.dealer_seat）。默认为 None=无庄家加权。
        self.dealer_seat = dealer_seat

    def rate(self, tile, played_counts, turn_no):
        """返回 0-100 危险度。turn_no 从 1 计。"""
        played = int(played_counts.get(int(tile), 0))
        played = min(max(played, 0), 4)
        d = _BASE_BY_PLAYED[played]
        progress = min(max(turn_no - 1, 0) / EST_TOTAL_TURNS, 1.0)
        if played <= 1:
            d += self.late_game_boost * progress
        num = self._tile_number(tile)
        if played == 0 and 3 <= num <= 7:
            d += self.mid_tile_boost
        return min(d, 100.0)

    def level(self, score):
        if score >= 75:
            return "EXTREME"
        if score >= 50:
            return "HIGH"
        if score >= 25:
            return "MID"
        return "LOW"

    @staticmethod
    def _tile_number(tile):
        t = int(tile)
        if 1 <= t <= 9:
            return t
        if 21 <= t <= 29:
            return t - 20
        return t - 40


class EvEstimator:
    """真·1 步 EV 估算器（决策层从"启发式危险度"升级到"近似最优"）。

    对每张候选弃牌计算：
        EV = E[收益] - E[损失]
           = P(进张改善)×gain_step  -  Σ_seat P(被该座位点炮)×penalty_seat

    - 损失侧：用 OpponentModel.danger_to_all_opponents（基础危险×座位持有概率×庄家加权）
      作为「该张被打出后被各对手点炮」的相对概率（0~1，已含庄家加成）。
    - 收益侧：用 shanten/ukeire（进张张数）估计「下一张摸到即改善向听」的概率，
      gain 随向听数降低而增大（听牌 > 一向听 > …）。
    - 这是 principled 的近似（非蒙特卡洛精确解），但比查表式危险度更接近真实最优。

    与 DangerModel 的关系：DangerModel 是「单张危险度」兜底层；EvEstimator 在其之上
    做「整手牌的候选牌 EV 排序」，直接给出"该打哪张"的近似最优解。
    """

    def __init__(self, opponent_model=None, danger_model=None,
                 gain_value=1.0, dealer_penalty=1.5, total_tiles=112,
                 dealer_seat=None):
        # 延迟导入避免与 opponent_model 的循环依赖
        if opponent_model is not None:
            self.opp = opponent_model
        else:
            from .opponent_model import OpponentModel
            self.opp = OpponentModel(
                danger_model=danger_model or DangerModel(),
                dealer_seat=dealer_seat)
        self.gain_value = gain_value
        self.dealer_penalty = dealer_penalty
        self.total_tiles = total_tiles

    def ev_of_discard(self, hand14, played_counts, turn_no,
                      seat_unseen=None, dealer_seat=None):
        """返回按 EV 降序排列的候选弃牌列表。

        每项: {tile, shanten_after, ukeire_count, danger(期望损失), ev}
        """
        from .shanten import discard_analysis

        if len(hand14) != 14:
            raise ValueError(f"需要 14 张手牌,得到 {len(hand14)}")
        rows = discard_analysis(hand14, played_counts)
        seen_total = sum(int(v) for v in played_counts.values())
        # 牌墙剩余 ≈ 总牌 - 已见 - 自己手里13张(打牌前);夹逼保护
        unseen = max(1, self.total_tiles - seen_total - 13)
        # 把 dealer_seat 归一化为 SEATS 字符串(或 None)；庄家必为
        # top/left/right 之一(或 bottom=自家庄)，与 dangers 的键(top/left/right)
        # 比较时才能正确命中，避免"默认 bottom 永不命中"导致庄家加权静默失效。
        from .opponent_model import normalize_seat
        dealer = normalize_seat(dealer_seat) or self.opp.dealer_seat
        results = []
        for row in rows:
            tile = row["tile"]
            sh_after = row["shanten_after"]
            ukeire_count = row["ukeire_count"]
            dangers = self.opp.danger_to_all_opponents(
                tile, played_counts, turn_no, seat_unseen)
            exp_loss = 0.0
            for seat, dval in dangers.items():
                p = min(max(dval, 0.0), 100.0) / 100.0
                pen = self.dealer_penalty if seat == dealer else 1.0
                exp_loss += p * pen
            p_improve = min(ukeire_count / unseen, 1.0) if ukeire_count else 0.0
            gain_step = self.gain_value * (1.0 / (1.0 + max(sh_after, 0)))
            exp_gain = p_improve * gain_step
            ev = exp_gain - exp_loss
            results.append({
                "tile": tile,
                "shanten_after": sh_after,
                "ukeire_count": ukeire_count,
                "danger": round(exp_loss, 4),
                "ev": round(ev, 4),
            })
        results.sort(key=lambda r: r["ev"], reverse=True)
        return results
