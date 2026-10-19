# -*- coding: utf-8 -*-
"""温州茶 — 牌编码与牌墙。

数字牌沿用 brain_v0 阿法编码: 1-9万 / 21-29筒 / 41-49条。
字牌扩展: 61-64 东南西北, 71中 72发 73白。
财神为翻牌决定的具体牌 id(白板替身表示财神本尊)。
默认桌选项「无中发」: 牌墙不含 71/72。
"""

from __future__ import annotations

from typing import Iterable, Sequence

# 数字
WAN = tuple(range(1, 10))
TONG = tuple(range(21, 30))
TIAO = tuple(range(41, 50))
NUMERIC = WAN + TONG + TIAO

# 风 / 箭
EAST, SOUTH, WEST, NORTH = 61, 62, 63, 64
WINDS = (EAST, SOUTH, WEST, NORTH)
ZHONG, FA, BAI = 71, 72, 73
DRAGONS_FULL = (ZHONG, FA, BAI)
DRAGONS_NO_ZHONG_FA = (BAI,)  # 无中发

TILE_NAMES = {
    **{n: f"{n}万" for n in WAN},
    **{n: f"{n - 20}筒" for n in TONG},
    **{n: f"{n - 40}条" for n in TIAO},
    EAST: "东",
    SOUTH: "南",
    WEST: "西",
    NORTH: "北",
    ZHONG: "中",
    FA: "发",
    BAI: "白",
}

COPIES_PER_TILE = 4


def tile_name(tile_id: int) -> str:
    return TILE_NAMES.get(int(tile_id), f"?{tile_id}")


def suit_of(tile_id: int) -> str:
    t = int(tile_id)
    if 1 <= t <= 9:
        return "m"
    if 21 <= t <= 29:
        return "p"
    if 41 <= t <= 49:
        return "s"
    if t in WINDS:
        return "w"
    if t in DRAGONS_FULL:
        return "d"
    return "?"


def is_honor(tile_id: int) -> bool:
    return suit_of(tile_id) in ("w", "d")


def is_numeric(tile_id: int) -> bool:
    return suit_of(tile_id) in ("m", "p", "s")


def legal_tile_ids(*, no_zhong_fa: bool = True) -> frozenset[int]:
    dragons = DRAGONS_NO_ZHONG_FA if no_zhong_fa else DRAGONS_FULL
    return frozenset(NUMERIC + WINDS + dragons)


def build_deck(*, no_zhong_fa: bool = True) -> list[int]:
    """构造完整牌墙(每牌 4 张)。无中发时总数 128; 含中发时 136。"""
    ids = sorted(legal_tile_ids(no_zhong_fa=no_zhong_fa))
    deck: list[int] = []
    for tid in ids:
        deck.extend([tid] * COPIES_PER_TILE)
    return deck


def deck_size(*, no_zhong_fa: bool = True) -> int:
    return len(build_deck(no_zhong_fa=no_zhong_fa))


def assert_tiles_in_rules(tiles: Iterable[int], *, no_zhong_fa: bool = True) -> None:
    legal = legal_tile_ids(no_zhong_fa=no_zhong_fa)
    bad = [int(t) for t in tiles if int(t) not in legal]
    if bad:
        raise ValueError(f"牌不在当前桌规则内(no_zhong_fa={no_zhong_fa}): {bad}")


def counts_of(tiles: Sequence[int]) -> dict[int, int]:
    d: dict[int, int] = {}
    for t in tiles:
        t = int(t)
        d[t] = d.get(t, 0) + 1
    return d
