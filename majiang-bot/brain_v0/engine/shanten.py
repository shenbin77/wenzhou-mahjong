# -*- coding: utf-8 -*-
"""向听数/听牌/胡牌判定。红中为固定财神(百搭),手中红中全部按 joker 处理。

算法:按花色分解实牌,直接记录补成面子/搭子/雀头所需的财神数。
合并三门花色的有限状态,再分配纯财神组,避免枚举 27 种替代牌。
open_meld_count 表示已经固定的碰/杠面子数,不把副露重新拆成顺子。
"""

from collections import Counter
from functools import lru_cache

from .tiles import CAISHEN_ID, NUMERIC_TILES, SUIT_RANGES, split_by_suit
from ._suit_decomp import suit_decompositions
_suit_decompositions = suit_decompositions  # alias for external compat


@lru_cache(maxsize=300000)
def _standard_shanten_plain(tiles_tuple, open_meld_count=0):
    """无财神标准型向听。tiles_tuple 为排序后实牌元组（不含财神）。"""
    counts = {}
    for t in tiles_tuple:
        counts[t] = counts.get(t, 0) + 1
    n = len(tiles_tuple)
    suits = split_by_suit(counts)

    best = 8
    # 单花色分解结果缓存: list of (melds, partials, has_pair)
    per_suit_opts = []
    for sc in suits.values():
        if not sc:
            continue
        opts = suit_decompositions(sc)  # {(m, p): max_pair}
        entries = [(m, p, pr > 0) for (m, p), pr in opts.items()]
        per_suit_opts.append(entries)

    # 跨花色笛卡尔积（花色最多 3 个，每花色选项有限）
    def combine(idx, melds, partials, pair):
        nonlocal best
        if idx == len(per_suit_opts):
            rem = 4 - melds
            take = min(partials, rem)
            s = 8 - 2 * melds - take
            # 标准型向听公式中的雀头独立扣 1 分。搭子已按剩余面子槽
            # 截断，不能因为恰好填满最后一个槽就错误丢弃雀头。
            if pair:
                s -= 1
            if n == 13 - 3 * open_meld_count and s < 0:
                s = 0
            if s < best:
                best = s
            return
        for m, p, pr in per_suit_opts[idx]:
            nm, np_ = melds + m, partials + p
            if nm <= 4 and np_ + nm <= 5:  # 块数(面子+搭子+对子位)≤5 的粗筛
                combine(idx + 1, nm, np_, pair or pr)

    combine(0, open_meld_count, 0, False)
    return best


def _qidui_shanten_plain(tiles_tuple):
    """无财神七对向听。"""
    counts = {}
    for t in tiles_tuple:
        counts[t] = counts.get(t, 0) + 1
    n = len(tiles_tuple)
    pairs = sum(c // 2 for c in counts.values())
    s = 6 - pairs
    if n == 13 and s < 0:
        s = 0
    return s


def _validate_open_meld_count(open_meld_count):
    if isinstance(open_meld_count, bool) or not isinstance(open_meld_count, int):
        raise ValueError("open_meld_count 必须是0..4的整数")
    if not 0 <= open_meld_count <= 4:
        raise ValueError("open_meld_count 必须在0..4")
    return open_meld_count


def validate_tiles(tiles, open_meld_count=0):
    open_meld_count = _validate_open_meld_count(open_meld_count)
    hand = [int(t) for t in tiles]
    valid = set(NUMERIC_TILES) | {CAISHEN_ID}
    invalid = sorted(set(hand) - valid)
    if invalid:
        raise ValueError(f"非法牌编码: {invalid}")
    maximum = 14 - 3 * open_meld_count
    if len(hand) > maximum:
        raise ValueError(f"手牌不能超过{maximum}张,得到{len(hand)}张")
    over = {t: c for t, c in Counter(hand).items() if c > 4}
    if over:
        raise ValueError(f"同一种牌最多4张: {over}")
    return hand


@lru_cache(maxsize=100000)
def _wild_suit_options(counts, joker_budget):
    """返回 (财神使用数, 面子数, 搭子数, 雀头) 的有限可行状态。

    每次取最小实牌,可丢弃或放入一个组;顺子允许它位于任一位置,
    缺牌直接消耗财神。有实牌的邻位也可选财神,保留实牌给其他组。
    纯财神组在三门合并后统一分配,因此不会重复消耗财神。
    """
    try:
        first = next(i for i, count in enumerate(counts) if count)
    except StopIteration:
        return ((0, 0, 0, 0),)
    branches = []

    def add_branch(consumed, cost, meld=0, partial=0, pair=0):
        if cost > joker_budget:
            return
        remaining = list(counts)
        for index, amount in consumed:
            remaining[index] -= amount
        branches.append((tuple(remaining), cost, meld, partial, pair))

    # Ignore exactly one real tile; remaining copies may still form other groups.
    add_branch(((first, 1),), 0)
    for real_used in range(1, min(3, counts[first]) + 1):
        add_branch(((first, real_used),), 3 - real_used, meld=1)
    for real_used in range(1, min(2, counts[first]) + 1):
        cost = 2 - real_used
        add_branch(((first, real_used),), cost, partial=1)
        add_branch(((first, real_used),), cost, pair=1)
    for offset in (1, 2):
        other = first + offset
        if other < 9 and counts[other]:
            add_branch(((first, 1), (other, 1)), 0, partial=1)
    for start in range(max(0, first - 2), min(first, 6) + 1):
        others = [i for i in range(start, start + 3) if i != first]
        assignments = [(((first, 1),), 0)]
        for other in others:
            choices = []
            for consumed, cost in assignments:
                if counts[other]:
                    choices.append((consumed + ((other, 1),), cost))
                if cost < joker_budget:
                    choices.append((consumed, cost + 1))
            assignments = choices
        for consumed, cost in assignments:
            add_branch(consumed, cost, meld=1)

    # For equal cost / meld / partial counts, retaining a pair always dominates.
    states = {}
    for remaining, cost, meld, partial, pair in branches:
        for used, m, p, head in _wild_suit_options(remaining, joker_budget - cost):
            nm = m + meld
            if nm > 4:
                continue
            np = min(4 - nm, p + partial)
            key = (used + cost, nm, np)
            states[key] = max(states.get(key, 0), head or pair)
    return tuple((*key, head) for key, head in states.items())


def _standard_shanten_wild(real_key, jokers, open_meld_count):
    counts = Counter(real_key)
    suits = split_by_suit(counts)
    states = {(0, open_meld_count, 0): 0}
    for suit in suits.values():
        if not suit:
            continue
        vector = tuple(suit.get(i, 0) for i in range(1, 10))
        combined = {}
        for (used, melds, partials), head in states.items():
            for cost, m, p, pair in _wild_suit_options(vector, jokers):
                nm = melds + m
                if used + cost > jokers or nm > 4:
                    continue
                key = (used + cost, nm, min(4 - nm, partials + p))
                combined[key] = max(combined.get(key, 0), head or pair)
        states = combined
    best = 8
    for (used, melds, partials), head in states.items():
        spare = jokers - used
        for extra_melds in range(min(4 - melds, spare // 3) + 1):
            m = melds + extra_melds
            remaining = spare - 3 * extra_melds
            for extra_pair in range(1 + int(not head and remaining >= 2)):
                p = min(4 - m, partials + (remaining - 2 * extra_pair) // 2)
                best = min(best, 8 - 2 * m - p - int(head or extra_pair))
    return best


@lru_cache(maxsize=100000)
def _calc_shanten_cached(real_key, k, use_qidui, open_meld_count=0):
    best = (
        _standard_shanten_wild(real_key, k, open_meld_count)
        if k else _standard_shanten_plain(real_key, open_meld_count)
    )
    if use_qidui and not open_meld_count:
        counts = Counter(real_key)
        pairs = sum(count // 2 for count in counts.values())
        filled_singles = min(k, sum(count % 2 for count in counts.values()))
        best = min(best, 6 - pairs - filled_singles - (k - filled_singles) // 2)
    if len(real_key) + k == 13 - 3 * open_meld_count:
        best = max(0, best)
    return best


def calc_shanten(tiles, use_qidui=True, open_meld_count=0):
    """向听数。-1=胡牌;13 张下限 0。tiles 含红中即按财神计。"""
    hand = validate_tiles(tiles, open_meld_count)
    if not hand:
        # 空手牌无意义：返回 0 与 is_win([]) == False 保持一致（保守下界），
        # 避免 _calc_shanten_cached 在 real 为空时返回无意义的 99。
        return 0
    k = hand.count(CAISHEN_ID)
    real = tuple(sorted(t for t in hand if t != CAISHEN_ID))
    return _calc_shanten_cached(real, k, bool(use_qidui), open_meld_count)


def is_win(tiles, use_qidui=True, open_meld_count=0):
    return calc_shanten(tiles, use_qidui=use_qidui, open_meld_count=open_meld_count) == -1


def calc_ting_tiles(tiles, use_qidui=True, open_meld_count=0):
    """听牌集合，包含可以作为进张的红中财神。"""
    hand = validate_tiles(tiles, open_meld_count)
    expected = 13 - 3 * open_meld_count
    if len(hand) != expected:
        # 听牌只在 13 张时定义：14 张已是"打哪张"的待定状态，
        # hand+[t] 会变 15 张触发 validate_tiles 抛错，故前置校验直接报错。
        raise ValueError(f"calc_ting_tiles 仅接受{expected}张手牌")
    ting = []
    for t in list(NUMERIC_TILES) + [CAISHEN_ID]:
        if hand.count(t) >= 4:
            continue
        if is_win(hand + [t], use_qidui=use_qidui, open_meld_count=open_meld_count):
            ting.append(t)
    return ting


def calc_ukeire(tiles, visible_counts=None, use_qidui=True, open_meld_count=0):
    """返回能降低向听数的进张及其可见信息下剩余张数。"""
    hand = validate_tiles(tiles, open_meld_count)
    current = calc_shanten(hand, use_qidui, open_meld_count)
    visible = Counter(hand)
    if visible_counts:
        for tile, count in visible_counts.items():
            visible[int(tile)] += max(0, int(count))
    waits = {}
    for tile in list(NUMERIC_TILES) + [CAISHEN_ID]:
        remaining = max(0, 4 - visible[tile])
        if remaining and calc_shanten(hand + [tile], use_qidui, open_meld_count) < current:
            waits[tile] = remaining
    return waits


def discard_analysis(tiles, played_counts=None, use_qidui=True, open_meld_count=0):
    """14张手牌的弃牌效率明细，先最小向听，再最大化有效进张数。"""
    hand = validate_tiles(tiles, open_meld_count)
    expected = 14 - 3 * open_meld_count
    if len(hand) != expected:
        raise ValueError(f"需要 {expected} 张,得到 {len(hand)}")
    candidates = []
    seen = set()
    for i, t in enumerate(hand):
        if t in seen:
            continue
        seen.add(t)
        rest = hand[:i] + hand[i + 1:]
        sh_after = calc_shanten(rest, use_qidui, open_meld_count)
        candidates.append((t, rest, sh_after))
    min_shanten = min(shanten for _, _, shanten in candidates)
    rows = []
    for t, rest, sh_after in candidates:
        # 向听更差的弃牌不可能跨越1000分层级，跳过昂贵的进张枚举。
        ukeire = (
            calc_ukeire(rest, played_counts, use_qidui, open_meld_count)
            if sh_after == min_shanten else {}
        )
        total = sum(ukeire.values())
        rows.append({
            "tile": t,
            "shanten_after": sh_after,
            "ukeire": ukeire,
            "ukeire_count": total,
            "eff_score": -1000 * sh_after + total,
        })
    rows.sort(key=lambda row: (row["shanten_after"], -row["ukeire_count"], row["tile"]))
    return rows


def best_discard_by_efficiency(tiles, use_qidui=True, played_counts=None, open_meld_count=0):
    """兼容接口：返回 ``[(score, tile)]``，score 越大越优。"""
    return [
        (row["eff_score"], row["tile"])
        for row in discard_analysis(tiles, played_counts, use_qidui, open_meld_count)
    ]
