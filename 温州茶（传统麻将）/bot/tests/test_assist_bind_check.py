# -*- coding: utf-8 -*-
"""Binding persistence, automatic mode check, readiness lamp, auto gate,
menu state rules and copy de-duplication for the main assist window."""
from __future__ import annotations

import sys
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import numpy as np

BOT = Path(__file__).resolve().parent.parent
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import assist_window
from assist_window import TeaAssistWindow


class Var:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FakeWidget:
    def __init__(self):
        self.config = {}
        self.pack_calls = []
        self.forget_calls = 0

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def pack(self, **kwargs):
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.forget_calls += 1


class ImmediateExecutor:
    def submit(self, fn):
        fn()
        return SimpleNamespace(done=lambda: True)


class FakeRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        return "timer"

    def after_cancel(self, _timer):
        pass


def _bare_window():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = Var("温州茶 · 2人")
    window.account = Var("")
    window.serial = Var("emulator-5556")
    window.status = Var("")
    window.bound_status = Var("")
    window.phase_status = Var("")
    window.problem_status = Var("")
    window.phase_label = None
    window.problem_label = None
    window.continuous = False
    window.full_auto_running = False
    window.live_info = Var("")
    window.display_source = None
    window.timer = None
    window.video_future = None
    window._binding = None
    window._check_state = "unchecked"
    window._check_mode_key = None
    window._check_detected = None
    return window


# ---------------------------------------------------------------- binding IO
def test_binding_file_roundtrip(tmp_path):
    target = tmp_path / ".binding.json"
    binding = {"account": "玉姐", "serial": "emulator-5556",
               "mode": "tea_2p", "mode_label": "温州茶 · 2人"}
    assert assist_window.save_binding(binding, target)
    assert assist_window.load_binding(target) == binding
    assist_window.clear_binding(target)
    assert not target.exists()
    assert assist_window.load_binding(target) is None


def test_load_binding_rejects_empty_account(tmp_path):
    target = tmp_path / ".binding.json"
    target.write_text('{"account": "  ", "serial": "x"}', encoding="utf-8")
    assert assist_window.load_binding(target) is None


# ------------------------------------------------------------- table reading
def test_detect_table_mode_uses_existing_recognizers(monkeypatch):
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    monkeypatch.setattr(assist_window, "detect_shu_rule_text", lambda _i: True)
    assert assist_window.detect_table_mode(image)[0] == "shu_4p"
    monkeypatch.setattr(assist_window, "detect_shu_rule_text", lambda _i: False)
    monkeypatch.setattr(assist_window, "score_tea_player_count",
                        lambda _i: (2, 0.99, 0.20))
    assert assist_window.detect_table_mode(image)[0] == "tea_2p"
    monkeypatch.setattr(assist_window, "score_tea_player_count",
                        lambda _i: (4, 0.98, 0.15))
    assert assist_window.detect_table_mode(image)[0] == "tea_4p"
    monkeypatch.setattr(assist_window, "score_tea_player_count",
                        lambda _i: (None, 0.50, 0.01))
    detected, detail = assist_window.detect_table_mode(image)
    assert detected is None and "未确认" in detail
    assert assist_window.detect_table_mode(None)[0] is None
    assert assist_window.detect_table_mode(
        np.zeros((100, 100, 3), dtype=np.uint8))[0] is None


def test_read_table_mode_grab_failure_is_unreadable(monkeypatch):
    import live_capture

    def boom(**_kwargs):
        raise RuntimeError("adb offline")

    monkeypatch.setattr(live_capture, "capture_frame", boom)
    detected, detail = assist_window.read_table_mode("emulator-5556")
    assert detected is None and "抓图失败" in detail


# ------------------------------------------------------------------- binding
def test_bind_empty_account_writes_nothing(tmp_path, monkeypatch):
    target = tmp_path / ".binding.json"
    monkeypatch.setattr(assist_window, "BINDING_PATH", target)
    window = _bare_window()
    window._bind()
    assert not target.exists()
    assert "账号标记为空" in window.status.get()
    assert window._binding is None


def test_bind_persists_and_unbind_clears(tmp_path, monkeypatch):
    target = tmp_path / ".binding.json"
    monkeypatch.setattr(assist_window, "BINDING_PATH", target)
    window = _bare_window()
    window.account.set("玉姐")
    window._bind()
    assert target.exists()
    saved = assist_window.load_binding(target)
    assert saved["account"] == "玉姐"
    assert saved["serial"] == "emulator-5556"
    assert saved["mode"] == "tea_2p"
    assert window._is_bound()
    assert "已绑定（玉姐）" in window.bound_status.get()
    assert "已确认（emulator-5556）" in window.bound_status.get()
    window.mode.set("温州茶 · 4人")
    assert not window._is_bound()
    assert "玩法已变更" in window._binding_mismatch_reason()
    window.mode.set("温州茶 · 2人")
    window._unbind()
    assert not target.exists()
    assert window._binding is None
    assert not window._is_bound()
    assert window.bound_status.get() == "账号：未绑定 · 设备：未确认"


def test_restore_binding_refills_window(tmp_path, monkeypatch):
    target = tmp_path / ".binding.json"
    assist_window.save_binding(
        {"account": "拾则", "serial": "emulator-5558", "mode": "tea_4p",
         "mode_label": "温州茶 · 4人"}, target)
    monkeypatch.setattr(assist_window, "BINDING_PATH", target)
    window = _bare_window()
    window._restore_binding()
    assert window.account.get() == "拾则"
    assert window.serial.get() == "emulator-5558"
    assert window.mode.get() == "温州茶 · 4人"
    assert window._is_bound()


# -------------------------------------------------------------- mode check
def _check_window(result, starts):
    window = _bare_window()
    window.account.set("玉姐")
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._check_state = "unchecked"
    window._check_mode_key = None
    window._check_detail = ""
    window._check_detected = None
    window._check_pending = None
    window._check_events = Queue()
    window._check_poll_timer = None
    window._check_executor = ImmediateExecutor()
    window.root = FakeRoot()
    window._mode_reader = lambda _serial: result
    window._ensure_recorder = lambda: None
    window._set_operational_status = lambda *a, **k: None
    window.start = lambda: starts.append("follow")
    return window


def test_check_match_releases_pending_follow():
    starts = []
    window = _check_window(("tea_2p", "识别到温州茶 · 2人"), starts)
    assert not window._ready(pending="follow")
    assert window._check_state == "checking"
    window._poll_mode_check()
    assert window._check_state == "match"
    assert starts == ["follow"]
    assert "已自动核对" in window.status.get()
    assert window._ready()


def test_check_mismatch_blocks_start():
    starts = []
    window = _check_window(("tea_4p", "识别到温州茶 · 4人"), starts)
    assert not window._ready(pending="follow")
    window._poll_mode_check()
    assert window._check_state == "mismatch"
    assert starts == []
    assert "不一致" in window.status.get()
    assert not window._ready()
    assert starts == []


def test_check_unknown_blocks_and_rechecks_on_retry():
    starts = []
    calls = []

    def reader(_serial):
        calls.append(1)
        return None, "规则文字未确认"

    window = _check_window(None, starts)
    window._mode_reader = reader
    assert not window._ready(pending="follow")
    window._poll_mode_check()
    assert window._check_state == "unknown"
    assert "未识别" in window.status.get()
    assert starts == []
    assert not window._ready(pending="follow")
    assert len(calls) == 2  # retrying start re-reads the table
    assert window._check_state == "checking"


def test_check_result_for_previous_mode_is_discarded():
    starts = []
    window = _check_window(("tea_2p", "识别到温州茶 · 2人"), starts)
    assert not window._ready(pending="follow")
    window.mode.set("温州茶 · 4人")
    window._poll_mode_check()
    assert window._check_state == "unchecked"
    assert starts == []


# ---------------------------------------------------------------------- lamp
def _lamp_window():
    window = _bare_window()
    window.account.set("玉姐")
    window.lamp_status = Var("")
    window.lamp_dot = FakeWidget()
    window.lamp_label = FakeWidget()
    window._check_state = "unchecked"
    window._check_mode_key = None
    window._check_detected = None
    window._binding = None
    return window


def test_lamp_states():
    window = _lamp_window()
    window._channel_ready = lambda: True
    window._refresh_lamp()
    assert window.lamp_status.get().startswith("待处理")
    assert "未绑定" in window.lamp_status.get()
    assert window.lamp_dot.config["fg"] == "#a55700"
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window._refresh_lamp()
    assert window.lamp_status.get() == "就绪"
    assert window.lamp_dot.config["fg"] == "#146c2e"
    window._check_state = "mismatch"
    window._check_detected = "tea_4p"
    window._refresh_lamp()
    assert window.lamp_status.get().startswith("异常")
    assert window.lamp_dot.config["fg"] == "#c62828"
    window._check_state = "match"
    window._check_detected = None
    window._channel_ready = lambda: False
    window._refresh_lamp()
    assert window.lamp_status.get().startswith("异常")
    assert "通道" in window.lamp_status.get()


# ----------------------------------------------------------------- auto gate
def test_auto_gate_state_transitions():
    window = _bare_window()
    window.account.set("玉姐")
    window.auto_button = FakeWidget()
    window.auto_state = Var("")
    window.auto_state_label = FakeWidget()
    window._binding = None
    window._check_state = "unchecked"
    window._check_mode_key = None
    window._refresh_auto_gate()
    assert window.auto_button.config["state"] == "disabled"
    assert window.auto_state.get() == "不可开启：未绑定、玩法未核对、未开始跟随"
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window.continuous = True
    window._refresh_auto_gate()
    assert window.auto_button.config["state"] == "normal"
    assert window.auto_state.get() == "● 可开启"
    window.full_auto_running = True
    window._refresh_auto_gate(force=True)
    assert window.auto_state.get() == "● 代打中"
    window.full_auto_running = False
    window.mode.set("温州茶 · 4人")
    window._refresh_auto_gate()
    assert window.auto_button.config["state"] == "disabled"
    assert "全自动仅开放" in window.auto_state.get()


def test_bound_text_hides_unbound_input_values():
    window = _bare_window()
    window.account.set("玩家1")  # typed / prefilled, but never bound
    assert window._bound_text() == "账号：未绑定 · 设备：未确认"
    window._binding = {"account": "玩家1", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window.account.set("玉姐")
    window._binding["account"] = "玉姐"
    assert window._bound_text() == "账号：已绑定（玉姐） · 设备：已确认（emulator-5556）"


def test_start_bind_flag_gates_follow_binding_state():
    window = _bare_window()
    window.account.set("玉姐")
    window.start_button = FakeWidget()
    window.bind_button = FakeWidget()
    window.unbind_button = FakeWidget()
    window.account_entry = FakeWidget()
    window.flag_button = FakeWidget()
    window.auto_button = FakeWidget()
    window.auto_state = Var("")
    window.auto_state_label = FakeWidget()
    window._refresh_binding_views()
    assert window.start_button.config["state"] == "disabled"
    assert window.unbind_button.config["state"] == "disabled"
    assert window.bind_button.config["text"] == "绑定"
    assert window.account_entry.config["state"] == "normal"
    assert window.flag_button.config["state"] == "disabled"
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._refresh_binding_views()
    assert window.start_button.config["state"] == "normal"
    assert window.unbind_button.config["state"] == "normal"
    assert window.bind_button.config["text"] == "重新绑定"
    assert window.account_entry.config["state"] == "readonly"
    window.continuous = True
    window._refresh_binding_views()
    assert window.flag_button.config["state"] == "normal"
    window.continuous = False
    window._binding = None
    window._refresh_binding_views()
    assert window.start_button.config["state"] == "disabled"
    assert window.flag_button.config["state"] == "disabled"


def test_mode_change_invalidates_binding_and_check():
    window = _bare_window()
    window.account.set("玉姐")
    window.lamp_status = Var("")
    window.lamp_dot = FakeWidget()
    window.lamp_label = FakeWidget()
    window.auto_button = FakeWidget()
    window.auto_state = Var("")
    window.auto_state_label = FakeWidget()
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window._reset_round_display = lambda *_a: None
    window.stop_video_review = lambda: None
    window.mode.set("温州茶 · 4人")
    window._on_mode_change()
    assert window._check_state == "unchecked"
    assert window.lamp_status.get().startswith("待处理")
    assert "玩法已变更" in window.lamp_status.get()
    assert window.bound_status.get() == "账号：未绑定 · 设备：未确认"
    assert "绑定已失效" in window.auto_state.get()


# ------------------------------------------------------------ HUD + menu rules
def test_hud_collapses_until_follow_starts():
    window = _bare_window()
    window.hud_panel = FakeWidget()
    window.hud_idle_label = FakeWidget()
    window.offline_label = None
    window._hud_expanded = False
    window._set_hud_expanded(True)
    assert window.hud_idle_label.forget_calls == 1
    assert window.hud_panel.pack_calls
    assert window._hud_expanded is True
    window._set_hud_expanded(False)
    assert window.hud_panel.forget_calls == 1
    assert window._hud_expanded is False


def test_menu_entry_state_rules():
    window = _bare_window()
    window.video_future = None
    assert window._menu_entry_enabled("停止录像复核") is False
    window.video_future = object()
    assert window._menu_entry_enabled("停止录像复核") is True
    assert window._menu_entry_enabled("下一局") is False
    window.continuous = True
    assert window._menu_entry_enabled("下一局") is True
    window.full_auto_running = True
    assert window._menu_entry_enabled("下一局") is False


# ------------------------------------------------------------- source hygiene
def test_source_has_no_checkbox_or_deleted_menu_residue():
    source = (BOT / "assist_window.py").read_text(encoding="utf-8")
    for residue in ("已核对本局玩法", "confirm_mode", "_on_confirm",
                    "_edit_account", "仅本地记录",
                    'label="账号"', 'label="填入模拟器序列号"',
                    'label="跟随会话日志"'):
        assert residue not in source, residue
    assert 'label="历史会话日志"' in source
    assert 'label="设备序列号…"' in source


def test_legend_survives_in_exactly_one_place():
    window_source = (BOT / "assist_window.py").read_text(encoding="utf-8")
    log_source = (BOT / "follow_session_log.py").read_text(encoding="utf-8")
    assert window_source.count("状态色：") == 0
    assert log_source.count("绿=已验证/正常") == 1


def test_idle_copy_is_not_duplicated():
    source = (BOT / "assist_window.py").read_text(encoding="utf-8")
    assert "实时跟随 · 尚未开始" not in source
    assert "尚未开始。进入真实牌桌" not in source
    assert 'text="进入真实牌桌后点“开始跟随”。"' in source


# ------------------------------------------------------------- real Tk window
def test_real_window_binding_menu_and_idle_chrome(tmp_path, monkeypatch):
    import tkinter as tk

    target = tmp_path / ".binding.json"
    assist_window.save_binding(
        {"account": "玉姐", "serial": "emulator-5558", "mode": "tea_4p",
         "mode_label": "温州茶 · 4人"}, target)
    monkeypatch.setattr(assist_window, "BINDING_PATH", target)
    monkeypatch.setattr(assist_window, "load_window_state", lambda: {})
    monkeypatch.setattr(assist_window, "save_window_state",
                        lambda *_a, **_k: None)
    root = tk.Tk()
    root.withdraw()
    window = TeaAssistWindow(root)
    try:
        assert window.account.get() == "玉姐"
        assert window.serial.get() == "emulator-5558"
        assert window.mode.get() == "温州茶 · 4人"
        assert window._is_bound()
        assert "已绑定（玉姐）" in window.bound_status.get()
        assert window.lamp_status.get().startswith("待处理")
        assert not hasattr(window, "confirm_mode")
        assert "disabled" in window.auto_button.state()
        assert window.problem_status.get() == "状态：—"
        assert window.live_info.get() == "只读画面，不会代点。"
        assert window.live_label.master is window.preview_panel
        assert window.bind_button.cget("text") == "重新绑定"
        assert "disabled" not in window.unbind_button.state()
        assert "readonly" in window.account_entry.state()
        assert window.start_button.cget("state") == "normal"
        assert "disabled" in window.flag_button.state()
        # The compact log no longer consumes half the idle window.
        assert window.follow_log_frame.pack_info()["expand"] == 0
        assert window.picture.pack_info()["expand"] == 1
        assert window.preflight_button.cget("text") == "连接自检"
        assert window._hud_expanded is False
        assert window.hud_panel.winfo_manager() == ""
        assert window.hud_idle_label.winfo_manager() == "pack"
        labels = []
        raw_index = {}
        for index in range(int(window.menu.index("end")) + 1):
            try:
                label = window.menu.entrycget(index, "label")
            except tk.TclError:
                continue
            if label:
                labels.append(label)
                raw_index[label] = index
        assert labels == [
            "设备序列号…", "房间号", "窗口置顶",
            "手动指定财神", "财神牌（茶）",
            "离线预检（旧截图）", "抓图并分析", "导入截图", "修正手牌并重算",
            "历史逐帧复核", "导入录像复核（每秒1帧）", "停止录像复核",
            "历史会话日志", "下一局"]
        window._refresh_menu_state()
        assert window.menu.entrycget(
            raw_index["停止录像复核"], "state") == "disabled"
        assert window.menu.entrycget(
            raw_index["下一局"], "state") == "disabled"
        window._unbind()
        assert not target.exists()
        assert not window._is_bound()
        assert window.bound_status.get() == "账号：未绑定 · 设备：未确认"
        assert window.start_button.cget("state") == "disabled"
        assert "disabled" in window.unbind_button.state()
        window.account.set("")
        window._bind()
        assert not target.exists()
        assert "账号标记为空" in window.status.get()
    finally:
        window.close()
