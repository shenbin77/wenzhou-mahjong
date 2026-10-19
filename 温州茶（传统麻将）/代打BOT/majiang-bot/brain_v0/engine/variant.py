# -*- coding: utf-8 -*-
"""玩法分发钩子: 茶 vs 红中放炮隔离。

识牌/回放管线按 variant + player_count 调用, 避免把抖音红中结算套到温州茶。
"""

from __future__ import annotations

from typing import Any

from . import wenzhou_rules
from .tea import VARIANT_ID as TEA_VARIANT
from .tea import settle_by_player_count as settle_tea_by_player_count
from .tea import settle_2p as tea_settle_2p
from .tea import settle_4p as tea_settle_4p

HONGZHONG_VARIANT = "wenzhou_hongzhong"
TEA_ALIASES = frozenset({TEA_VARIANT, "tea", "温州茶", "温州麻将茶"})
HONGZHONG_ALIASES = frozenset(
    {HONGZHONG_VARIANT, "hongzhong", "红中", "温州红中", "放炮麻将"}
)


def settle_for_variant(
    variant: str,
    *,
    player_count: int,
    win_type: str,
    winner_seat: int,
    dealer_seat: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """统一结算入口。

    - 温州茶 → engine.tea.settle_2p / settle_4p(按人数)
    - 红中放炮 → 既有 wenzhou_rules.score_*(仅 4 人点数制); 返回同形 dict
    """
    v = (variant or "").strip()
    if v in TEA_ALIASES or v.endswith("茶"):
        return settle_tea_by_player_count(
            player_count,
            win_type=win_type,
            winner_seat=winner_seat,
            dealer_seat=dealer_seat,
            **kwargs,
        )
    if v in HONGZHONG_ALIASES or "红中" in v:
        if win_type == "zimo":
            deltas = wenzhou_rules.score_zimo(
                winner_seat,
                dealer_seat,
                fan_types=kwargs.get("fan_types", ()),
                base=kwargs.get("base", 1),
                gang_records=kwargs.get("gang_records", ()),
            )
        elif win_type == "fangpao":
            deltas = wenzhou_rules.score_dianpao(
                winner_seat,
                dealer_seat,
                kwargs["loser_seat"],
                fan_types=kwargs.get("fan_types", ()),
                base=kwargs.get("base", 1),
                gang_records=kwargs.get("gang_records", ()),
            )
        elif win_type == "draw":
            from .scoring import settle_hand

            deltas = settle_hand(
                "draw",
                winner_seat=winner_seat,
                dealer_seat=dealer_seat,
                gang_records=kwargs.get("gang_records", ()),
            )
        else:
            raise ValueError(f"未知 win_type: {win_type}")
        return {
            "deltas": deltas,
            "variant": HONGZHONG_VARIANT,
            "player_count": 4,
        }
    raise ValueError(f"未知 variant: {variant!r} (茶请用 {TEA_VARIANT!r})")


__all__ = [
    "HONGZHONG_VARIANT",
    "TEA_ALIASES",
    "TEA_VARIANT",
    "settle_for_variant",
    "tea_settle_2p",
    "tea_settle_4p",
]
