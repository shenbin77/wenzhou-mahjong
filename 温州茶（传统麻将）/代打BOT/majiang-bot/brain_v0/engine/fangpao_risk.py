# -*- coding: utf-8 -*-
"""放炮风险（温州红中放炮）结构化打分。

与 DangerModel / EvEstimator 的关系
--------------------------------
- ``DangerModel.rate``：单张启发式危险度 0–100（已出张数 + 巡目抬升 + 中张加权）。
  本模块以其为 **base_danger**，并可选叠庄家/对手座位信息。
- ``OpponentModel.danger_to_all_opponents``：按座位持有概率 + 庄家加成，得到对各对手
  的点炮危险度。本模块取加权汇总写入 ``components``，并用
  ``max(base, dealer_weighted_max)`` 一类规则得到最终 ``fangpao_risk``。
- ``EvEstimator``：在危险度之上做「收益 − 损失」的 1 步 EV，用于排序弃牌。
  **放炮风险分 ≠ EV**：风险分只刻画「打出后被点炮」的危险侧；EV 还含进张收益。
  决策层可同时看 ``fangpao_risk``（避险）与 ``ev``（综合）。

红中财神（tile=55）打出风险定义
--------------------------------
温州红中放炮中红中为固定财神。Advisor 通常不打红中（除非手牌全是红中）。
本 API 对红中的 ``fangpao_risk``：
- 已出 4 张 → 0（与其它牌一致）；
- 否则复用 DangerModel 基底（无中张 mid boost，因 _tile_number(55)∉[3,7]），
  并在 components 中标记 ``is_joker=True``、``joker_note``。
- 含义：**若打出红中，对手以财神点炮胡的启发式危险度**（非「红中作普通序数牌」）。
  不额外臆造「财神必胡」满分；与普通牌同一套已出/巡目曲线，便于单测与交叉校验。
"""
from __future__ import annotations

from typing import Any

from .danger import DangerModel
from .opponent_model import OpponentModel, normalize_seat
from .tiles import CAISHEN_ID, TILE_NAMES

def _played_of(played_counts: dict | None, tile: int) -> int:
    if not played_counts:
        return 0
    return int(played_counts.get(int(tile), played_counts.get(str(tile), 0)))


def fangpao_risk(
    tile: int,
    played_counts: dict | None,
    turn_no: int,
    *,
    dealer_seat: str | int | None = None,
    seat_unseen: dict | None = None,
    open_info: Any = None,  # 预留：副露/听牌公开信息；v0 未用
    danger_model: DangerModel | None = None,
    opponent_model: OpponentModel | None = None,
) -> dict:
    """单张候选弃牌的放炮风险。

    Returns
    -------
    dict with keys:
      tile, name, fangpao_risk (0-100), level, components{...}
    """
    del open_info  # v0 hook：识牌对齐 open 信息后接入，勿假用
    tile = int(tile)
    played = min(max(_played_of(played_counts, tile), 0), 4)
    danger = danger_model or DangerModel()
    dealer = normalize_seat(dealer_seat)
    if opponent_model is None:
        opp = OpponentModel(danger_model=danger, dealer_seat=dealer)
    else:
        opp = opponent_model
        if dealer is not None and opp.dealer_seat is None:
            opp.dealer_seat = dealer

    base = float(danger.rate(tile, {tile: played} if not played_counts else
                             {int(k): int(v) for k, v in dict(played_counts).items()},
                             int(turn_no or 1)))

    by_seat = opp.danger_to_all_opponents(
        tile,
        {int(k): int(v) for k, v in dict(played_counts or {}).items()}
        if played_counts
        else {},
        int(turn_no or 1),
        seat_unseen,
    )
    dealer_weighted_max = max(by_seat.values()) if by_seat else 0.0
    dealer_weighted_sum = round(sum(by_seat.values()), 2) if by_seat else 0.0

    # 最终分：已出尽 → 0；否则取 base 与庄家加权座位最大危险的较大者（避险保守）
    if played >= 4:
        score = 0.0
    else:
        score = min(100.0, max(base, float(dealer_weighted_max)))

    is_joker = tile == CAISHEN_ID
    components = {
        "base_danger": round(base, 2),
        "played": played,
        "by_seat": by_seat,
        "dealer_seat": dealer or getattr(opp, "dealer_seat", None),
        "dealer_weighted_max": round(float(dealer_weighted_max), 2),
        "dealer_weighted_sum": dealer_weighted_sum,
        "is_joker": is_joker,
        "joker_note": (
            "红中财神：风险=对手以财神点炮胡的启发式危险度；已出4张则0"
            if is_joker
            else None
        ),
        "open_info_used": False,  # 钩子：未来接 open 听牌信息
    }
    level = danger.level(score)
    return {
        "tile": tile,
        "name": TILE_NAMES.get(tile, str(tile)),
        "fangpao_risk": round(score, 2),
        "level": level,
        "components": components,
    }


def rank_discards_by_fangpao_risk(
    hand: list[int],
    played_counts: dict | None,
    turn_no: int,
    *,
    dealer_seat: str | int | None = None,
    seat_unseen: dict | None = None,
    open_info: Any = None,
    danger_model: DangerModel | None = None,
    opponent_model: OpponentModel | None = None,
) -> list[dict]:
    """整手按放炮风险升序（越安全越靠前）；同风险按 tile id。"""
    danger = danger_model or DangerModel()
    dealer = normalize_seat(dealer_seat)
    opp = opponent_model or OpponentModel(danger_model=danger, dealer_seat=dealer)
    seen = set()
    rows = []
    for t in hand:
        t = int(t)
        if t in seen:
            continue
        seen.add(t)
        rows.append(
            fangpao_risk(
                t,
                played_counts,
                turn_no,
                dealer_seat=dealer,
                seat_unseen=seat_unseen,
                open_info=open_info,
                danger_model=danger,
                opponent_model=opp,
            )
        )
    rows.sort(key=lambda r: (r["fangpao_risk"], r["tile"]))
    return rows
