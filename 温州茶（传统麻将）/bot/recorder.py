"""Append-only decision recorder for the read-only Mahjong assist window.

The recorder owns decision stability, semantic de-duplication and result-frame
diffing.  It never sends input to the game or emulator.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


SCHEMA = "assist_record/v4"
VALID_MODES = frozenset({"tea_2p", "tea_4p", "shu_4p"})
TIMEOUT_SECONDS = 30


def _aware_now() -> datetime:
    return datetime.now().astimezone()


def _as_aware(value: datetime) -> datetime:
    return value.astimezone() if value.tzinfo is not None else value.astimezone()


def _sorted_multiset(tile_ids: list[int | None]) -> tuple[int | None, ...]:
    return tuple(sorted(tile_ids, key=lambda value: (value is None, value or 0)))


def semantic_key(
    mode: str, tile_ids: list[int | None], round_no: int, caishen_id: int | None,
) -> str:
    """Return the frozen v4 semantic key, preserving duplicate tiles."""
    if mode not in VALID_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    tiles = ",".join("null" if value is None else str(int(value))
                     for value in _sorted_multiset(tile_ids))
    payload = f"{mode}|{tiles}|{int(round_no)}|{caishen_id if caishen_id is not None else 'null'}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decision_id(mode: str, key: str) -> str:
    return f"{mode}_{key[:16]}"


def _frame_ref(path_value: str | Path | None) -> dict[str, str]:
    if not path_value:
        raise ValueError("decision frame path is required")
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _tile_reason(item: dict[str, Any]) -> str:
    if item.get("id", item.get("tile_id")) is not None and item.get("accepted", True):
        return "ok"
    reason = item.get("reason")
    if reason == "low_score":
        return "low_score"
    if reason == "ambiguous":
        return "ambiguous"
    return "no_box"


def normalize_tiles(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Reduce vision diagnostics to the exact v4 per-tile schema."""
    source = result.get("record_tiles") or result.get("vision_diagnostics") or []
    normalized = []
    for item in source:
        tile_id = item.get("id", item.get("tile_id"))
        reason = _tile_reason(item)
        normalized.append({
            "id": int(tile_id) if tile_id is not None and reason == "ok" else None,
            "score": float(item.get("score", 0.0)),
            "margin": float(item.get("margin", 0.0)),
            "reason": reason,
        })
    if normalized:
        return normalized
    for tile_id in result.get("hand_ids") or []:
        normalized.append({"id": int(tile_id), "score": 1.0, "margin": 1.0,
                           "reason": "ok"})
    return normalized


def normalize_candidates(
    mode: str,
    candidates: list[Any] | tuple[Any, ...] | None,
    *,
    hand_ids: list[int] | None = None,
    caishen_id: int | None = None,
    meld_count: int = 0,
) -> list[dict[str, int | float]]:
    """Normalize tea tuples and Shu objects to the frozen Top-5 shape."""
    candidates = list(candidates or [])
    rows: list[dict[str, int | float]] = []
    if mode.startswith("tea_"):
        if not candidates:
            return []
        if not hand_ids or caishen_id is None:
            raise ValueError("tea candidate normalization needs hand and caishen")
        from tea_bot import engine_to_vision, vision_to_engine
        from brain_v0.engine.tea.shanten import discard_analysis

        engine_hand = [vision_to_engine(int(tile)) for tile in hand_ids]
        analysis = {
            int(row["tile"]): row
            for row in discard_analysis(
                engine_hand, caishen_id=vision_to_engine(int(caishen_id)),
                meld_count=meld_count,
            )
        }
        for item in candidates:
            if isinstance(item, dict):
                score = item["final_score"]
                # Display rows already use vision IDs; engine tuples do not.
                tile = vision_to_engine(int(item["tile"]))
            else:
                score, tile = item
            detail = analysis[int(tile)]
            rows.append({
                "tile": int(engine_to_vision(int(tile))),
                "final_score": float(score),
                "shanten_after": int(detail["shanten_after"]),
                "ukeire_count": int(detail["ukeire_count"]),
            })
    elif mode == "shu_4p":
        for item in candidates:
            rows.append({
                "tile": int(item["tile"]),
                "final_score": float(item["final_score"]),
                "shanten_after": int(item["shanten_after"]),
                "ukeire_count": int(item["ukeire_count"]),
            })
    else:
        raise ValueError(f"unsupported mode: {mode}")
    rows.sort(key=lambda row: (-float(row["final_score"]), int(row["tile"])))
    return rows[:5]


def infer_rule_gate(image_path: str | Path | None, mode: str) -> tuple[str | None, bool]:
    """Read the existing native-size rule templates and return canonical text."""
    if not image_path or not Path(image_path).is_file():
        return None, False
    import cv2
    import numpy as np
    from mode_vision import (detect_shu_rule_text, detect_tea_no_zhong_fa,
                             detect_tea_player_count)

    image = cv2.imdecode(np.fromfile(str(image_path), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None, False
    if detect_shu_rule_text(image):
        return "放炮麻将 红中 4人", mode == "shu_4p"
    players = detect_tea_player_count(image)
    if players in (2, 4):
        no_zhong_fa = detect_tea_no_zhong_fa(image)
        text = f"温州茶 {players}人" + (" 无中发" if no_zhong_fa else "")
        return text, mode == f"tea_{players}p"
    return None, False


def _expected_counts(mode: str, meld_count: int) -> tuple[set[int], int]:
    if mode.startswith("tea_"):
        wait_count = 16 - 3 * meld_count
        return {wait_count, wait_count + 1}, wait_count + 1
    wait_count = 13 - 3 * meld_count
    return {wait_count, wait_count + 1}, wait_count + 1


def _caishen_id(result: dict[str, Any], mode: str) -> int | None:
    if mode == "shu_4p":
        return 55
    if "caishen_id" in result:
        value = result["caishen_id"]
        return int(value) if value is not None else None
    label = result.get("caishen") or result.get("caishen_selected")
    if not label:
        return None
    from tile_vision import TILE_IDS
    return TILE_IDS.get(label)


@dataclass
class _ActiveDecision:
    state: str
    key: str
    mode: str
    round_no: int
    caishen_id: int | None
    room_id: str | None
    hand: tuple[int | None, ...]
    stable_count: int
    payload: dict[str, Any]
    decision_frame: dict[str, str]
    suggested_at: datetime | None = None
    result_hand: tuple[int | None, ...] | None = None
    result_stable_count: int = 0
    transitions: list[str] = field(default_factory=lambda: ["detected"])


class AssistRecorder:
    """One-session v4 recorder with at most one active decision."""

    def __init__(
        self,
        output_path: str | Path,
        *,
        account: str,
        room_id: str | None = None,
        round_no: int = 1,
        timeout_seconds: int = TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = _aware_now,
    ) -> None:
        account = account.strip()
        if not account:
            raise ValueError("account is required")
        if round_no < 1:
            raise ValueError("round must start at 1")
        self.output_path = Path(output_path)
        self.account = account
        self.room_id = room_id.strip() if room_id and room_id.strip() else None
        self.round = int(round_no)
        self.timeout = timedelta(seconds=timeout_seconds)
        self.clock = clock
        self.active: _ActiveDecision | None = None
        self.last_closed_key: str | None = None
        self.closed_keys: set[str] = set()
        self.last_status: str | None = None

    @property
    def state(self) -> str | None:
        return self.active.state if self.active is not None else self.last_status

    def set_room_id(self, value: str | None) -> None:
        self.room_id = value.strip() if value and value.strip() else None

    def next_round(self) -> int:
        self.round += 1
        if self.active is not None and self.active.state == "detected":
            self.active = None
        return self.round

    def _rule_gate(self, result: dict[str, Any], mode: str) -> tuple[str | None, bool]:
        if "screen_rule_text" in result or "gate_ok" in result:
            text = result.get("screen_rule_text")
            return text, bool(result.get("gate_ok"))
        return infer_rule_gate(result.get("image"), mode)

    def _abstain_reason(
        self,
        *,
        result: dict[str, Any],
        mode: str,
        tiles: list[dict[str, Any]],
        caishen_id: int | None,
        screen_rule_text: str | None,
        gate_ok: bool,
    ) -> tuple[str | None, str | None]:
        if mode.startswith("tea_") and caishen_id is None:
            return None, "caishen_unset"
        if not gate_ok:
            return ("gate_mismatch" if screen_rule_text else "gate_unreadable"), None
        meld_count = int(result.get("meld_count", 0) or 0)
        valid_counts, _ = _expected_counts(mode, meld_count)
        if len(tiles) not in valid_counts:
            return "wrong_count", None
        if any(tile["id"] is None for tile in tiles):
            return "unrecognized", None
        if result.get("status") == "abstain":
            reason = str(result.get("reason") or "")
            if any(word in reason for word in ("张", "数量", "暗手")):
                return "wrong_count", None
            return "unrecognized", None
        return None, None

    def _payload(
        self,
        result: dict[str, Any],
        *,
        mode: str,
        tiles: list[dict[str, Any]],
        caishen_id: int | None,
        screen_rule_text: str | None,
        gate_ok: bool,
        candidates: list[dict[str, int | float]],
    ) -> dict[str, Any]:
        has_unknown = any(tile["id"] is None for tile in tiles)
        shanten = result.get("shanten")
        return {
            "mode": mode,
            "screen_rule_text": screen_rule_text,
            "gate_ok": gate_ok,
            "tiles": tiles,
            "caishen_id": caishen_id,
            "melds": list(result.get("melds") or []),
            "shanten": None if has_unknown or shanten is None else int(shanten),
            "bot_suggest": int(candidates[0]["tile"]) if candidates else None,
            "bot_candidates": candidates,
        }

    def _append(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _close(
        self,
        status: str,
        *,
        now: datetime,
        human_discard: int | None = None,
        agreed: bool | None = None,
        abstain_reason: str | None = None,
        diff_note: str | None = None,
        result_frame: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.active is None:
            raise RuntimeError("no active decision")
        active = self.active
        record = {
            "schema": SCHEMA,
            "decision_id": decision_id(active.mode, active.key),
            "timestamp": _as_aware(now).isoformat(),
            "account": self.account,
            "room_id": active.room_id,
            "mode": active.mode,
            "screen_rule_text": active.payload["screen_rule_text"],
            "gate_ok": active.payload["gate_ok"],
            "round": active.round_no,
            "tiles": active.payload["tiles"],
            "caishen_id": active.caishen_id,
            "melds": active.payload["melds"],
            "shanten": active.payload["shanten"],
            "bot_suggest": active.payload["bot_suggest"],
            "bot_candidates": active.payload["bot_candidates"],
            "human_discard": human_discard,
            "agreed": agreed,
            "status": status,
            "abstain_reason": abstain_reason,
            "diff_note": diff_note,
            "decision_frame": active.decision_frame,
            "result_frame": result_frame,
        }
        active.transitions.append(status)
        self._append(record)
        self.last_closed_key = active.key
        self.closed_keys.add(active.key)
        self.last_status = status
        self.active = None
        return record

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        now = _as_aware(now or self.clock())
        if (self.active is not None and self.active.state == "suggested"
                and self.active.suggested_at is not None
                and now - self.active.suggested_at >= self.timeout):
            return self._close("timeout", now=now, agreed=None)
        return None

    def observe(self, result: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
        """Consume one analyzed frame and return a record only when it closes."""
        now = _as_aware(now or self.clock())
        timed_out = self.tick(now)
        if timed_out is not None:
            return timed_out
        if result.get("source") == "manual_correction":
            return None
        mode = result.get("mode")
        if mode not in VALID_MODES:
            return None
        tiles = normalize_tiles(result)
        tile_ids = [tile["id"] for tile in tiles]
        hand = _sorted_multiset(tile_ids)
        caishen_id = _caishen_id(result, mode)
        key = semantic_key(mode, tile_ids, self.round, caishen_id)
        screen_rule_text, gate_ok = self._rule_gate(result, mode)

        if self.active is not None and self.active.state == "suggested":
            if hand == self.active.hand:
                self.active.result_hand = None
                self.active.result_stable_count = 0
                return None
            if hand == self.active.result_hand:
                self.active.result_stable_count += 1
            else:
                self.active.result_hand = hand
                self.active.result_stable_count = 1
            if self.active.result_stable_count < 2:
                return None
            decision_counts = Counter(value for value in self.active.hand if value is not None)
            result_counts = Counter(value for value in hand if value is not None)
            disappeared = list((decision_counts - result_counts).elements())
            appeared = list((result_counts - decision_counts).elements())
            result_frame = _frame_ref(result.get("image"))
            if (len(disappeared) == 1 and not appeared
                    and all(value is not None for value in self.active.hand)
                    and all(value is not None for value in hand)):
                human_discard = int(disappeared[0])
                suggest = self.active.payload["bot_suggest"]
                return self._close(
                    "resolved", now=now, human_discard=human_discard,
                    agreed=(human_discard == suggest), result_frame=result_frame,
                )
            return self._close(
                "resolved", now=now, agreed=None, diff_note="diff_failed",
                result_frame=result_frame,
            )

        # A detected decision owns the session lock until it reaches a terminal
        # state. Frames with a different semantic key cannot replace it.
        if (self.active is not None
                and (self.active.state != "detected" or self.active.key != key)):
            return None

        abstain_reason, diff_note = self._abstain_reason(
            result=result, mode=mode, tiles=tiles, caishen_id=caishen_id,
            screen_rule_text=screen_rule_text, gate_ok=gate_ok,
        )
        if key in self.closed_keys:
            return None
        frame = _frame_ref(result.get("image"))
        is_abstention = abstain_reason is not None or diff_note == "caishen_unset"
        candidates: list[dict[str, int | float]] = []
        if not is_abstention:
            meld_count = int(result.get("meld_count", 0) or 0)
            _, discard_count = _expected_counts(mode, meld_count)
            if result.get("status") != "advise" or len(tiles) != discard_count:
                return None
            raw_candidates = result.get("candidates")
            if raw_candidates is None:
                raw_candidates = result.get("options")
            candidates = normalize_candidates(
                mode, raw_candidates,
                hand_ids=[int(value) for value in tile_ids if value is not None],
                caishen_id=caishen_id,
                meld_count=meld_count,
            )
            if not candidates:
                return None
        payload = self._payload(
            result, mode=mode, tiles=tiles, caishen_id=caishen_id,
            screen_rule_text=screen_rule_text, gate_ok=gate_ok, candidates=candidates,
        )
        if self.active is None:
            self.active = _ActiveDecision(
                state="detected", key=key, mode=mode, round_no=self.round,
                caishen_id=caishen_id, room_id=self.room_id,
                hand=hand, stable_count=1,
                payload=payload, decision_frame=frame,
            )
            return None
        if self.active.state != "detected":
            return None
        if self.active.key != key:
            return None
        self.active.stable_count += 1
        self.active.payload = payload
        self.active.decision_frame = frame
        if self.active.stable_count >= 2:
            self.active.state = "suggested"
            self.active.suggested_at = now
            self.active.transitions.append("suggested")
            if is_abstention:
                return self._close(
                    "abstained", now=now, abstain_reason=abstain_reason,
                    diff_note=diff_note,
                )
        return None
