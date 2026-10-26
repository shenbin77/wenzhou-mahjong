"""解析层唯一对外入口：``resolve(image, action_name) -> ResolvedTarget | Denied``。

## 这个模块只做一件事

**把"某个画面上的某个目标在哪"变成一个可复核的结论，或者一个明确的拒绝。**
它不点击、不发 adb、不读设备状态。所有"要不要点、能不能点"的判断留在别的
模块（``tap_executor`` / ``auto_play_loop``），本层只负责"看"。

## 拒绝链路（每一步都可能 deny）

1. 帧不可用 / 尺寸不是 1600x900 → ``frame_unverified``
2. 画面没登记（指纹不认识）→ ``screen_unknown`` / ``screen_ambiguous``
3. 该画面没登记这个 action → ``action_unregistered``
4. 定位器找不到可信候选 → ``target_missing``
5. 找到了但中心不在该 action 允许的几何带内 → ``target_geometry``

任何一条触发都返回 :class:`Denied`，**绝不返回"最接近的猜测"**。
`Denied.code` 是给上层做统计/断点用的稳定枚举，`Denied.reason` 是给人看的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import anchor as anchor_mod
from . import shapes
from .palette import validate_frame
from .registry import (
    ActionSpec,
    FRAME_SIZE,
    ScreenMatch,
    ScreenRegistry,
    build_default_registry,
)

__all__ = [
    "ResolvedTarget",
    "Denied",
    "resolve",
    "explain",
    "default_registry",
    "box_contains_point",
]

Box = tuple[int, int, int, int]
Point = tuple[int, int]

_DEFAULT_REGISTRY: ScreenRegistry | None = None


def default_registry() -> ScreenRegistry:
    """进程内共享的默认注册表（首次调用时从真实帧构建）。"""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def box_contains_point(box: Box, point: Point) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


@dataclass(frozen=True)
class ResolvedTarget:
    """一次成功的解析。

    ``score`` 的含义随 ``method`` 变化，**不是概率**：

    * ``anchor``：模板相关分（``TM_CCOEFF_NORMED`` 最高分）；
    * ``color_shape`` / ``color_region``：画面识别分（这一帧像不像该画面）。
      形状本身的证据（fill/aspect/margin/area）在 ``evidence["chosen"]`` 里。
    """

    action: str
    point: Point
    box: Box
    screen_id: str
    method: str
    score: float
    evidence: dict = field(default_factory=dict)
    ok: bool = True

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "action": self.action,
            "screen_id": self.screen_id,
            "method": self.method,
            "point": list(self.point),
            "box": list(self.box),
            "score": round(float(self.score), 4),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Denied:
    """一次被拒的解析。``code`` 稳定可枚举，``reason`` 供人读。"""

    action: str
    code: str
    reason: str
    screen_id: str | None = None
    evidence: dict = field(default_factory=dict)
    ok: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": False,
            "action": self.action,
            "screen_id": self.screen_id,
            "code": self.code,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _deny(action: str, code: str, reason: str, screen_id: str | None = None,
          evidence: dict | None = None) -> Denied:
    return Denied(action=action, code=code, reason=reason, screen_id=screen_id,
                  evidence=evidence or {})


def _locate_color(image: np.ndarray, spec_action: ActionSpec, screen_id: str,
                  match: ScreenMatch) -> ResolvedTarget | Denied:
    limits = spec_action.limits_or(
        shapes.REGION_LIMITS if spec_action.locator == "color_region" else shapes.BUTTON_LIMITS)
    finder = shapes.find_regions if spec_action.locator == "color_region" else shapes.find_buttons
    candidates = finder(image, spec_action.color, roi=spec_action.roi, limits=limits)
    scanned = shapes.scan_shapes(image, spec_action.color, roi=spec_action.roi, limits=limits)
    rejected = [record.as_dict() for record in scanned if not record.ok]
    evidence = {
        "roi": list(spec_action.roi),
        "color": spec_action.color,
        "candidates": len(candidates),
        "rejected": rejected[:6],
        "rejected_total": len(rejected),
        "limits": limits.as_dict(),
        "screen_score": round(match.score, 4),
        "screen_margin": round(match.margin, 4),
    }
    if len(candidates) < spec_action.min_candidates:
        return _deny(
            spec_action.name, "target_missing",
            (f"{screen_id} 的 {spec_action.name}：{spec_action.color} 在 ROI "
             f"{spec_action.roi} 内只找到 {len(candidates)} 个合格候选"
             f"（要求 ≥{spec_action.min_candidates}）"),
            screen_id=screen_id, evidence=evidence,
        )
    chosen = shapes.pick(candidates, spec_action.pick, roi=spec_action.roi)
    if chosen is None:
        return _deny(spec_action.name, "target_missing",
                     f"{screen_id} 的 {spec_action.name}：选取策略 {spec_action.pick} 无结果",
                     screen_id=screen_id, evidence=evidence)
    evidence["chosen"] = chosen.as_dict()
    if spec_action.expect_center is not None and not box_contains_point(
            spec_action.expect_center, chosen.center):
        return _deny(
            spec_action.name, "target_geometry",
            (f"{screen_id} 的 {spec_action.name}：命中中心 {chosen.center} 不在允许带 "
             f"{spec_action.expect_center} 内"),
            screen_id=screen_id, evidence=evidence,
        )
    return ResolvedTarget(
        action=spec_action.name, point=chosen.center, box=chosen.box,
        screen_id=screen_id, method=spec_action.locator,
        score=float(match.score), evidence=evidence,
    )


def _locate_anchor(image: np.ndarray, spec_action: ActionSpec, screen_id: str,
                   match: ScreenMatch, registry: ScreenRegistry) -> ResolvedTarget | Denied:
    template = registry.template(spec_action.anchor_id or "")
    evidence = {
        "roi": list(spec_action.roi),
        "anchor_id": spec_action.anchor_id,
        "min_score": spec_action.anchor_min_score,
        "min_margin": spec_action.anchor_min_margin,
        "screen_score": round(match.score, 4),
    }
    if template is None:
        return _deny(spec_action.name, "target_missing",
                     f"{screen_id} 的 {spec_action.name}：锚点模板 {spec_action.anchor_id} 未加载",
                     screen_id=screen_id, evidence=evidence)
    hit = anchor_mod.match(
        image, template, spec_action.roi,
        min_score=spec_action.anchor_min_score,
        min_margin=spec_action.anchor_min_margin,
    )
    if hit is None:
        return _deny(
            spec_action.name, "target_missing",
            (f"{screen_id} 的 {spec_action.name}：锚点 {spec_action.anchor_id} 在多尺度下"
             f"没有可信命中（或次名差不足）"),
            screen_id=screen_id, evidence=evidence)
    evidence.update({"anchor": hit.as_dict()})
    if spec_action.expect_center is not None and not box_contains_point(
            spec_action.expect_center, hit.center):
        return _deny(
            spec_action.name, "target_geometry",
            (f"{screen_id} 的 {spec_action.name}：锚点中心 {hit.center} 不在允许带 "
             f"{spec_action.expect_center} 内"),
            screen_id=screen_id, evidence=evidence)
    return ResolvedTarget(
        action=spec_action.name, point=hit.center, box=hit.box, screen_id=screen_id,
        method="anchor", score=hit.score, evidence=evidence,
    )


def resolve(image, action_name: str, registry: ScreenRegistry | None = None):
    """唯一的对外入口。返回 :class:`ResolvedTarget` 或 :class:`Denied`。"""
    reg = registry or default_registry()
    if not isinstance(action_name, str) or not action_name:
        return _deny(str(action_name), "action_unregistered", "动作名为空")
    error = validate_frame(image, FRAME_SIZE)
    if error:
        return _deny(action_name, "frame_unverified", error)
    match = reg.identify(image)
    if not match.ok:
        return _deny(action_name, match.code, match.reason, screen_id=None,
                     evidence={"screen": match.as_dict()})
    screen_id = match.screen_id or ""
    spec_action = reg.action(screen_id, action_name)
    if spec_action is None:
        return _deny(
            action_name, "action_unregistered",
            (f"画面 {screen_id} 没有登记动作 {action_name!r}；该画面允许："
             f"{list(reg.allowed_actions(screen_id))}"),
            screen_id=screen_id, evidence={"screen": match.as_dict()},
        )
    if spec_action.locator == "anchor":
        return _locate_anchor(image, spec_action, screen_id, match, reg)
    return _locate_color(image, spec_action, screen_id, match)


def explain(image, action_name: str, registry: ScreenRegistry | None = None) -> dict:
    """resolve 的日志版：把结果压成一个可序列化 dict（可直接写 jsonl）。"""
    return resolve(image, action_name, registry).as_dict()
