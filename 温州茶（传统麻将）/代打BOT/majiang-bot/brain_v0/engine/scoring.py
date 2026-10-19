# -*- coding: utf-8 -*-
"""官方计分器(02_游戏规则对齐.md)。

自摸:闲家各输2、胡者赢6;庄家自摸闲家各输3。
接炮:点炮方输1;点炮方为庄或胡者为庄时变 2。
七小对/对对胡/清一色/杠上花/单吊/天胡/地胡 均 2 倍;
多倍型叠加方式官方未明确(相乘/相加/取最高),配置化,默认相乘,待实战验证后调整。
杠分独立结算:暗杠每人2、补杠每人1、直杠放杠者3。
"""

from .tiles import CAISHEN_ID

DOUBLE_FANS = ("qidui", "duidui", "qingyise", "gangshanghua", "dandiao", "tianhu", "dihu")

_CONTEXT_ONLY_FANS = ("gangshanghua", "dandiao", "tianhu", "dihu")


class ScoreCalculator:
    def __init__(self, stacking="multiply", use_zhuangxian=True):
        if stacking not in ("multiply", "add", "max"):
            raise ValueError(f"未知倍型叠加方式 {stacking}")
        self.stacking = stacking
        self.use_zhuangxian = use_zhuangxian

    def hu_multiplier(self, fan_types):
        fans = [f for f in fan_types if f in DOUBLE_FANS]
        n = len(fans)
        if n == 0:
            return 1
        if self.stacking == "multiply":
            return 2 ** n
        if self.stacking == "add":
            return 1 + 2 * n
        return 2


def _counts(tiles):
    d = {}
    for t in tiles:
        d[t] = d.get(t, 0) + 1
    return d


def _same_suit_group(t):
    if 1 <= t <= 9:
        return "m"
    if 21 <= t <= 29:
        return "p"
    if 41 <= t <= 49:
        return "s"
    return "?"


def _triplets_only_decomposition(counts, jokers):
    """能否由四组刻子和一对组成，财神可以补任意位置。"""
    if sum(counts.values()) + jokers != 14:
        return False

    pair_choices = []
    for tile, count in counts.items():
        if count >= 2:
            pair_choices.append((tile, 2, 0))
        if count >= 1 and jokers >= 1:
            pair_choices.append((tile, 1, 1))
    if jokers >= 2:
        pair_choices.append((None, 0, 2))

    for tile, real_used, joker_used in pair_choices:
        remaining = dict(counts)
        if tile is not None:
            remaining[tile] -= real_used
        jok = jokers - joker_used
        needed = sum((3 - count % 3) % 3 for count in remaining.values())
        if needed > jok:
            continue
        groups = sum((count + (3 - count % 3) % 3) // 3 for count in remaining.values())
        spare = jok - needed
        if spare % 3 == 0 and groups + spare // 3 == 4:
            return True
    return False


def detect_fans(hand13, win_tile, allow_context_fans=()):
    """从 胡牌前13张 + 进张 推断可自动判定的 2 倍牌型。

    返回 fans 列表,可能含 qidui/duidui/qingyise/dandiao;
    gangshanghua/tianhu/dihu 属局面信息,由 allow_context_fans 显式传入。
    """
    from .shanten import calc_shanten, calc_ting_tiles

    h13 = [int(t) for t in hand13]
    wt = int(win_tile)
    full = h13 + [wt]
    k = full.count(CAISHEN_ID)
    real = [t for t in full if t != CAISHEN_ID]
    fans = []

    counts = _counts(real)
    suits = {_same_suit_group(t) for t in real}
    single_suit = suits == {"m"} or suits == {"p"} or suits == {"s"}

    if single_suit and real:
        fans.append("qingyise")
    if _qidui_shape(counts, k):
        fans.append("qidui")
    elif _triplets_only_decomposition(counts, k):
        fans.append("duidui")

    for f in allow_context_fans:
        if f in _CONTEXT_ONLY_FANS:
            fans.append(f)
    return fans


def _qidui_shape(counts, jokers):
    """十四张暗手能否成七对;四同算两对,财神先补单张再自成对。"""
    pairs = sum(c // 2 for c in counts.values())
    singles = sum(c % 2 for c in counts.values())
    total = sum(counts.values()) + jokers
    if total != 14:
        return False
    filled_singles = min(jokers, singles)
    usable_pairs = pairs + filled_singles + (jokers - filled_singles) // 2
    return usable_pairs >= 7


def _no_sequence_possible(counts):
    """清一色内是否存在顺子分解可能——保守判定:存在同花色三连张即可顺。"""
    for t in counts:
        base = t if t <= 9 else (21 if t <= 29 else 41)
        pos = t - base + 1
        if pos <= 7 and counts.get(t, 0) and counts.get(t + 1, 0) and counts.get(t + 2, 0):
            return False
    return True


def settle_hand(win_type, winner_seat, dealer_seat, loser_seat=None,
                fan_types=(), base=1, stacking="multiply", use_zhuangxian=True,
                gang_records=()):
    """一手牌完整结算:胡分+杠分。返回 {seat: delta},保证零和。"""
    calc = ScoreCalculator(stacking=stacking, use_zhuangxian=use_zhuangxian)
    mult = base * calc.hu_multiplier(fan_types)
    deltas = {i: 0 for i in range(4)}

    seats = set(range(4))
    if dealer_seat not in seats:
        raise ValueError("dealer_seat 必须在0..3")
    if win_type != "draw" and winner_seat not in seats:
        raise ValueError("winner_seat 必须在0..3")
    w_is_d = winner_seat == dealer_seat
    if win_type == "draw":
        pass
    elif win_type == "zimo":
        per = (3 if (use_zhuangxian and w_is_d) else 2) * mult
        deltas[winner_seat] += per * 3
        for s in range(4):
            if s != winner_seat:
                deltas[s] -= per
    elif win_type == "fangpao":
        if loser_seat is None:
            raise ValueError("fangpao 必须提供 loser_seat")
        if loser_seat not in seats:
            raise ValueError("loser_seat 必须在0..3")
        if loser_seat == winner_seat:
            raise ValueError("点炮者与胡者是同一人")
        unit = 2 if (use_zhuangxian and (w_is_d or loser_seat == dealer_seat)) else 1
        pay = unit * mult
        deltas[loser_seat] -= pay
        deltas[winner_seat] += pay
    else:
        raise ValueError(f"未知胡牌方式 {win_type}")

    for g in gang_records:
        gt, gs = g["type"], g["seat"]
        if gt == "an":
            for s in range(4):
                if s != gs:
                    deltas[s] -= 2
                    deltas[gs] += 2
        elif gt == "bu":
            for s in range(4):
                if s != gs:
                    deltas[s] -= 1
                    deltas[gs] += 1
        elif gt == "zhi":
            fs = g.get("from_seat")
            if fs is None or fs == gs:
                raise ValueError("直杠需提供合法 from_seat")
            deltas[fs] -= 3
            deltas[gs] += 3
        else:
            raise ValueError(f"未知杠类型 {gt}")

    if sum(deltas.values()) != 0:
        raise AssertionError(f"结算非零和: {deltas}")
    return deltas
