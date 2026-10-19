# -*- coding: utf-8 -*-
"""温州红中放炮 — 对外规则门面（薄封装）。

目标玩法语义（见 AGENTS.md / 桌面规则条文）:
- 红中 (tile_id=55) 为可替代任意牌的财神（赖子）
- 可胡七对
- 点炮胡（fangpao）与自摸（zimo）
- 庄闲算分（庄家自摸/庄家点炮或庄家胡点炮时单位翻倍）

本模块不重新实现向听/计分算法，而是收束 ``shanten`` / ``scoring`` /
``tiles`` 已有能力，便于识牌与决策侧只依赖一个入口。
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from .scoring import ScoreCalculator, detect_fans, settle_hand
from .shanten import calc_shanten, calc_ting_tiles, is_win
from .tiles import CAISHEN_ID, NUMERIC_TILES, TILE_NAMES, tile_name

JOKER_ID = CAISHEN_ID  # 红中财神
TARGET_TILE_IDS = frozenset(NUMERIC_TILES) | {CAISHEN_ID}


def is_joker(tile_id: int) -> bool:
    return int(tile_id) == JOKER_ID


def joker_count(tiles: Sequence[int]) -> int:
    return sum(1 for t in tiles if is_joker(t))


def hand_shanten(tiles: Sequence[int], *, use_qidui: bool = True, open_meld_count: int = 0) -> int:
    """向听数；默认计入七对路径。"""
    return calc_shanten(list(tiles), use_qidui=use_qidui, open_meld_count=open_meld_count)


def hand_is_win(tiles: Sequence[int], *, use_qidui: bool = True, open_meld_count: int = 0) -> bool:
    """14 张（或含副露等价张数）是否已胡。红中作赖子。"""
    return bool(is_win(list(tiles), use_qidui=use_qidui, open_meld_count=open_meld_count))


def ting_tiles(tiles13: Sequence[int], *, use_qidui: bool = True, open_meld_count: int = 0) -> list[int]:
    """听牌进张列表（含红中作赖子可听的情况）。"""
    return list(calc_ting_tiles(list(tiles13), use_qidui=use_qidui, open_meld_count=open_meld_count))


def fans_for_win(hand13: Sequence[int], win_tile: int, allow_context_fans: Iterable[str] = ()) -> list[str]:
    """自动可判定番型（含七对等）；情境番由 allow_context_fans 传入。"""
    return list(detect_fans(list(hand13), int(win_tile), allow_context_fans=tuple(allow_context_fans)))


def score_dianpao(
    winner_seat: int,
    dealer_seat: int,
    loser_seat: int,
    *,
    fan_types: Sequence[str] = (),
    base: int = 1,
    stacking: str = "multiply",
    use_zhuangxian: bool = True,
    gang_records: Sequence[Mapping] = (),
) -> dict[int, int]:
    """点炮胡结算，返回四家 delta（零和）。"""
    return settle_hand(
        "fangpao",
        winner_seat=winner_seat,
        dealer_seat=dealer_seat,
        loser_seat=loser_seat,
        fan_types=fan_types,
        base=base,
        stacking=stacking,
        use_zhuangxian=use_zhuangxian,
        gang_records=gang_records,
    )


def score_zimo(
    winner_seat: int,
    dealer_seat: int,
    *,
    fan_types: Sequence[str] = (),
    base: int = 1,
    stacking: str = "multiply",
    use_zhuangxian: bool = True,
    gang_records: Sequence[Mapping] = (),
) -> dict[int, int]:
    """自摸结算，返回四家 delta（零和）。"""
    return settle_hand(
        "zimo",
        winner_seat=winner_seat,
        dealer_seat=dealer_seat,
        fan_types=fan_types,
        base=base,
        stacking=stacking,
        use_zhuangxian=use_zhuangxian,
        gang_records=gang_records,
    )


def describe_tile(tile_id: int) -> str:
    return tile_name(int(tile_id))


__all__ = [
    "JOKER_ID",
    "TARGET_TILE_IDS",
    "ScoreCalculator",
    "describe_tile",
    "fans_for_win",
    "hand_is_win",
    "hand_shanten",
    "is_joker",
    "joker_count",
    "score_dianpao",
    "score_zimo",
    "ting_tiles",
]
