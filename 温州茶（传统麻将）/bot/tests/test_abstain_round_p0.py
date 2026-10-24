# -*- coding: utf-8 -*-
"""P0: abstain 帧不得提前 return，必须先做换局检测。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import assist_window  # noqa: E402
from assist_window import TeaAssistWindow  # noqa: E402


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _window(pick: str = "", manual: bool = False) -> TeaAssistWindow:
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.continuous_round = None
    window.continuous_caishen = None
    window.timer = None
    window.status = _Value("")
    window.live_info = _Value("")
    window.caishen = _Value(pick)
    window.manual_caishen = _Value(manual)
    window.current = {"status": "advise", "suggestion": "w1"}
    window.hand = _Value("旧手牌")
    window.preview = object()
    window.picture = SimpleNamespace(configure=lambda **kwargs: None)
    window._invalidate_calls = []
    window._invalidate_live_advice = lambda: window._invalidate_calls.append("invalidate")
    return window


def test_abstain_with_caishen_change_adopts_new_round_and_clears_manual(monkeypatch):
    """abstain + 财神变化：自动切新局、清手选、本帧无建议。"""
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda _p: None)
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence",
        lambda p: ("dong", 1.0) if p == "old" else ("t8", 1.0))
    window = _window(pick="dong", manual=True)
    assert window._accept_continuous_result({"status": "advise", "image": "old"})
    assert window.continuous_caishen == "dong"
    assert not window._accept_continuous_result({
        "status": "abstain", "image": "new", "reason": "手牌不完整",
    })
    assert window.continuous is True
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert window.continuous_caishen == "t8"
    assert "财神牌已变化" in window.status.get()
    assert "自动建立新局基线" in window.status.get()
    assert "等待下一张稳定画面" in window.live_info.get()


def test_abstain_with_hud_change_and_unreadable_caishen_waits_for_card(monkeypatch):
    """abstain + HUD 变化、财神不可读：切新局并等待财神。"""
    monkeypatch.setattr(
        assist_window, "tea_round_fingerprint",
        lambda p: (b"O" * 100) if p == "old" else (b"N" * 100))
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence",
        lambda p: ("dong", 1.0) if p == "old" else (None, 0.0))
    window = _window(pick="dong", manual=True)
    assert window._accept_continuous_result({"status": "advise", "image": "old"})
    assert window.continuous_round == b"O" * 100
    assert window.continuous_caishen == "dong"
    assert not window._accept_continuous_result({
        "status": "abstain", "image": "new", "reason": "遮挡",
    })
    assert window.continuous is True
    assert window.continuous_round == b"N" * 100
    assert window.continuous_caishen is None  # 等待新财神
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert "房号或局数已变化" in window.status.get()
    assert "手选财神已清空" in window.status.get()


def test_abstain_same_caishen_keeps_round_baseline(monkeypatch):
    """abstain + 同一财神：保留本局基线，只撤销建议。"""
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda _p: b"same-hud")
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence", lambda _p: ("t8", 1.0))
    window = _window()
    assert window._accept_continuous_result({"status": "advise", "image": "f1"})
    assert window.continuous_round == b"same-hud"
    assert window.continuous_caishen == "t8"
    assert window._accept_continuous_result({
        "status": "abstain", "image": "f2", "reason": "暂时遮挡",
        "hand": ["w1", "w2"],
    })
    assert window.continuous is True
    assert window.continuous_round == b"same-hud"
    assert window.continuous_caishen == "t8"
    assert window._invalidate_calls == ["invalidate"]
    assert "保留当前识牌证据" in window.live_info.get()


def test_abstain_both_evidence_unreadable_fail_closed(monkeypatch):
    """abstain + 两路证据都不可读：不继承旧建议、不猜新财神。"""
    monkeypatch.setattr(
        assist_window, "tea_round_fingerprint",
        lambda p: b"locked" if p == "clear" else None)
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence",
        lambda p: ("s1", 1.0) if p == "clear" else (None, 0.0))
    window = _window()
    assert window._accept_continuous_result({"status": "advise", "image": "clear"})
    assert window.continuous_round == b"locked"
    assert window.continuous_caishen == "s1"
    resets = []
    window._reset_round_display = lambda reason: resets.append(reason)
    assert window._accept_continuous_result({
        "status": "abstain", "image": "obscured", "reason": "未完整",
    })
    assert window.continuous_round == b"locked"
    assert window.continuous_caishen == "s1"  # 不猜新财神、不清掉旧基线
    assert resets == []  # 不抹掉当前截图/识牌证据
    assert window._invalidate_calls == ["invalidate"]
    assert "保留当前识牌证据" in window.live_info.get()


def test_non_abstain_caishen_change_still_adopts_new_round(monkeypatch):
    """原有非 abstain 路径不回归：advise 帧财神变化仍切新局。"""
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda _p: None)
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence",
        lambda p: ("dong", 1.0) if p == "old" else ("t8", 1.0))
    window = _window(pick="dong", manual=True)
    assert window._accept_continuous_result({"status": "advise", "image": "old"})
    assert not window._accept_continuous_result({"status": "advise", "image": "new"})
    assert window.continuous_caishen == "t8"
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert "财神牌已变化" in window.status.get()


def test_non_abstain_same_round_still_advises(monkeypatch):
    """原有非 abstain 路径不回归：同局 advise 仍放行。"""
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda _p: b"hud")
    monkeypatch.setattr(
        assist_window, "tea_round_caishen_evidence", lambda _p: ("t8", 1.0))
    window = _window()
    assert window._accept_continuous_result({"status": "advise", "image": "a"})
    assert window._accept_continuous_result({"status": "advise", "image": "b"})
    assert window.continuous_round == b"hud"
    assert window.continuous_caishen == "t8"
    assert window._invalidate_calls == []
