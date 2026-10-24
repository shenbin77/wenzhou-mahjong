# -*- coding: utf-8 -*-
"""Read-only enrichment for the unified follow pipeline.

Fills display / jsonl facts from existing readers when they exist.
Never invents values, never clicks, never flips CLAIM_TAP_CHANNEL_READY.

Wired facts:
  - 牌河 / 最近出牌  → discard_vision ONLY for whitelisted tea_2p
                      (tea_4p / shu_4p uncalibrated → rivers stay None / 未识别)
  - 余牌            → optional card_reader / result.wall_cards
                      LIVE ASSIST: card_reader NOT connected (WALL_CARD_READER_CONNECTED=False);
                      stays 未识别 unless analysis/result already supplies wall_cards.
  - 房号 / 局数      → room_round_vision (+ manual overrides)
  - 副露类型         → stacked_kongs geometry: 3 same ≠ 碰; logical_count==4 match → 杠;
                      otherwise 未识别 for 碰/杠 ambiguity
  - countdown       → countdown_vision via table_state (caller passes image)
  - claim buttons   → claim_vision.read_claims (display-only)
  - vision snapshot → compact audit blob for follow_session.jsonl
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "UNKNOWN",
    "RIVER_READ_MODES",
    "WALL_CARD_READER_CONNECTED",
    "MeldTypeReading",
    "ClaimButtonsReading",
    "Enrichment",
    "river_read_enabled",
    "classify_meld_types",
    "format_meld_types",
    "format_claim_buttons",
    "promote_discards",
    "enrich_follow_result",
    "build_vision_snapshot",
]

UNKNOWN = "未识别"

# River OCR / discard_vision is calibrated only for tea_2p table geometry.
RIVER_READ_MODES = frozenset({"tea_2p"})

# Live assist window does NOT wire a real wall/card reader yet.
# Keep 余牌 as 未识别 unless analysis already supplies wall_cards.
WALL_CARD_READER_CONNECTED = False

CardReader = Callable[[Any], int | None]


def river_read_enabled(mode: str | None) -> bool:
    """True only for calibrated tea_2p; tea_4p/shu_4p stay fail-closed."""
    return str(mode or "").strip() in RIVER_READ_MODES


@dataclass(frozen=True)
class MeldTypeReading:
    types: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaimButtonsReading:
    known: bool = False
    buttons: tuple[str, ...] = ()
    decision: str | None = None
    reason: str = ""
    display_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Enrichment:
    result: dict[str, Any]
    room_id: str | None = None
    round_no: int | None = None
    round_total: int | None = None
    wall_cards: int | None = None
    meld_types: MeldTypeReading = field(default_factory=MeldTypeReading)
    claim_buttons: ClaimButtonsReading = field(default_factory=ClaimButtonsReading)
    vision_snapshot: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _suit_runs() -> tuple[range, ...]:
    return (range(1, 10), range(21, 30), range(41, 50))


def _as_tile_list(group: Sequence[Any]) -> list[int] | None:
    try:
        return [int(t) for t in group]
    except (TypeError, ValueError):
        return None


def _valid_visible_boxes(boxes: Any, expected_n: int) -> bool:
    """Require exactly expected_n geometric boxes, each an xyxy-like 4-number box."""
    if not isinstance(boxes, (list, tuple)) or len(boxes) != expected_n:
        return False
    for box in boxes:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        try:
            [float(v) for v in box]
        except (TypeError, ValueError):
            return False
    return True


def _stacked_kong_match(
    tiles: Sequence[int],
    stacked_kongs: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """True only when logical_count==4 AND three valid visible_boxes prove a kong.

    Missing / empty / wrong-count / invalid-format visible_boxes => False (未识别).
    No fallback to tile_id + logical_count alone.
    """
    if not stacked_kongs or not tiles:
        return False
    if len(set(tiles)) != 1:
        return False
    tile_id = int(tiles[0])
    visible_n = len(tiles)
    for item in stacked_kongs:
        if not isinstance(item, Mapping):
            continue
        if int(item.get("logical_count") or 0) != 4:
            continue
        try:
            kong_tile = int(item.get("tile_id"))
        except (TypeError, ValueError):
            continue
        if kong_tile != tile_id:
            continue
        # Hard gate: geometric evidence required; no boxes => 未识别.
        if not _valid_visible_boxes(item.get("visible_boxes"), visible_n):
            continue
        return True
    return False


def _classify_one_meld(
    group: Sequence[Any],
    *,
    stacked_kongs: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    tiles = _as_tile_list(group)
    if tiles is None:
        return UNKNOWN
    if not tiles:
        return UNKNOWN
    # Sequence chi: three consecutive same-suit tiles.
    if len(tiles) == 3 and len(set(tiles)) == 3:
        if any(all(t in suit for t in tiles) for suit in _suit_runs()) and max(tiles) - min(tiles) == 2:
            return "吃"
    if len(set(tiles)) == 1:
        # Engine-expanded 4-face kong group.
        if len(tiles) == 4:
            return "杠"
        # Three same faces: NEVER auto-碰. Need stacked_kongs geometry for 杠.
        if len(tiles) == 3:
            if _stacked_kong_match(tiles, stacked_kongs):
                return "杠"
            return UNKNOWN
        return UNKNOWN
    return UNKNOWN


def classify_meld_types(
    melds: Any,
    stacked_kongs: Sequence[Mapping[str, Any]] | None = None,
) -> MeldTypeReading:
    """Classify 副露 types with stacked_kongs geometry for 碰/杠.

    Rules:
      - 三张同牌 alone ≠ 碰 (cannot confirm 碰 vs concealed-face 杠).
      - stacked_kongs.logical_count==4 + three valid visible_boxes → 杠;
        missing/empty/invalid visible_boxes → 未识别 (no fallback).
      - four identical tiles already in the meld list → 杠.
      - otherwise 碰/杠 ambiguity → 未识别.
    """
    if melds is None:
        return MeldTypeReading(reason="无副露读数")
    if not isinstance(melds, (list, tuple)):
        return MeldTypeReading(reason="副露格式不可读")
    if len(melds) == 0:
        return MeldTypeReading(types=(), labels=(), reason="ok_empty")
    kongs: list[Mapping[str, Any]] = []
    if isinstance(stacked_kongs, (list, tuple)):
        kongs = [x for x in stacked_kongs if isinstance(x, Mapping)]
    types: list[str] = []
    for group in melds:
        if not isinstance(group, (list, tuple)):
            types.append(UNKNOWN)
            continue
        types.append(_classify_one_meld(group, stacked_kongs=kongs))
    return MeldTypeReading(types=tuple(types), labels=tuple(types), reason="ok")


def format_meld_types(reading: MeldTypeReading | None, *, meld_count: int | None = None) -> str:
    if reading is None:
        return f"副露类型：{UNKNOWN}"
    if reading.types:
        return "副露类型：" + "/".join(reading.types)
    if reading.reason == "ok_empty" or meld_count == 0:
        return "副露类型：无"
    return f"副露类型：{UNKNOWN}"


def format_claim_buttons(reading: ClaimButtonsReading | None) -> str:
    if reading is None or not reading.known:
        return f"鸣牌按钮：{UNKNOWN}"
    if not reading.buttons:
        return f"鸣牌按钮：{UNKNOWN}"
    return "鸣牌按钮：" + "/".join(reading.buttons) + "（只读）"


_CLAIM_KIND_ZH = {
    "hu": "胡",
    "gold": "金钮",
    "pass": "过",
    "cancel": "取消",
    "peng": "碰",
    "kong": "杠",
    "chi": "吃",
}


def _read_claim_buttons(image: Any) -> ClaimButtonsReading:
    if image is None:
        return ClaimButtonsReading(reason="无画面")
    try:
        from claim_vision import read_claims

        state = read_claims(image)
    except Exception as exc:  # noqa: BLE001
        return ClaimButtonsReading(reason=f"鸣牌按钮读取失败：{exc}")
    kinds: list[str] = []
    for button in getattr(state, "buttons", ()) or ():
        kind = getattr(button, "kind", None) or UNKNOWN
        kinds.append(_CLAIM_KIND_ZH.get(str(kind), str(kind)))
    return ClaimButtonsReading(
        known=bool(getattr(state, "known", False)),
        buttons=tuple(kinds),
        decision=getattr(state, "decision", None),
        reason=str(getattr(state, "reason", "") or ""),
        display_only=True,
    )


def promote_discards(result: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(result or {})
    block = out.get("discards") or out.get("discard") or {}
    if not isinstance(block, Mapping):
        block = {}
    for key in ("incoming_tile", "incoming_source", "opponent_river", "own_river", "river_tiles"):
        if out.get(key) is None and key in block:
            out[key] = block.get(key)
    discards_obj = out.get("discards")
    if discards_obj is not None and not isinstance(discards_obj, Mapping):
        for key in ("incoming_tile", "incoming_source", "opponent_river", "own_river", "river_tiles"):
            if out.get(key) is None and hasattr(discards_obj, key):
                out[key] = getattr(discards_obj, key)
        try:
            if hasattr(discards_obj, "as_dict"):
                out["discards"] = discards_obj.as_dict()
        except Exception:
            pass
    return out


def _null_river_fields(out: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Force own/opponent rivers to None so HUD shows 未识别, never 我0/对0."""
    out["own_river"] = None
    out["opponent_river"] = None
    out["river_tiles"] = None
    block = out.get("discards")
    payload: dict[str, Any]
    if isinstance(block, Mapping):
        payload = dict(block)
    else:
        payload = {}
    payload["own_river"] = None
    payload["opponent_river"] = None
    payload["river_tiles"] = None
    payload.setdefault("incoming_tile", out.get("incoming_tile"))
    payload.setdefault("incoming_source", out.get("incoming_source"))
    payload["reason"] = reason
    out["discards"] = payload
    return out


def _scrub_empty_river_lists(out: dict[str, Any]) -> dict[str, Any]:
    """Empty [] is not trusted river evidence — would display as 我0/对0."""
    for key in ("own_river", "opponent_river", "river_tiles"):
        val = out.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 0:
            out[key] = None
    block = out.get("discards")
    if isinstance(block, Mapping):
        payload = dict(block)
        block_changed = False
        for key in ("own_river", "opponent_river", "river_tiles"):
            val = payload.get(key)
            if isinstance(val, (list, tuple)) and len(val) == 0:
                payload[key] = None
                block_changed = True
        if block_changed:
            out["discards"] = payload
    return out


def _ensure_discards(
    result: dict[str, Any],
    image: Any,
    *,
    discard_key: str | None,
    read_river: bool,
) -> dict[str, Any]:
    out = promote_discards(result)
    out = _scrub_empty_river_lists(out)
    mode = str(out.get("mode") or "")
    # Hard whitelist: only tea_2p may carry / read rivers.
    if not river_read_enabled(mode):
        return _null_river_fields(
            out,
            reason="弃牌视觉只在温州茶 2 人(tea_2p)布局上标定过；本模式不读牌河",
        )
    # Non-empty rivers or a concrete incoming tile mean analysis already filled discards.
    has_river = (
        (isinstance(out.get("own_river"), (list, tuple)) and len(out.get("own_river") or ()) > 0)
        or (isinstance(out.get("opponent_river"), (list, tuple)) and len(out.get("opponent_river") or ()) > 0)
        or out.get("incoming_tile") is not None
    )
    if has_river or not read_river or image is None:
        return out
    try:
        from discard_vision import read_discards_sequential

        state = read_discards_sequential(image, key=discard_key or "follow")
        payload = state.as_dict()
        out["discards"] = payload
        # Fail-closed: empty rivers are only trusted when table geometry validated.
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        table = evidence.get("table") if isinstance(evidence.get("table"), dict) else {}
        opp_ok = bool((table.get("opponent") or {}).get("valid"))
        own_ok = bool((table.get("own") or {}).get("valid"))
        reason = str(payload.get("reason") or "")
        geometry_ok = opp_ok or own_ok
        if (not geometry_ok) or ("不可信" in reason) or ("不是温州茶" in reason):
            # Keep discards payload for audit; do not invent 0/0 rivers.
            payload = dict(payload)
            payload["own_river"] = None
            payload["opponent_river"] = None
            payload["river_tiles"] = None
            out["discards"] = payload
            if out.get("incoming_tile") is None:
                out["incoming_tile"] = payload.get("incoming_tile")
            if out.get("incoming_source") is None:
                out["incoming_source"] = payload.get("incoming_source")
            out["own_river"] = None
            out["opponent_river"] = None
            return out
        for key in ("incoming_tile", "incoming_source", "opponent_river", "own_river", "river_tiles"):
            if out.get(key) is None:
                out[key] = payload.get(key)
    except Exception as exc:  # noqa: BLE001
        out.setdefault("discards", {"incoming_tile": None, "reason": f"弃牌读取失败：{exc}"})
    # Defense: empty [] from a "successful" read must not become 河0/0 in HUD.
    return _scrub_empty_river_lists(out)


def _resolve_room_round(
    image: Any,
    *,
    room_id: str | None,
    round_no: int | None,
    read_room: bool,
) -> tuple[str | None, int | None, int | None, dict[str, Any]]:
    manual_room = (str(room_id).strip() or None) if room_id is not None else None
    manual_round = round_no
    meta: dict[str, Any] = {
        "source": "manual" if (manual_room or manual_round is not None) else None
    }
    if not read_room or image is None:
        return manual_room, manual_round, None, meta
    try:
        from room_round_vision import read_room_round

        reading = read_room_round(image)
        meta["room_round_reading"] = reading.as_dict()
        meta["hud_visible"] = bool(reading.hud_visible)
    except Exception as exc:  # noqa: BLE001
        meta["room_round_error"] = str(exc)
        return manual_room, manual_round, None, meta
    room = manual_room if manual_room else reading.room_id
    rnd = manual_round if manual_round is not None else reading.round_no
    total = reading.round_total
    if manual_room or manual_round is not None:
        meta["source"] = (
            "manual+ocr"
            if (reading.room_id or reading.round_no is not None)
            else "manual"
        )
    elif reading.room_id or reading.round_no is not None:
        meta["source"] = "ocr"
    else:
        meta["source"] = "unrecognized"
    return room, rnd, total, meta


def _resolve_wall(
    image: Any,
    result: Mapping[str, Any],
    card_reader: CardReader | None,
) -> int | None:
    wall = result.get("wall_cards")
    if wall is not None:
        try:
            value = int(wall)
            return value if value >= 0 else None
        except (TypeError, ValueError):
            wall = None
    if card_reader is None or image is None:
        return None
    try:
        value = card_reader(image)
    except Exception:
        return None
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def build_vision_snapshot(
    result: Mapping[str, Any],
    *,
    room_id: str | None = None,
    round_no: int | None = None,
    wall_cards: int | None = None,
    meld_types: MeldTypeReading | None = None,
    claim_buttons: ClaimButtonsReading | None = None,
    room_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(result or {})
    discards = result.get("discards") if isinstance(result.get("discards"), Mapping) else {}
    diagnostics = result.get("vision_diagnostics") or result.get("diagnostics") or []
    if not isinstance(diagnostics, (list, tuple)):
        diagnostics = []
    own = result.get("own_river")
    if own is None:
        own = discards.get("own_river")
    opp = result.get("opponent_river")
    if opp is None:
        opp = discards.get("opponent_river")
    return {
        "mode": result.get("mode"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "hand_ids": list(result.get("hand_ids") or [])
        if isinstance(result.get("hand_ids"), (list, tuple))
        else [],
        "concealed_count": (
            len(result["hand_ids"])
            if isinstance(result.get("hand_ids"), (list, tuple))
            else None
        ),
        "melds": list(result.get("melds") or [])
        if isinstance(result.get("melds"), (list, tuple))
        else [],
        "meld_count": result.get("meld_count"),
        "meld_types": list((meld_types.types if meld_types else ()) or ()),
        "incoming_tile": result.get("incoming_tile", discards.get("incoming_tile")),
        "incoming_source": result.get(
            "incoming_source", discards.get("incoming_source")
        ),
        "own_river": list(own) if isinstance(own, (list, tuple)) else None,
        "opponent_river": list(opp) if isinstance(opp, (list, tuple)) else None,
        "wall_cards": wall_cards if wall_cards is not None else result.get("wall_cards"),
        "room_id": room_id,
        "round_no": round_no,
        "room_meta": dict(room_meta or {}),
        "claim_buttons": (
            claim_buttons.as_dict()
            if claim_buttons
            else {"known": False, "display_only": True}
        ),
        "diagnostics_count": len(diagnostics),
        "suggestion_id": result.get("suggestion_id"),
        "shanten": result.get("shanten"),
        "claim_tap_channel_ready": False,
        "read_only": True,
    }


def enrich_follow_result(
    result: Mapping[str, Any] | None,
    image: Any = None,
    *,
    room_id: str | None = None,
    round_no: int | None = None,
    wall_cards: int | None = None,
    card_reader: CardReader | None = None,
    discard_key: str | None = None,
    read_river: bool = True,
    read_room: bool = True,
    read_claims_display: bool = True,
) -> Enrichment:
    base = dict(result or {})
    notes: list[str] = []
    enriched = _ensure_discards(
        base, image, discard_key=discard_key, read_river=read_river
    )

    wall = wall_cards
    if wall is None:
        wall = _resolve_wall(image, enriched, card_reader)
    if wall is not None:
        enriched["wall_cards"] = wall

    room, rnd, total, room_meta = _resolve_room_round(
        image, room_id=room_id, round_no=round_no, read_room=read_room
    )
    if room_meta.get("source") in {None, "unrecognized"} and room is None and rnd is None:
        notes.append("room_round_unrecognized")

    melds = enriched.get("melds")
    stacked = enriched.get("stacked_kongs")
    if not isinstance(stacked, (list, tuple)):
        stacked = None
    meld_types = classify_meld_types(melds, stacked_kongs=stacked)
    enriched["meld_types"] = list(meld_types.types)

    if wall is None and card_reader is None and not WALL_CARD_READER_CONNECTED:
        notes.append("wall_card_reader_not_connected")

    claim = ClaimButtonsReading(display_only=True, reason="未读")
    if read_claims_display:
        claim = _read_claim_buttons(image)
    enriched["claim_buttons"] = claim.as_dict()

    snap = build_vision_snapshot(
        enriched,
        room_id=room,
        round_no=rnd,
        wall_cards=wall,
        meld_types=meld_types,
        claim_buttons=claim,
        room_meta=room_meta,
    )
    enriched["vision_snapshot"] = snap

    return Enrichment(
        result=enriched,
        room_id=room,
        round_no=rnd,
        round_total=total,
        wall_cards=wall,
        meld_types=meld_types,
        claim_buttons=claim,
        vision_snapshot=snap,
        notes=notes,
    )
