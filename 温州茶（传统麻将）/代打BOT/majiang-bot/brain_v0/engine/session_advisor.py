# -*- coding: utf-8 -*-
"""Session 级止损止盈建议（舅舅窍门：赢继续、输就走、输大分立刻走）。

规则（用户转述舅舅）：
- 一圈约16把，总体是赢的（哪怕几分）→ 继续打
- 开始输了（哪怕几分）→ 马上不打了，休息几小时
- 输大分了（超过10分）→ 马上不打了，休息几小时

本质：短线盈亏动量 + 硬止损。它**不直接提升单局胜率**，而是通过
「牌运好/状态好时多打、差时离场」来提升**整体期望收益**。
这正好接上 game_log 已有的 session 级盈亏统计（stats_from_records.session_stats）。
"""
from dataclasses import dataclass


@dataclass
class SessionAdvisorConfig:
    rounds_per_circle: int = 16
    big_loss_exit: float = 10.0   # 单局或累计输超过此分 → 硬离场
    loss_exit: float = 0.0       # 本圈盈亏 < 此值（即转负）即离场（舅舅"开始输了就走"）
    rest_hours: float = 3.0
    win_continue: bool = True     # 本圈非负(赢/平)则继续


class SessionAdvisor:
    def __init__(self, cfg: SessionAdvisorConfig = None):
        self.cfg = cfg or SessionAdvisorConfig()

    def advise(self, circle_pnl: float, last_round_pnl: float = 0.0,
               session_pnl: float = None) -> dict:
        """返回 {action, reason, rest_hours}。action: 'continue' | 'rest'。

        circle_pnl: 当前这一圈(约16把)的累计盈亏
        last_round_pnl: 最近一局的盈亏（用于硬止损单局大输）
        session_pnl: 整个 session 累计盈亏（可选，用于累计大输）
        """
        cfg = self.cfg
        # 1) 硬止损：单局或累计输大分
        big_loss = False
        if last_round_pnl <= -cfg.big_loss_exit:
            big_loss = True
            reason = f"单局输 {last_round_pnl} 分 ≥ 阈值 {cfg.big_loss_exit}，触发硬止损"
        elif session_pnl is not None and session_pnl <= -cfg.big_loss_exit:
            big_loss = True
            reason = f"累计输 {session_pnl} 分 ≤ -{cfg.big_loss_exit}，触发硬止损"
        if big_loss:
            return {"action": "rest", "reason": reason, "rest_hours": cfg.rest_hours}
        # 2) 开始输就走（舅舅：转负即离场）
        if circle_pnl < cfg.loss_exit:
            return {"action": "rest",
                    "reason": f"本圈盈亏 {circle_pnl} 转负，按窍门离场休息",
                    "rest_hours": cfg.rest_hours}
        # 3) 赢（非负）则继续
        if cfg.win_continue and circle_pnl >= cfg.loss_exit:
            return {"action": "continue",
                    "reason": f"本圈盈亏 {circle_pnl} 非负，继续打",
                    "rest_hours": 0.0}
        return {"action": "continue", "reason": "默认继续", "rest_hours": 0.0}
