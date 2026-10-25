"""ADB connection is verified before persisting a device binding; no taps."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import device_connection
import assist_window
from instance_profile import profile_paths
from assist_window import TeaAssistWindow


class Var:
    def __init__(self, v=""):
        self.v=v
    def get(self): return self.v
    def set(self,v): self.v=v


class Exec:
    def submit(self,fn,*args):
        value=fn(*args)
        return SimpleNamespace(done=lambda:True,result=lambda:value)


class Root:
    def __init__(self): self.calls=[]
    def after(self,ms,fn):
        self.calls.append(fn)
        return "timer"


def fake_subprocess(monkeypatch, *, state="device", image_ok=True, shape=(900,1600)):
    im=np.zeros((shape[0],shape[1],3),dtype=np.uint8)
    png=cv2.imencode(".png", im)[1].tobytes() if image_ok else b"not an image"
    calls=[]
    def run_fixed(cmd,**kw):
        calls.append(cmd)
        if cmd[1:3]==["devices","-l"]:
            return SimpleNamespace(stdout=f"List of devices attached\nemulator-5556 {state} product:test\n")
        if cmd[1:4]==["-s","emulator-5556","exec-out"]:
            return SimpleNamespace(stdout=png)
        pytest.fail("Unexpected ADB command")
    monkeypatch.setattr(device_connection.subprocess,"run",run_fixed)
    return calls


def test_connection_requires_online_and_screenshot(monkeypatch):
    calls=fake_subprocess(monkeypatch)
    result=device_connection.verify_device("emulator-5556","adb.exe")
    assert result.success and result.width==1600 and result.height==900
    assert any("screencap" in c for c in calls)


def test_offline_device_never_screenshots(monkeypatch):
    calls=fake_subprocess(monkeypatch,state="offline")
    assert not device_connection.verify_device("emulator-5556","adb.exe").success
    assert len(calls)==1


def test_invalid_or_wrong_size_screenshot_cannot_bind(monkeypatch):
    fake_subprocess(monkeypatch,image_ok=False)
    assert not device_connection.verify_device("emulator-5556","adb.exe").success
    fake_subprocess(monkeypatch,shape=(720,1280))
    result=device_connection.verify_device("emulator-5556","adb.exe")
    assert not result.success and "1600×900" in result.detail


def setup_window(tmp_path):
    win=TeaAssistWindow.__new__(TeaAssistWindow)
    win._profile_paths=profile_paths(tmp_path,"emulator-5556")
    win.mode=Var("温州茶 · 2人")
    win.account=Var("玉姐")
    win.serial=Var("emulator-5556")
    win.status=Var()
    win._binding=None
    win._binding_future=None
    win._binding_fields_pending=None
    win._check_executor=Exec()
    win.root=Root()
    win._set_operational_status=lambda *args,**kw:None
    win._refresh_binding_views=lambda:None
    win._refresh_bind_controls=lambda:None
    return win


def test_successful_probe_saves_binding(monkeypatch,tmp_path):
    win=setup_window(tmp_path)
    monkeypatch.setattr(assist_window,"verify_device",
        lambda serial,adb:device_connection.DeviceConnection(True,serial,"连接成功",1600,900))
    win._bind()
    assert not win._profile_paths.binding.exists()
    win._poll_binding_probe()
    assert win._is_bound() and win._profile_paths.binding.exists()
    assert win.account.get()=="玉姐"
    assert "连接成功" in win.status.get()


def test_failure_does_not_persist(monkeypatch,tmp_path):
    win=setup_window(tmp_path)
    monkeypatch.setattr(assist_window,"verify_device",
        lambda serial,adb:device_connection.DeviceConnection(False,serial,"ADB离线"))
    win._bind()
    win._poll_binding_probe()
    assert win._binding is None and not win._profile_paths.binding.exists()
    assert "ADB离线" in win.status.get()


def test_cannot_connect_wrong_serial_to_existing_window(monkeypatch,tmp_path):
    win=setup_window(tmp_path)
    win.serial.set("emulator-5558")
    opened=[]
    win._open_other_device=lambda serial:opened.append(serial)
    monkeypatch.setattr(assist_window,"verify_device",lambda *a:pytest.fail("Must not probe"))
    win._bind()
    assert not win._profile_paths.binding.exists()
    assert opened==["emulator-5558"]
    assert win.serial.get()=="emulator-5556"


def test_revocation_during_async_check_cannot_resurrect(monkeypatch,tmp_path):
    win=setup_window(tmp_path)
    monkeypatch.setattr(assist_window,"verify_device",
        lambda serial,adb:device_connection.DeviceConnection(True,serial,"连接成功",1600,900))
    win._bind()
    win._binding_fields_pending=None
    win._poll_binding_probe()
    assert not win._profile_paths.binding.exists()
