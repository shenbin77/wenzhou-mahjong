# -*- coding: utf-8 -*-
"""牌定义。编码对齐阿法算牌 API:1-9万 / 21-29筒 / 41-49条 / 55红中(固定财神)。"""

CAISHEN_ID = 55
TOTAL_PER_TILE = 4
DECK_TILE_COUNT = 112

TILE_NAMES = {}
for _n in range(1, 10):
    TILE_NAMES[_n] = f"{_n}万"
for _n in range(21, 30):
    TILE_NAMES[_n] = f"{_n-20}筒"
for _n in range(41, 50):
    TILE_NAMES[_n] = f"{_n-40}条"
TILE_NAMES[CAISHEN_ID] = "红中(财神)"

NUMERIC_TILES = sorted(t for t in TILE_NAMES if t != CAISHEN_ID)

SUIT_RANGES = {
    "m": (1, 9),
    "p": (21, 29),
    "s": (41, 49),
}


def all_numeric_tiles():
    """27 种数字牌各 4 张,共 108 张;加 4 张红中 = 112。"""
    deck = []
    for t in NUMERIC_TILES:
        deck.extend([t] * TOTAL_PER_TILE)
    deck.extend([CAISHEN_ID] * TOTAL_PER_TILE)
    return deck


def tile_name(t):
    return TILE_NAMES.get(int(t), f"?{t}")


def same_suit(a, b):
    for lo, hi in SUIT_RANGES.values():
        if lo <= a <= hi and lo <= b <= hi:
            return True
    return False


def is_straight_candidate(t):
    """该牌能否作为顺子中间牌(存在 t-1,t,t+1 且不跨花色边界)。"""
    for lo, hi in SUIT_RANGES.values():
        if lo + 1 <= t <= hi - 1:
            return True
    return False


def split_by_suit(counts):
    """把 {tile: count} 拆成三门花色的独立计数 dict。"""
    suits = {"m": {}, "p": {}, "s": {}}
    for t, c in counts.items():
        if c == 0:
            continue
        if 1 <= t <= 9:
            suits["m"][t] = c
        elif 21 <= t <= 29:
            suits["p"][t - 20] = c
        elif 41 <= t <= 49:
            suits["s"][t - 40] = c
    return suits


def counts_of(tiles):
    d = {}
    for t in tiles:
        d[t] = d.get(t, 0) + 1
    return d
