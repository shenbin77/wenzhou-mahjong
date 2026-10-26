# -*- coding: utf-8 -*-
"""Structured follow-session JSONL logger for tea auto-play debugging.

Writes one JSON object per cycle covering vision/hand, gate/abstain reasons,
caishen state, and decisions. This module never clicks and never flips
``CLAIM_TAP_CHANNEL_READY`` — it only records the constant so audits can
prove the claim-tap channel stayed closed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from action_policy import CLAIM_TAP_CHANNEL_READY

__all__ = [
    "FOLLOW_SESSION_SCHEMA",
    "FOLLOW_SESSION_FILENAME",
    "FollowSessionLogError",
    "SessionLogger",
    "build_follow_record",
    "abstain_reasons_from_event",
    "format_follow_line",
    "read_follow_lines",
    "resolve_follow_session_path",
]

FOLLOW_SESSION_SCHEMA = "follow_session/v1"
FOLLOW_SESSION_FILENAME = "follow_session.jsonl"


class FollowSessionLogError(RuntimeError):
    """Raised when follow_session.jsonl cannot be created or appended."""



def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def abstain_reasons_from_event(event: Mapping[str, Any]) -> list[str]:
    """Collect human-readable abstain / block reasons from one ledger event."""
    reasons: list[str] = []
    status = event.get("status")
    if status in {"abstain", "error"}:
        reason = event.get("reason")
        if reason:
            reasons.append(str(reason))
    turn = event.get("turn_gate") or {}
    if isinstance(turn, Mapping):
        state = turn.get("state")
        if state in {"wait", "blocked"} and turn.get("reason"):
            reasons.append(f"turn_gate:{turn.get('reason')}")
        code = turn.get("code")
        if code and code not in {None, "", "ok"}:
            reasons.append(f"turn_code:{code}")
    claim = event.get("claim")
    if isinstance(claim, Mapping):
        claim_status = claim.get("status")
        blocked = claim.get("blocked_reason")
        claim_reason = claim.get("claim_reason") or claim.get("reason")
        if claim_status in {"abstain", "blocked", "read_failed"} and claim_reason:
            reasons.append(f"claim:{claim_reason}")
        if blocked:
            reasons.append(f"claim_blocked:{blocked}")
        l2 = claim.get("l2_reason")
        if claim_status in {"abstain", "blocked"} and l2:
            reasons.append(f"claim_l2:{l2}")
    table = event.get("table_state") or {}
    if isinstance(table, Mapping):
        for note in _as_list(table.get("notes")):
            if note:
                reasons.append(f"table:{note}")
    guard = event.get("guard")
    if guard in {"visible", "uncertain"} and event.get("reason"):
        # Action-row visible is not itself an abstain, but uncertain is.
        if guard == "uncertain":
            reasons.append(f"action_guard:{event.get('reason')}")
    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for item in reasons:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _vision_block(event: Mapping[str, Any], analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    source = analysis if isinstance(analysis, Mapping) else {}
    hand_ids = source.get("hand_ids")
    if hand_ids is None:
        table = event.get("table_state") or {}
        if isinstance(table, Mapping):
            hand_ids = table.get("hand_ids")
    snap = event.get("vision_snapshot") if isinstance(event.get("vision_snapshot"), Mapping) else None
    if snap is None and isinstance(source.get("vision_snapshot"), Mapping):
        snap = source.get("vision_snapshot")
    facts = event.get("frame_facts") if isinstance(event.get("frame_facts"), Mapping) else {}
    block = {
        "status": event.get("status") if event.get("status") is not None else source.get("status"),
        "reason": event.get("reason") if event.get("event") in {"idle", "error", "stop"} else source.get("reason"),
        "hand_ids": list(hand_ids) if isinstance(hand_ids, (list, tuple)) else [],
        "concealed_count": (
            (event.get("table_state") or {}).get("concealed_count")
            if isinstance(event.get("table_state"), Mapping)
            else source.get("concealed_count")
        ),
        "suggestion": event.get("suggestion", source.get("suggestion")),
        "suggestion_id": event.get("suggestion_id", source.get("suggestion_id")),
        "shanten": source.get("shanten"),
        # Unified follow vision snapshot stays read-only even though the separate
        # auto-play claim executor is available.
        "incoming_tile": facts.get("last_discard", source.get("incoming_tile")),
        "incoming_source": facts.get("last_discard_source", source.get("incoming_source")),
        "own_river": facts.get("river_own", source.get("own_river")),
        "opponent_river": facts.get("river_opp", source.get("opponent_river")),
        "meld_types": facts.get("meld_types", source.get("meld_types")),
        "wall_cards": facts.get("wall_cards", source.get("wall_cards")),
        "room_id": facts.get("room_id"),
        "round_no": facts.get("round_no"),
        "claim_buttons": facts.get("claim_buttons", source.get("claim_buttons")),
        "countdown_status": (event.get("table_state") or {}).get("countdown_status")
        if isinstance(event.get("table_state"), Mapping) else source.get("countdown_status"),
        "countdown_seconds": (event.get("table_state") or {}).get("countdown_seconds")
        if isinstance(event.get("table_state"), Mapping) else source.get("countdown_seconds"),
        "claim_tap_channel_ready": False,
        "read_only": True,
    }
    if snap:
        block["snapshot"] = dict(snap)
    return block


def _caishen_block(event: Mapping[str, Any], analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    source = analysis if isinstance(analysis, Mapping) else {}
    table = event.get("table_state") if isinstance(event.get("table_state"), Mapping) else {}
    label = event.get("caishen")
    if label is None:
        label = table.get("caishen") if table else source.get("caishen")
    cid = table.get("caishen_id") if table else source.get("caishen_id")
    known = table.get("caishen_known") if table else None
    if known is None:
        known = bool(label) and str(label).strip() not in {"", "?", "unknown", "None"}
    return {
        "label": label,
        "id": cid,
        "known": bool(known),
        "source": source.get("caishen_source") or table.get("caishen_source"),
    }


def _gates_block(event: Mapping[str, Any]) -> dict[str, Any]:
    turn = event.get("turn_gate") if isinstance(event.get("turn_gate"), Mapping) else {}
    return {
        "turn": dict(turn) if turn else None,
        "action_guard": event.get("guard"),
        "guard_reason": event.get("reason") if event.get("event") in {"idle", "guard"} and event.get("guard") else None,
        "stop_kind": event.get("gate") or (event.get("error") if event.get("event") == "error" else None),
        "abstain_reasons": abstain_reasons_from_event(event),
    }


def _decision_block(event: Mapping[str, Any]) -> dict[str, Any]:
    claim = event.get("claim") if isinstance(event.get("claim"), Mapping) else None
    kind = "none"
    if claim is not None:
        kind = "claim"
    elif event.get("event") in {"action", "dry_run_plan"} or event.get("suggestion_id") is not None:
        kind = "discard"
    return {
        "kind": kind,
        "event": event.get("event"),
        "suggestion_id": event.get("suggestion_id"),
        "tap": event.get("tap"),
        "verified": event.get("verified"),
        "claim": claim,
        # Never imply a live claim tap: channel readiness is recorded only.
        "claim_tap_channel_ready": bool(CLAIM_TAP_CHANNEL_READY),
        "auto_click": (claim or {}).get("auto_click") if claim else None,
    }


def _table_block(event: Mapping[str, Any]) -> dict[str, Any] | None:
    table = event.get("table_state")
    if not isinstance(table, Mapping):
        return None
    keys = (
        "mode", "status", "caishen", "caishen_known", "concealed_count",
        "logical_meld_slots", "wall_cards", "countdown_status", "countdown_seconds",
        "incoming_tile", "incoming_source", "discards_trusted", "notes",
        "action_discs_seen", "action_disc_count",
    )
    return {key: table.get(key) for key in keys}


def build_follow_record(
    event: Mapping[str, Any],
    *,
    session: str | Path | None = None,
    tick: int | None = None,
    analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one follow-session JSONL row from an auto_play ledger event."""
    channel_note = ("claim_channel_ready_gated"
                    if CLAIM_TAP_CHANNEL_READY else "claim_channel_closed")
    record: dict[str, Any] = {
        "schema": FOLLOW_SESSION_SCHEMA,
        "at": event.get("ts") or _now_iso(),
        "session": str(session) if session is not None else event.get("session"),
        "tick": tick if tick is not None else event.get("tick"),
        "round_seq": event.get("round_seq"),
        "frame": event.get("frame"),
        "event": event.get("event"),
        "action_id": event.get("action_id"),
        "action_source": event.get("action_source"),
        "vision": _vision_block(event, analysis),
        "caishen": _caishen_block(event, analysis),
        "gates": _gates_block(event),
        "table": _table_block(event),
        "decision": _decision_block(event),
        "card_count": event.get("card_count"),
        "consecutive_errors": event.get("consecutive_errors"),
        "claim_tap_channel_ready": bool(CLAIM_TAP_CHANNEL_READY),
        "safety": channel_note,
    }
    return record



def format_follow_line(record: Mapping[str, Any]) -> str | None:
    """One human-readable gate/abstain/decision line for the UI log panel.

    Returns ``None`` for session headers and other non-cycle rows so the
    scrollable viewer stays focused on live decisions. Never mutates
    ``CLAIM_TAP_CHANNEL_READY``.
    """
    if not isinstance(record, Mapping):
        return None
    if record.get("kind") == "session_header":
        return None
    at = str(record.get("at") or "")
    # Prefer local clock fragment HH:MM:SS
    clock = at[11:19] if len(at) >= 19 else (at or "--:--:--")
    tick = record.get("tick")
    tick_s = f"t{tick}" if tick is not None else "t-"
    event = record.get("event") or "-"
    gates = record.get("gates") if isinstance(record.get("gates"), Mapping) else {}
    turn = gates.get("turn") if isinstance(gates.get("turn"), Mapping) else {}
    turn_state = turn.get("state") or "-"
    turn_code = turn.get("code") or ""
    gate_s = turn_state if not turn_code or turn_code in {"ok", ""} else f"{turn_state}/{turn_code}"
    abstain = gates.get("abstain_reasons") if isinstance(gates, Mapping) else None
    if not isinstance(abstain, list):
        abstain = []
    abstain_s = "；".join(str(x) for x in abstain[:4]) if abstain else ""
    vision = record.get("vision") if isinstance(record.get("vision"), Mapping) else {}
    v_status = vision.get("status")
    if v_status and not abstain_s and v_status in {"abstain", "error"}:
        reason = vision.get("reason")
        if reason:
            abstain_s = str(reason)
    decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else {}
    kind = decision.get("kind") or "none"
    if kind == "discard":
        sid = decision.get("suggestion_id")
        decision_s = f"discard#{sid}" if sid is not None else "discard"
    elif kind == "claim":
        claim = decision.get("claim") if isinstance(decision.get("claim"), Mapping) else {}
        action = (claim or {}).get("action") or "claim"
        cstat = (claim or {}).get("status") or ""
        decision_s = f"claim:{action}" + (f"({cstat})" if cstat else "")
    else:
        decision_s = "none"
    stop_kind = gates.get("stop_kind") if isinstance(gates, Mapping) else None
    parts = [clock, tick_s, f"evt={event}", f"gate={gate_s}", f"dec={decision_s}"]
    if abstain_s:
        parts.append(f"abstain={abstain_s}")
    if stop_kind:
        parts.append(f"stop={stop_kind}")
    channel = record.get("claim_tap_channel_ready")
    if channel is True:
        parts.append("WARN_CLAIM_CHANNEL_READY")
    return "  ".join(parts)


def read_follow_lines(
    path: str | Path | None,
    *,
    limit: int = 200,
) -> list[str]:
    """Read the latest formatted gate/abstain/decision lines from a JSONL log."""
    if path is None:
        return []
    target = Path(path)
    if not target.is_file():
        return []
    lines: list[str] = []
    try:
        with target.open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text = format_follow_line(record)
                if text:
                    lines.append(text)
    except OSError:
        return []
    if limit > 0 and len(lines) > limit:
        return lines[-limit:]
    return lines


def resolve_follow_session_path(
    session_dir: str | Path | None,
    *,
    search_roots: list[str | Path] | None = None,
) -> Path | None:
    """Prefer ``session_dir/follow_session.jsonl``, else newest under search roots."""
    if session_dir is not None:
        candidate = Path(session_dir) / FOLLOW_SESSION_FILENAME
        if candidate.is_file():
            return candidate
    roots = [Path(p) for p in (search_roots or [])]
    newest: Path | None = None
    newest_mtime = -1.0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            matches = list(root.rglob(FOLLOW_SESSION_FILENAME))
        except OSError:
            continue
        for match in matches:
            try:
                mtime = match.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest = match
                newest_mtime = mtime
    return newest

class SessionLogger:
    """Append-only JSONL writer for follow-session debug records."""

    def __init__(
        self,
        session_dir: str | Path | None,
        *,
        enabled: bool = True,
        filename: str = FOLLOW_SESSION_FILENAME,
    ) -> None:
        self.session_dir = Path(session_dir) if session_dir else None
        self.enabled = bool(enabled)
        self.filename = filename
        self.path: Path | None = (
            self.session_dir / filename if self.session_dir is not None else None
        )
        self._started = False
        self.rows_written = 0

    def _ensure_parent(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def ensure_ready(self, meta: Mapping[str, Any] | None = None) -> Path:
        """Create session_dir/follow_session.jsonl or raise FollowSessionLogError."""
        if not self.enabled:
            raise FollowSessionLogError(
                "SessionLogger disabled; live follow cannot write follow_session.jsonl"
            )
        if self.path is None:
            raise FollowSessionLogError(
                "SessionLogger has no session_dir; cannot write follow_session.jsonl"
            )
        path = self.write_header(meta)
        if path is None or not path.is_file():
            raise FollowSessionLogError(
                f"follow_session.jsonl missing after write_header: {self.path}"
            )
        return path

    def write_header(self, meta: Mapping[str, Any] | None = None) -> Path | None:
        """Write a one-time session header (dev corpus marker)."""
        if not self.enabled or self.path is None:
            return None
        if self._started:
            if self.path.is_file():
                return self.path
            # Header flag set but file vanished — recreate.
            self._started = False
        try:
            self._ensure_parent()
            header = {
                "schema": FOLLOW_SESSION_SCHEMA,
                "kind": "session_header",
                "at": _now_iso(),
                "session": str(self.session_dir),
                "claim_tap_channel_ready": bool(CLAIM_TAP_CHANNEL_READY),
                "note": "follow-session debug log; never enables claim taps",
            }
            if meta:
                header.update(dict(meta))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
                handle.flush()
            if not self.path.is_file():
                raise FollowSessionLogError(
                    f"follow_session.jsonl not on disk after header write: {self.path}"
                )
        except FollowSessionLogError:
            raise
        except OSError as exc:
            raise FollowSessionLogError(
                f"cannot create follow_session.jsonl at {self.path}: {exc}"
            ) from exc
        self._started = True
        self.rows_written += 1
        return self.path

    def append(
        self,
        event: Mapping[str, Any],
        *,
        tick: int | None = None,
        analysis: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> Path | None:
        """Append one structured follow-session row derived from a ledger event."""
        if not self.enabled or self.path is None:
            return None
        if not self._started or not self.path.is_file():
            self.write_header(meta)
        record = build_follow_record(
            event,
            session=self.session_dir,
            tick=tick,
            analysis=analysis,
        )
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError as exc:
            raise FollowSessionLogError(
                f"cannot append follow_session.jsonl at {self.path}: {exc}"
            ) from exc
        self.rows_written += 1
        return self.path

    def read_all(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return rows
