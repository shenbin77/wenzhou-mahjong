# -*- coding: utf-8 -*-
"""温州茶 — 牌型探测(八对 / 半清 / 清一色无序 / 碰碰胡骨架)。

完整向听/标准型分解仍可后续接到 shanten; 此处先覆盖计分与胡种分类所需的可判形状。
温州茶为 16 张制(闲家起手 16, 庄 17 摸打), 胡牌手牌+进张合计 17 张:
  - 标准型: 1 将 + 5 面子
  - 八对: 8 对 + 1 单张 = 17
"""

from __future__ import annotations

from typing import Sequence

from .tiles import BAI, counts_of, is_honor, is_numeric, suit_of


def _joker_split(tiles: Sequence[int], caishen_id: int | None) -> tuple[dict[int, int], int]:
    """把财神张数拆出。白板在翻出财神为本尊时由调用方已替换为 caishen_id。"""
    real: dict[int, int] = {}
    jokers = 0
    for t in tiles:
        t = int(t)
        if caishen_id is not None and t == int(caishen_id):
            jokers += 1
        else:
            real[t] = real.get(t, 0) + 1
    return real, jokers


def is_8dui_shape(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """17 张能否成 8 对 + 1 单。四同计两对。财神先补单再自成对。"""
    if len(tiles) != 17:
        return False
    counts, jokers = _joker_split(tiles, caishen_id)
    pairs = sum(c // 2 for c in counts.values())
    singles = sum(c % 2 for c in counts.values())
    # 八对形: 8 对 + 1 单 → pairs 目标 8, 另留 1 单
    # 总张 = 16(对) + 1 = 17
    filled = min(jokers, singles)
    jokers_left = jokers - filled
    singles_left = singles - filled
    pairs_total = pairs + filled + jokers_left // 2
    jokers_left %= 2
    # 剩余单张 + 残余财神 应恰好凑成「1 只单吊头」
    leftover = singles_left + jokers_left
    return pairs_total >= 8 and leftover == 1 and pairs_total + (1 if leftover else 0) >= 9


def is_ruan_8dui(tiles: Sequence[int], caishen_id: int | None) -> bool:
    """软 8 对: 成形且至少用一张财神替牌(非归位场景由上层标 guiwei)。"""
    if caishen_id is None:
        return False
    counts, jokers = _joker_split(tiles, caishen_id)
    if jokers <= 0:
        return False
    return is_8dui_shape(tiles, caishen_id)


def is_ying_8dui(tiles: Sequence[int], caishen_id: int | None) -> bool:
    """硬 8 对(双翻): 无财神成形; 或财神仅作单吊头 B; 或两财神成形(公开规则算双翻)。"""
    if len(tiles) != 17:
        return False
    counts, jokers = _joker_split(tiles, caishen_id)
    if jokers == 0:
        return is_8dui_shape(tiles, None)
    # 财神只做单吊头: 实牌恰好 8 对
    if (
        jokers == 1
        and all(c % 2 == 0 for c in counts.values())
        and sum(c // 2 for c in counts.values()) == 8
    ):
        return True
    # 两财神且整体成 8 对形 → 双翻硬 8 对
    if jokers >= 2 and is_8dui_shape(tiles, caishen_id):
        return True
    return False


def is_banqing(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """半清: 一种数牌花色 + 风字牌(可含财神)。须含至少一张字牌且只有一门数牌。"""
    suits = set()
    has_honor = False
    for t in tiles:
        t = int(t)
        if caishen_id is not None and t == int(caishen_id):
            continue
        if is_honor(t):
            has_honor = True
            continue
        if is_numeric(t):
            suits.add(suit_of(t))
    return has_honor and len(suits) == 1


def is_qingyise_unordered(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """清一色(无序): 仅一门数牌(可含财神), 无字牌; 不要求标准面子成形即可胡。"""
    suits = set()
    for t in tiles:
        t = int(t)
        if caishen_id is not None and t == int(caishen_id):
            continue
        if is_honor(t):
            return False
        if is_numeric(t):
            suits.add(suit_of(t))
        else:
            return False
    return len(suits) == 1


def is_pengpeng_skeleton(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """碰碰胡骨架: 1 将 + 5 刻(含碰), 17 张。财神可补刻/将。"""
    if len(tiles) != 17:
        return False
    counts, jokers = _joker_split(tiles, caishen_id)
    # 枚举将
    pair_opts: list[tuple[int | None, int, int]] = []
    for tile, c in counts.items():
        if c >= 2:
            pair_opts.append((tile, 2, 0))
        if c >= 1 and jokers >= 1:
            pair_opts.append((tile, 1, 1))
    if jokers >= 2:
        pair_opts.append((None, 0, 2))
    for tile, used, j_used in pair_opts:
        rem = dict(counts)
        if tile is not None:
            rem[tile] -= used
            if rem[tile] == 0:
                del rem[tile]
        jok = jokers - j_used
        need = 0
        groups = 0
        for c in rem.values():
            r = c % 3
            if r == 1:
                need += 2
            elif r == 2:
                need += 1
            groups += (c + (3 - c % 3) % 3) // 3
        if need > jok:
            continue
        spare = jok - need
        if spare % 3 == 0 and groups + spare // 3 == 5:
            return True
    return False


def detect_pattern_tags(
    tiles: Sequence[int],
    *,
    caishen_id: int | None = None,
    context_tags: Sequence[str] = (),
) -> list[str]:
    """自动可判标签 + 情境标签透传。"""
    tags: list[str] = []
    if is_ying_8dui(tiles, caishen_id):
        tags.append("ying8dui")
    elif is_ruan_8dui(tiles, caishen_id):
        tags.append("ruan8dui")
    if is_qingyise_unordered(tiles, caishen_id):
        tags.append("qingyise_unordered")
    elif is_banqing(tiles, caishen_id):
        tags.append("banqing")
    if is_pengpeng_skeleton(tiles, caishen_id):
        # 无财神或全部归位才标硬碰碰 → 双翻; 简化: 无财神 → pengpeng_hard
        _, jokers = _joker_split(tiles, caishen_id)
        if jokers == 0:
            tags.append("pengpeng_hard")
        else:
            tags.append("pengpeng_soft")
    for t in context_tags:
        if t and t not in tags:
            tags.append(str(t))
    return tags
