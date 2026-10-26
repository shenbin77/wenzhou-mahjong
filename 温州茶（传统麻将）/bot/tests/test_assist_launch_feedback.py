"""启动结论的送达：控制台可见 vs ``pythonw`` 静默。

桌面快捷方式走 ``pythonw.exe``，其 ``sys.stdout`` 为 ``None``，``print`` 会被
静默丢弃。2026-10-26 用户遇到「辅助器打不开」，实际是启动器因为**没有在线雷电
窗口**而 fail-closed 正常退出——但那句原因一个字也没送到，看起来就像程序没启动。

本文件把这条送达链路钉死：**有控制台就只打印，无控制台就必须弹窗，且反馈本身
绝不允许把启动流程搞炸。**
"""

from __future__ import annotations

import sys
import tkinter
import types

import pytest

import assist_window


class _FakeRoot:
    """``tk.Tk()`` 的最小替身，只记录被调用了什么。"""

    def __init__(self) -> None:
        self.withdrawn = False
        self.destroyed = False

    def withdraw(self) -> None:
        self.withdrawn = True

    def destroy(self) -> None:
        self.destroyed = True


@pytest.fixture
def fake_tk(monkeypatch):
    """把 ``assist_window.tk`` 换成替身，返回被创建出来的 root 列表。"""
    created: list[_FakeRoot] = []

    def _tk_ctor():
        root = _FakeRoot()
        created.append(root)
        return root

    monkeypatch.setattr(assist_window, "tk", types.SimpleNamespace(Tk=_tk_ctor))
    return created


@pytest.fixture
def warning_calls(monkeypatch):
    """拦下真正的 ``messagebox.showwarning``，记录 (标题, 正文)。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tkinter.messagebox, "showwarning",
        lambda title, message: calls.append((title, message)),
    )
    return calls


def test_prints_to_stdout_when_a_console_exists(capsys):
    assist_window.report_launch_result("测试结论")
    assert "测试结论" in capsys.readouterr().out


def test_shows_a_dialog_when_there_is_no_console(fake_tk, warning_calls, monkeypatch):
    """``pythonw`` 下必须弹窗，否则用户永远看不到 fail-closed 的原因。"""
    monkeypatch.setattr(sys, "stdout", None)
    assist_window.report_launch_result("没有空闲在线雷电窗口")

    assert warning_calls == [("温州麻将辅助器", "没有空闲在线雷电窗口")]
    # 弹窗用的临时 root 必须 withdraw 且被销毁，不能留下空窗口。
    assert len(fake_tk) == 1
    assert fake_tk[0].withdrawn
    assert fake_tk[0].destroyed


def test_does_not_show_a_dialog_when_a_console_exists(fake_tk, warning_calls):
    assist_window.report_launch_result("控制台已经看得见")
    assert warning_calls == []
    assert fake_tk == []


def test_never_raises_even_when_the_dialog_cannot_be_shown(monkeypatch):
    """反馈失败绝不能阻止启动/退出流程。"""
    monkeypatch.setattr(sys, "stdout", None)

    def _boom():
        raise RuntimeError("没有显示服务")

    monkeypatch.setattr(assist_window, "tk", types.SimpleNamespace(Tk=_boom))
    assist_window.report_launch_result("弹窗失败也不许炸")


def test_new_process_initialization_revokes_stale_click_authority(monkeypatch):
    calls = []
    import full_auto_control
    monkeypatch.setattr(
        full_auto_control, "revoke_pending_command",
        lambda serial, root=None: calls.append((serial, root)) or True)

    assist_window.revoke_startup_click_authority("emulator-5556")

    assert calls == [("emulator-5556", None)]


def test_new_process_initialization_fails_closed_when_revoke_fails(
        monkeypatch):
    import full_auto_control
    monkeypatch.setattr(
        full_auto_control, "revoke_pending_command",
        lambda _serial, root=None: False)

    with pytest.raises(RuntimeError, match="遗留全自动命令"):
        assist_window.revoke_startup_click_authority("emulator-5556")


def test_duplicate_child_startup_preserves_pending_stop(tmp_path):
    import full_auto_control
    full_auto_control.write_command(
        "emulator-5556", "stop", root=tmp_path)

    assist_window.revoke_startup_click_authority(
        "emulator-5556", root=tmp_path)

    assert full_auto_control.consume_command(
        "emulator-5556", root=tmp_path) == "stop"
