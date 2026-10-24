# -*- coding: utf-8 -*-
"""Unified per-frame table snapshot (P0-5).

Every decide / discard path should refresh a :class:`TableState` from the
**current** frame before acting. The structure is intentionally flat and
JSON-friendly so auto_play / assist ledgers can dump it without a second
schema.

This module does not talk to ADB or click anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from countdown_vision import CountdownReading, detect_countdown
from turn_gate import (
    action_ui_evidence,
    known_caishen_id,
    logical_meld_slots,
    meld_total,
)

__all__ = [
    "TableState",
    "WALL_CARDS_STOP_AT",
    "build_table_state",
]

#: Audit P0: stop auto-play when remaining wall/room cards fall to this count.
WALL_CARDS_STOP_AT = 16


@dataclass(frozen=True)
class TableState:
    """Current-frame facts required before discard/claim decisions."""

    mode: str | None = None
    status: str | None = None
    caishen: str | None = None
    caishen_id: int | None = None
    caishen_known: bool = False
    hand_ids: tuple[int, ...] = ()
    concealed_count: int | None = None
    physical_boxes: int | None = None
    meld_count: int = 0
    meld_total_physical: int = 0
    logical_meld_slots: int = 0
    action_discs_seen: bool = False
    action_disc_count: int = 0
    claim_decision: str | None = None
    countdown_status: str = "absent"
    countdown_seconds: int | None = None
    wall_cards: int | None = None
    discards_trusted: bool = False
    incoming_tile: int | None = None
    incoming_source: str | None = None
    notes: tuple[str, ...] = ()
    action_ui: dict[str, Any] = field(default_factory=dict)
    countdown_evidence: dict[str, Any] = field(default_factory=dict)

    def ledger(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hand_ids"] = list(self.hand_ids)
        payload["notes"] = list(self.notes)
        return payload

    @property
    def should_stop_for_wall(self) -> bool:
        return self.wall_cards is not None and self.wall_cards <= WALL_CARDS_STOP_AT

    @property
    def should_stop_for_unknown_caishen(self) -> bool:
        return not self.caishen_known


def build_table_state(
    result: Mapping[str, Any] | None,
    image: Any = None,
    *,
    wall_cards: int | None = None,
    discards_trusted: bool = False,
    incoming_tile: int | None = None,
    incoming_source: str | None = None,
    action_ui: Mapping[str, Any] | None = None,
    countdown: CountdownReading | None = None,
) -> TableState:
    """Build a TableState from this-frame analysis + optional vision hooks."""
    result = dict(result or {})
    notes: list[str] = []

    ui = dict(action_ui) if action_ui is not None else action_ui_evidence(image)
    if countdown is None:
        countdown = detect_countdown(image)
    # Prefer live countdown detector over the action_ui stub fields.
    ui = dict(ui)
    ui["countdown_status"] = countdown.status
    ui["countdown_seconds"] = countdown.seconds

    hand_ids_raw = result.get("hand_ids") or ()
    hand_ids: list[int] = []
    if isinstance(hand_ids_raw, (list, tuple)):
        for value in hand_ids_raw:
            try:
                hand_ids.append(int(value))
            except (TypeError, ValueError):
                notes.append("hand_ids_partial")
                hand_ids = []
                break

    boxes = result.get("concealed_boxes") or result.get("hand_boxes") or []
    physical = len(boxes) if isinstance(boxes, (list, tuple)) else None
    if physical is None and hand_ids:
        physical = len(hand_ids)

    caishen = result.get("caishen")
    if caishen is not None:
        caishen = str(caishen) or None
    cid = known_caishen_id(result)
    caishen_known = cid is not None or bool(caishen)

    melds = result.get("melds")
    meld_count = 0
    if isinstance(melds, (list, tuple)):
        meld_count = len([g for g in melds if isinstance(g, (list, tuple)) and g])
    elif result.get("meld_count") is not None:
        try:
            meld_count = int(result.get("meld_count") or 0)
        except (TypeError, ValueError):
            meld_count = 0

    if wall_cards is not None and wall_cards <= WALL_CARDS_STOP_AT:
        notes.append(f"wall_cards<={WALL_CARDS_STOP_AT}")
    if not caishen_known:
        notes.append("caishen_unknown")
    if countdown.status in {"low", "expired", "unreadable"}:
        notes.append(f"countdown_{countdown.status}")

    return TableState(
        mode=(str(result["mode"]) if result.get("mode") is not None else None),
        status=(str(result["status"]) if result.get("status") is not None else None),
        caishen=caishen,
        caishen_id=cid,
        caishen_known=caishen_known,
        hand_ids=tuple(hand_ids),
        concealed_count=len(hand_ids) if hand_ids else None,
        physical_boxes=physical,
        meld_count=meld_count,
        meld_total_physical=meld_total(result),
        logical_meld_slots=logical_meld_slots(result),
        action_discs_seen=bool(ui.get("action_discs_seen")),
        action_disc_count=int(ui.get("action_disc_count") or 0),
        claim_decision=ui.get("claim_decision"),
        countdown_status=str(countdown.status),
        countdown_seconds=countdown.seconds,
        wall_cards=wall_cards,
        discards_trusted=bool(discards_trusted),
        incoming_tile=incoming_tile,
        incoming_source=incoming_source,
        notes=tuple(notes),
        action_ui=dict(ui),
        countdown_evidence=dict(countdown.evidence),
    )
