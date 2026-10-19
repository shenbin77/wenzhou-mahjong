# -*- coding: utf-8 -*-
"""温州茶 — 软胡 / 硬胡 / 双翻 判定与倍率。

倍率:
  软胡 ×1, 硬胡 ×2, 双翻 ×4

硬胡(摘要, 与公开规则 + 用户确认对齐):
  - 自摸
  - 无财神或财神归位(财神当自身, 不替他牌)
  - 软 8 对
  - 三财神(可不成形)直接胡
  - 抢杠胡
双翻:
  - 硬 8 对
  - 三财神且成形
  - 天胡 / 地胡 / 单吊 / 全球神
  - 碰碰胡(无财神或财神归位)
  - 杠上花(桌选项杠上花双翻开启时)
  - 财神牛对 等
软胡: 有财神替牌且非上列硬/双翻路径。
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Sequence


class HuKind(str, Enum):
    RUAN = "ruan"  # 软胡 ×1
    YING = "ying"  # 硬胡 ×2
    SHUANGFAN = "shuangfan"  # 双翻 ×4


MULTIPLIER = {
    HuKind.RUAN: 1,
    HuKind.YING: 2,
    HuKind.SHUANGFAN: 4,
}

# 情境/牌型标签 → 默认归入的种类(取最高)
SHUANGFAN_TAGS = frozenset(
    {
        "ying8dui",
        "hard_8dui",
        "san_caishen_shaped",  # 三财神且成形
        "tianhu",
        "dihu",
        "dandiao",
        "quanqiushen",
        "pengpeng_hard",  # 碰碰胡硬
        "gangshanghua",  # 仅当桌选项开启双翻时由调用方传入
        "caishen_niudui",
        "shuang_caishen_guiwei",
    }
)

YING_TAGS = frozenset(
    {
        "zimo",
        "no_caishen",
        "caishen_guiwei",
        "ruan8dui",
        "soft_8dui",
        "san_caishen_unshaped",  # 三财神不成形成硬
        "qiangganghu",
        "banqing",  # 半清本身不抬到双翻; 若同时硬则仍硬
        "qingyise_unordered",
    }
)


def multiplier_for(kind: HuKind | str) -> int:
    if isinstance(kind, str):
        kind = HuKind(kind)
    return MULTIPLIER[kind]


def classify_hu(
    tags: Sequence[str] | Iterable[str],
    *,
    gangshanghua_as_shuangfan: bool = True,
) -> HuKind:
    """按标签集合取最高档。双翻 > 硬 > 软。"""
    tagset = {str(t) for t in tags}
    if not gangshanghua_as_shuangfan:
        tagset.discard("gangshanghua")
    if tagset & SHUANGFAN_TAGS:
        return HuKind.SHUANGFAN
    if tagset & YING_TAGS:
        return HuKind.YING
    # 显式软标或无标 → 软
    return HuKind.RUAN


def kind_label_zh(kind: HuKind | str) -> str:
    k = HuKind(kind) if isinstance(kind, str) else kind
    return {HuKind.RUAN: "软胡", HuKind.YING: "硬胡", HuKind.SHUANGFAN: "双翻"}[k]
