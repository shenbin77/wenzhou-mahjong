"""Pure display/refresh logic for the real-time assist window v2.

Nothing here touches tkinter, ADB or the game.  The window feeds it frame
results; it owns latency statistics, the display state machine and the
adaptive polling cadence.  Recording stays in ``recorder.AssistRecorder``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime

from tile_vision import TILE_IDS

BASE_INTERVAL_MS = 800
MIN_INTERVAL_MS = 300
MAX_INTERVAL_MS = 5000
STALE_SECONDS = 3.0
WAITING_TITLE = "等待摸牌"
STAGES = ("capture", "recognize", "advise")

FrameKey = tuple[tuple[str, ...] | None, str | None, tuple | None]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass(frozen=True)
class FrameTiming:
    """Segments plus measured capture-to-render and refresh upper bound.

    ``refresh_ms`` includes the wait since the previous completed render. It
    bounds a change that happened just after that render; it is not a measured
    game hand-change timestamp.
    """

    capture_ms: float = 0.0
    recognize_ms: float = 0.0
    advise_ms: float = 0.0
    cycle_ms: float = 0.0
    folder: str = ""
    capture_to_render_ms: float | None = None
    refresh_ms: float | None = None

    @property
    def total_ms(self) -> float:
        if self.refresh_ms is not None:
            return self.refresh_ms
        return self.capture_ms + self.recognize_ms + self.advise_ms

    def as_dict(self) -> dict[str, float | None]:
        return {
            "capture_ms": round(self.capture_ms, 1),
            "recognize_ms": round(self.recognize_ms, 1),
            "advise_ms": round(self.advise_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "cycle_ms": round(self.cycle_ms, 1),
            "capture_to_render_ms": (round(self.capture_to_render_ms, 1)
                                     if self.capture_to_render_ms is not None else None),
        }


class LatencyTracker:
    """Rolling per-frame latency window plus the adaptive poll interval."""

    def __init__(
        self,
        window: int = 60,
        *,
        base_ms: int = BASE_INTERVAL_MS,
        min_ms: int = MIN_INTERVAL_MS,
        max_ms: int = MAX_INTERVAL_MS,
    ) -> None:
        self.window = max(1, int(window))
        self.base_ms = int(base_ms)
        self.min_ms = int(min_ms)
        self.max_ms = int(max_ms)
        self.frames: deque[FrameTiming] = deque(maxlen=self.window)
        self.interval_ms = self.base_ms
        self.folder = ""
        self.periods: deque[float] = deque(maxlen=self.window)
        self.last_period_ms = 0.0

    def percentile(self, stage: str, fraction: float) -> float | None:
        if stage == "total":
            values = [frame.total_ms for frame in self.frames]
        else:
            values = [float(getattr(frame, f"{stage}_ms")) for frame in self.frames]
        return round(_percentile(values, fraction), 1) if values else None

    def summary(self) -> dict[str, float | None]:
        summary = {
            f"{stage}_{name}": self.percentile(stage, fraction)
            for stage in (*STAGES, "total")
            for name, fraction in (("p50", 0.5), ("p95", 0.95), ("max", 1.0))
        }
        summary["period_p50"] = (round(_percentile(list(self.periods), 0.5), 1)
                                 if self.periods else None)
        summary["period_p95"] = (round(_percentile(list(self.periods), 0.95), 1)
                                 if self.periods else None)
        return summary

    def record(self, timing: FrameTiming, *, period_ms: float | None = None) -> int:
        """Record one frame; ``period_ms`` is the measured capture-to-capture span."""
        self.frames.append(timing)
        if period_ms is not None and period_ms > 0:
            self.last_period_ms = float(period_ms)
            self.periods.append(float(period_ms))
        # Target-period cadence (changed 2026-10-25; evidence in
        # _takeover/repair_assist_report.md section 2): interval_ms is
        # the wait AFTER this frame has been processed, so size it to
        # what remains of the target period instead of stacking a whole
        # base interval on top of the processing time.  The old formula
        # (wait = max(base, cycle*1.2)) pushed the live capture-to-
        # capture period to ~2.1 s (measured p95 2126 ms) while offline
        # processing p95 is only 337 ms.  Target period stays
        # max(base, cycle*1.2); the min clamp keeps a >= MIN_INTERVAL_MS
        # idle gap after every frame, so the poller can never busy-spin
        # even on fast or degenerate frames.
        target_period = max(float(self.base_ms), timing.cycle_ms * 1.2)
        wait = target_period - timing.cycle_ms
        self.interval_ms = int(max(self.min_ms, min(self.max_ms, wait)))
        return self.interval_ms

    def set_folder(self, label: str, since: float, now: float) -> int:
        """Lower the cadence once, when the same folded frame keeps arriving."""
        if label not in {frame.folder for frame in self.frames}:
            self.folder = label
            return self.interval_ms
        if not label or self.interval_ms >= self.max_ms:
            return self.interval_ms
        span = max(1, len(self.frames) - 1)
        average = sum(frame.cycle_ms for frame in self.frames) / span
        self.interval_ms = int(max(self.min_ms, min(self.max_ms, average * 1.2)))
        return self.interval_ms


def tile_label(tile_id) -> str:
    for label, value in TILE_IDS.items():
        if value == tile_id:
            return label
    return ""


def hand_multiset(tile_ids) -> tuple[str, ...] | None:
    """Normalised multiset used to detect a *changed* hand, not a redraw."""
    if not tile_ids:
        return None
    labels = []
    for tile_id in tile_ids:
        label = tile_label(tile_id)
        if not label:
            return None
        labels.append(label)
    return tuple(sorted(labels))


def frame_key(result: dict) -> FrameKey:
    """Cheap identity of what a frame would display; equal keys redraw nothing."""
    rows = result.get("candidates")
    candidates = None
    if rows:
        candidates = tuple(
            (str(row.get("tile")), row.get("shanten_after"), row.get("ukeire_count"))
            for row in rows[:3]
        )
    return (hand_multiset(result.get("hand_ids")), result.get("suggestion"), candidates)


@dataclass(frozen=True)
class DisplayState:
    """Exactly what the window should show for the latest frame."""

    status: str = "wait"
    title: str = WAITING_TITLE
    reason: str | None = None
    actionable: bool = False
    suggestion: str | None = None
    suggestion_label: str | None = None
    candidates: tuple = ()
    hand_labels: tuple = ()
    caishen_label: str | None = None
    caishen_source: str | None = None
    caishen_guard: bool = False
    caishen_id: int | None = None
    meld_count: int = 0
    shanten: int | None = None
    advice_is_stale: bool = True
    frame_key: FrameKey = (None, None, None)


class LiveRefreshState:
    """Display-side refresh state machine; records nothing."""

    def __init__(self, live_age_seconds: float = STALE_SECONDS) -> None:
        self.live_age_seconds = float(live_age_seconds)
        self.state = DisplayState(reason="未开始观察")
        self.updated_at: datetime | None = None
        self.last_frame_at: float | None = None
        self.frames_seen = 0

    def _caishen_guard(self, result: dict) -> bool:
        """Tea advice needs a known caishen; Shu fixes red Zhong."""
        if not str(result.get("mode", "")).startswith("tea_"):
            return False
        return result.get("caishen_id") is None and not result.get("caishen")

    @staticmethod
    def _low_confidence(result: dict) -> bool:
        critical = ("low_score", "ambiguous", "more_than_four")
        for item in result.get("vision_diagnostics") or []:
            if not item.get("accepted", True) and item.get("reason") in critical:
                return True
        return False

    def reset(self, reason: str = "已重置本局显示") -> DisplayState:
        self.state = DisplayState(reason=reason)
        self.updated_at = None
        return self.state

    def mark_stale(self, now: float, reason: str | None = None) -> DisplayState:
        """Never keep showing an old frame as if it were the current advice."""
        if (self.state.actionable and self.last_frame_at is not None
                and now - self.last_frame_at > self.live_age_seconds):
            self.state = replace(
                self.state, actionable=False, advice_is_stale=True,
                status="abstain", title="等待画面", suggestion=None,
                suggestion_label=None, candidates=(),
                reason=reason or "画面已超过 3 秒未更新",
            )
        return self.state

    def update(self, result: dict, *, now: float,
               now_dt: datetime | None = None) -> DisplayState:
        self.frames_seen += 1
        self.last_frame_at = now
        mode = str(result.get("mode") or "")
        diagnostics = result.get("vision_diagnostics") or []
        display_labels = result.get("display_hand_labels")
        if isinstance(display_labels, (list, tuple)):
            labels = [str(label) for label in display_labels if label]
        else:
            labels = [str(item.get("label")) for item in diagnostics
                      if item.get("accepted", True) and item.get("label")]
            if not labels:
                labels = [str(label) for label in (result.get("hand") or []) if label]
        candidates = tuple(dict(row) for row in (result.get("candidates") or [])[:3])
        caishen = result.get("caishen") or None
        caishen_source = result.get("caishen_source")
        if mode.startswith("shu_") and not caishen:
            caishen, caishen_source = "hongzhong", "rule"
        shanten = result.get("shanten")
        guard = self._caishen_guard(result)
        meld_count = int(result.get("meld_count", 0) or 0)
        shanten_value = int(shanten) if isinstance(shanten, int) else None
        key = frame_key(result)

        def build(status: str, title: str, reason: str | None, *,
                  actionable: bool, suggestion: str | None,
                  suggestion_label: str | None) -> DisplayState:
            return DisplayState(
                candidates=candidates, hand_labels=tuple(labels),
                caishen_label=caishen, caishen_source=caishen_source,
                caishen_guard=guard, caishen_id=result.get("caishen_id"),
                meld_count=meld_count, shanten=shanten_value,
                advice_is_stale=False, frame_key=key,
                status=status, title=title, reason=reason,
                actionable=actionable, suggestion=suggestion,
                suggestion_label=suggestion_label,
            )
        status = str(result.get("status") or "abstain")
        actionable = False
        suggestion = None
        suggestion_id = result.get("suggestion_id")
        if suggestion_id is None and result.get("suggestion"):
            suggestion_id = TILE_IDS.get(str(result["suggestion"]))

        if status == "wait":
            title, reason = WAITING_TITLE, (
                "已听牌，等待摸牌（算法推断）" if shanten_value == 0 else "等待中（算法推断，座位未独立确认）")
        elif status == "hu":
            title, reason = "可胡", "识别为可胡牌，请核对游戏按钮与番型"
            actionable = True
        elif status == "advise":
            if guard:
                status, title, reason = "abstain", "识别中", "等待本局财神，未启用打牌建议"
            elif not result.get("suggested_boxes"):
                status, title, reason = "abstain", "识别中", "建议牌未在画面中定位"
            elif self._low_confidence(result):
                status, title, reason = "abstain", "识别中", "手牌置信不足"
            elif not result.get("suggestion"):
                status, title, reason = "abstain", "识别中", "建议牌名缺失"
            else:
                title, reason = "出牌建议", None
                actionable = True
                suggestion = str(result["suggestion"])
        else:
            status = "abstain"
            title = "识别中"
            reason = str(result.get("reason") or "画面不可用")

        if not actionable:
            title = str(result.get("display_title") or title)
            reason = str(result.get("display_reason") or reason)

        self.state = build(
            status, title, None if actionable else reason,
            actionable=actionable, suggestion=suggestion,
            suggestion_label=tile_label(suggestion_id) if suggestion_id is not None else None,
        )
        self.updated_at = now_dt or datetime.now()
        return self.state

    @staticmethod
    def advice_progress(result: dict) -> float:
        """Recognised-tile share of a frame, used to prefer clear frames."""
        diagnostics = result.get("vision_diagnostics") or []
        if not diagnostics:
            return 1.0 if result.get("status") != "abstain" else 0.0
        accepted = sum(1 for item in diagnostics if item.get("accepted", True))
        return accepted / len(diagnostics)


def describe_age(seconds: float | None) -> str:
    if seconds is None:
        return "尚未更新"
    if seconds < 1:
        return "刚刚更新"
    return f"{seconds:.1f} 秒前更新"
