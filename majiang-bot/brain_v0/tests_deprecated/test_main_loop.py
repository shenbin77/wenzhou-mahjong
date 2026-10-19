# -*- coding: utf-8 -*-
"""src 实时记牌器修复回归测试：覆盖 review 问题5(手牌合法性)、6(最小化窗口可恢复)、
7(优雅关闭/强制退出日志)、8(共享状态必须经 tracker 带锁方法)。

不依赖真实游戏窗口/模拟器：main.py 的 capture_window / pil_to_cv2 /
TileRecognizer / Advisor 全部 monkeypatch 成桩件。

运行: python -m pytest tests/test_main_loop.py -q
"""
import os
import sys
import types

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import main as main_mod  # noqa: E402
from config import CAPTURE_REGIONS, MAX_HAND_TILES  # noqa: E402
from tracker import TileTracker  # noqa: E402


HAND_13 = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
           "1s", "2s", "3s", "4s"]
HAND_14 = HAND_13 + ["5s"]


class FakeWindow:
    """模拟 pygetwindow 窗口对象（宽高可控）。"""

    def __init__(self, width=1280, height=626):
        self.width = width
        self.height = height
        self.left = 0
        self.top = 0
        self.title = "fake"


def make_app(monkeypatch, *, window=None, capture_result="img",
             capture_raises=None, stop_after_sleeps=1):
    """构造一个脱离真实环境的 MahjongTrackerApp。

    capture_result: "img" 返回假图像；None 表示「窗口最小化，跳过本帧」。
    stop_after_sleeps: 第 N 次 sleep 后自动置 running=False；None 表示 sleep 为空操作
    （由错误计数等其它条件驱动退出）。
    """
    app = main_mod.MahjongTrackerApp()
    app.window = window if window is not None else FakeWindow()

    calls = {"capture": 0, "sleep": 0}

    def fake_capture(window=None):
        calls["capture"] += 1
        if capture_raises is not None:
            raise capture_raises
        if capture_result is None:
            return None
        return object()

    monkeypatch.setattr(main_mod, "capture_window", fake_capture)
    monkeypatch.setattr(
        main_mod, "capture_all_regions",
        lambda img: {name: object() for name in CAPTURE_REGIONS})
    monkeypatch.setattr(main_mod, "pil_to_cv2", lambda img: img)

    rec = types.SimpleNamespace(
        recognize_hand_tiles=lambda cv: list(HAND_14),
        recognize_river=lambda cv: [("1p", 0.9)],
    )
    app.recognizer = rec

    advice_calls = []

    class FakeAdvisor:
        def advise(self, hand, played, turn_no, dealer_seat=None):
            advice_calls.append((len(hand), turn_no, dealer_seat))
            return {"options": [], "shanten": 0}

    app.advisor = FakeAdvisor()

    # init()/persist()/dashboard 不参与本测试
    monkeypatch.setattr(app, "init", lambda: True)
    monkeypatch.setattr(app.tracker, "persist", lambda *a, **k: None)
    app.dashboard_thread = None

    def fake_sleep(sec=0):
        calls["sleep"] += 1
        if stop_after_sleeps is not None and calls["sleep"] > stop_after_sleeps:
            app.running = False

    monkeypatch.setattr(main_mod.time, "sleep", fake_sleep)
    return app, calls, advice_calls


# ---------------------------------------------------------------- 问题5
def test_update_hand_tiles_rejects_illegal_counts():
    """问题5: 非 13/14 张的手牌不得污染 current_hand。"""
    t = TileTracker()
    t.reset()
    assert t.update_hand_tiles(HAND_13[:5]) is False
    assert t.current_hand == []
    assert t.update_hand_tiles(["x"] * (MAX_HAND_TILES + 3)) is False
    assert t.current_hand == []
    # 合法值才落地
    assert t.update_hand_tiles(HAND_13) is True
    assert t.current_hand == HAND_13
    assert t.update_hand_tiles(HAND_14) is True
    assert len(t.current_hand) == MAX_HAND_TILES


def test_main_loop_skips_illegal_hand(monkeypatch):
    """问题5: 主流程收到异常张数时跳过更新并告警，且不触发 advisor。"""
    app, calls, advice_calls = make_app(monkeypatch)
    app.recognizer.recognize_hand_tiles = lambda cv: HAND_13[:6]
    app.run()
    assert app.tracker.current_hand == []
    assert advice_calls == []


# ---------------------------------------------------------------- 问题6
def test_minimized_window_is_recoverable_and_not_counted(monkeypatch):
    """问题6: 窗口最小化(宽或高<=0)/capture 返回 None → 跳过该帧，不计 error_count。"""
    app, calls, _ = make_app(monkeypatch, window=FakeWindow(0, 0))
    app.max_consecutive_errors = 3
    app.run()
    assert app.error_count == 0, "最小化窗口不应计入停机错误"
    assert calls["capture"] == 0, "窗口不可见时应直接跳过截图"
    assert app.running is False


def test_capture_returning_none_not_counted(monkeypatch):
    """问题6: capture_window 返回 None（隐藏窗口）同样可恢复、不计错误。"""
    app, calls, _ = make_app(monkeypatch, capture_result=None)
    app.max_consecutive_errors = 3
    app.run()
    assert app.error_count == 0
    assert calls["capture"] >= 1


def test_unknown_exception_still_counts(monkeypatch):
    """问题6 反向: 真正的未知异常仍累加 error_count 并在达上限时停机。"""
    boom = RuntimeError("识别链路炸了")
    app, calls, _ = make_app(monkeypatch, capture_raises=boom,
                             stop_after_sleeps=None)
    app.max_consecutive_errors = 3
    app.run()
    assert app.error_count == 3, f"未知异常应累计到上限: {app.error_count}"
    assert app.running is False


# ---------------------------------------------------------------- 问题7
def test_shutdown_logs_forced_exit_on_join_timeout(monkeypatch, capsys):
    """问题7: join 超时后必须明确记录「强制退出」，不再静默即弃。"""
    app, _, _ = make_app(monkeypatch)

    class AliveThread:
        def __init__(self):
            self.joined_with = None

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.joined_with = timeout

    thread = AliveThread()
    app.dashboard_thread = thread
    app._shutdown()
    out = capsys.readouterr().out
    assert thread.joined_with == app.dashboard_join_timeout
    assert "强制退出" in out


def test_shutdown_reports_graceful_close_when_thread_exits(monkeypatch, capsys):
    """问题7: 线程在超时内退出时报告优雅关闭，并使用了配置化的 join 超时。"""
    app, _, _ = make_app(monkeypatch)

    class OkThread:
        def __init__(self):
            self.alive = True
            self.joined_with = None

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.joined_with = timeout
            self.alive = False

    thread = OkThread()
    app.dashboard_thread = thread
    app._shutdown()
    out = capsys.readouterr().out
    assert thread.joined_with == app.dashboard_join_timeout
    assert "强制退出" not in out
    assert "优雅关闭" in out


# ---------------------------------------------------------------- 问题8
def test_main_loop_only_uses_locked_tracker_accessors(monkeypatch):
    """问题8: 主循环不得绕过 tracker 直接读写共享 dict/list。"""
    app, calls, advice_calls = make_app(monkeypatch)
    locked = {"played_counts": 0, "history": 0, "advice": 0}
    real_counts = app.tracker.get_played_counts
    real_history = app.tracker.get_play_history
    real_advice = app.tracker.update_advice

    def counts():
        locked["played_counts"] += 1
        return real_counts()

    def history():
        locked["history"] += 1
        return real_history()

    def update_advice(advice):
        locked["advice"] += 1
        return real_advice(advice)

    monkeypatch.setattr(app.tracker, "get_played_counts", counts)
    monkeypatch.setattr(app.tracker, "get_play_history", history)
    monkeypatch.setattr(app.tracker, "update_advice", update_advice)
    # 识别结果现在必须先进入 pending；预置一手已确认牌，专门验证
    # 主循环对已接受状态的带锁读取与 advice 写入。
    real_reset = app.tracker.reset
    def reset_with_confirmed_hand():
        real_reset()
        assert app.tracker.update_hand_tiles(HAND_14)
    monkeypatch.setattr(app.tracker, "reset", reset_with_confirmed_hand)

    app.run()

    assert locked["played_counts"] >= 1, "未走带锁快照读取 played_counts"
    assert locked["history"] >= 1, "未走带锁快照读取 play_history"
    assert locked["advice"] >= 1, "latest_advice 未经带锁 update_advice 写入"
    # 源码层面兜底：禁止裸属性读写
    src = open(os.path.join(SRC, "main.py"), encoding="utf-8").read()
    for banned in ("tracker.played_counts", "tracker.play_history",
                   "tracker.latest_advice =", "tracker.current_hand"):
        assert banned not in src, f"main.py 仍存在绕过 tracker 的共享状态访问: {banned}"
    assert advice_calls and advice_calls[0][0] == MAX_HAND_TILES
