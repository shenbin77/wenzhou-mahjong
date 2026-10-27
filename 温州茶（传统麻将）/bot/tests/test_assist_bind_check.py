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
from assist_window import TeaAssistWindow, format_match_summary
from player_identity import PlayerIdentity


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
    window._identity_state = "unchecked"
    window._identity_key = None
    window._identity_detail = ""
    window._identity_detected = None
    window._identity_pending = None
    window._identity_events = Queue()
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


def test_match_summary_never_invents_unread_score():
    assert format_match_summary(round_total=16) == (
        "本场进度：—/16 · 累计积分：待结算识别")
    assert format_match_summary(3, 16, -20) == "本场进度：3/16 · 累计积分：-20"


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
    assert window._binding is None
    assert "玩家昵称为空" in window.status.get()


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
    assert "账号：玉姐" in window.bound_status.get()
    assert "emulator-5556" in window.bound_status.get()
    window.mode.set("温州茶 · 4人")
    assert not window._is_bound()
    assert "玩法已变更" in window._binding_mismatch_reason()
    window.mode.set("温州茶 · 2人")
    window._unbind()
    assert not target.exists()
    assert window._binding is None
    assert not window._is_bound()
    assert window.bound_status.get() == "账号：未绑定 · 雷电：尚未连接"


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


def test_restore_rejects_device_name_as_player_identity(tmp_path, monkeypatch):
    from instance_profile import profile_paths
    profile = profile_paths(tmp_path, "emulator-5556")
    assist_window.save_binding(
        {"account": "雷电5556", "serial": "emulator-5556", "mode": "tea_2p"},
        profile.binding)
    window = _bare_window()
    window._profile_paths = profile
    window._restore_binding()
    assert window._binding is None
    assert not profile.binding.exists()


# -------------------------------------------------------------- mode check
def _check_window(result, starts):
    window = _bare_window()
    window.account.set("玉姐")
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._identity_state = "match"
    window._identity_key = ("玉姐", "emulator-5556")
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


# ----------------------------------------------------------- player identity
def _identity_window(result, starts):
    window = _bare_window()
    window.account.set("玉姐")
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._check_executor = ImmediateExecutor()
    window.root = FakeRoot()
    window._identity_reader = lambda _serial, _account: result
    window._set_operational_status = lambda *a, **k: None
    window.start = lambda: starts.append("follow")
    return window


def test_identity_match_releases_pending_follow():
    starts = []
    result = PlayerIdentity("match", "玉姐", "玉姐", 1.0, .45,
                            "画面本人昵称已核验为“玉姐”")
    window = _identity_window(result, starts)
    assert not window._ready(pending="follow")
    assert window._identity_state == "checking"
    window._poll_identity_check()
    assert window._identity_gate_ok()
    assert starts == ["follow"]
    assert "身份已核验" in window.status.get()


def test_identity_mismatch_blocks_follow_and_marks_red():
    starts = []
    result = PlayerIdentity("mismatch", "玉姐", "拾则", .99, .44,
                            "配置玩家是“玉姐”，画面本人是“拾则”")
    window = _identity_window(result, starts)
    window.lamp_status = Var("")
    window.lamp_dot = FakeWidget()
    window.lamp_label = FakeWidget()
    assert not window._ready(pending="follow")
    window._poll_identity_check()
    assert not window._identity_gate_ok() and starts == []
    assert "已阻止跟随和代打" in window.status.get()
    assert "玩家不一致" in window.lamp_status.get()
    assert window.lamp_dot.config["fg"] == "#c62828"


def test_identity_hidden_name_blocks_without_guessing():
    starts = []
    result = PlayerIdentity("unknown", "玉姐", None, 0.0, 0.0,
                            "左下本人昵称未显示或被遮挡")
    window = _identity_window(result, starts)
    assert not window._ready(pending="follow")
    window._poll_identity_check()
    assert starts == [] and window._identity_state == "unknown"
    assert "已阻止推荐和代打" in window.status.get()


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
    window._identity_state = "match"
    window._identity_key = ("玉姐", "emulator-5556")
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
    assert window.auto_state.get() == "不可开启：未绑定"
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._refresh_auto_gate()
    assert window.auto_button.config["state"] == "normal"
    assert window.auto_state.get() == "● 可开启：先自动找位开局"
    window._identity_state = "match"
    window._identity_key = ("玉姐", "emulator-5556")
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
    assert window._bound_text() == "账号：未绑定 · 雷电：尚未连接"
    window._binding = {"account": "玩家1", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window.account.set("玉姐")
    window._binding["account"] = "玉姐"
    assert window._bound_text() == (
        "账号：玉姐（待画面核验） · 雷电：emulator-5556 已连接")
    window._identity_state = "match"
    window._identity_key = ("玉姐", "emulator-5556")
    assert window._bound_text() == (
        "账号：玉姐（画面已核验） · 雷电：emulator-5556 已连接")


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
    assert window.bind_button.config["text"] == "连接并绑定"
    assert window.account_entry.config["state"] == "normal"
    assert window.flag_button.config["state"] == "disabled"
    window._binding = {"account": "玉姐", "serial": "emulator-5556",
                       "mode": "tea_2p"}
    window._refresh_binding_views()
    assert window.start_button.config["state"] == "normal"
    assert window.unbind_button.config["state"] == "normal"
    assert window.bind_button.config["text"] == "已连接/重检"
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
    assert window.bound_status.get() == "账号：未绑定 · 雷电：尚未连接"
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
                    'label="填入模拟器序列号"',
                    'label="跟随会话日志"'):
        assert residue not in source, residue
    assert 'label="历史会话日志"' in source
    assert 'label="打开下一个雷电辅助器"' in source
    assert 'text="刷新雷电"' not in source


def test_legend_survives_in_exactly_one_place():
    window_source = (BOT / "assist_window.py").read_text(encoding="utf-8")
    log_source = (BOT / "follow_session_log.py").read_text(encoding="utf-8")
    assert window_source.count("状态色：") == 0
    assert log_source.count("绿=已验证/正常") == 1


def test_idle_copy_is_not_duplicated():
    source = (BOT / "assist_window.py").read_text(encoding="utf-8")
    assert "实时跟随 · 尚未开始" not in source
    assert "尚未开始。进入真实牌桌" not in source
    assert 'text="绑定雷电设备后点“开始跟随”，可先观察大厅。"' in source


# ------------------------------------------------------------- real Tk window
def test_real_window_binding_menu_and_idle_chrome(tmp_path, monkeypatch):
    import tkinter as tk

    monkeypatch.setattr(assist_window, "TEA_ROOT", tmp_path)
    from instance_profile import profile_paths
    target = profile_paths(tmp_path / "采集" / "assist_sessions",
                           "emulator-5558").binding
    assist_window.save_binding(
        {"account": "玉姐", "serial": "emulator-5558", "mode": "tea_4p",
         "mode_label": "温州茶 · 4人"}, target)
    monkeypatch.setattr(assist_window, "BINDING_PATH", target)
    monkeypatch.setattr(assist_window, "load_window_state", lambda *_a: {})
    monkeypatch.setattr(assist_window, "save_window_state",
                        lambda *_a, **_k: None)
    root = tk.Tk()
    root.withdraw()
    window = TeaAssistWindow(root, serial="emulator-5558")
    try:
        assert window.account.get() == "玉姐"
        assert window.serial.get() == "emulator-5558"
        assert window.mode.get() == "温州茶 · 4人"
        assert window._is_bound()
        assert "账号：玉姐" in window.bound_status.get()
        assert "emulator-5558" in window.bound_status.get()
        assert window.lamp_status.get().startswith("待处理")
        assert not hasattr(window, "confirm_mode")
        assert "disabled" in window.auto_button.state()
        assert window.problem_status.get() == "状态：—"
        # 首屏文案必须如实说明"开始跟随会代点并消耗房卡"，不能再声称只读。
        opening = window.live_info.get()
        assert "代你点击" in opening and "消耗房卡" in opening
        assert "不会代点" not in opening
        assert window.live_label.master is window.preview_panel
        assert window.bind_button.cget("text") == "已连接/重检"
        assert "disabled" not in window.unbind_button.state()
        assert "readonly" in window.account_entry.state()
        assert window.serial_label.cget("text") == "emulator-5558"
        assert window.start_button.cget("state") == "normal"
        assert "disabled" in window.flag_button.state()
        # The compact log no longer consumes half the idle window.
        assert window.follow_log_frame.pack_info()["expand"] == 0
        assert window.picture.pack_info()["expand"] == 1
        assert window.preflight_button.cget("text") == "身份/玩法核验"
        assert window._hud_expanded is False
        assert window.hud_panel.winfo_manager() == ""
        assert window.hud_idle_label.winfo_manager() == "pack"
        # Compact product HUD: room/round and autoplay are always above the
        # hand images; technical counters and raw hand codes remain hidden.
        sidebar = window.caishen_row.master
        ordered = sidebar.pack_slaves()
        assert window.room_identity_label.winfo_manager() == "pack"
        assert window.match_summary_label.winfo_manager() == "pack"
        assert window.match_summary.get() == "本场进度：—/16 · 累计积分：待结算识别"
        assert window.tap_panel.winfo_manager() == "pack"
        assert ordered.index(window.caishen_row) < ordered.index(window.room_identity_label)
        assert ordered.index(window.room_identity_label) < ordered.index(window.tap_panel)
        assert ordered.index(window.tap_panel) < ordered.index(window.hand_panel)
        assert window.auto_button.winfo_manager() == "pack"
        assert window.hand_label.winfo_manager() == ""
        assert not window.hud_panel.winfo_children()
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
            "打开下一个雷电辅助器", "房间号", "窗口置顶",
            "手动指定财神", "财神牌（茶）",
            "离线预检（旧截图）", "抓图并分析", "导入截图", "修正手牌并重算",
            "历史逐帧复核", "导入录像复核（每秒1帧）", "停止录像复核",
            "历史会话日志", "下一局", "退出程序"]
        window._refresh_menu_state()
        assert window.menu.entrycget(
            raw_index["停止录像复核"], "state") == "disabled"
        assert window.menu.entrycget(
            raw_index["下一局"], "state") == "disabled"
        window._unbind()
        assert not target.exists()
        assert not window._is_bound()
        assert window.bound_status.get() == "账号：未绑定 · 雷电：尚未连接"
        assert window.start_button.cget("state") == "disabled"
        assert "disabled" in window.unbind_button.state()
        window.account.set("")
        window._bind()
        assert not target.exists()
        assert "玩家昵称为空" in window.status.get()
    finally:
        window.close()


# ------------------------------------------------- identity bounded retry
import time as _time


def _identity_sequence_window(results, starts):
    """在 _identity_window 上换序列 reader，并模拟全自动意图已置位。"""
    window = _identity_window(results[0], starts)
    remaining = list(results)
    calls = []

    def reader(_serial, _account):
        calls.append(1)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    window._identity_reader = reader
    window._start_full_auto = lambda: starts.append("full_auto")
    window._full_auto_wanted = True
    window._auto_resume_pending = False
    return window, calls


def _pump_retry(window):
    retries = [cb for delay, cb in window.root.after_calls
               if delay == assist_window.IDENTITY_RETRY_INTERVAL_MS]
    assert retries, "应已排定身份重试"
    window.root.after_calls.clear()
    retries[-1]()


_UNKNOWN = PlayerIdentity("unknown", "玉姐", None, 0.0, 0.0,
                          "左下本人昵称未显示或被遮挡")
_MATCH = PlayerIdentity("match", "玉姐", "玉姐", 1.0, .45,
                        "画面本人昵称已核验为“玉姐”")
_MISMATCH = PlayerIdentity("mismatch", "玉姐", "拾则", .99, .44,
                           "配置玩家是“玉姐”，画面本人是“拾则”")


def test_identity_unknown_retries_then_match_starts_full_auto():
    """单帧 unknown 不再是终局：回合内续读，读中即按 pending 接管。"""
    starts = []
    window, calls = _identity_sequence_window(
        [_UNKNOWN, _UNKNOWN, _MATCH], starts)
    window._begin_identity_check(pending="full_auto")
    window._poll_identity_check()
    assert window._identity_state == "checking"
    assert window._full_auto_wanted is True
    assert "自动重试中" in window.status.get()
    _pump_retry(window)
    window._poll_identity_check()
    assert window._identity_state == "checking"
    _pump_retry(window)
    window._poll_identity_check()
    assert window._identity_gate_ok()
    assert starts == ["full_auto"]
    assert len(calls) == 3


def test_identity_mismatch_never_retries():
    """自信认出他人是硬安全失败：零重试、意图立即撤销。"""
    starts = []
    window, calls = _identity_sequence_window([_MISMATCH], starts)
    window._begin_identity_check(pending="full_auto")
    window._poll_identity_check()
    assert window._identity_state == "mismatch"
    assert window._full_auto_wanted is False
    assert not [1 for delay, _cb in window.root.after_calls
                if delay == assist_window.IDENTITY_RETRY_INTERVAL_MS]
    assert len(calls) == 1


def test_identity_retry_timeout_fails_closed():
    """预算耗尽仍未读到：终局 fail-closed + 明确超时文案。"""
    starts = []
    window, calls = _identity_sequence_window([_UNKNOWN], starts)
    window._begin_identity_check(pending="full_auto")
    window._poll_identity_check()
    assert window._identity_state == "checking"
    window._identity_retry_deadline = _time.perf_counter() - 1
    _pump_retry(window)
    assert window._identity_state == "unknown"
    assert window._full_auto_wanted is False
    assert "核验超时" in window.status.get()
    assert len(calls) == 1


def test_identity_pending_read_times_out_and_late_match_cannot_start():
    """异步读取没返回时也须到期停手；旧读取后来成功仍不得启动。"""
    starts = []
    window, _calls = _identity_sequence_window([_MATCH], starts)
    window._begin_identity_check(pending="full_auto")
    late_result = window._identity_events.get_nowait()
    old_generation = window._identity_generation
    window._identity_retry_deadline = _time.perf_counter() - 1

    window._poll_identity_check()

    assert window._identity_state == "unknown"
    assert window._identity_pending is None
    assert window._full_auto_wanted is False
    assert window._identity_generation > old_generation
    assert "核验超时" in window.status.get()
    window._identity_events.put(late_result)
    window._poll_identity_check()
    assert starts == []
    assert not window._identity_gate_ok()


def test_identity_queued_match_after_deadline_is_not_accepted():
    """结果已排队但轮询时超过预算，也不能以过期画面接管。"""
    starts = []
    window, _calls = _identity_sequence_window([_MATCH], starts)
    window._begin_identity_check(pending="full_auto")
    window._identity_retry_deadline = _time.perf_counter() - 1

    window._poll_identity_check()

    assert starts == []
    assert window._identity_state == "unknown"
    assert not window._identity_gate_ok()
    assert window._full_auto_wanted is False
    assert "核验超时" in window.status.get()


def test_identity_rebegin_does_not_extend_expired_inflight_check():
    """正在等待中的核验到期后，重复请求不能偷偷续期。"""
    starts = []
    window, _calls = _identity_sequence_window([_UNKNOWN], starts)
    window._begin_identity_check(pending="full_auto")
    expired = _time.perf_counter() - 1
    window._identity_retry_deadline = expired

    window._begin_identity_check(pending="full_auto")

    assert window._identity_retry_deadline == expired
    window._poll_identity_check()
    assert window._identity_state == "unknown"
    assert starts == []


def test_identity_retry_budget_not_refreshed_by_rebegin():
    """重复 begin（控制轮询重入）不许刷新预算，防无限重试。"""
    starts = []
    window, _calls = _identity_sequence_window([_UNKNOWN], starts)
    window._begin_identity_check(pending="full_auto")
    deadline = window._identity_retry_deadline
    assert deadline is not None
    window._begin_identity_check(pending="full_auto")
    assert window._identity_retry_deadline == deadline


def test_identity_retry_aborts_when_context_changes():
    """账号上下文变了，排定的重试不得复活旧核验。"""
    starts = []
    window, calls = _identity_sequence_window([_UNKNOWN], starts)
    window._begin_identity_check(pending="full_auto")
    window._poll_identity_check()
    window.account.set("烘熊瞎")
    _pump_retry(window)
    assert len(calls) == 1
