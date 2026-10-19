# -*- coding: utf-8 -*-
"""温州茶 — 一炮一响: 弃牌者和牌时逆时针优先。

座位编号约定: 0..n-1 按逆时针排列(与回放座位序一致即可)。
从出牌者的下家起, 逆时针扫描, 第一个在 winners 集合中的座位胜出。
"""

from __future__ import annotations

from typing import Iterable, Sequence


def counterclockwise_seats(from_seat: int, player_count: int) -> list[int]:
    if player_count < 2:
        raise ValueError("player_count >= 2")
    if not (0 <= from_seat < player_count):
        raise ValueError("from_seat 越界")
    return [(from_seat + offset) % player_count for offset in range(1, player_count)]


def pick_discard_winner(
    discarder_seat: int,
    claiming_seats: Sequence[int] | Iterable[int],
    *,
    player_count: int,
) -> int | None:
    """多人同时胡同一张弃牌时, 返回逆时针优先者; 无人则 None。"""
    claims = {int(s) for s in claiming_seats}
    if discarder_seat in claims:
        raise ValueError("出牌者不能同时是胡牌宣称者")
    for seat in counterclockwise_seats(int(discarder_seat), player_count):
        if seat in claims:
            return seat
    return None
