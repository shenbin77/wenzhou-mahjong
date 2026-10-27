"""Lobby-to-table read-only handoff regression: no ADB taps."""
from __future__ import annotations
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import lobby_follow
from lobby_preflight import LobbyAssessment
from lobby_follow import LobbyProgress
from assist_window import TeaAssistWindow
from invite_dialog import InvitationState
from threading import Event


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


def test_lobby_invitation_cancels_before_seating_and_resumes(monkeypatch, tmp_path):
    image = np.zeros((900, 1600, 3), np.uint8)
    monkeypatch.setattr(lobby_follow, "_capture_image", lambda *a, **k: image)
    monkeypatch.setattr(lobby_follow, "read_invitation", lambda _image:
        InvitationState(True, True, cancel_box=(500, 540, 750, 640),
                        cancel_center=(625, 590)))
    monkeypatch.setattr(lobby_follow, "assess_frame", lambda _image:
        (_ for _ in ()).throw(AssertionError("must consume invitation first")))
    calls = []
    class Executor:
        def __init__(self, **kwargs): calls.append(kwargs)
        def execute_invitation_cancel(self, point, **kwargs):
            assert self.authorization_check()
            calls.append((point, kwargs))
            return {"executed": True, "verified": True}
    monkeypatch.setattr(lobby_follow, "TapExecutor", Executor)
    out = lobby_follow.advance("emulator-5556", "adb.exe", tmp_path,
                               invitation_authorized=lambda: True)
    assert out.state == "lobby" and out.clicked and not out.terminal
    assert calls[0]["serial"] == "emulator-5556"
    assert calls[1][0] == (625, 590)


def test_lobby_invitation_incomplete_or_stopped_never_clicks(monkeypatch, tmp_path):
    monkeypatch.setattr(lobby_follow, "_capture_image", lambda *a, **k:
                        np.zeros((900, 1600, 3), np.uint8))
    monkeypatch.setattr(lobby_follow, "TapExecutor", lambda **kwargs:
        (_ for _ in ()).throw(AssertionError("must not click")))
    for state, authorized in [(InvitationState(True, False), lambda: True),
                              (InvitationState(True, True, cancel_center=(625, 590)),
                               lambda: False),
                              (InvitationState(True, True, cancel_center=(625, 590)), None)]:
        monkeypatch.setattr(lobby_follow, "read_invitation", lambda _img: state)
        out = lobby_follow.advance("emulator-5556", "adb.exe", tmp_path,
                                   invitation_authorized=authorized)
        assert out.terminal and not out.clicked


def test_lobby_invitation_unverified_stops_without_seating(monkeypatch, tmp_path):
    monkeypatch.setattr(lobby_follow, "_capture_image", lambda *a, **k:
                        np.zeros((900, 1600, 3), np.uint8))
    monkeypatch.setattr(lobby_follow, "read_invitation", lambda _image:
                        InvitationState(True, True, cancel_center=(625, 590)))
    class Executor:
        def __init__(self, **kwargs): pass
        def execute_invitation_cancel(self, *args, **kwargs):
            raise RuntimeError("post-click unknown")
    monkeypatch.setattr(lobby_follow, "TapExecutor", Executor)
    out = lobby_follow.advance("emulator-5556", "adb.exe", tmp_path,
                               invitation_authorized=lambda: True)
    assert out.terminal and "post-click unknown" in out.message


def test_stop_lobby_revokes_invitation_authority():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window._lobby_invitation_authority = Event()
    window._lobby_invitation_authority.set()
    window._lobby_timer = None
    window._stop_lobby_follow()
    assert not window._lobby_invitation_authority.is_set()


def test_invitation_revoked_after_fresh_frame_sends_no_tap(monkeypatch, tmp_path):
    import tap_executor as executor_module
    state = InvitationState(True, True, cancel_box=(500, 540, 750, 640),
                            cancel_center=(625, 590))
    monkeypatch.setattr(executor_module, "read_invitation", lambda _image: state)
    authority = Event()
    authority.set()
    executor = executor_module.TapExecutor(enabled=True, serial="emulator-5556",
                                           session_dir=tmp_path)
    executor.authorization_check = authority.is_set
    monkeypatch.setattr(executor, "_screenshot", lambda:
                        np.zeros((900, 1600, 3), np.uint8))
    foreground_calls = []
    def foreground():
        foreground_calls.append(True)
        if len(foreground_calls) == 2:
            authority.clear()
    monkeypatch.setattr(executor, "_require_game_foreground", foreground)
    sent = []
    monkeypatch.setattr(executor, "_adb", lambda *a, **k: sent.append(a))
    with pytest.raises(executor_module.TapAborted, match="权限已撤销"):
        executor.execute_invitation_cancel((625, 590))
    assert len(foreground_calls) == 2 and not sent


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


def test_ready_animation_without_dealt_hand_stays_in_waiting_room(monkeypatch,tmp_path):
    """Generic green-table IN_GAME is not enough to hand off to autoplay."""
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("in_game",None,"not_visible","generic green",False))
    monkeypatch.setattr(lobby_follow,"waiting_room_visual_evidence",lambda _img:
        {"matched":False,"member_overlay":False})
    monkeypatch.setattr(lobby_follow,"game_started_evidence",lambda _img:
        (False,{"watch":"in_game","start_button":False,
                "hand_count":0,"reason":"not_dealt"}))
    seen=[]
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        (seen.append(k) or lobby_follow.StartExecution(
            "wait","已准备，等待另一端开始并发牌",False,"waiting_room",image)))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="烘熊瞎")
    assert out.state=="waiting_room"
    assert out.started is False
    assert out.terminal is False
    assert seen


def test_generic_in_game_handoffs_only_with_dealt_hand(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:
        LobbyAssessment("in_game",None,"not_visible","generic green",False))
    monkeypatch.setattr(lobby_follow,"waiting_room_visual_evidence",lambda _img:
        {"matched":False,"member_overlay":False})
    monkeypatch.setattr(lobby_follow,"game_started_evidence",lambda _img:
        (True,{"watch":"in_game","start_button":False,
               "hand_count":17,"reason":"dealt_hand"}))
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        (_ for _ in ()).throw(AssertionError("already dealt")))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="烘熊瞎")
    assert out.state=="in_game"
    assert out.started is True


def test_post_seat_ready_animation_reenters_guarded_start(monkeypatch,tmp_path):
    image=np.zeros((900,1600,3),np.uint8)
    monkeypatch.setattr(lobby_follow,"_capture_image",lambda *a,**k:image)
    assessments=iter([
        LobbyAssessment("lobby",8,"ok","房卡8",True),
        LobbyAssessment("in_game",None,"not_visible","generic green",False),
    ])
    monkeypatch.setattr(lobby_follow,"assess_frame",lambda _img:next(assessments))
    monkeypatch.setattr(lobby_follow,"waiting_room_visual_evidence",lambda _img:
        {"matched":False,"member_overlay":False})
    monkeypatch.setattr(lobby_follow,"game_started_evidence",lambda _img:
        (False,{"watch":"in_game","start_button":False,
                "hand_count":0,"reason":"not_dealt"}))
    monkeypatch.setattr(lobby_follow,"execute_auto_seat",lambda *a,**k:
        lobby_follow.SeatExecution("seated","已坐下",True,"in_game",image))
    starts=[]
    monkeypatch.setattr(lobby_follow,"execute_auto_start",lambda *a,**k:
        (starts.append(k) or lobby_follow.StartExecution(
            "wait","等待本端开始按钮",False,"waiting_room",image)))
    out=lobby_follow.advance(
        "emulator-5556","adb.exe",tmp_path,
        allow_start=True,expected_account="烘熊瞎",sleep_fn=lambda _s:None)
    assert out.state=="waiting_room"
    assert out.started is False
    assert out.terminal is False
    assert len(starts)==1


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
