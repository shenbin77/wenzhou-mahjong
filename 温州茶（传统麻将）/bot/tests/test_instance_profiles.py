"""Cross-device profile isolation and migration without any game tap."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from instance_profile import (discover_profile_bindings, profile_paths,
                              read_assistant_desired_open,
                              read_binding_for_serial, safe_serial,
                              set_assistant_desired_open)
import assist_window
import instance_profile
import launch_multi
from assist_window import TeaAssistWindow


class Var:
    def __init__(self,value=""): self.value=value
    def get(self): return self.value
    def set(self,x): self.value=x


def test_profiles_are_independent(tmp_path):
    a=profile_paths(tmp_path,"emulator-5556")
    b=profile_paths(tmp_path,"emulator-5558")
    assert a.binding != b.binding and a.geometry != b.geometry and a.account != b.account
    x={"account":"alice","serial":"emulator-5556","mode":"tea_2p"}
    y={"account":"bob","serial":"emulator-5558","mode":"tea_2p"}
    assert assist_window.save_binding(x,a.binding)
    assert assist_window.save_binding(y,b.binding)
    assert assist_window.load_binding(a.binding)==x
    assert assist_window.load_binding(b.binding)==y
    assist_window.clear_binding(a.binding)
    assert assist_window.load_binding(b.binding)==y


@pytest.mark.parametrize("bad",["", "../../evil","emulator 1","X\\evil","?bad"])
def test_path_traversal_and_whitespace_rejected(tmp_path,bad):
    with pytest.raises(ValueError):
        profile_paths(tmp_path,bad)


def test_legacy_binding_cannot_cross_devices(tmp_path):
    f=tmp_path / "legacy.json"
    assist_window.save_binding({"account":"alice","serial":"emulator-5556","mode":"tea_2p"}, f)
    assert read_binding_for_serial(f,"emulator-5558") is None
    assert read_binding_for_serial(f,"emulator-5556")["account"]=="alice"


def test_ui_methods_write_per_serial(tmp_path):
    first=profile_paths(tmp_path,"emulator-5556")
    second=profile_paths(tmp_path,"emulator-5558")
    for profile,account in [(first,"A"),(second,"B")]:
        win=TeaAssistWindow.__new__(TeaAssistWindow)
        win._profile_paths=profile
        win.account=Var(account)
        win.serial=Var(profile.serial)
        win.mode=Var("温州茶 · 2人")
        win.status=Var()
        win.bound_status=Var()
        win.phase_status=Var()
        win.problem_status=Var()
        win._refresh_binding_views=lambda: None
        win._set_operational_status=lambda *a,**k: None
        win._binding=None
        win._save_verified_binding(win._current_binding_fields())
        assert assist_window.load_binding(profile.binding)["account"]==account
        win._unbind()
        assert not profile.binding.exists()
    assert first.binding != second.binding


def test_window_geometry_paths_are_independent(tmp_path):
    first=profile_paths(tmp_path,"emulator-5556")
    second=profile_paths(tmp_path,"emulator-5558")
    assist_window.save_window_state({"x":20},first.geometry)
    assist_window.save_window_state({"x":900},second.geometry)
    assert assist_window.load_window_state(first.geometry)["x"]==20
    assert assist_window.load_window_state(second.geometry)["x"]==900


def test_saved_profile_discovery_rejects_misplaced_cross_serial_binding(tmp_path):
    good=profile_paths(tmp_path,"emulator-5554")
    assist_window.save_binding(
        {"account":"玉姐","serial":"emulator-5554","mode":"tea_2p"},
        good.binding)
    wrong=profile_paths(tmp_path,"emulator-5556")
    assist_window.save_binding(
        {"account":"wrong","serial":"emulator-5558","mode":"tea_2p"},
        wrong.binding)

    found=discover_profile_bindings(tmp_path)

    assert set(found)=={"emulator-5554"}
    assert found["emulator-5554"]["account"]=="玉姐"


def test_assistant_lifecycle_intent_is_per_serial_and_boolean(tmp_path):
    set_assistant_desired_open(tmp_path,"emulator-5554",True)
    set_assistant_desired_open(tmp_path,"emulator-5556",False)

    assert read_assistant_desired_open(tmp_path,"emulator-5554") is True
    assert read_assistant_desired_open(tmp_path,"emulator-5556") is False
    assert read_assistant_desired_open(tmp_path,"emulator-5558",default=True) is True


@pytest.mark.parametrize("payload", [
    {"schema": "assistant_state/v0", "serial": "emulator-5554",
     "desired_open": True},
    {"schema": "assistant_state/v1", "serial": "emulator-5556",
     "desired_open": True},
    {"schema": "assistant_state/v1", "serial": "emulator-5554",
     "desired_open": "yes"},
])
def test_invalid_lifecycle_state_fails_closed_even_with_legacy_default(
        tmp_path, payload):
    path=profile_paths(tmp_path,"emulator-5554").assistant_state
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload),encoding="utf-8")

    assert read_assistant_desired_open(
        tmp_path,"emulator-5554",default=True) is False


def test_corrupt_lifecycle_state_fails_closed(tmp_path):
    path=profile_paths(tmp_path,"emulator-5554").assistant_state
    path.parent.mkdir(parents=True)
    path.write_text("{broken",encoding="utf-8")

    assert read_assistant_desired_open(
        tmp_path,"emulator-5554",default=True) is False


def test_atomic_profile_write_keeps_previous_json_when_replace_fails(
        tmp_path, monkeypatch):
    target=tmp_path/"profile.json"
    target.write_text('{"version":"old"}\n',encoding="utf-8")
    monkeypatch.setattr(
        instance_profile.os,"replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated crash")))

    with pytest.raises(OSError,match="simulated crash"):
        instance_profile.atomic_write_json(target,{"version":"new"})

    assert target.read_text(encoding="utf-8")=='{"version":"old"}\n'
    assert list(tmp_path.glob("*.tmp"))==[]


def test_network_adb_serial_makes_windows_safe_profile(tmp_path):
    p=profile_paths(tmp_path,"127.0.0.1:5555")
    assert ":" not in p.binding.parent.name
    assert p.serial=="127.0.0.1:5555"
    assert p.binding != profile_paths(tmp_path,"127.0.0.1_5555").binding


def test_migrating_legacy_binding_does_not_resurrect_after_unbind(tmp_path,monkeypatch):
    profile=profile_paths(tmp_path,"emulator-5556")
    old=tmp_path/"legacy.json"
    monkeypatch.setattr(assist_window,"BINDING_PATH",old)
    assist_window.save_binding(
        {"account":"alice","serial":"emulator-5556","mode":"tea_2p",
         "mode_label":"温州茶 · 2人"},old)
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._profile_paths=profile
    window.account=Var()
    window.serial=Var("emulator-5556")
    window.mode=Var("温州茶 · 2人")
    window.status=Var()
    window._binding=None
    window._refresh_binding_views=lambda:None
    window._set_operational_status=lambda *a,**k:None
    window._restore_binding()
    assert window._is_bound()
    assert profile.binding.exists() and not old.exists()
    window._unbind()
    assert not profile.binding.exists()
    window._restore_binding()
    assert window._binding is None


def test_misplaced_profile_binding_fails_closed(tmp_path,monkeypatch):
    profile=profile_paths(tmp_path,"emulator-5556")
    monkeypatch.setattr(assist_window,"BINDING_PATH",tmp_path/"no-legacy.json")
    assist_window.save_binding(
        {"account":"wrong-device","serial":"emulator-5558","mode":"tea_2p"},
        profile.binding)
    window=TeaAssistWindow.__new__(TeaAssistWindow)
    window._profile_paths=profile
    window.account=Var()
    window.serial=Var("emulator-5556")
    window.mode=Var("温州茶 · 2人")
    window._binding=None
    window._restore_binding()
    assert window._binding is None
    assert window.serial.get()=="emulator-5556"
    assert not profile.binding.exists()


def test_launcher_fails_closed_if_process_inspection_is_unavailable(monkeypatch):
    import builtins
    real_import=builtins.__import__
    def guarded_import(name,*args,**kwargs):
        if name=="psutil":
            raise ImportError("psutil intentionally unavailable in test")
        return real_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",guarded_import)
    assert launch_multi.existing_ui_processes() is None
