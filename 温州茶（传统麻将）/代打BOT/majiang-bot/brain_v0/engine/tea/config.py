# -*- coding: utf-8 -*-
"""温州茶 — 桌配置(对齐用户确认的创建房/回放规则条)。

已确认(回放顶栏 / 截图):
  进阶(2468) / 杠算分 / 杠上开花双翻 / 无中发 / 半清 / 清一色(无序)
  16 局 / 允许续桌 / 平摊消耗
  2 人桌: 无三背、无跟风
  4 人桌: 三背 + 跟风结算
用户账号: 晓红。勿用抖音/红中(熟)素材当茶规则证据。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

# 连庄底分进阶: 第 1~4 连庄对应 2/4/6/8
BASE_PROGRESSION_2468: tuple[int, ...] = (2, 4, 6, 8)

USER_ACCOUNT = "晓红"
VARIANT_ID = "wenzhou_tea"
VARIANT_LABEL = "温州茶"


@dataclass(frozen=True)
class TeaTableConfig:
    """一桌规则快照。"""

    player_count: int = 4
    rounds: int = 16
    base_progression: tuple[int, ...] = BASE_PROGRESSION_2468
    allow_continue_table: bool = True
    split_cost: bool = True  # 平摊消耗
    gang_scores: bool = True  # 杠算分
    gangshanghua_double: bool = True  # 杠上花双翻
    no_zhong_fa: bool = True  # 无中发
    banqing: bool = True  # 半清
    qingyise_unordered: bool = True  # 清一色(无序)
    # 由人数推导; 也可显式覆盖
    enable_sanbei: bool | None = None
    enable_genfeng: bool | None = None
    # 财神钱开关(默认开; 每张财神收他家人各 1 底单位外的固定分, 见 settle)
    caishen_money: bool = True
    caishen_money_per_opponent: int = 1

    def __post_init__(self) -> None:
        if self.player_count not in (2, 3, 4):
            raise ValueError(f"player_count 仅支持 2/3/4, 得到 {self.player_count}")
        if self.rounds < 1:
            raise ValueError("rounds 必须 >= 1")
        if not self.base_progression:
            raise ValueError("base_progression 不能为空")
        if any(b <= 0 for b in self.base_progression):
            raise ValueError("base_progression 各项须为正")

    @property
    def sanbei_enabled(self) -> bool:
        if self.enable_sanbei is not None:
            return bool(self.enable_sanbei)
        return self.player_count == 4

    @property
    def genfeng_enabled(self) -> bool:
        if self.enable_genfeng is not None:
            return bool(self.enable_genfeng)
        return self.player_count == 4

    def base_for_streak(self, dealer_streak: int) -> int:
        """dealer_streak: 1=首局坐庄, 2=连一..., 最大用 progression 末项。"""
        if dealer_streak < 1:
            raise ValueError("dealer_streak 从 1 起计")
        idx = min(dealer_streak, len(self.base_progression)) - 1
        return int(self.base_progression[idx])

    def rule_banner(self) -> str:
        """近似回放顶栏文案, 便于对照截图。"""
        parts = [
            f"进阶({''.join(str(b) for b in self.base_progression)})",
        ]
        if self.gang_scores:
            parts.append("杠算分")
        if self.gangshanghua_double:
            parts.append("杠上开花双翻")
        if self.no_zhong_fa:
            parts.append("无中发")
        if self.banqing:
            parts.append("半清")
        if self.qingyise_unordered:
            parts.append("清一色(无序)")
        parts.append(f"{self.player_count}人")
        return " / ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sanbei_enabled"] = self.sanbei_enabled
        d["genfeng_enabled"] = self.genfeng_enabled
        d["variant"] = VARIANT_ID
        d["rule_banner"] = self.rule_banner()
        return d

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TeaTableConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: data[k] for k in known if k in data}
        if "base_progression" in kwargs:
            kwargs["base_progression"] = tuple(kwargs["base_progression"])
        return cls(**kwargs)


def default_config_2p() -> TeaTableConfig:
    return TeaTableConfig(player_count=2)


def default_config_4p() -> TeaTableConfig:
    return TeaTableConfig(player_count=4)


def configs_match_confirmed_screenshots() -> dict[str, TeaTableConfig]:
    """用户确认截图对应的两套默认桌。"""
    return {
        "2p": default_config_2p(),
        "4p": default_config_4p(),
    }
