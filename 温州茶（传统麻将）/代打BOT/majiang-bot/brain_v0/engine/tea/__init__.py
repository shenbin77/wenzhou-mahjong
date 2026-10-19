# -*- coding: utf-8 -*-
"""温州茶(温州麻将)规则包。

与 engine.wenzhou_rules(红中放炮 / 抖音熟)隔离。目标桌账号 晓红;
证据只用来自温州(茶)回放/截图, 不用 douyin 红中素材。

公开入口:
  - TeaTableConfig / default_config_2p / default_config_4p
  - settle_2p / settle_4p / settle_by_player_count / settle_tea_hand
  - classify_hu / HuKind / pick_discard_winner
  - detect_pattern_tags / build_deck
"""

from .config import (
    BASE_PROGRESSION_2468,
    USER_ACCOUNT,
    VARIANT_ID,
    VARIANT_LABEL,
    TeaTableConfig,
    configs_match_confirmed_screenshots,
    default_config_2p,
    default_config_4p,
)
from .kinds import HuKind, classify_hu, kind_label_zh, multiplier_for
from .patterns import (
    detect_pattern_tags,
    is_8dui_shape,
    is_banqing,
    is_pengpeng_skeleton,
    is_qingyise_unordered,
    is_ruan_8dui,
    is_ying_8dui,
)
from .priority import counterclockwise_seats, pick_discard_winner
from .settle import (
    resolve_hu_kind,
    settle_2p,
    settle_4p,
    settle_by_player_count,
    settle_tea_hand,
)
from .tiles import (
    BAI,
    EAST,
    FA,
    NORTH,
    SOUTH,
    WEST,
    ZHONG,
    assert_tiles_in_rules,
    build_deck,
    deck_size,
    legal_tile_ids,
    tile_name,
)

__all__ = [
    "BASE_PROGRESSION_2468",
    "BAI",
    "EAST",
    "FA",
    "HuKind",
    "NORTH",
    "SOUTH",
    "TeaTableConfig",
    "USER_ACCOUNT",
    "VARIANT_ID",
    "VARIANT_LABEL",
    "WEST",
    "ZHONG",
    "assert_tiles_in_rules",
    "build_deck",
    "classify_hu",
    "configs_match_confirmed_screenshots",
    "counterclockwise_seats",
    "deck_size",
    "default_config_2p",
    "default_config_4p",
    "detect_pattern_tags",
    "is_8dui_shape",
    "is_banqing",
    "is_pengpeng_skeleton",
    "is_qingyise_unordered",
    "is_ruan_8dui",
    "is_ying_8dui",
    "kind_label_zh",
    "legal_tile_ids",
    "multiplier_for",
    "pick_discard_winner",
    "resolve_hu_kind",
    "settle_2p",
    "settle_4p",
    "settle_by_player_count",
    "settle_tea_hand",
    "tile_name",
]
