"""Lobby-to-table read-only handoff regression: no ADB taps."""
from __future__ import annotations
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import assist_window
import lobby_follow
from lobby_preflight import LobbyAssessment
from assist_window import TeaAssistWindow


class Variable:
    def __init__(self,value=""): self.value=value
    def get(self): return self.value
    def set(self,value): self.value=value


class Widget:
    def __init__(self): self.data={}
    def configure(self,**kw): self.data.update(kw)


class Root:
    def __init__(self): self.scheduled=[]
    def after(self,ms,fn):
        self.scheduled.append((ms,fn))
        return "timer"
    def after_cancel(self,token): self.scheduled.append(("cancel",token))


def test_lobby_probe_uses_read_only_capture(monkeypatch,tmp_path):
    im=np.zeros((900,1600,3),np.uint8)
    encoded=cv2.imencode(".png",im)[1].tobytes()
    seen=[]
    monkeypatch.setattr(lobby_follow, "capture_frame",lambda **kw:(seen.append(kw) or encoded,None))
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda image:
        LobbyAssessment("lobby",None,"digit_templates_incomplete",
                        "无法可靠读取房卡",False))
    out=lobby_follow.probe("emulator-5556","adb.exe",tmp_path)
    assert out.state=="lobby"
    assert seen[0]["serial"]=="emulator-5556"
    assert not out.can_consider_entry


def test_start_in_lobby_bypasses_table_mode_gate_until_in_game():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window.continuous=False
    window.video_future=None
    window._lobby_active=False
    window._lobby_bootstrap_enabled=True
    window._lobby_in_game_verified=False
    window.mode=Variable("温州茶 · 2人")
    window._is_bound=lambda:True
    actions=[]
    window._begin_lobby_follow=lambda:actions.append("lobby")
    window._ready=lambda *a,**k: (_ for _ in ()).throw(AssertionError("table check too early"))
    window.start()
    assert actions==["lobby"]


def test_lobby_watcher_handoffs_after_in_game():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._lobby_active=True
    window._lobby_in_game_verified=False
    window._lobby_timer=None
    window._lobby_future=SimpleNamespace(done=lambda:True,
        result=lambda:LobbyAssessment("in_game",None,"not_visible","对局",False))
    window.start_button=Widget()
    window.root=Root()
    window.status=Variable()
    callbacks=[]
    window.start=lambda:callbacks.append("start")
    window._poll_lobby_probe()
    assert callbacks==["start"]
    assert window._lobby_in_game_verified is True
    assert window._lobby_active is False


def test_lobby_unknown_never_starts_or_clicks():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._lobby_active=True
    window._lobby_in_game_verified=False
    window._lobby_timer=None
    window._lobby_future=SimpleNamespace(done=lambda:True,
        result=lambda:LobbyAssessment("unknown",None,"scene_unknown","不能确认游戏画面",False))
    window.start_button=Widget()
    window.root=Root()
    window.status=Variable()
    window._set_operational_status=lambda *a,**k:None
    window.start=lambda: (_ for _ in ()).throw(AssertionError("should not follow"))
    window._poll_lobby_probe()
    assert window._lobby_active is False
    assert "不能确认" in window.status.get()
