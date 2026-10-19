# -*- coding: utf-8 -*-
"""对手牌推断（层A：剩余牌按座位分布概率 + 庄家加权）。

设计前提（与记牌器一致）：已知信息 = 自己手牌 + 4家出牌河 + 碰/杠。
某牌一旦"已见张数"确定，剩余张必在 3 家对手手里。

- 层A（确定可做）：把某牌的剩余张数按"各对手尚未露出的牌量"比例分给 3 家，
  得「某座位持有该牌的概率」。这是记牌的自然延伸，纯概率。
- 庄家加权（呼应"小心庄家"）：庄家胡牌赔付更大/更易听，对其出牌危险度额外加成。
- 听牌范围（基础）：基于某座位已打牌 + 未见牌，给其可能听的牌的「范围上限」
  （信息不完全，只能给范围，越近终局越准）。

不依赖识图精度，纯概率，可单测。直接服务"少点炮"：知道对手大概率听什么 → 避开打那些牌。
"""
from .tiles import TILE_NAMES
from .danger import DangerModel

SEATS = ("bottom", "top", "left", "right")
OPPONENT_SEATS = ("top", "left", "right")
TOTAL_PER_TILE = 4

# 座位索引约定：0=bottom(自家) 1=top 2=left 3=right
_SEAT_NAME_TO_INDEX = {name: i for i, name in enumerate(SEATS)}


def normalize_seat(seat):
    """把 dealer_seat 归一化为 SEATS 字符串之一(或 None)。

    支持直接传 'bottom'/'top'/'left'/'right'，也支持 0-3 整数座位
    (0=bottom,1=top,2=left,3=right)。None 表示"未知/无庄家加权"，
    此时庄家加成与庄家惩罚均不生效（保守默认）。
    """
    if seat is None:
        return None
    if isinstance(seat, int):
        return SEATS[seat % 4]
    if isinstance(seat, str) and seat.strip().isdigit():
        return SEATS[int(seat) % 4]
    return str(seat).strip().lower()


class OpponentModel:
    def __init__(self, danger_model=None, dealer_seat=None, dealer_bonus=0.25):
        self.danger = danger_model or DangerModel()
        self.dealer_seat = normalize_seat(dealer_seat)
        self.dealer_bonus = dealer_bonus  # 庄家危险度加成系数（0~1）

    # ---------- 层A：剩余牌按座位分布 ----------
    def remaining_per_tile(self, played_counts, tile):
        """某牌剩余张数 = 4 - 已见(played_counts 含自己手牌+河牌+碰杠)。"""
        seen = int(played_counts.get(int(tile), 0))
        return max(0, TOTAL_PER_TILE - seen)

    def seat_distribution(self, tile, played_counts, seat_unseen=None):
        """某牌在 3 家对手手中的分布概率，返回 {seat: prob}（对对手归一化）。

        seat_unseen: {seat: 该座位尚未露出的牌量}；缺省按均匀分配剩余张。
        启发：某座位已露出越少（手中藏牌越多），持有剩余牌的概率越高。
        """
        rem = self.remaining_per_tile(played_counts, tile)
        if rem <= 0:
            return {s: 0.0 for s in OPPONENT_SEATS}
        if not seat_unseen:
            p = 1.0 / len(OPPONENT_SEATS)
            return {s: p for s in OPPONENT_SEATS}
        weights = {s: max(1, seat_unseen.get(s, 0)) for s in OPPONENT_SEATS}
        total = sum(weights.values())
        return {s: weights[s] / total for s in OPPONENT_SEATS}

    # ---------- 庄家加权后的「打某牌给某座位的危险度」 ----------
    def danger_to_seat(self, tile, played_counts, turn_no, target_seat,
                       seat_unseen=None):
        """0-100：你打出 tile 时，被 target_seat 胡（点炮）的危险度。

        基础 = 该牌剩余危险(DangerModel) × 该座位持有概率；
        若 target_seat 是庄家再叠加 dealer_bonus 加成。
        """
        base = self.danger.rate(tile, played_counts, turn_no) / 100.0  # 0~1
        hold = self.seat_distribution(tile, played_counts, seat_unseen).get(target_seat, 0.0)
        d = base * (0.3 + 0.7 * hold)  # 持有概率越高越危险，但留底避免恒为0
        if target_seat == self.dealer_seat:
            d *= (1.0 + self.dealer_bonus)
        return round(min(d, 1.0) * 100.0, 1)

    def danger_to_all_opponents(self, tile, played_counts, turn_no, seat_unseen=None):
        """返回每个对手座位的危险度（含庄家加权）。"""
        return {s: self.danger_to_seat(tile, played_counts, turn_no, s, seat_unseen)
                for s in OPPONENT_SEATS}

    # ---------- 听牌范围（基础版，仅范围上限） ----------
    def listen_candidates(self, seat_discards, played_counts):
        """某座位「可能听的牌」的粗略集合：剩余>0 且 非其已打出的牌。

        信息不完全，这是范围上限（偏宽），越近终局越应收窄。
        seat_discards: 该座位已打出的牌 id 集合。
        """
        out = []
        for tid in TILE_NAMES:
            if self.remaining_per_tile(played_counts, tid) <= 0:
                continue
            if int(tid) in set(int(x) for x in seat_discards):
                continue
            out.append(int(tid))
        return out
