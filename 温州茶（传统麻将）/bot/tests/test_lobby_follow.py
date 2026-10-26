"""Lobby-to-table read-only handoff regression: no ADB taps."""
from __future__ import annotations
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import lobby_follow
from lobby_preflight import LobbyAssessment
from lobby_follow import LobbyProgress
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


def test_start_preflights_restart_settlement_before_lobby_gate():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window.continuous=False
    window.video_future=None
    window._lobby_active=False
    window._lobby_bootstrap_enabled=True
    window._lobby_in_game_verified=False
    window.mode=Variable("温州茶 · 2人")
    window._is_bound=lambda:True
    actions=[]
    window._begin_startup_round_transition=lambda mode:actions.append(("recover",mode))
    window._ready=lambda *a,**k: (_ for _ in ()).throw(AssertionError("table check too early"))
    window.start()
    assert actions==[("recover","follow")]


def test_lobby_watcher_handoffs_after_in_game():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._lobby_active=True
    window._lobby_in_game_verified=False
    window._lobby_timer=None
    window._lobby_future=SimpleNamespace(done=lambda:True,
        result=lambda:LobbyProgress("in_game","对局",None,"not_visible",seated=True))
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
        result=lambda:LobbyProgress("unknown","不能确认游戏画面",None,"scene_unknown",terminal=True))
    window.start_button=Widget()
    window.root=Root()
    window.status=Variable()
    window._set_operational_status=lambda *a,**k:None
    window.start=lambda: (_ for _ in ()).throw(AssertionError("should not follow"))
    window._poll_lobby_probe()
    assert window._lobby_active is False
    assert "不能确认" in window.status.get()


def test_waiting_room_stays_observing_without_start_click():
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._lobby_active=True
    window._lobby_in_game_verified=False
    window._lobby_timer=None
    window._lobby_future=SimpleNamespace(done=lambda:True,
        result=lambda:LobbyProgress(
            "waiting_room","已坐下；等待开始游戏",None,"not_visible",seated=True))
    window.start_button=Widget()
    window.root=Root()
    window.status=Variable()
    window._set_operational_status=lambda *a,**k:None
    window.start=lambda: (_ for _ in ()).throw(AssertionError("not in game yet"))
    window._poll_lobby_probe()
    assert window._lobby_active is True
    assert any(ms==1200 for ms,_fn in window.root.scheduled)
    assert "等待开始游戏" in window.status.get()


def test_advance_waiting_room_runs_guarded_start(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("waiting_room",None,"not_visible","waiting",False))
    seen=[]
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        (seen.append(k) or lobby_follow.StartExecution(
            "started","已开始",True,"in_game",image)))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="玉姐",room_cards_snapshot=9,
        sleep_fn=lambda _s:None)
    assert out.state=="in_game" and out.started and out.clicked
    assert seen[0]["expected_account"]=="玉姐"
    assert seen[0]["room_cards"]==9


def test_advance_waiting_room_identity_block_is_terminal(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("waiting_room",None,"not_visible","waiting",False))
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        lobby_follow.StartExecution(
            "blocked","玩家不匹配",False,"waiting_room",image))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="玉姐")
    assert out.terminal is True
    assert out.clicked is False


def test_member_overlay_waiting_room_cannot_be_handed_to_in_game(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("in_game",None,"not_visible","generic green",False))
    monkeypatch.setattr(lobby_follow,"waiting_room_visual_evidence",lambda _img:
        {"matched":True,"member_overlay":True})
    seen=[]
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        (seen.append(k) or lobby_follow.StartExecution(
            "wait","已收起面板，等待对手",True,"waiting_room",image)))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="玉姐")
    assert out.state=="waiting_room"
    assert out.terminal is False
    assert out.started is False
    assert seen


def test_advance_can_seat_even_when_room_card_ocr_is_unknown(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("lobby",None,"digit_templates_incomplete",
                        "房卡未知",False))
    seen=[]
    monkeypatch.setattr(lobby_follow,"execute_auto_seat",lambda *a,**k:
        (seen.append(k) or lobby_follow.SeatExecution(
            "seated","已坐下",True,"waiting_room",image)))
    out=lobby_follow.advance("emulator-5556","adb.exe",tmp_path,sleep_fn=lambda _s:None)
    assert out.seated is True
    assert out.state=="waiting_room"
    assert out.clicked is True
    assert out.room_cards is None
    assert seen and seen[0]["mode"]=="tea_2p"


def test_advance_failed_after_click_is_terminal(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("lobby",8,"ok","房卡8",True))
    monkeypatch.setattr(lobby_follow,"execute_auto_seat",lambda *a,**k:
        lobby_follow.SeatExecution("failed","点后状态未知",True,"unknown",image))
    out=lobby_follow.advance("emulator-5556","adb.exe",tmp_path,sleep_fn=lambda _s:None)
    assert out.terminal is True
    assert out.clicked is True
    assert out.seated is False


def test_advance_no_click_block_can_retry(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("lobby",8,"ok","房卡8",True))
    monkeypatch.setattr(lobby_follow,"execute_auto_seat",lambda *a,**k:
        lobby_follow.SeatExecution("blocked","暂时没有空座",False,"lobby",image))
    out=lobby_follow.advance("emulator-5556","adb.exe",tmp_path,sleep_fn=lambda _s:None)
    assert out.state=="lobby"
    assert out.terminal is False
    assert out.clicked is False


def test_tap_lobby_rechecks_foreground_before_input(monkeypatch):
    calls=[]
    class Done:
        stdout=b"topResumedActivity=com.mengliaoba.wenzhoumj/org.cocos2dx.lua.AppActivity"
    def runner(command,**kwargs):
        calls.append(list(command))
        return Done()
    monkeypatch.setattr(lobby_follow,"read_foreground",
                        lambda adb,serial,runner=None: calls.append(["foreground",serial]))
    lobby_follow._tap_lobby("emulator-5556","adb.exe",115,267,runner=runner)
    assert calls[0]==["foreground","emulator-5556"]
    assert calls[1][-5:]==["shell","input","tap","115","267"]
