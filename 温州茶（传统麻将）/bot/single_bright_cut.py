# -*- coding: utf-8 -*-
"""single_bright_cut.py — 「P0 单亮牌摸切」状态检测器（纯函数，零副作用）。

用途
====
听牌后轮到我方摸切时，游戏把整手牌压暗、只亮起刚摸的那一张（下称
「单亮牌」态）。既有链路对这种帧必然弃权：``analyze_snapshot`` 在压暗
下读不全手牌（实测 17 张只有个位数被 accepted），``turn_gate`` 的整手
平均亮度又只会得出「手牌压暗，非我方回合」——循环因此卡死，直到人工
点一下亮牌（房间 951441 第 2 局、房间 014353 第 2 局均实机复现，
两次都是人工点亮牌才解开）。

本模块只做**检测**：输入当前帧 + ``analyze_snapshot`` 的返回体（与
``turn_gate.evaluate_turn`` 同一份），输出「是否命中单亮牌态 + 亮牌
框与牌面证据」。命中后的点击与点后验证沿用 ``TapExecutor``（四道闸
+ 手牌 -1 验证），本模块不截图、不调 ADB、不写文件、不做任何点击。

五点严规格（逐条对应实现）
==========================
1. **物理框守恒**：``len(concealed_boxes(result))`` 必须恰等于出牌态
   应有张数 ``17 - logical_meld_slots(result)``（与 ``turn_gate`` 同一
   口径，杠按 3 个逻辑槽计）。少一张是「未摸牌」——实测
   ``424821/r08/frame_01`` 有 16 框且单亮图案一模一样，靠本条排除。
2. **恰 1 亮 + 暗簇内聚**：逐框面亮度（``tile_vision.face_background``，
   与 ``turn_gate`` 同一把尺）里恰有 1 框 ≥ :data:`BRIGHT_FACE_MIN`，
   其余全部 ≤ :data:`DIM_FACE_MAX`、极差 ≤ :data:`DIM_CLUSTER_MAX_SPREAD`，
   且亮框与暗簇均值之差 ≥ :data:`MIN_BRIGHT_GAP`（亮度分布双峰且暗簇
   内聚）。2 个亮框的帧（实测 ``20261023_205534``，14 框含 2 个亮面）
   靠「恰 1」排除；正常出牌态是本图案的**镜像**（1 个暗框 = 财神角标
   展示位 + 其余全亮，实测 play7_5556 020629），同样不命中。
3. **连续 2 帧一致**：:class:`SingleBrightCutTracker` 要求连续
   :data:`REQUIRED_FRAMES` 帧命中，且亮牌位置（框中心漂移 ≤
   :data:`CENTER_TOLERANCE_PX`）与牌面一致（两帧 label 相同；label
   缺失时用 ``tile_vision.tile_signature`` 的余弦相似度 ≥
   :data:`MIN_FACE_SIGNATURE_SIMILARITY` 兜底，两样都拿不到即不一致）。
4. **上下文三条同时成立**：本局财神已知、``mode == "tea_2p"``、行动
   圆盘不可见（``claim_vision.read_claims`` 未定位到任何圆盘）。
5. **动作只有亮牌**：命中产出的 ``tap_point`` 恒为亮牌框中心；执行方
   不许改点其它牌。

另加两道前置排除（与既有门禁同序）：画面必须是已验证的 1600×900；
``turn_gate.detect_settled_screen`` 的任一信号命中即不命中——结算帧
（双信号）与胡提示面板帧（``central_panel`` 单信号，实测
play6_5556 01:58 / play3_5556 01:42 两帧 panel 占比 7.86% / 6.29%）
都在这里出局，不与面板处理路径抢帧。

亮度阈值实测依据（1600×900 原生帧，逐框 face_background）
=========================================================
* 证据帧 ``D:\\温州麻将\\capture_5556.png``（951441，财神 t5）：17 框，
  16 框恰为 **100.0**，亮框（南，box ``[1390, 768, 1480, 892]``）
  **214.0**，已被模板接受（tile_id 62）；
* ``_takeover/play7_5556/frames/20261025_020539*.png`` 起连续 4 帧
  （014353 第 2 局，财神 dong）：17 框同图案，亮框为白板（214.0），
  逐帧亮框位置与 label 不变，循环同期逐帧弃权（「识别不完整」）；
* 压暗值恒为 100.0、亮面值 214.0（普通亮面 214–245，见 tile_vision
  实测注记），故阈值两侧留足余量：亮 ≥ 170、暗 ≤ 150（与
  ``turn_gate.MIN_HAND_FACE_MEAN`` 同界）、暗簇极差 ≤ 40、亮暗差 ≥ 50
  （实测 114）。

已知局限（fail-closed 方向，不误动作）
=====================================
刚摸的牌恰好是本局财神牌时，其牌面带「财」角标、面亮度被游戏压到
~100–115（见 tile_vision 实测注记），本检测器会漏检该局的单亮牌态
（维持现状弃权），不会误点。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

import cv2
import numpy as np

from tile_vision import TILE_IDS, face_background, tile_signature
from turn_gate import (
    MODE_TEA_2P,
    TILES_PER_HAND,
    VERIFIED_FRAME_SIZE,
    concealed_boxes,
    detect_settled_screen,
    known_caishen_id,
    logical_meld_slots,
)

# ---------------------------------------------------------------- 常量
BRIGHT_FACE_MIN = 170.0          # 亮框面亮度下限（实测亮面 214.0）
DIM_FACE_MAX = 150.0             # 暗框面亮度上限（实测压暗 100.0；与 turn_gate 压暗界同值）
DIM_CLUSTER_MAX_SPREAD = 40.0    # 暗簇极差上限（实测 0.0）
MIN_BRIGHT_GAP = 50.0            # 亮框与暗簇均值的最小差（实测 114.0）
REQUIRED_FRAMES = 2              # 连续一致帧数（规格第 3 条）
CENTER_TOLERANCE_PX = 20         # 两帧亮框中心允许漂移（像素）
MIN_FACE_SIGNATURE_SIMILARITY = 0.90  # label 缺失时牌面签名余弦相似度下限


@dataclass(frozen=True)
class SingleBrightCut:
    """单帧检测（或经 tracker 确认后）的结论。

    Attributes:
        hit: 本帧是否命中单亮牌态（五点规格的第 1/2/4/5 条与前置排除）。
        confirmed: 是否已连续 :data:`REQUIRED_FRAMES` 帧一致（含本帧）；
            单帧 ``detect`` 直出时恒为 ``False``，只有经
            :class:`SingleBrightCutTracker` 喂帧后才可能为 ``True``。
            **执行方只许在 confirmed 时点击。**
        reason: 人类可读依据，直接进台账。
        bright_index: 亮牌在暗手框序（``concealed_boxes`` 顺序）中的下标。
        bright_box: 亮牌物理框 ``(x0, y0, x1, y1)``，1600×900 坐标。
        tap_point: 唯一允许的点击点 = 亮牌框中心。
        tile_id / tile_label: 亮牌牌面（来自同框诊断；未接受时 label 是
            top-1 读数、tile_id 为 ``None``）。
        streak: 当前连续一致帧数（tracker 回填；单帧检测命中时为 1）。
        evidence: 本帧实测值（张数、逐框亮度、双峰统计等），可进台账。
        face_signature: 亮牌字形签名（仅供 tracker 跨帧比对，不进台账）。
    """

    hit: bool
    reason: str
    bright_index: int | None = None
    bright_box: tuple[int, int, int, int] | None = None
    tap_point: tuple[int, int] | None = None
    tile_id: int | None = None
    tile_label: str | None = None
    streak: int = 0
    confirmed: bool = False
    evidence: dict = field(default_factory=dict)
    face_signature: np.ndarray | None = field(
        default=None, repr=False, compare=False)

    def ledger(self) -> dict:
        """台账用摘要（JSON 可序列化，不含签名数组）。"""
        return {
            "hit": self.hit, "confirmed": self.confirmed,
            "streak": self.streak, "reason": self.reason,
            "bright_index": self.bright_index,
            "bright_box": (list(self.bright_box)
                           if self.bright_box is not None else None),
            "tap_point": (list(self.tap_point)
                          if self.tap_point is not None else None),
            "tile_id": self.tile_id, "tile_label": self.tile_label,
            "evidence": dict(self.evidence),
        }


def _as_int(value) -> int | None:
    """把可能是 numpy / str 的值安全转成 int，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def box_face_values(image, boxes: Sequence[Sequence[int]]) -> list[float | None]:
    """逐框面亮度 p70(V)；与 ``turn_gate.hand_face_brightness`` 完全相同的
    裁剪与口径（同一把尺），只是逐框返回而不是取均值。不可测的框记 None。
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return [None] * len(boxes or [])
    height, width = image.shape[:2]
    values: list[float | None] = []
    for box in boxes or []:
        try:
            x0, y0, x1, y1 = (int(v) for v in box)
        except (TypeError, ValueError):
            values.append(None)
            continue
        x0, y0 = max(0, min(x0, width)), max(0, min(y0, height))
        x1, y1 = max(0, min(x1, width)), max(0, min(y1, height))
        if x1 - x0 < 2 or y1 - y0 < 2:
            values.append(None)
            continue
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            values.append(None)
            continue
        try:
            values.append(float(face_background(crop)))
        except (ValueError, cv2.error):
            values.append(None)
    return values


def brightness_pattern(values: Sequence[float | None]) -> tuple[int | None, dict]:
    """纯图案判定（规格第 2 条）：恰 1 亮 + 其余构成同一低亮簇。

    Args:
        values: 逐框面亮度（与 boxes 同序）。

    Returns:
        ``(亮框下标, stats)``；不命中时下标为 ``None``，stats 里带
        ``bright_count`` / ``dim_mean`` / ``dim_spread`` / ``gap`` 等实测值。
    """
    stats: dict = {"bright_count": 0, "dim_mean": None, "dim_spread": None,
                   "gap": None}
    if values is None or len(values) < 2:
        stats["pattern_error"] = "框数不足"
        return None, stats
    if any(v is None for v in values):
        stats["pattern_error"] = "有框亮度不可测"
        return None, stats
    numeric = [float(v) for v in values]
    bright = [i for i, v in enumerate(numeric) if v >= BRIGHT_FACE_MIN]
    stats["bright_count"] = len(bright)
    if len(bright) != 1:
        return None, stats
    index = bright[0]
    dims = [v for i, v in enumerate(numeric) if i != index]
    dim_mean = float(np.mean(dims))
    dim_spread = float(max(dims) - min(dims))
    gap = numeric[index] - dim_mean
    stats.update({"dim_mean": round(dim_mean, 2),
                  "dim_spread": round(dim_spread, 2),
                  "gap": round(gap, 2),
                  "bright_value": numeric[index],
                  "dim_max": max(dims)})
    if max(dims) > DIM_FACE_MAX:
        return None, stats
    if dim_spread > DIM_CLUSTER_MAX_SPREAD:
        return None, stats
    if gap < MIN_BRIGHT_GAP:
        return None, stats
    return index, stats


def _diagnostic_for_box(result: dict, box) -> dict | None:
    """在 vision_diagnostics 里按框坐标找回同一张牌的诊断条目。"""
    target = [int(v) for v in box]
    for item in result.get("vision_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if item.get("region") not in (None, "concealed"):
            continue
        raw = item.get("box")
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        try:
            if [int(v) for v in raw] == target:
                return item
        except (TypeError, ValueError):
            continue
    return None


def detect_single_bright_cut(image, result: dict | None, *,
                             caishen_label: str | None = None,
                             mode: str | None = None) -> SingleBrightCut:
    """判定单帧是否命中「单亮牌摸切」态（规格第 1/2/4/5 条 + 前置排除）。

    Args:
        image: 当前帧 BGR 图（只读），必须是已验证的 1600×900。
        result: ``assist_tea.analyze_snapshot`` 的返回体（只读）；框与副露
            会计与 ``turn_gate.evaluate_turn`` 取同一份。
        caishen_label: 调用方已知的本局财神 label（如 ``"t5"``）；缺省从
            ``result`` 解析。
        mode: 调用方已知的玩法；缺省取 ``result["mode"]``。

    Returns:
        :class:`SingleBrightCut`；``hit`` 只代表单帧图案与门禁全过，
        是否可执行由 tracker 的 ``confirmed`` 决定（规格第 3 条）。
    """
    def miss(reason: str, **evidence) -> SingleBrightCut:
        return SingleBrightCut(hit=False, reason=reason, evidence=evidence)

    if result is None or not isinstance(result, dict):
        return miss("分析结果缺失：不命中")
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return miss("画面不可用：不命中")
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        return miss(f"画面尺寸 {width}×{height} 未验证：不命中",
                    frame_size=[width, height])
    # 规格第 4 条之一：玩法必须是 tea_2p（4 人局布局未验收）。
    effective_mode = mode or result.get("mode")
    if effective_mode != MODE_TEA_2P:
        return miss(f"模式不是 tea_2p（{effective_mode!r}）：不命中")
    # 规格第 4 条之二：本局财神已知。
    caishen_id = TILE_IDS.get(caishen_label) if caishen_label else None
    if caishen_id is None:
        caishen_id = known_caishen_id(result)
    if caishen_id is None:
        return miss("本局财神未知：不命中")
    # 前置排除：结算/结束/模态弹窗/胡提示面板（任一信号命中即出局）。
    settled, settled_evidence = detect_settled_screen(image)
    if settled or settled_evidence.get("settled_signals"):
        return miss("结算/面板类信号命中（"
                    + "+".join(settled_evidence.get("settled_signals") or [])
                    + "）：不命中",
                    panel_area_pct=settled_evidence.get("panel_area_pct"))
    if settled_evidence.get("settled_unknown"):
        return miss("画面类型不可判读：不命中")
    # 规格第 4 条之三：行动圆盘不可见（圆盘在场时由行动路径处理）。
    try:
        from claim_vision import read_claims
        claim_state = read_claims(image)
    except Exception as exc:  # noqa: BLE001 - 读不出行动行 = fail closed
        return miss(f"行动行读取失败：不命中（{exc}）")
    if claim_state.known or claim_state.buttons:
        return miss("行动圆盘可见：不命中（由行动路径处理）",
                    claim_buttons=[b.kind for b in claim_state.buttons])
    # 规格第 1 条：物理框守恒（与 turn_gate 同一口径）。
    boxes = concealed_boxes(result)
    expected = TILES_PER_HAND - logical_meld_slots(result)
    if expected < 1 or len(boxes) != expected:
        return miss(f"物理框 {len(boxes)} 张 != 出牌态应有 {expected} 张：不命中",
                    physical_boxes=len(boxes), expected_concealed=expected)
    # 规格第 2 条：恰 1 亮 + 暗簇内聚。
    values = box_face_values(image, boxes)
    if any(v is None for v in values):
        return miss("逐框亮度不可测：不命中",
                    physical_boxes=len(boxes))
    index, stats = brightness_pattern(values)
    evidence = {
        "physical_boxes": len(boxes),
        "expected_concealed": expected,
        "caishen_id": caishen_id,
        "face_values": [round(float(v), 1) for v in values],
        **stats,
    }
    if index is None:
        return miss(
            "亮度图案不符（亮框数 %s，暗簇均值 %s，极差 %s，亮暗差 %s）：不命中"
            % (stats.get("bright_count"), stats.get("dim_mean"),
               stats.get("dim_spread"), stats.get("gap")),
            **evidence)
    # 规格第 5 条：动作目标只来自亮牌框定位。
    box = tuple(int(v) for v in boxes[index])
    tap_point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    diagnostic = _diagnostic_for_box(result, box)
    tile_label = diagnostic.get("label") if diagnostic else None
    tile_id = _as_int(diagnostic.get("tile_id")) if diagnostic else None
    signature = None
    try:
        signature = tile_signature(image[box[1]:box[3], box[0]:box[2]])
    except Exception:  # noqa: BLE001 - 签名只是跨帧兜底证据，拿不到不挡命中
        signature = None
    hit_reason = (
        "单亮牌摸切态：%d 框中第 %d 框独亮 %.1f，其余暗簇均值 %s、极差 %s，"
        "亮牌 label=%r"
        % (len(boxes), index, float(values[index]),
           stats.get("dim_mean"), stats.get("dim_spread"), tile_label))
    return SingleBrightCut(
        hit=True,
        reason=hit_reason,
        bright_index=index, bright_box=box, tap_point=tap_point,
        tile_id=tile_id,
        tile_label=tile_label if isinstance(tile_label, str) else None,
        streak=1, evidence=evidence, face_signature=signature)


def _same_bright_tile(first: SingleBrightCut, second: SingleBrightCut) -> bool:
    """两帧命中的亮牌是否同一张：位置（中心漂移）+ 牌面（label/签名）。"""
    if first.bright_box is None or second.bright_box is None:
        return False
    if first.tap_point is None or second.tap_point is None:
        return False
    if (abs(first.tap_point[0] - second.tap_point[0]) > CENTER_TOLERANCE_PX
            or abs(first.tap_point[1] - second.tap_point[1]) > CENTER_TOLERANCE_PX):
        return False
    if first.tile_label and second.tile_label:
        return first.tile_label == second.tile_label
    if first.face_signature is not None and second.face_signature is not None:
        a = np.asarray(first.face_signature, dtype=np.float64).ravel()
        b = np.asarray(second.face_signature, dtype=np.float64).ravel()
        if a.shape != b.shape:
            return False
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        if norm <= 0:
            return False
        return float(a @ b) / norm >= MIN_FACE_SIGNATURE_SIMILARITY
    return False


class SingleBrightCutTracker:
    """薄状态封装（规格第 3 条）：连续一致才确认。

    调用方每个 tick 把 ``(image, result)`` 喂给 :meth:`feed`；只有返回
    的 ``confirmed`` 为 ``True`` 时才许沿 ``tap_point`` 执行摸切。
    任何一帧不命中、或亮牌位置/牌面变了，连续计数清零重来。换局时
    调用方应 :meth:`reset`（与 turn_gate 的 round 隔离同一纪律）。
    """

    def __init__(self, required_frames: int = REQUIRED_FRAMES):
        self.required_frames = max(1, int(required_frames))
        self._last: SingleBrightCut | None = None
        self._streak = 0

    def reset(self) -> None:
        """清空连续帧状态（换局/停机后调用）。"""
        self._last = None
        self._streak = 0

    def feed(self, image, result: dict | None, *,
             caishen_label: str | None = None,
             mode: str | None = None) -> SingleBrightCut:
        """检测本帧并推进连续一致计数。"""
        return self.observe(detect_single_bright_cut(
            image, result, caishen_label=caishen_label, mode=mode))

    def observe(self, verdict: SingleBrightCut) -> SingleBrightCut:
        """把一份现成的单帧检测结论喂进连续一致判定（便于离线复核）。"""
        if not verdict.hit:
            self.reset()
            return verdict
        if self._last is not None and _same_bright_tile(self._last, verdict):
            self._streak += 1
        else:
            self._streak = 1
        self._last = verdict
        confirmed = self._streak >= self.required_frames
        return replace(
            verdict, streak=self._streak, confirmed=confirmed,
            reason=verdict.reason + (f"；连续 {self._streak} 帧一致，已确认"
                                     if confirmed else
                                     f"；连续 {self._streak}/{self.required_frames} 帧，待确认"))
