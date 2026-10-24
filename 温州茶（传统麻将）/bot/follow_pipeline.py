# -*- coding: utf-8 -*-
"""Single continuous read-only follow path (P1 unify).

Pipeline owned by both the live window (开始跟随) and AutoPlayLoop::

    实时画面 → 识别事实 → TableState → 安全门禁 → 推荐/执行 → 点击后验证 → 每帧日志

This module never clicks, never talks to ADB, and never flips
``CLAIM_TAP_CHANNEL_READY``. Missing decision facts force abstain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from action_policy import CLAIM_TAP_CHANNEL_READY
from follow_enrich import enrich_follow_result
from session_logger import FollowSessionLogError, SessionLogger
from table_state import TableState, build_table_state

__all__ = [
    "DECISION_FACTS",
    "DISPLAY_FACTS",
    "FrameFacts",
    "FollowFrameOutcome",
    "FollowPipeline",
    "missing_decision_facts",
    "build_frame_facts",
    "format_frame_facts_display",
]

# Facts required before an actionable discard/claim recommendation.
DECISION_FACTS: tuple[str, ...] = (
    "mode",
    "caishen",
    "concealed",  # dark hand ids present
)

# Facts the UI / JSONL should persist or show when available.
DISPLAY_FACTS: tuple[str, ...] = (
    "mode",           # 玩法
    "room_round",     # 房号/局数
    "caishen",        # 财神
    "concealed",      # 暗手
    "melds",          # 副露
    "meld_types",     # 副露类型
    "river",          # 牌河
    "last_discard",   # 最近出牌
    "turn",           # 轮到谁
    "countdown",      # 倒计时
    "wall_cards",     # 剩余牌数
    "claim_buttons",  # 鸣牌按钮（只读）
)

UNKNOWN = "未识别"


@dataclass(frozen=True)
class FrameFacts:
    """Normalized per-frame facts for display + gate checks."""

    mode: str | None = None
    room_id: str | None = None
    round_no: int | None = None
    round_seq: int | None = None
    caishen: str | None = None
    caishen_known: bool = False
    concealed: tuple[Any, ...] = ()
    concealed_count: int | None = None
    melds: tuple[Any, ...] = ()
    meld_count: int = 0
    meld_types: tuple[str, ...] = ()
    river_own: tuple[Any, ...] | None = None
    river_opp: tuple[Any, ...] | None = None
    last_discard: Any = None
    last_discard_source: str | None = None
    turn_status: str | None = None
    turn_actionable: bool = False
    countdown_status: str = "absent"
    countdown_seconds: int | None = None
    wall_cards: int | None = None
    claim_buttons: tuple[str, ...] = ()
    claim_buttons_known: bool = False
    round_total: int | None = None
    missing_decision: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def ledger(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("concealed", "melds", "meld_types", "river_own", "river_opp",
                    "claim_buttons", "missing_decision", "notes"):
            value = payload.get(key)
            if isinstance(value, tuple):
                payload[key] = list(value)
        return payload


@dataclass
class FollowFrameOutcome:
    """One observe() result on the unified path."""

    table: TableState
    facts: FrameFacts
    event: dict[str, Any]
    abstain: bool
    display: dict[str, str]
    log_path: Path | None = None


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _river_parts(result: Mapping[str, Any]) -> tuple[
    tuple[Any, ...] | None, tuple[Any, ...] | None, Any, str | None
]:
    incoming = result.get("incoming_tile")
    source = result.get("incoming_source")
    opp = result.get("opponent_river")
    own = result.get("own_river")
    block = result.get("discard") or result.get("discards") or {}
    if isinstance(block, Mapping):
        if incoming is None:
            incoming = block.get("incoming_tile")
        source = source or block.get("incoming_source")
        if opp is None:
            opp = block.get("opponent_river")
        if own is None:
            own = block.get("own_river")
    # Empty []/() is unknown, not zero — HUD must show 未识别, never 我0/对0.
    own_t = tuple(own) if isinstance(own, (list, tuple)) and len(own) > 0 else None
    opp_t = tuple(opp) if isinstance(opp, (list, tuple)) and len(opp) > 0 else None
    src = str(source) if source is not None else None
    return own_t, opp_t, incoming, src


def missing_decision_facts(
    result: Mapping[str, Any] | None,
    table: TableState | None = None,
) -> list[str]:
    """Return decision-fact names that are missing (fail-closed → abstain)."""
    result = dict(result or {})
    missing: list[str] = []
    mode = result.get("mode")
    if mode is None or str(mode).strip() == "":
        missing.append("mode")
    caishen_known = False
    if table is not None:
        caishen_known = bool(table.caishen_known)
    else:
        from turn_gate import known_caishen_id
        caishen_known = known_caishen_id(result) is not None
    if not caishen_known:
        missing.append("caishen")
    hand = result.get("hand_ids") or ()
    if not isinstance(hand, (list, tuple)) or len(hand) == 0:
        # physical boxes alone are not enough for a decision without ids
        missing.append("concealed")
    return missing



def _resolve_meld_types(meld_types, result):
    if meld_types is not None:
        return tuple(meld_types or ())
    raw = result.get("meld_types")
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw)
    return ()


def _resolve_claim_buttons(claim_buttons, result):
    if claim_buttons is not None:
        return tuple(claim_buttons or ()), True
    block = result.get("claim_buttons")
    if isinstance(block, Mapping):
        buttons = block.get("buttons") or ()
        if isinstance(buttons, (list, tuple)):
            return tuple(str(x) for x in buttons), bool(block.get("known"))
    return (), False


def build_frame_facts(
    result: Mapping[str, Any] | None,
    table: TableState,
    *,
    room_id: str | None = None,
    round_no: int | None = None,
    round_seq: int | None = None,
    turn_status: str | None = None,
    turn_actionable: bool = False,
    meld_types: tuple[str, ...] | None = None,
    claim_buttons: tuple[str, ...] | None = None,
    claim_buttons_known: bool = False,
    round_total: int | None = None,
) -> FrameFacts:
    result = dict(result or {})
    hand = result.get("hand_ids") or table.hand_ids
    melds = result.get("melds") or ()
    if not isinstance(melds, (list, tuple)):
        melds = ()
    own, opp, incoming, src = _river_parts(result)
    if incoming is None:
        incoming = table.incoming_tile
    if src is None:
        src = table.incoming_source
    missing = missing_decision_facts(result, table)
    notes = list(table.notes)
    if missing:
        notes.append("missing_decision:" + ",".join(missing))
    return FrameFacts(
        mode=(str(result["mode"]) if result.get("mode") is not None
              else table.mode),
        room_id=(str(room_id).strip() or None) if room_id is not None else None,
        round_no=round_no,
        round_seq=round_seq,
        caishen=table.caishen or (str(result["caishen"]) if result.get("caishen") else None),
        caishen_known=bool(table.caishen_known),
        concealed=_as_tuple(hand),
        concealed_count=(
            table.concealed_count if table.concealed_count is not None
            else (len(hand) if isinstance(hand, (list, tuple)) and hand else None)
        ),
        melds=_as_tuple(melds),
        meld_count=int(table.meld_count or 0),
        meld_types=_resolve_meld_types(meld_types, result),
        river_own=own,
        river_opp=opp,
        last_discard=incoming,
        last_discard_source=src,
        turn_status=turn_status if turn_status is not None else table.status,
        turn_actionable=bool(turn_actionable),
        countdown_status=table.countdown_status,
        countdown_seconds=table.countdown_seconds,
        wall_cards=table.wall_cards,
        claim_buttons=_resolve_claim_buttons(claim_buttons, result)[0],
        claim_buttons_known=(
            bool(claim_buttons_known) if claim_buttons is not None
            else _resolve_claim_buttons(None, result)[1]
        ),
        round_total=round_total,
        missing_decision=tuple(missing),
        notes=tuple(notes),
    )


def format_frame_facts_display(facts: FrameFacts) -> dict[str, str]:
    """Chinese HUD labels; never invents values (unknown → 未识别)."""
    mode = facts.mode or UNKNOWN
    if facts.room_id or facts.round_no is not None:
        room = facts.room_id or UNKNOWN
        rnd = str(facts.round_no) if facts.round_no is not None else UNKNOWN
        room_round = f"房号:{room} 局数:{rnd}"
    else:
        room_round = f"房号/局数：{UNKNOWN}"
    caishen = facts.caishen if facts.caishen_known and facts.caishen else UNKNOWN
    if facts.concealed_count is not None:
        concealed = f"暗手:{facts.concealed_count}张"
    else:
        concealed = f"暗手：{UNKNOWN}"
    # meld_count is authoritative once we have any table snapshot.
    if facts.concealed_count is not None or facts.caishen_known or facts.melds:
        melds = f"副露:{int(facts.meld_count)}组"
    else:
        melds = f"副露：{UNKNOWN}"
    if facts.meld_types:
        meld_types = "副露类型：" + "/".join(str(t) for t in facts.meld_types)
    elif facts.concealed_count is not None or facts.caishen_known or facts.melds is not None:
        meld_types = "副露类型：无" if int(facts.meld_count or 0) == 0 else f"副露类型：{UNKNOWN}"
    else:
        meld_types = f"副露类型：{UNKNOWN}"
    own_n = len(facts.river_own) if facts.river_own is not None else None
    opp_n = len(facts.river_opp) if facts.river_opp is not None else None
    if own_n is None and opp_n is None:
        river = f"牌河：{UNKNOWN}"
    else:
        river = (
            f"牌河:我{UNKNOWN if own_n is None else own_n}/"
            f"对{UNKNOWN if opp_n is None else opp_n}"
        )
    src_map = {
        "opponent": "对家", "self": "我方", "own": "我方",
    }
    if facts.last_discard is not None:
        who = src_map.get(str(facts.last_discard_source or ""), UNKNOWN)
        last = f"最近出牌:{who}:{facts.last_discard}"
    else:
        last = f"最近出牌：{UNKNOWN}"
    status = str(facts.turn_status or "").lower()
    if status == "advise" and facts.turn_actionable:
        turn = "轮到：我方可出"
    elif status in {"wait", "idle"}:
        turn = "轮到：等待"
    elif status == "abstain":
        turn = "轮到：弃权(事实不足)"
    else:
        turn = f"轮到：{UNKNOWN}"
    cd_status = str(facts.countdown_status or "").lower()
    if cd_status == "expired":
        countdown = "倒计时：0秒"
    elif cd_status in {"present", "low"} and facts.countdown_seconds is not None:
        countdown = f"倒计时：{facts.countdown_seconds}秒"
    else:
        countdown = f"倒计时：{UNKNOWN}"
    if facts.wall_cards is None:
        wall = f"余牌：{UNKNOWN}"
    else:
        wall = f"余牌：{facts.wall_cards}"
    if facts.claim_buttons_known and facts.claim_buttons:
        claim_buttons = "鸣牌按钮：" + "/".join(str(b) for b in facts.claim_buttons) + "（只读）"
    else:
        claim_buttons = f"鸣牌按钮：{UNKNOWN}"
    return {
        "mode": f"玩法：{mode}",
        "room_round": room_round,
        "caishen": f"财神：{caishen}",
        "concealed": concealed,
        "melds": melds,
        "meld_types": meld_types,
        "river": river,
        "last_discard": last,
        "turn": turn,
        "countdown": countdown,
        "wall_cards": wall,
        "claim_buttons": claim_buttons,
    }


class FollowPipeline:
    """Owns SessionLogger + per-frame TableState observe for 开始跟随."""

    def __init__(
        self,
        session_dir: str | Path,
        *,
        mode: str | None = None,
        enabled: bool = True,
        card_reader=None,
        read_river: bool = True,
        read_room: bool = True,
        read_claims_display: bool = True,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.mode = mode
        # Live assist has no reliable wall OCR yet — keep None unless caller injects one.
        self.card_reader = card_reader
        from follow_enrich import WALL_CARD_READER_CONNECTED, river_read_enabled

        self.wall_card_reader_connected = bool(
            WALL_CARD_READER_CONNECTED and card_reader is not None
        )
        # River OCR only for calibrated tea_2p; uncalibrated modes force off.
        self.read_river = bool(read_river) and river_read_enabled(mode)
        self.read_room = bool(read_room)
        self.read_claims_display = bool(read_claims_display)
        self.logger = SessionLogger(self.session_dir, enabled=enabled)
        self.tick_index = 0
        self.started = False
        self.last_outcome: FollowFrameOutcome | None = None
        if CLAIM_TAP_CHANNEL_READY:
            # Soft stamp only; never enable clicks from here.
            self._channel_note = "UNEXPECTED_CLAIM_CHANNEL_READY"
        else:
            self._channel_note = "claim_channel_closed"

    @property
    def follow_log_path(self) -> Path | None:
        return self.logger.path

    def start(self, meta: Mapping[str, Any] | None = None) -> Path:
        """Create session_dir/follow_session.jsonl immediately or raise."""
        payload = {
            "source": "assist_window_follow",
            "mode": self.mode,
            "claim_tap_channel_ready": bool(CLAIM_TAP_CHANNEL_READY),
            "pipeline": "follow_pipeline/v1",
            "note": "unified read-only follow path; no ADB clicks",
        }
        if meta:
            payload.update(dict(meta))
        if not self.logger.enabled:
            raise FollowSessionLogError(
                "FollowPipeline logger disabled; refuse live follow without jsonl"
            )
        path = self.logger.ensure_ready(payload)
        self.started = True
        self.tick_index = 0
        return path

    def observe(
        self,
        result: Mapping[str, Any] | None,
        image: Any = None,
        *,
        frame_path: str | Path | None = None,
        room_id: str | None = None,
        round_no: int | None = None,
        round_seq: int | None = None,
        wall_cards: int | None = None,
        discards_trusted: bool = False,
        incoming_tile: Any = None,
        incoming_source: str | None = None,
        turn_status: str | None = None,
        turn_actionable: bool = False,
        event_name: str | None = None,
        extra_event: Mapping[str, Any] | None = None,
    ) -> FollowFrameOutcome:
        """Build TableState, gate on decision facts, append follow_session.jsonl."""
        result_dict = dict(result or {})
        if self.mode and not result_dict.get("mode"):
            result_dict["mode"] = self.mode
        if incoming_tile is not None and "incoming_tile" not in result_dict:
            result_dict["incoming_tile"] = incoming_tile
        if incoming_source is not None and "incoming_source" not in result_dict:
            result_dict["incoming_source"] = incoming_source
        enrichment = enrich_follow_result(
            result_dict,
            image,
            room_id=room_id,
            round_no=round_no,
            wall_cards=wall_cards,
            card_reader=getattr(self, "card_reader", None),
            discard_key=str(self.session_dir),
            read_river=getattr(self, "read_river", True),
            read_room=getattr(self, "read_room", True),
            read_claims_display=getattr(self, "read_claims_display", True),
        )
        result_dict = enrichment.result
        room_id = enrichment.room_id if enrichment.room_id is not None else room_id
        round_no = enrichment.round_no if enrichment.round_no is not None else round_no
        wall_cards = enrichment.wall_cards
        table = build_table_state(
            result_dict,
            image,
            wall_cards=wall_cards,
            discards_trusted=bool(discards_trusted),
            incoming_tile=result_dict.get("incoming_tile"),
            incoming_source=result_dict.get("incoming_source"),
        )
        facts = build_frame_facts(
            result_dict,
            table,
            room_id=room_id,
            round_no=round_no,
            round_seq=round_seq,
            turn_status=turn_status if turn_status is not None else result_dict.get("status"),
            turn_actionable=turn_actionable,
            meld_types=enrichment.meld_types.types,
            claim_buttons=enrichment.claim_buttons.buttons,
            claim_buttons_known=bool(enrichment.claim_buttons.known),
            round_total=enrichment.round_total,
        )
        display = format_frame_facts_display(facts)
        missing = list(facts.missing_decision)
        force_abstain = bool(missing)
        status = result_dict.get("status")
        if force_abstain and status == "advise":
            status = "abstain"
        reason = result_dict.get("reason")
        if force_abstain:
            gap = ",".join(missing)
            reason = f"决策事实不足({gap})；本帧弃权" + (f"；{reason}" if reason else "")
        if event_name:
            event_kind = event_name
        elif force_abstain:
            event_kind = "idle"
        elif status == "advise" and result_dict.get("suggestion_id") is not None:
            event_kind = "recommend"
        else:
            event_kind = "idle"
        self.tick_index += 1
        event: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(),
            "event": event_kind,
            "status": status,
            "reason": reason,
            "suggestion": None if force_abstain else result_dict.get("suggestion"),
            "suggestion_id": None if force_abstain else result_dict.get("suggestion_id"),
            "caishen": facts.caishen,
            "frame": str(frame_path) if frame_path is not None else result_dict.get("frame_path") or result_dict.get("image"),
            "round_seq": round_seq,
            "table_state": table.ledger(),
            "frame_facts": facts.ledger(),
            "display": display,
            "vision_snapshot": enrichment.vision_snapshot,
            "claim_tap_channel_ready": bool(CLAIM_TAP_CHANNEL_READY),
            "pipeline": "follow_pipeline/v1",
            "safety": self._channel_note,
        }
        if extra_event:
            # Caller extras must not overwrite safety / decision fields.
            protected = {
                "status",
                "suggestion",
                "suggestion_id",
                "safety",
                "claim_tap_channel_ready",
                "pipeline",
                "event",
                "table_state",
                "frame_facts",
                "display",
                "vision_snapshot",
                "abstain",
            }
            merged = {
                key: value
                for key, value in dict(extra_event).items()
                if key not in protected
            }
            event.update(merged)
            event["claim_tap_channel_ready"] = bool(CLAIM_TAP_CHANNEL_READY)
        if not self.started:
            self.start({"mode": facts.mode or self.mode})
        log_path = self.logger.append(
            event,
            tick=self.tick_index,
            analysis=result_dict,
        )
        if self.logger.enabled and (
            log_path is None or not Path(log_path).is_file()
        ):
            raise FollowSessionLogError(
                f"follow_session.jsonl missing after observe append: {self.logger.path}"
            )
        outcome = FollowFrameOutcome(
            table=table,
            facts=facts,
            event=event,
            abstain=force_abstain or status == "abstain",
            display=display,
            log_path=log_path,
        )
        self.last_outcome = outcome
        return outcome
