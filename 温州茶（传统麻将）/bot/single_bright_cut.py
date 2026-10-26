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

多亮白名单（亮簇 1..N 张）
=========================
实机反馈：压暗态下亮起的牌不止一张（``_takeover/wb_live/5558_t4.png``
实测 17 框里 15 框 100.0 + 2 框 214.0 = 南/北；用户截图另有「1条×2 +
南」）。「恰 1 亮」因此不成立，循环卡在「识牌不完整/等待摸牌」。

:func:`detect_bright_subset_cut` 复用同一把亮度尺，把「亮簇」整组作为
**可打白名单**：物理框守恒、暗簇内聚、亮暗差、亮簇张数 ≤ 手牌半数这些
条件与单亮同源，只是把「恰 1 亮」放宽为「1..N 亮」，并在产出白名单前
先剔除财神位（「财」角标 / 牌面 == 本局财神）。白名单本身**不是**出牌
决定——打哪张仍只认既有算牌链路的建议，且要求白名单连续 2 帧一致；任何
一处不成立就弃权并记 ``bright_subset_unconfirmed``，绝不猜牌。
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
# --- 多亮白名单（亮簇 1..N 张）-------------------------------------------
# 实测（2026-10-25 用户实机 + ``_takeover/wb_live/5558_t4.png``）：同一「请出牌」
# 压暗态下亮起的牌不止一张——17 框里 15 框 100.0、2 框 214.0（南 + 北），另两
# 张用户截图是「1条×2 + 南」与「南 + 北」。故「恰 1 亮」放宽为「亮簇 = 可打
# 白名单」，但打哪张仍只认算牌器的建议，绝不猜。
MAX_BRIGHT_CANDIDATE_DIVISOR = 2  # 亮簇上限 = 手牌张数 // 该值（超出即不命中）
BRIGHT_CANDIDATE_MAX_ABS = 9      # 绝对兜底上限：亮框多于 9 张必不是「压暗少数亮」
# 多亮白名单未确认时的门禁名（进台账 gate 字段，绝不点击）。
BRIGHT_SUBSET_GATE = "bright_subset_unconfirmed"


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


def _face_signature(image, box) -> np.ndarray | None:
    """亮/候选牌框的字形签名；只是跨帧兜底证据，拿不到不算错。"""
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    try:
        x0, y0, x1, y1 = (int(v) for v in box)
    except (TypeError, ValueError):
        return None
    height, width = image.shape[:2]
    x0, y0 = max(0, min(x0, width)), max(0, min(y0, height))
    x1, y1 = max(0, min(x1, width)), max(0, min(y1, height))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    try:
        return tile_signature(image[y0:y1, x0:x1])
    except Exception:  # noqa: BLE001 - 签名只是兜底证据，拿不到不挡命中
        return None


# ---------------------------------------------------------------- 门禁小件
# 单亮与多亮两个检测器共用同一批门禁小件：措辞、口径、证据字段完全一致，
# 唯一差别是调用顺序（多亮先做便宜的亮度图案，命中后才做重量级的结算/圆盘
# 判定——两者都在任何点击之前，顺序只影响每帧开销，不影响安全边界）。
def _frame_gate(image) -> tuple[str | None, dict]:
    """画面可用性（尺寸必须是已验证的 1600×900）。"""
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return "画面不可用：不命中", {}
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        return (f"画面尺寸 {width}×{height} 未验证：不命中",
                {"frame_size": [width, height]})
    return None, {}


def _mode_caishen_gate(result: dict, mode: str | None,
                       caishen_label: str | None
                       ) -> tuple[str | None, dict, int | None]:
    """玩法必须是 tea_2p，且本局财神已知（规格第 4 条之一/之二）。"""
    effective_mode = mode or result.get("mode")
    if effective_mode != MODE_TEA_2P:
        return f"模式不是 tea_2p（{effective_mode!r}）：不命中", {}, None
    caishen_id = TILE_IDS.get(caishen_label) if caishen_label else None
    if caishen_id is None:
        caishen_id = known_caishen_id(result)
    if caishen_id is None:
        return "本局财神未知：不命中", {}, None
    return None, {}, caishen_id


def _settled_gate(image) -> tuple[str | None, dict]:
    """结算/牌局结束/模态弹窗：任一信号命中即出局。"""
    settled, evidence = detect_settled_screen(image)
    if settled or evidence.get("settled_signals"):
        return ("结算/面板类信号命中（"
                + "+".join(evidence.get("settled_signals") or [])
                + "）：不命中",
                {"panel_area_pct": evidence.get("panel_area_pct")})
    if evidence.get("settled_unknown"):
        return "画面类型不可判读：不命中", {}
    return None, {}


def _claim_gate(image) -> tuple[str | None, dict]:
    """行动圆盘不可见（圆盘在场时由行动路径处理）。"""
    try:
        from claim_vision import read_claims
        claim_state = read_claims(image)
    except Exception as exc:  # noqa: BLE001 - 读不出行动行 = fail closed
        return f"行动行读取失败：不命中（{exc}）", {}
    if claim_state.known or claim_state.buttons:
        return ("行动圆盘可见：不命中（由行动路径处理）",
                {"claim_buttons": [b.kind for b in claim_state.buttons]})
    return None, {}


def _hand_gate(result: dict) -> tuple[str | None, dict, list, int]:
    """物理框守恒：牌框数必须恰等于出牌态应有张数（规格第 1 条）。"""
    boxes = concealed_boxes(result)
    expected = TILES_PER_HAND - logical_meld_slots(result)
    evidence = {"physical_boxes": len(boxes), "expected_concealed": expected}
    if expected < 1 or len(boxes) != expected:
        return (f"物理框 {len(boxes)} 张 != 出牌态应有 {expected} 张：不命中",
                evidence, boxes, expected)
    return None, evidence, boxes, expected


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
    reason, frame_evidence = _frame_gate(image)
    if reason:
        return miss(reason, **frame_evidence)
    reason, caishen_evidence, caishen_id = _mode_caishen_gate(
        result, mode, caishen_label)
    if reason:
        return miss(reason, **caishen_evidence)
    # 前置排除：结算/结束/模态弹窗/胡提示面板（任一信号命中即出局）。
    reason, settled_evidence = _settled_gate(image)
    if reason:
        return miss(reason, **settled_evidence)
    # 规格第 4 条之三：行动圆盘不可见（圆盘在场时由行动路径处理）。
    reason, claim_evidence = _claim_gate(image)
    if reason:
        return miss(reason, **claim_evidence)
    # 规格第 1 条：物理框守恒（与 turn_gate 同一口径）。
    reason, box_evidence, boxes, expected = _hand_gate(result)
    if reason:
        return miss(reason, **box_evidence)
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
    signature = _face_signature(image, box)
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


# ==================================================================
# 多亮白名单（亮簇 1..N 张）
# ==================================================================
# 问题：压暗态下亮起的牌不止一张（实测 2 张、用户截图 3 张），
# :func:`detect_single_bright_cut` 的「恰 1 亮」不成立 → 循环停在「识牌不
# 完整」/「等待摸牌」，必须人工点牌。
#
# 解法：把「亮簇」整组当作**可打白名单**交给既有算牌链路，打哪张仍只认算
# 牌器的建议（等于：建议牌 ∈ 白名单 且 白名单连续 2 帧一致 才点）。任何一
# 处不成立就弃权（gate :data:`BRIGHT_SUBSET_GATE`），绝不猜牌。
@dataclass(frozen=True)
class BrightCandidate:
    """白名单里的一张亮牌（可打候选）。

    Attributes:
        index: 亮牌在暗手框序（``concealed_boxes`` 顺序）中的下标。
        box: 物理框 ``(x0, y0, x1, y1)``，1600×900 坐标。
        tap_point: 该候选的点击点 = 框中心。
        tile_id / tile_label: 牌面（来自同框诊断）；读不出时 ``None``。
        face_value: 实测面亮度 p70(V)。
        is_caishen: 该框是否被财神证据命中（角标或牌面 == 本局财神）。
    """

    index: int
    box: tuple[int, int, int, int]
    tap_point: tuple[int, int]
    tile_id: int | None
    tile_label: str | None
    face_value: float
    is_caishen: bool = False
    face_signature: np.ndarray | None = field(
        default=None, repr=False, compare=False)

    def ledger(self) -> dict:
        """台账用摘要（JSON 可序列化，不含签名数组）。"""
        return {
            "index": self.index, "box": list(self.box),
            "tap_point": list(self.tap_point),
            "tile_id": self.tile_id, "tile_label": self.tile_label,
            "face_value": round(float(self.face_value), 1),
        }


@dataclass(frozen=True)
class BrightSubsetCut:
    """多亮白名单的单帧检测（或经 tracker 确认后）的结论。

    Attributes:
        hit: 本帧是否命中「压暗 + 少数亮」图案且门禁全过。
        reason: 人类可读依据，直接进台账。
        candidates: 白名单（已剔除财神位）；按框序升序。
        excluded: 被剔除的财神位证据（进台账，便于复核）。
        bright_count: 剔除财神位**之前**的亮框张数（1..N）。
        unresolved: 是否有候选牌面读不出（``tile_id is None``）。
        streak / confirmed: 连续一致帧数与确认标志；只有 ``confirmed`` 且
            :attr:`multi_candidate` 才许进入多亮执行路径。
        evidence: 本帧实测值（张数、逐框亮度、分簇统计等）。
    """

    hit: bool
    reason: str
    candidates: tuple[BrightCandidate, ...] = ()
    excluded: tuple[dict, ...] = ()
    bright_count: int = 0
    unresolved: bool = False
    streak: int = 0
    confirmed: bool = False
    evidence: dict = field(default_factory=dict)

    @property
    def multi_candidate(self) -> bool:
        """亮框是否**多于一张**（单亮仍走原有单亮路径，行为不变）。"""
        return self.bright_count >= 2

    def whitelist_ids(self) -> tuple[int, ...]:
        """白名单里能确定牌面的 id（牌面读不出的候选不在其中）。"""
        return tuple(int(c.tile_id) for c in self.candidates
                     if c.tile_id is not None)

    def ledger(self) -> dict:
        """台账用摘要（JSON 可序列化）。"""
        return {
            "hit": self.hit, "confirmed": self.confirmed,
            "streak": self.streak, "gate": BRIGHT_SUBSET_GATE,
            "reason": self.reason,
            "bright_count": self.bright_count,
            "multi_candidate": self.multi_candidate,
            "unresolved": self.unresolved,
            "whitelist": [c.ledger() for c in self.candidates],
            "excluded_caishen": [dict(item) for item in self.excluded],
            "evidence": dict(self.evidence),
        }


def bright_subset_pattern(values: Sequence[float | None]
                          ) -> tuple[list[int] | None, dict]:
    """纯图案判定：亮簇 1..N 张 + 暗簇内聚（多亮版 :func:`brightness_pattern`）。

    与 :func:`brightness_pattern` 用同一套常量语义（``BRIGHT_FACE_MIN`` /
    ``DIM_FACE_MAX`` / ``DIM_CLUSTER_MAX_SPREAD`` / ``MIN_BRIGHT_GAP``），
    只把「恰 1 亮」放宽成「亮簇 1..N」，N 限为手牌张数的一半（且 ≤
    :data:`BRIGHT_CANDIDATE_MAX_ABS`）——亮框过半说明这根本不是「压暗 +
    少数亮」图案（正常出牌态是它的镜像：1 暗 + 其余全亮）。

    Args:
        values: 逐框面亮度（与 boxes 同序）。

    Returns:
        ``(亮框下标升序列表, stats)``；不命中时下标为 ``None``，stats 带
        ``bright_count`` / ``dim_count`` / ``mid_count`` / ``dim_mean`` /
        ``dim_spread`` / ``gap`` / ``max_bright_candidates`` 等实测值。
    """
    stats: dict = {"bright_count": 0, "dim_count": 0, "mid_count": 0,
                   "dim_mean": None, "dim_spread": None, "gap": None,
                   "bright_min": None, "bright_max": None,
                   "max_bright_candidates": 0}
    if values is None or len(values) < 2:
        stats["pattern_error"] = "框数不足"
        return None, stats
    if any(v is None for v in values):
        stats["pattern_error"] = "有框亮度不可测"
        return None, stats
    numeric = [float(v) for v in values]
    cap = min(max(1, len(numeric) // MAX_BRIGHT_CANDIDATE_DIVISOR),
              BRIGHT_CANDIDATE_MAX_ABS)
    stats["max_bright_candidates"] = cap
    bright = [i for i, v in enumerate(numeric) if v >= BRIGHT_FACE_MIN]
    dims = [i for i, v in enumerate(numeric) if v <= DIM_FACE_MAX]
    mids = [i for i, v in enumerate(numeric)
            if DIM_FACE_MAX < v < BRIGHT_FACE_MIN]
    stats["bright_count"] = len(bright)
    stats["dim_count"] = len(dims)
    stats["mid_count"] = len(mids)
    if bright:
        stats["bright_min"] = min(numeric[i] for i in bright)
        stats["bright_max"] = max(numeric[i] for i in bright)
    if not bright:
        stats["pattern_error"] = "没有亮框（整手压暗或全暗）"
        return None, stats
    if len(bright) > cap:
        stats["pattern_error"] = (f"亮框 {len(bright)} 张超过上限 {cap}"
                                 "（更像正常出牌态）")
        return None, stats
    if mids:
        stats["pattern_error"] = f"亮暗之间有 {len(mids)} 个中间亮度框"
        return None, stats
    if not dims:
        stats["pattern_error"] = "没有暗簇（整手全亮）"
        return None, stats
    dim_values = [numeric[i] for i in dims]
    dim_mean = float(np.mean(dim_values))
    dim_spread = float(max(dim_values) - min(dim_values))
    gap = float(stats["bright_min"]) - dim_mean
    stats.update({"dim_mean": round(dim_mean, 2),
                  "dim_spread": round(dim_spread, 2),
                  "gap": round(gap, 2),
                  "dim_max": max(dim_values),
                  "dim_min": min(dim_values)})
    if dim_spread > DIM_CLUSTER_MAX_SPREAD:
        return None, stats
    if gap < MIN_BRIGHT_GAP:
        return None, stats
    return bright, stats


def detect_bright_subset_cut(image, result: dict | None, *,
                             caishen_label: str | None = None,
                             mode: str | None = None) -> BrightSubsetCut:
    """判定单帧是否命中「压暗 + 少数亮」的多亮牌态（亮簇 = 可打白名单）。

    流程与 :func:`detect_single_bright_cut` 同源（同一批门禁小件、同一把
    亮度尺），差别只有两点：

    1. 图案判定用 :func:`bright_subset_pattern`（亮簇 1..N），
    2. 命中后把亮簇**剔除财神位**再产出白名单——财神牌自带「财」高亮/角标，
       天然与普通亮牌混在一起，若不去掉会被当成可打牌。

    为了每帧开销，先做便宜的亮度图案判定，图案命中后才做结算/圆盘前置
    排除；两者都在任何点击之前，顺序只影响开销、不影响安全边界。

    注意：亮簇只有 1 张时本函数同样命中，但调用方必须优先走单亮路径
    （:func:`detect_single_bright_cut`，行为已实机验证）；:attr:`BrightSubsetCut.multi_candidate`
    为 ``True`` 才是「单亮路径表达不了」的多亮情形。

    Args:
        image: 当前帧 BGR 图（只读），必须是已验证的 1600×900。
        result: ``assist_tea.analyze_snapshot`` 的返回体（只读）。
        caishen_label: 调用方已知的本局财神 label（如 ``"t5"``）。
        mode: 调用方已知的玩法；缺省取 ``result["mode"]``。

    Returns:
        :class:`BrightSubsetCut`；``hit`` 只代表单帧图案与门禁全过，可执行
        性由 tracker 的 ``confirmed`` 决定。
    """
    def miss(reason: str, **evidence) -> BrightSubsetCut:
        return BrightSubsetCut(hit=False, reason=reason, evidence=evidence)

    if result is None or not isinstance(result, dict):
        return miss("分析结果缺失：不命中")
    reason, frame_evidence = _frame_gate(image)
    if reason:
        return miss(reason, **frame_evidence)
    reason, caishen_evidence, caishen_id = _mode_caishen_gate(
        result, mode, caishen_label)
    if reason:
        return miss(reason, **caishen_evidence)
    reason, box_evidence, boxes, expected = _hand_gate(result)
    if reason:
        return miss(reason, **box_evidence)
    values = box_face_values(image, boxes)
    if any(v is None for v in values):
        return miss("逐框亮度不可测：不命中", **box_evidence)
    indices, stats = bright_subset_pattern(values)
    evidence = {
        **box_evidence,
        "caishen_id": caishen_id,
        "face_values": [round(float(v), 1) for v in values],
        **stats,
    }
    if indices is None:
        return miss(
            "多亮图案不符（亮框 %s 张 / 暗框 %s 张 / 中间 %s 张，暗簇均值 %s、"
            "极差 %s、亮暗差 %s，%s）：不命中"
            % (stats.get("bright_count"), stats.get("dim_count"),
               stats.get("mid_count"), stats.get("dim_mean"),
               stats.get("dim_spread"), stats.get("gap"),
               stats.get("pattern_error") or "分布不满足白名单条件"),
            **evidence)
    # 重量级前置排除推迟到图案命中之后（见 docstring）。
    reason, settled_evidence = _settled_gate(image)
    if reason:
        return miss(reason, **{**evidence, **settled_evidence})
    reason, claim_evidence = _claim_gate(image)
    if reason:
        return miss(reason, **{**evidence, **claim_evidence})
    effective_caishen_label = caishen_label if caishen_label else result.get("caishen")
    candidates: list[BrightCandidate] = []
    excluded: list[dict] = []
    for index in indices:
        box = tuple(int(v) for v in boxes[index])
        tap_point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        diagnostic = _diagnostic_for_box(result, box)
        tile_id = _as_int(diagnostic.get("tile_id")) if diagnostic else None
        raw_label = diagnostic.get("label") if diagnostic else None
        tile_label = raw_label if isinstance(raw_label, str) else None
        # 财神位必须先从亮簇剔除：游戏给财神牌加「财」高亮/角标，亮度与普通
        # 亮牌无法区分，留着会被当成可打牌。三条证据任一成立即剔除。
        marked = bool(diagnostic.get("caishen_marked")) if diagnostic else False
        is_caishen = bool(
            marked
            or (caishen_id is not None and tile_id is not None
                and int(tile_id) == int(caishen_id))
            or (effective_caishen_label and tile_label
                and tile_label == effective_caishen_label))
        if is_caishen:
            excluded.append({"index": index, "box": list(box),
                             "tile_id": tile_id, "tile_label": tile_label,
                             "caishen_marked": marked,
                             "face_value": round(float(values[index]), 1)})
            continue
        candidates.append(BrightCandidate(
            index=index, box=box, tap_point=tap_point, tile_id=tile_id,
            tile_label=tile_label, face_value=float(values[index]),
            is_caishen=is_caishen,
            face_signature=_face_signature(image, box)))
    evidence["excluded_caishen_count"] = len(excluded)
    evidence["whitelist_count"] = len(candidates)
    if not candidates:
        return miss(f"亮簇 {len(indices)} 张全部是财神位：没有可打白名单",
                    **evidence)
    unresolved = any(c.tile_id is None for c in candidates)
    reason_text = (
        "多亮白名单态：%d 框中 %d 框亮（%s），暗簇均值 %s、极差 %s、亮暗差 %s；"
        "剔除财神位 %d 张后白名单 %d 张（%s）%s"
        % (len(boxes), len(indices),
           "/".join("%d:%.0f" % (i, float(values[i])) for i in indices),
           stats.get("dim_mean"), stats.get("dim_spread"), stats.get("gap"),
           len(excluded), len(candidates),
           "/".join("%r" % (c.tile_label,) for c in candidates),
           "；有候选牌面读不出，弃权" if unresolved else ""))
    return BrightSubsetCut(
        hit=True, reason=reason_text, candidates=tuple(candidates),
        excluded=tuple(excluded), bright_count=len(indices),
        unresolved=unresolved, streak=1, evidence=evidence)


def _same_candidate_sets(first: BrightSubsetCut,
                         second: BrightSubsetCut) -> bool:
    """两帧白名单是否一致：张数、位置（中心漂移）、牌面（id/签名）。"""
    if len(first.candidates) != len(second.candidates):
        return False
    for left, right in zip(first.candidates, second.candidates):
        if (abs(left.tap_point[0] - right.tap_point[0]) > CENTER_TOLERANCE_PX
                or abs(left.tap_point[1] - right.tap_point[1])
                > CENTER_TOLERANCE_PX):
            return False
        if left.tile_id is not None and right.tile_id is not None:
            if int(left.tile_id) != int(right.tile_id):
                return False
            continue
        if left.face_signature is not None and right.face_signature is not None:
            a = np.asarray(left.face_signature, dtype=np.float64).ravel()
            b = np.asarray(right.face_signature, dtype=np.float64).ravel()
            if a.shape != b.shape:
                return False
            norm = float(np.linalg.norm(a) * np.linalg.norm(b))
            if norm <= 0:
                return False
            if float(a @ b) / norm < MIN_FACE_SIGNATURE_SIMILARITY:
                return False
            continue
        # 两帧都拿不到牌面身份（id 与签名都缺）：位置与张数是仅剩的锚点。
        # 这类白名单带 unresolved 标记，调用方一律弃权（绝不点击），故此
        # 处按「同一张」计只影响台账与连续帧计数，不会解锁任何动作。
        if left.tile_id is None and right.tile_id is None:
            continue
        return False
    return True


class BrightSubsetCutTracker:
    """多亮白名单的连续帧确认器（与 :class:`SingleBrightCutTracker` 同纪律）。

    调用方每个 tick 喂 ``(image, result)``；只有 ``confirmed`` 为 ``True``
    且 ``multi_candidate`` 为 ``True`` 时才许进入多亮执行路径（且仍要求本帧
    算牌建议落在白名单内）。白名单张数、位置或牌面任何一处变了，连续计数
    从 1 重来；不命中即清零。换局时调用方应 :meth:`reset`。
    """

    def __init__(self, required_frames: int = REQUIRED_FRAMES):
        self.required_frames = max(1, int(required_frames))
        self._last: BrightSubsetCut | None = None
        self._streak = 0

    def reset(self) -> None:
        """清空连续帧状态（换局/停机后调用）。"""
        self._last = None
        self._streak = 0

    def feed(self, image, result: dict | None, *,
             caishen_label: str | None = None,
             mode: str | None = None) -> BrightSubsetCut:
        """检测本帧并推进连续一致计数。"""
        return self.observe(detect_bright_subset_cut(
            image, result, caishen_label=caishen_label, mode=mode))

    def observe(self, verdict: BrightSubsetCut) -> BrightSubsetCut:
        """把一份现成的单帧检测结论喂进连续一致判定（便于离线复核）。"""
        if not verdict.hit:
            self.reset()
            return verdict
        if self._last is not None and _same_candidate_sets(self._last, verdict):
            self._streak += 1
        else:
            self._streak = 1
        self._last = verdict
        confirmed = self._streak >= self.required_frames
        return replace(
            verdict, streak=self._streak, confirmed=confirmed,
            reason=verdict.reason + (
                f"；连续 {self._streak} 帧一致，已确认"
                if confirmed else
                f"；连续 {self._streak}/{self.required_frames} 帧，待确认"))
