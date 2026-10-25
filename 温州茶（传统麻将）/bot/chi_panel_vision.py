# -*- coding: utf-8 -*-
"""吃牌二次候选面板的只读识别（温州茶原生帧 1600x900）。

点下「吃」圆盘后，游戏在行动行上方弹出候选面板：1–3 组顺子候选
（每组 3 张牌）横排展示，右侧一颗蓝色 ``取消`` 圆盘。本模块只回答
一件事：**面板在不在、候选组分别是什么牌、每组的实测框在哪**。
不点击、不发 ADB、不写文件；读不唯一一律按不完整处理（fail closed）。

标定依据（离线归档帧，``采集/assist_sessions``，只读回放）
------------------------------------------------------------
面板帧的判据是 ``claim_vision.read_claims`` 读到一颗 ``cancel`` 圆盘
（实测框 [1401,554,1498,646]，中心 (1451,600)）。对 5 张这样的帧用
``tile_vision`` 在面板 ROI 内做牌框检测 + 逐框分类，结果一致：

* 候选牌框恒在同一行：y 恒为 [550, 655]（高 105），宽 75–77；
* 组内相邻框间隙 3–4px，组间间隙 20px → 分组阈值取 12px；
* 每组 3 张恰构成含来牌的顺子（如来牌 t8 → (t6,t7,t8)/(t7,t8,t9)；
  来牌 s4 → (s2,s3,s4)/(s3,s4,s5)/(s4,s5,s6)），分类分 0.93–0.98；
* 3 组面板时最左一组可能被中央弃牌行压住：检测框会与上方弃牌框
  纵向合并成高框（y0 被顶到 ROI 上沿、高 155）。此时仅当框宽仍在
  牌宽范围内，才允许对底部 105px 段（面板行本身）重分类一次；
  框宽异常（如 95px 的横向合并框）不修，直接判读不全。

帧清单（房间/局数可回查）：20261024_130737（2 组，t）、
20261024_000443（2 组，w）、20261024_004048（3 组，w）、
20261024_001052（3 组，s，最左组首张被弃牌行压住读不出）、
20261024_130741（2 组，t）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from claim_vision import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    KIND_CANCEL,
    ClaimButton,
    read_claims,
)

__all__ = [
    "ChiPanelGroup",
    "ChiPanelState",
    "PANEL_ROI",
    "read_chi_panel",
]


# ---------------------------------------------------------------------------
# 面板几何（标定见模块 docstring；坐标均为 1600x900 全帧口径）
# ---------------------------------------------------------------------------

#: 面板搜索区：覆盖 1–3 组候选（最左组约 x=600 起）并避开手牌行
#: （y>=720）与中央罗盘（y<=490）。取消圆盘在 x>=1401，不在此区内，
#: 由 ``read_claims`` 单独给出。
PANEL_ROI: tuple[int, int, int, int] = (560, 500, 1400, 700)

#: 候选牌框的实测行：y0=550、y1=655（5 帧全一致）。容差吸收动画帧。
PANEL_TILE_Y0: int = 550
PANEL_TILE_Y1: int = 655
PANEL_TILE_Y_TOL: int = 8
PANEL_TILE_H_MIN: int = 90
PANEL_TILE_H_MAX: int = 120
PANEL_TILE_W_MIN: int = 60
PANEL_TILE_W_MAX: int = 90

#: 组内框间隙实测 3–4px、组间 20px，分组阈值取中点 12px。
GROUP_GAP_MIN_PX: int = 12


@dataclass(frozen=True)
class ChiPanelGroup:
    """面板上的一组候选：3 张牌（引擎编码，升序）与其实测框。

    Attributes:
        tiles: 顺子 3 张的引擎牌 ID（升序）。
        box: 3 张牌框的并集 ``(x0, y0, x1, y1)``，全帧坐标。
        tile_boxes: 每张牌各自的实测框（左→右）。
    """

    tiles: tuple[int, int, int]
    box: tuple[int, int, int, int]
    tile_boxes: tuple[tuple[int, int, int, int], ...]

    @property
    def center(self) -> tuple[int, int]:
        """整组并集框的中心（点组时点中间那张牌所在区域）。"""
        x0, y0, x1, y1 = self.box
        return ((x0 + x1) // 2, (y0 + y1) // 2)


@dataclass(frozen=True)
class ChiPanelState:
    """一帧的候选面板读数。

    Attributes:
        present: 有面板证据（取消圆盘或面板行牌框任一出现）。
        complete: 面板被**完整**读出——取消圆盘在、至少一组候选、
            每个面板行牌框都认出牌面、每组恰 3 张、无未解析区域。
            只有 ``complete`` 时才允许把 ``groups`` 喂给选组决策。
        groups: 完整读出的候选组（左→右）；不完整时可能为空或残缺，
            调用方不得据此点击。
        cancel: ``read_claims`` 实测的取消圆盘；没有为 ``None``。
        tile_reads: 逐框读数明细（台账用）：框、label、tile_id、
            accepted、score、是否经底部重裁修复。
        reason: 中文说明（完整/不完整的具体原因）。
    """

    present: bool = False
    complete: bool = False
    groups: tuple[ChiPanelGroup, ...] = ()
    cancel: ClaimButton | None = None
    tile_reads: tuple[dict[str, Any], ...] = ()
    reason: str = ""

    def ledger(self) -> dict[str, Any]:
        """JSON 可序列化的台账行。"""
        return {
            "present": self.present,
            "complete": self.complete,
            "groups": [list(group.tiles) for group in self.groups],
            "group_boxes": [list(group.box) for group in self.groups],
            "cancel": (list(self.cancel.box) if self.cancel is not None
                       else None),
            "tile_reads": [dict(read) for read in self.tile_reads],
            "reason": self.reason,
        }


def _default_vision():
    """茶牌识器（进程内缓存，与牌河/手牌同一套模板与门槛）。"""
    from discard_vision import load_vision

    return load_vision()


def _classify(vision, image: np.ndarray, box: Sequence[int]) -> dict[str, Any]:
    """分类一个牌框；任何异常都折成"没认出来"。"""
    x0, y0, x1, y1 = (int(v) for v in box)
    crop = image[y0:y1, x0:x1]
    if crop is None or crop.size == 0:
        return {"tile_id": None, "label": None, "accepted": False,
                "score": 0.0}
    try:
        result = vision.classify_crop(crop, caishen_label=None)
    except Exception:  # noqa: BLE001 - 单框失败不能拖垮整帧
        return {"tile_id": None, "label": None, "accepted": False,
                "score": 0.0}
    return {
        "tile_id": result.get("tile_id"),
        "label": result.get("label"),
        "accepted": bool(result.get("accepted")),
        "score": float(result.get("score") or 0.0),
    }


def read_chi_panel(image: Any, vision: Any = None) -> ChiPanelState:
    """读一帧里的吃候选面板（纯函数，不点击不落盘）。

    Args:
        image: 1600x900 的 BGR 帧（``numpy`` 数组）。尺寸不符直接判
            无面板证据（fail closed 的读数：``present=False``）。
        vision: 可注入的 ``TileVision``（测试用）；缺省用
            :func:`_default_vision`。

    Returns:
        :class:`ChiPanelState`。``complete=False`` 时 ``groups`` 不得
        用于任何点击决策。
    """
    if (not isinstance(image, np.ndarray) or image.ndim != 3
            or image.shape[1] != FRAME_WIDTH or image.shape[0] != FRAME_HEIGHT):
        return ChiPanelState(reason="帧尺寸不是 1600x900，面板读数无效")

    claims = read_claims(image)
    cancel = next((b for b in claims.buttons if b.kind == KIND_CANCEL), None)

    vision = vision if vision is not None else _default_vision()
    _count, raw_boxes = vision.count_tiles(image, roi=PANEL_ROI)
    boxes: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    merged = list(raw_boxes or []) + list(
        getattr(vision, "last_meld_boxes", []) or [])
    for box in merged:
        key = tuple(int(v) for v in box[:4])
        if key not in seen:
            seen.add(key)
            boxes.append(key)
    boxes.sort(key=lambda b: (b[0], b[1]))
    diagnostics = getattr(vision, "detection_diagnostics", {}) or {}
    unresolved = diagnostics.get("unresolved_regions") or []

    # 只认底边落在面板行（y1=655±8）的框：中央罗盘/弃牌行的框底边不在
    # 这条线上，忽略且不污染完整性；底边在线上但形状不对（被弃牌行
    # 纵向合并的高框等）算面板行框，读不出就判不完整。
    row_boxes = [b for b in boxes
                 if abs(b[3] - PANEL_TILE_Y1) <= PANEL_TILE_Y_TOL]
    present = cancel is not None or bool(row_boxes)
    if not present:
        return ChiPanelState(
            cancel=cancel,
            reason="无候选面板证据（无取消圆盘、面板行无牌框）")

    problems: list[str] = []
    if cancel is None:
        problems.append("未实测到取消圆盘")
    if unresolved:
        problems.append(f"面板区有 {len(unresolved)} 处未解析区域")
    if not row_boxes:
        problems.append("面板行没有牌框")

    tile_reads: list[dict[str, Any]] = []
    tiles_by_box: dict[tuple[int, int, int, int], int] = {}
    for box in row_boxes:
        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0
        read = _classify(vision, image, box)
        repaired = False
        shape_ok = (abs(y0 - PANEL_TILE_Y0) <= PANEL_TILE_Y_TOL
                    and PANEL_TILE_H_MIN <= height <= PANEL_TILE_H_MAX
                    and PANEL_TILE_W_MIN <= width <= PANEL_TILE_W_MAX)
        if not read["accepted"] and not shape_ok:
            # 与弃牌行纵向合并的高框：框宽仍是牌宽时，对底部一个牌高
            # 的区段（面板行本身）重分类一次。修不好就保持未认出。
            if (y0 < PANEL_TILE_Y0 - PANEL_TILE_Y_TOL
                    and PANEL_TILE_W_MIN <= width <= PANEL_TILE_W_MAX):
                bottom = (x0, y1 - (PANEL_TILE_Y1 - PANEL_TILE_Y0), x1, y1)
                retry = _classify(vision, image, bottom)
                if retry["accepted"] and retry["tile_id"] is not None:
                    read = retry
                    repaired = True
                    box = bottom
        tile_reads.append({
            "box": list(box), "label": read["label"],
            "tile_id": read["tile_id"], "accepted": read["accepted"],
            "score": round(read["score"], 4), "repaired": repaired,
        })
        if read["accepted"] and read["tile_id"] is not None and shape_ok_or_repaired(shape_ok, repaired):
            tiles_by_box[box] = int(read["tile_id"])
        else:
            problems.append(f"面板行牌框 {list(box)} 未认出牌面")

    # 按 x 间隙分组：组内 3–4px、组间 20px，阈值 12px。
    grouped: list[list[tuple[int, int, int, int]]] = []
    for read in tile_reads:
        box = tuple(read["box"])
        if box not in tiles_by_box:
            continue
        if grouped and box[0] - grouped[-1][-1][2] < GROUP_GAP_MIN_PX:
            grouped[-1].append(box)
        else:
            grouped.append([box])

    groups: list[ChiPanelGroup] = []
    for members in grouped:
        if len(members) != 3:
            problems.append(f"候选组牌框数 {len(members)} != 3")
            continue
        tiles = tuple(sorted(tiles_by_box[b] for b in members))
        union = (min(b[0] for b in members), min(b[1] for b in members),
                 max(b[2] for b in members), max(b[3] for b in members))
        groups.append(ChiPanelGroup(
            tiles=(int(tiles[0]), int(tiles[1]), int(tiles[2])),
            box=union, tile_boxes=tuple(members)))
    if not groups:
        problems.append("没有完整读出的候选组")

    complete = not problems
    reason = ("候选面板完整读出" if complete
              else "候选面板读不完整：" + "；".join(problems))
    return ChiPanelState(
        present=True, complete=complete, groups=tuple(groups),
        cancel=cancel, tile_reads=tuple(tile_reads), reason=reason)


def shape_ok_or_repaired(shape_ok: bool, repaired: bool) -> bool:
    """框形状合格，或经底部重裁修复成功——两种情况都算读出。"""
    return bool(shape_ok or repaired)
