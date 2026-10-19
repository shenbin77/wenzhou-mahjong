# -*- coding: utf-8 -*-
"""
温州茶牌型分析器 — tile ID列表 → 胡牌标签。

输入: 17张牌ID列表 (胡牌时的手牌+进张)
输出: {hu: bool, tags: [...], hu_kind: str, multiplier: int, ...}

牌ID编码 (brain_v0/engine/tea/tiles.py):
  1-9: 万, 21-29: 筒, 41-49: 条
  61-64: 东南西北, 73: 白 (无中发)

注意: tile_vision用的白=67与引擎的白=73不一致，
本分析器使用引擎编码(73)。调用方需先转换：
  vision_id 67 → engine_id 73

复用引擎模块:
  - patterns.detect_pattern_tags: 牌型标签探测
  - kinds.classify_hu / multiplier_for: 软硬双翻判定
"""

from __future__ import annotations

from typing import Sequence

from .tiles import (
    counts_of, is_honor, is_numeric, suit_of,
    legal_tile_ids, assert_tiles_in_rules,
)
from .patterns import (
    detect_pattern_tags,
    is_8dui_shape,
    is_banqing,
    is_pengpeng_skeleton,
    is_qingyise_unordered,
    is_ruan_8dui,
    is_ying_8dui,
)
from .kinds import HuKind, classify_hu, multiplier_for, kind_label_zh


def _can_form_melds(counts: dict[int, int], jokers: int) -> bool:
    """递归检查剩余牌能否全部分解为面子(刻/顺)，财神可替。"""
    for tile in sorted(counts.keys()):
        c = counts[tile]
        if c <= 0:
            continue

        # 尝试刻子
        if c >= 3:
            counts[tile] -= 3
            if _can_form_melds(counts, jokers):
                counts[tile] += 3
                return True
            counts[tile] += 3

        # 财神补刻子
        if c >= 1 and jokers >= 1:
            need = 3 - c
            if 0 < need <= jokers:
                counts[tile] = 0
                if _can_form_melds(counts, jokers - need):
                    counts[tile] = c
                    return True
                counts[tile] = c

        # 尝试顺子 (仅数牌)
        if is_numeric(tile):
            s = suit_of(tile)
            t1, t2 = tile + 1, tile + 2
            if suit_of(t1) == s and suit_of(t2) == s:
                c1 = counts.get(t1, 0)
                c2 = counts.get(t2, 0)
                need1 = max(0, 1 - c1)
                need2 = max(0, 1 - c2)
                if c >= 1 and need1 + need2 <= jokers:
                    counts[tile] -= 1
                    if c1 > 0:
                        counts[t1] -= 1
                    if c2 > 0:
                        counts[t2] -= 1
                    for t in [tile, t1, t2]:
                        if counts.get(t, 0) <= 0:
                            counts.pop(t, None)
                    if _can_form_melds(counts, jokers - need1 - need2):
                        counts[tile] = counts.get(tile, 0) + 1
                        if c1 > 0:
                            counts[t1] = counts.get(t1, 0) + 1
                        if c2 > 0:
                            counts[t2] = counts.get(t2, 0) + 1
                        return True
                    counts[tile] = counts.get(tile, 0) + 1
                    if c1 > 0:
                        counts[t1] = counts.get(t1, 0) + 1
                    if c2 > 0:
                        counts[t2] = counts.get(t2, 0) + 1
        return False

    if jokers % 3 == 0:
        return True
    return False


def is_standard_win(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """标准胡: 1将 + 5面子 = 17张。财神可替。"""
    if len(tiles) != 17:
        return False
    real: dict[int, int] = {}
    jokers = 0
    for t in tiles:
        t = int(t)
        if caishen_id is not None and t == int(caishen_id):
            jokers += 1
        else:
            real[t] = real.get(t, 0) + 1

    pair_options = []
    for tile, c in real.items():
        if c >= 2:
            pair_options.append((tile, 2, 0))
        if c >= 1 and jokers >= 1:
            pair_options.append((tile, 1, 1))
    if jokers >= 2:
        pair_options.append((None, 0, 2))

    for tile, used_real, used_joker in pair_options:
        rem = dict(real)
        if tile is not None:
            rem[tile] -= used_real
            if rem[tile] <= 0:
                del rem[tile]
        jok_left = jokers - used_joker
        total_rem = sum(rem.values()) + jok_left
        if total_rem != 15:
            continue
        if _can_form_melds(rem, jok_left):
            return True
    return False


def is_winning_hand(
    tiles: Sequence[int],
    caishen_id: int | None = None,
) -> bool:
    """判断17张是否为胡牌形 (标准/八对/碰碰胡)。"""
    if len(tiles) != 17:
        return False
    if is_8dui_shape(tiles, caishen_id):
        return True
    if is_pengpeng_skeleton(tiles, caishen_id):
        return True
    if is_standard_win(tiles, caishen_id):
        return True
    return False


def analyze(
    tiles: Sequence[int],
    *,
    caishen_id: int | None = None,
    context_tags: Sequence[str] = (),
    no_zhong_fa: bool = True,
) -> dict:
    """
    牌型分析主入口。

    参数:
        tiles: 17张牌ID列表 (引擎编码：白=73)
        caishen_id: 财神牌ID，无财神为None
        context_tags: 情境标签，如 ["zimo", "gangshanghua", "no_caishen"]
        no_zhong_fa: 是否无中发 (默认True)

    返回:
        {
            "hu": bool,
            "tags": [...],
            "hu_kind": "ruan"|"ying"|"shuangfan",
            "hu_kind_zh": "软胡"|"硬胡"|"双翻",
            "multiplier": 1|2|4,
            "tile_count": int,
            "caishen_used": int,
        }
    """
    tiles = [int(t) for t in tiles]

    result: dict = {
        "hu": False,
        "tags": [],
        "hu_kind": HuKind.RUAN.value,
        "hu_kind_zh": kind_label_zh(HuKind.RUAN),
        "multiplier": 1,
        "tile_count": len(tiles),
        "caishen_used": 0,
    }

    try:
        assert_tiles_in_rules(tiles, no_zhong_fa=no_zhong_fa)
    except ValueError as e:
        result["error"] = str(e)
        return result

    if len(tiles) != 17:
        result["error"] = f"胡牌需17张，得到{len(tiles)}张"
        return result

    if caishen_id is not None:
        result["caishen_used"] = sum(1 for t in tiles if t == int(caishen_id))

    hu = is_winning_hand(tiles, caishen_id)
    result["hu"] = hu
    if not hu:
        return result

    tags = detect_pattern_tags(tiles, caishen_id=caishen_id, context_tags=context_tags)
    result["tags"] = tags

    all_tags = list(tags)
    if caishen_id is None or result["caishen_used"] == 0:
        if "no_caishen" not in all_tags:
            all_tags.append("no_caishen")

    kind = classify_hu(all_tags)
    result["hu_kind"] = kind.value
    result["hu_kind_zh"] = kind_label_zh(kind)
    result["multiplier"] = multiplier_for(kind)
    result["tags"] = all_tags

    return result


def quick_tags(tiles: Sequence[int], caishen_id: int | None = None) -> list[str]:
    """快捷：只返回标签列表。"""
    return analyze(tiles, caishen_id=caishen_id)["tags"]


def is_hu(tiles: Sequence[int], caishen_id: int | None = None) -> bool:
    """快捷：只判断是否胡牌。"""
    return analyze(tiles, caishen_id=caishen_id)["hu"]
