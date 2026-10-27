"""Read-only start-up/room-card gate for a SINGLE account's tea-2p lobby.

This module never taps the game and never creates/joins a room.
It wraps Hana's existing session_watch classifier and reports whether
account cards are reliably readable; failures are visible, not guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from session_watch import WatchState, classify_watch

LOW_CARDS_REMINDER_AT = 2


@dataclass(frozen=True)
class LobbyAssessment:
    state: str
    room_cards: int | None
    card_status: str
    warning: str
    can_consider_entry: bool
    requires_manual_step: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "room_cards": self.room_cards,
            "card_status": self.card_status,
            "warning": self.warning,
            "can_consider_entry": self.can_consider_entry,
            "requires_manual_step": self.requires_manual_step,
        }


def assess_frame(image, *, low_at: int = LOW_CARDS_REMINDER_AT) -> LobbyAssessment:
    observed = classify_watch(image)
    state = observed.state.value
    if observed.state == WatchState.LOGIN_SCREEN:
        return LobbyAssessment(state, None, "not_readable",
                               "检测到登录页，停止自动操作；请人工确认账号。", False)
    if observed.state == WatchState.SETTLEMENT:
        return LobbyAssessment(state, None, "not_applicable",
                               "当前是结算画面，暂不自动离桌或开房。", False)
    if observed.state == WatchState.WAITING_ROOM:
        return LobbyAssessment(state, None, "not_visible",
                               "已在两人等候室；房卡数只可在大厅核验，不能假设余额充足。", False)
    if observed.state == WatchState.IN_GAME:
        return LobbyAssessment(state, None, "not_visible",
                               "已在牌局：交给现有实时跟随/代打引擎，不执行大厅动作。", False)
    if observed.state != WatchState.LOBBY:
        return LobbyAssessment(state, None, "scene_unknown",
                               "当前画面无法确认为比赛场大厅，停止场次操作。", False)

    evidence = observed.evidence.get("room_cards") or {}
    count = observed.room_cards
    status = str(evidence.get("status") or "unknown")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or status != "ok":
        return LobbyAssessment(state, None, status,
                               "无法可靠读取房卡余额（" + status + "）；"
                               "可无扣费入座，开局仅走用户已授权的专用门禁且绝不充值。", False)
    if count == 0:
        return LobbyAssessment(state, 0, "ok",
                               "房卡已用完：已禁止新开局，请手动处理（不会自动充值）。", False)
    if count <= low_at:
        return LobbyAssessment(state, count, "ok",
                               f"房卡只剩 {count} 张，请注意余额；不会自动充值。", True)
    return LobbyAssessment(state, count, "ok",
                           f"房卡余额 {count} 张；不自动充值。", True)


def assess_single_session(serial: str, assessment: LobbyAssessment) -> dict:
    """Evaluate a single account. Never require another emulator/account.

    tea_2p describes table RULES, not the number of BOT-controlled seats.
    """
    if not serial or not serial.strip() or any(c.isspace() for c in serial):
        return {"ready": False, "reason": "当前设备序列号无效", "execute": False}
    if not assessment.can_consider_entry:
        return {"ready": False, "reason": assessment.warning, "execute": False}
    return {
        "ready": True,
        "serial": serial,
        "reason": "当前单账号大厅/房卡预检通过；仍须核对目标桌位和玩法",
        "requires_table_proof": True,
        "execute": False,
    }


@dataclass(frozen=True)
class SessionScene:
    """A read-only routing fact. It never grants permission to click."""
    kind: str
    reason: str
    evidence: dict
    state: Any = None

    def ledger(self) -> dict:
        return {"kind": self.kind, "reason": self.reason, **self.evidence}


def classify_session_scene(image, *, round_probe=None) -> SessionScene:
    """One priority order for explicit restart and the running play loop.

    Exact overlays win over their obscured background. A missing gameplay
    fact is not evidence of a lobby. Unknown scenes remain observation-only.
    Each executor still owns its independent fresh-frame and authority gates.
    """
    import numpy as np
    from invite_dialog import read_invitation
    from promo_popup import read_promo_popup
    from timeout_dissolve import read_timeout_dissolve
    from match_end import read_match_end, precise_settlement_evidence
    from lobby_autostart import read_lobby_room
    from lobby_start import waiting_room_visual_evidence

    if (not isinstance(image, np.ndarray) or image.ndim != 3
            or image.shape[:2] != (900, 1600) or image.dtype != np.uint8):
        return SessionScene("observe", "等待可核验的1600×900原生帧", {})
    try:
        for kind, reader in (("timeout_dissolve", read_timeout_dissolve),
                             ("invitation", read_invitation),
                             ("promo", read_promo_popup)):
            state = reader(image)
            if state.present:
                return SessionScene(kind, state.reason, {kind: state.ledger()}, state)
            if kind == "promo" and state.reason == "关闭按钮模板缺失":
                return SessionScene("blocked", "促销遮罩无法核验：关闭按钮模板缺失",
                                    {kind: state.ledger()})
        match = read_match_end(image)
        if match.present:
            return SessionScene("match_end", match.reason,
                                {"match_end": match.ledger()})
        settled, evidence = precise_settlement_evidence(
            image, round_probe=round_probe, check_match=False)
        if settled:
            return SessionScene("next_round", "已确认局间下一局三重门", evidence)
        waiting = waiting_room_visual_evidence(image)
        if waiting.get("matched"):
            return SessionScene("waiting_room", "已确认等候室开始按钮",
                                {"waiting_room": waiting})
        room = read_lobby_room(image)
        if room.present:
            return SessionScene("lobby", room.reason, {"lobby_room": room.ledger()})
        observed = classify_watch(image)
        if observed.state == WatchState.LOGIN_SCREEN:
            return SessionScene("blocked", "登录页不能沿用当前玩家授权",
                                {"watch": observed.to_dict()})
        if observed.state == WatchState.IN_GAME:
            # This only routes to the existing identity/mode/hand gates.
            return SessionScene("in_game", "牌桌候选，仍须通过当前局内门禁",
                                {"watch": observed.to_dict()})
        return SessionScene("observe", "场景暂不可确认；零点击继续观察",
                            {"watch": observed.to_dict()})
    except Exception as exc:
        return SessionScene("observe",
                            f"场景证据读取失败，零点击观察：{type(exc).__name__}: {exc}",
                            {})
