"""No-click lobby status and room-card warnings."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import lobby_preflight
from session_watch import WatchResult, WatchState


def _state(state, cards=None, status="ok"):
    return WatchResult(state, {"room_cards":{"status":status,"value":cards}}, [], cards)


def test_unknown_card_template_blocks_entry(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch", lambda _: _state(WatchState.LOBBY,None,"digit_templates_incomplete"))
    result=lobby_preflight.assess_frame(object())
    assert result.can_consider_entry is False
    assert "无法可靠读取房卡" in result.warning


def test_zero_card_blocks_no_auto_topup(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch",lambda _: _state(WatchState.LOBBY,0))
    result=lobby_preflight.assess_frame(object())
    assert result.room_cards==0 and not result.can_consider_entry
    assert "不会自动充值" in result.warning


def test_low_card_triggers_notice(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch",lambda _: _state(WatchState.LOBBY,1))
    result=lobby_preflight.assess_frame(object())
    assert result.room_cards==1 and result.can_consider_entry
    assert "只剩 1 张" in result.warning


def test_one_account_is_enough_for_lobby_preflight(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch",lambda _: _state(WatchState.LOBBY,8))
    result=lobby_preflight.assess_frame(object())
    one=lobby_preflight.assess_single_session("emulator-5556", result)
    assert one["ready"] and not one["execute"] and one["requires_table_proof"]
    assert one["serial"] == "emulator-5556"


def test_waiting_room_does_not_claim_cards(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch",lambda _: _state(WatchState.WAITING_ROOM))
    result=lobby_preflight.assess_frame(object())
    assert not result.can_consider_entry
    assert result.room_cards is None


def test_login_screen_is_hard_stop(monkeypatch):
    monkeypatch.setattr(lobby_preflight, "classify_watch",lambda _: _state(WatchState.LOGIN_SCREEN))
    result=lobby_preflight.assess_frame(object())
    assert "登录页" in result.warning
