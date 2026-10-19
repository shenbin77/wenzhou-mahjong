# -*- coding: utf-8 -*-
"""温州茶 — 结算(2p/4p 钩子)。

核心公式(对齐公开进阶分 + 用户确认桌选项):
  单位分 = 底分(进阶2468) × 软1/硬2/双翻4
  自摸: 每位输家付 单位分; 庄家相关倍率已含在底分进阶里(连庄抬底)
  点炮: 放炮者付 单位分×2(点炮加倍); 其余人不付牌型分
  2 人: 仅两家互付; 无三背、无跟风
  4 人: 可叠加杠分、财神钱、跟风、三背承包

杠(杠算分开启时):
  明/直杠: 放杠者付 1(默认单位, 与底分无关的「分」)
  补杠: 每人付 1
  暗杠: 每人付 2
  (2p 时「每人」= 唯一对手)

三背(仅 4p 且开启): 庄胜时, 达三背的闲家承包其余两闲应付的牌型分
  (不含杠分/财神钱/跟风)。本模块接受已判定的 sanbei_seat。

跟风(仅 4p 且开启): 传入 genfeng_records[{payer, per_opponent_score}]。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import TeaTableConfig, default_config_2p, default_config_4p
from .kinds import HuKind, classify_hu, multiplier_for


def _empty_deltas(n: int) -> dict[int, int]:
    return {i: 0 for i in range(n)}


def _assert_zero_sum(deltas: Mapping[int, int]) -> None:
    if sum(deltas.values()) != 0:
        raise AssertionError(f"结算非零和: {dict(deltas)}")


def resolve_hu_kind(
    tags: Sequence[str],
    cfg: TeaTableConfig,
) -> HuKind:
    return classify_hu(tags, gangshanghua_as_shuangfan=cfg.gangshanghua_double)


def settle_tea_hand(
    *,
    win_type: str,
    winner_seat: int,
    dealer_seat: int,
    dealer_streak: int,
    player_count: int | None = None,
    loser_seat: int | None = None,
    hu_tags: Sequence[str] = (),
    hu_kind: str | HuKind | None = None,
    gang_records: Sequence[Mapping[str, Any]] = (),
    caishen_counts: Mapping[int, int] | None = None,
    genfeng_records: Sequence[Mapping[str, Any]] = (),
    sanbei_seat: int | None = None,
    config: TeaTableConfig | None = None,
) -> dict[str, Any]:
    """一手完整结算。

    返回:
      {
        "deltas": {seat: int},
        "base": int,
        "hu_kind": str,
        "multiplier": int,
        "unit": int,
        "config_banner": str,
        "breakdown": {...},
      }
    """
    if config is None:
        n = int(player_count if player_count is not None else 4)
        config = default_config_2p() if n == 2 else default_config_4p()
        if n not in (2, 4):
            config = TeaTableConfig(player_count=n)
    n = config.player_count
    if player_count is not None and int(player_count) != n:
        raise ValueError("player_count 与 config.player_count 不一致")

    seats = set(range(n))
    if winner_seat not in seats or dealer_seat not in seats:
        raise ValueError("winner/dealer 座位越界")

    base = config.base_for_streak(dealer_streak)
    kind = HuKind(hu_kind) if hu_kind is not None else resolve_hu_kind(hu_tags, config)
    mult = multiplier_for(kind)
    unit = base * mult

    deltas = _empty_deltas(n)
    breakdown: dict[str, Any] = {
        "hu": {},
        "gang": [],
        "caishen": [],
        "genfeng": [],
        "sanbei": None,
    }

    # --- 牌型分 ---
    if win_type == "draw":
        pass
    elif win_type == "zimo":
        payers = [s for s in range(n) if s != winner_seat]
        for s in payers:
            deltas[s] -= unit
            deltas[winner_seat] += unit
        breakdown["hu"] = {"type": "zimo", "unit": unit, "payers": payers}
    elif win_type == "fangpao":
        if loser_seat is None:
            raise ValueError("fangpao 需要 loser_seat")
        if loser_seat not in seats or loser_seat == winner_seat:
            raise ValueError("loser_seat 非法")
        # 点炮加倍
        pay = unit * 2
        deltas[loser_seat] -= pay
        deltas[winner_seat] += pay
        breakdown["hu"] = {"type": "fangpao", "unit": unit, "pay": pay, "loser": loser_seat}
    else:
        raise ValueError(f"未知 win_type: {win_type}")

    # --- 三背: 庄胜时承包其他闲家牌型分 ---
    if (
        config.sanbei_enabled
        and sanbei_seat is not None
        and win_type != "draw"
        and winner_seat == dealer_seat
        and sanbei_seat != winner_seat
    ):
        if sanbei_seat not in seats:
            raise ValueError("sanbei_seat 越界")
        # 把其他闲家因牌型付给庄的分, 转由三背者承担
        transferred = 0
        for s in range(n):
            if s in (winner_seat, sanbei_seat):
                continue
            # 该闲家若向庄付了正的牌型分(在 fangpao 时可能为 0)
            # 简化: 自摸时每个闲家付 unit; 点炮时仅 loser 付 pay
            owed = 0
            if win_type == "zimo":
                owed = unit
            elif win_type == "fangpao" and s == loser_seat:
                owed = unit * 2
            if owed > 0:
                # 闲家拿回, 三背者代付
                deltas[s] += owed
                deltas[sanbei_seat] -= owed
                transferred += owed
        breakdown["sanbei"] = {"seat": sanbei_seat, "transferred": transferred}

    # --- 杠分 ---
    if config.gang_scores:
        for g in gang_records:
            gt = g["type"]
            gs = int(g["seat"])
            if gs not in seats:
                raise ValueError("杠座位越界")
            if gt == "an":
                for s in range(n):
                    if s != gs:
                        deltas[s] -= 2
                        deltas[gs] += 2
                breakdown["gang"].append({"type": "an", "seat": gs, "per": 2})
            elif gt == "bu":
                for s in range(n):
                    if s != gs:
                        deltas[s] -= 1
                        deltas[gs] += 1
                breakdown["gang"].append({"type": "bu", "seat": gs, "per": 1})
            elif gt in ("zhi", "ming"):
                fs = g.get("from_seat")
                if fs is None or int(fs) == gs or int(fs) not in seats:
                    raise ValueError("直/明杠需要合法 from_seat")
                deltas[int(fs)] -= 1
                deltas[gs] += 1
                breakdown["gang"].append({"type": gt, "seat": gs, "from_seat": int(fs), "per": 1})
            else:
                raise ValueError(f"未知杠类型 {gt}")

    # --- 财神钱 ---
    if config.caishen_money and caishen_counts:
        per = config.caishen_money_per_opponent
        for seat, cnt in caishen_counts.items():
            seat = int(seat)
            cnt = int(cnt)
            if cnt <= 0 or seat not in seats:
                continue
            for s in range(n):
                if s == seat:
                    continue
                pay = per * cnt
                deltas[s] -= pay
                deltas[seat] += pay
            breakdown["caishen"].append({"seat": seat, "count": cnt, "per_opponent": per})

    # --- 跟风 ---
    if config.genfeng_enabled:
        for gf in genfeng_records:
            payer = int(gf["payer"])
            per = int(gf.get("per_opponent_score", 1))
            if payer not in seats:
                raise ValueError("跟风 payer 越界")
            for s in range(n):
                if s == payer:
                    continue
                deltas[payer] -= per
                deltas[s] += per
            breakdown["genfeng"].append({"payer": payer, "per": per})
    elif genfeng_records:
        # 2p 等关闭跟风时忽略传入, 但记下
        breakdown["genfeng_ignored"] = list(genfeng_records)

    _assert_zero_sum(deltas)
    return {
        "deltas": deltas,
        "base": base,
        "hu_kind": kind.value,
        "multiplier": mult,
        "unit": unit,
        "player_count": n,
        "config_banner": config.rule_banner(),
        "sanbei_enabled": config.sanbei_enabled,
        "genfeng_enabled": config.genfeng_enabled,
        "breakdown": breakdown,
    }


# ---- 显式 2p/4p 钩子(供识牌/回放管线按人数调用) ----

def settle_2p(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("player_count", None)
    kwargs["config"] = kwargs.get("config") or default_config_2p()
    if kwargs["config"].player_count != 2:
        raise ValueError("settle_2p 需要 2 人配置")
    return settle_tea_hand(player_count=2, **kwargs)


def settle_4p(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("player_count", None)
    kwargs["config"] = kwargs.get("config") or default_config_4p()
    if kwargs["config"].player_count != 4:
        raise ValueError("settle_4p 需要 4 人配置")
    return settle_tea_hand(player_count=4, **kwargs)


def settle_by_player_count(player_count: int, **kwargs: Any) -> dict[str, Any]:
    """统一入口: 按人数分发到 2p/4p 钩子。"""
    if player_count == 2:
        return settle_2p(**kwargs)
    if player_count == 4:
        return settle_4p(**kwargs)
    kwargs.setdefault("config", TeaTableConfig(player_count=player_count))
    return settle_tea_hand(player_count=player_count, **kwargs)
