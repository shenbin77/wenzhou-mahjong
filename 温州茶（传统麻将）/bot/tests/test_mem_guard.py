"""mem_guard 的单元测试。

纪律：**全部使用注入的 sampler 与注入的 clock**，绝不依赖真实内存读数
（真实 RSS 会随机器负载漂移，测不出确定结论），也不启动任何真实进程。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import mem_guard  # noqa: E402
from mem_guard import ResourceGuard  # noqa: E402


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------

class ScriptedSampler:
    """按脚本返回读数的采样器。

    Args:
        readings: 依次返回的原始读数（dict / None / 抛异常的哨兵）。
            用尽之后重复最后一项。
    """

    def __init__(self, readings: list) -> None:
        self.readings = readings
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        index = min(self.calls - 1, len(self.readings) - 1)
        item = self.readings[index]
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """每次被调用都自动前进的可控时钟，保证相邻采样的 ts 不同。"""

    def __init__(self, now: float = 1000.0, step: float = 1.0) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def reading(rss: float | None, commit: float | None = None) -> dict:
    return {"rss_mb": rss, "commit_mb": commit, "available": True, "reason": ""}


UNREADABLE = {"rss_mb": None, "commit_mb": None, "available": False, "reason": "unavailable"}

#: 系统级读数：32G 机器、只剩 200MB 可用（"被别人挤爆"的典型形态）。
SYSTEM_TIGHT = {
    "total_mb": 32768.0, "available_mb": 200.0, "used_percent": 99.4,
    "commit_mb": 40000.0, "commit_limit_mb": 41000.0, "ok": True,
}
#: 系统级读数：健康。
SYSTEM_OK = {
    "total_mb": 32768.0, "available_mb": 20000.0, "used_percent": 38.0,
    "commit_mb": 12000.0, "commit_limit_mb": 41000.0, "ok": True,
}
SYSTEM_UNREADABLE = {
    "total_mb": None, "available_mb": None, "used_percent": None,
    "commit_mb": None, "commit_limit_mb": None, "ok": False,
}

PROCESS_KEYS = {"rss_mb", "commit_mb", "over", "reason", "ts", "available"}
SYS_KEYS = {
    "sys_reading_ok", "sys_total_mb", "sys_available_mb",
    "sys_used_percent", "sys_commit_mb", "sys_commit_limit_mb",
}


def make_guard(readings, *, limit_mb: int = 100, interval_s: float = 0.0, consecutive: int = 2,
               system_readings=None):
    """构造一个带注入 sampler/clock 的 guard。interval_s=0 表示每次都真采。"""
    sampler = ScriptedSampler(readings)
    system_sampler = ScriptedSampler(system_readings or [SYSTEM_OK])
    clock = FakeClock()
    guard = ResourceGuard(
        limit_mb=limit_mb,
        interval_s=interval_s,
        consecutive=consecutive,
        sampler=sampler,
        system_sampler=system_sampler,
        clock=clock,
    )
    return guard, sampler, clock


# --------------------------------------------------------------------------
# 正常值 / 超限
# --------------------------------------------------------------------------

def test_normal_value_not_over():
    """正常读数：over=False、available=True、should_abort()=False。"""
    guard, _, _ = make_guard([reading(50.0, 80.0)])
    snap = guard.sample()
    assert snap["available"] is True
    assert snap["rss_mb"] == 50.0
    assert snap["commit_mb"] == 80.0
    assert snap["over"] is False
    assert snap["reason"] == "ok"
    assert guard.should_abort() is False


def test_commit_only_still_judges(limit_mb=100):
    """只有 commit 可用时也能判定（rss 缺失不算不可用）。"""
    guard, _, _ = make_guard([reading(None, 500.0)])
    snap = guard.sample()
    assert snap["available"] is True
    assert snap["rss_mb"] is None
    assert snap["over"] is True


def test_none_limit_mb_means_observe_only():
    """limit_mb=None（未设阈值）不崩、永不超限——未标定阶段的"纯观测"模式。

    这样调用方可以把"环境变量没配"直接透传进来，不必塞一个假的大数字
    （假阈值写进遥测会看起来像已标定）。
    """
    guard, _, _ = make_guard([reading(50000.0, 60000.0)], limit_mb=None)
    snap = guard.sample()
    assert snap["available"] is True
    assert snap["rss_mb"] == 50000.0
    assert snap["over"] is False
    assert "no-limit" in snap["reason"]
    assert guard.should_abort() is False
    assert guard.should_abort() is False
    assert guard.peak_mb == 60000.0          # 峰值照样记，标定数据不丢


def test_boundary_equal_to_limit_not_over():
    """等于限值不算超限（严格大于才算），避免阈值抖动。"""
    guard, _, _ = make_guard([reading(100.0, 100.0)])
    assert guard.sample()["over"] is False


# --------------------------------------------------------------------------
# 迟滞：连续 consecutive 次才 abort
# --------------------------------------------------------------------------

def test_single_over_does_not_abort():
    """只超限一次 → should_abort() 仍为 False（迟滞防抖）。"""
    guard, _, _ = make_guard([reading(500.0, 800.0)])
    assert guard.sample()["over"] is True
    assert guard.should_abort() is False
    assert guard.over_streak == 1


def test_two_consecutive_over_aborts():
    """连续两次超限 → should_abort() 为 True。"""
    guard, _, _ = make_guard([reading(500.0, 800.0)])
    assert guard.should_abort() is False
    assert guard.should_abort() is True
    assert guard.over_streak == 2


def test_consecutive_three_requires_three():
    """consecutive=3 时，前两次都不 abort，第三次才 abort。"""
    guard, _, _ = make_guard([reading(500.0)], consecutive=3)
    assert guard.should_abort() is False
    assert guard.should_abort() is False
    assert guard.should_abort() is True


def test_drop_below_limit_resets_streak():
    """超限一次 → 回落一次 → 再超限一次：仍不 abort（计数已清零）。"""
    guard, _, _ = make_guard([reading(500.0), reading(10.0), reading(500.0)])
    assert guard.should_abort() is False   # 第 1 次超限，streak=1
    assert guard.should_abort() is False   # 回落，streak=0
    assert guard.over_streak == 0
    assert guard.should_abort() is False   # 再次超限，streak=1，仍不到 2


# --------------------------------------------------------------------------
# 读数不可用
# --------------------------------------------------------------------------

def test_unavailable_never_over_and_never_aborts():
    """读不到 → available=False、over=False、should_abort()=False（不许误杀）。"""
    guard, _, _ = make_guard([UNREADABLE])
    snap = guard.sample()
    assert snap["available"] is False
    assert snap["over"] is False
    assert snap["reason"] == "unavailable"
    assert snap["rss_mb"] is None
    assert snap["commit_mb"] is None
    assert guard.should_abort() is False


def test_sampler_raising_is_unavailable():
    """采样器抛异常一律归为不可用，绝不向外抛。"""
    guard, _, _ = make_guard([RuntimeError("boom")])
    snap = guard.sample()
    assert snap["available"] is False
    assert snap["over"] is False
    assert "boom" in guard.last_error
    assert guard.should_abort() is False


def test_sampler_returning_garbage_is_unavailable():
    """采样器返回非 dict / 空读数一律归为不可用。"""
    guard, _, _ = make_guard([None])
    assert guard.sample()["available"] is False
    assert guard.should_abort() is False

    guard2, _, _ = make_guard([{"rss_mb": "abc", "commit_mb": None}])
    assert guard2.sample()["available"] is False


def test_unavailable_clears_an_existing_streak():
    """已经超限一次后读数不可用：计数清零，不 abort。"""
    guard, _, _ = make_guard([reading(500.0), UNREADABLE])
    assert guard.should_abort() is False
    assert guard.over_streak == 1
    assert guard.should_abort() is False
    assert guard.over_streak == 0


# --------------------------------------------------------------------------
# 节流
# --------------------------------------------------------------------------

def test_sample_throttled_within_interval():
    """interval_s 内第二次 sample() 返回同一份结果（ts 也是上次的）。"""
    sampler = ScriptedSampler([reading(10.0, 20.0), reading(999.0, 999.0)])
    clock = FakeClock(now=1000.0, step=1.0)
    guard = ResourceGuard(limit_mb=100, interval_s=10.0, consecutive=2, sampler=sampler, clock=clock)

    first = guard.sample()
    clock.advance(1.0)
    second = guard.sample()

    assert second == first
    assert second["ts"] == first["ts"]
    assert second["rss_mb"] == 10.0      # 没被第二份脚本值污染
    assert sampler.calls == 1            # 只真采了一次

    clock.advance(20.0)
    third = guard.sample()
    assert third["ts"] != first["ts"]
    assert third["rss_mb"] == 999.0
    assert sampler.calls == 2


def test_throttled_snapshot_does_not_double_count_streak():
    """节流期间反复问 should_abort()，不能把同一次采样凑成连续多次。"""
    sampler = ScriptedSampler([reading(500.0)])
    clock = FakeClock(now=1000.0, step=1.0)
    guard = ResourceGuard(limit_mb=100, interval_s=100.0, consecutive=2, sampler=sampler, clock=clock)

    assert guard.should_abort() is False   # streak=1
    assert guard.should_abort() is False   # 节流命中，不重复计数
    assert guard.over_streak == 1


def test_reset_clears_streak_and_cache_but_keeps_peak():
    """reset() 清零计数与缓存，但保留观测峰值（峰值是标定证据，不能中途清掉）。"""
    guard, sampler, _ = make_guard([reading(500.0), reading(900.0)])
    guard.sample()
    guard.sample()
    assert guard.peak_mb == 900.0
    guard.should_abort()
    assert guard.over_streak == 1
    guard.reset()
    assert guard.over_streak == 0
    assert guard.peak_mb == 900.0          # 峰值保留
    before = sampler.calls
    guard.sample()
    assert sampler.calls == before + 1     # 缓存已清，必定真采一次


def test_peak_tracks_max_observed_value():
    """peak_mb 记录会话内最大读数，用于事后标定 limit_mb。"""
    guard, _, _ = make_guard([reading(10.0, 20.0), reading(900.0, 1200.0), reading(30.0, 40.0)])
    assert guard.peak_mb is None
    guard.sample()
    assert guard.peak_mb == 20.0
    guard.sample()
    assert guard.peak_mb == 1200.0
    guard.sample()                          # 回落后峰值不降
    assert guard.peak_mb == 1200.0


# --------------------------------------------------------------------------
# 系统级采样（只观测，绝不参与 should_abort 判定）
# --------------------------------------------------------------------------

def test_sample_carries_system_keys_and_keeps_the_original_six():
    """sample() 新增系统级键，原有 6 个键一个都不许改名/删除。"""
    guard, _, _ = make_guard([reading(50.0, 80.0)], system_readings=[SYSTEM_OK])
    snap = guard.sample()
    assert PROCESS_KEYS <= set(snap)
    assert SYS_KEYS <= set(snap)
    assert snap["sys_available_mb"] == 20000.0
    assert snap["sys_used_percent"] == 38.0
    assert snap["sys_reading_ok"] is True


def test_system_pressure_does_not_trigger_abort():
    """【红线】系统内存见底也不 abort —— 熔断依据只有自身 RSS/commit。"""
    guard, _, _ = make_guard([reading(50.0, 80.0)], system_readings=[SYSTEM_TIGHT])
    snap = guard.sample()
    assert snap["sys_available_mb"] == 200.0        # 系统确实被挤爆
    assert snap["over"] is False                    # 但自身没超限
    assert guard.should_abort() is False
    assert guard.should_abort() is False


def test_process_over_still_aborts_regardless_of_system():
    """反过来：自身连续超限，即使系统很空闲，照样 abort（不被系统读数稀释）。"""
    guard, _, _ = make_guard([reading(5000.0, 6000.0)], system_readings=[SYSTEM_OK])
    assert guard.should_abort() is False
    assert guard.should_abort() is True


def test_system_sampler_failure_is_isolated():
    """系统级采样器抛异常/返回垃圾：只影响 sys_* 键，进程级判定不受牵连。"""
    guard, _, _ = make_guard([reading(50.0)], system_readings=[RuntimeError("boom")])
    snap = guard.sample()
    assert snap["available"] is True                # 进程级照常
    assert snap["over"] is False
    assert snap["sys_reading_ok"] is False
    assert snap["sys_available_mb"] is None
    assert "boom" in guard.last_system_error
    assert guard.should_abort() is False

    guard2, _, _ = make_guard([reading(50.0)], system_readings=[None])
    snap2 = guard2.sample()
    assert snap2["available"] is True
    assert snap2["sys_reading_ok"] is False


def test_unavailable_snapshot_has_same_shape():
    """进程级读不到时，返回的字典形状也必须包含系统级键（不许 KeyError）。"""
    guard, _, _ = make_guard([UNREADABLE], system_readings=[SYSTEM_OK])
    snap = guard.sample()
    assert set(snap) == PROCESS_KEYS | SYS_KEYS
    assert snap["available"] is False
    assert snap["sys_reading_ok"] is True


def test_min_sys_available_tracks_trough():
    """min_sys_available_mb 记录谷值（判断'被别人挤爆'的关键量），reset 不清。"""
    guard, _, _ = make_guard(
        [reading(10.0)], system_readings=[SYSTEM_OK, SYSTEM_TIGHT, SYSTEM_OK]
    )
    guard.sample()
    guard.sample()
    assert guard.min_sys_available_mb == 200.0
    guard.sample()                                  # 回升后谷值不升
    assert guard.min_sys_available_mb == 200.0
    guard.reset()
    assert guard.min_sys_available_mb == 200.0      # 与 peak_mb 一样是标定证据


def test_watch_reports_system_trough():
    """--watch 汇总里带上 min_sys_available_mb。"""
    summary = mem_guard.watch(
        6.0, interval=1.0, sampler=ScriptedSampler([reading(10.0)]),
        system_sampler=ScriptedSampler([SYSTEM_OK, SYSTEM_TIGHT]),
        sleep=lambda _s: None, clock=FakeClock(step=1.0),
    )
    assert summary["samples"] >= 2
    assert summary["min_sys_available_mb"] == 200.0
    assert summary["sys_reading_ok"] is True


def test_cli_probe_prints_system_line(capsys):
    """--probe 打印系统级那一行（可用内存/总量/提交）。"""
    rc = mem_guard.main(
        ["--probe"], sampler=ScriptedSampler([reading(12.0, 30.0)]),
        system_sampler=ScriptedSampler([SYSTEM_TIGHT]), clock=FakeClock(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "sys: available_mb=200.00" in out
    assert "total_mb=32768.00" in out
    assert "used_percent=99.40" in out


@pytest.mark.skipif(os.name != "nt", reason="ctypes 兜底只在 Windows 有意义")
def test_ctypes_system_read_works_without_psutil():
    """psutil 缺失时 ctypes GlobalMemoryStatusEx 兜底是否真的跑通。"""
    out = mem_guard._ctypes_system_read()
    assert out["ok"] is True
    assert out["total_mb"] is not None and out["total_mb"] > 0
    assert out["available_mb"] is not None and out["available_mb"] > 0
    # 真实提交内存：commit <= commit_limit
    assert out["commit_mb"] is not None and out["commit_limit_mb"] is not None
    assert out["commit_mb"] <= out["commit_limit_mb"]


def test_system_commit_comes_from_ctypes_not_psutil(monkeypatch):
    """提交内存只能由 ctypes 提供：psutil 的 swap_memory 是另一个语义（差 165 倍）。

    本机实测：psutil swap_memory().used=172.23MB vs ctypes 真实提交=28390.33MB。
    绝不能让"有没有装 psutil"决定同一把键的含义。
    """
    monkeypatch.setattr(
        mem_guard,
        "_psutil_system_read",
        lambda: {"total_mb": 32768.0, "available_mb": 1000.0, "used_percent": 90.0,
                 "commit_mb": None, "commit_limit_mb": None, "ok": True},
    )
    monkeypatch.setattr(
        mem_guard,
        "_ctypes_system_read",
        lambda: {"total_mb": 32768.0, "available_mb": 1000.0, "used_percent": 90.0,
                 "commit_mb": 28390.33, "commit_limit_mb": 118707.61, "ok": True},
    )
    out = mem_guard.system_reading()
    assert out["commit_mb"] == 28390.33
    assert out["commit_limit_mb"] == 118707.61
    assert out["available_mb"] == 1000.0          # 物理三件套仍取自 psutil


def test_system_commit_none_when_ctypes_unavailable(monkeypatch):
    """ctypes 也拿不到时，commit 是 None，而不是塞一个语义不同的页面文件数。"""
    monkeypatch.setattr(
        mem_guard,
        "_psutil_system_read",
        lambda: {"total_mb": 32768.0, "available_mb": 1000.0, "used_percent": 90.0,
                 "commit_mb": None, "commit_limit_mb": None, "ok": True},
    )
    monkeypatch.setattr(mem_guard, "_ctypes_system_read", lambda: dict(SYSTEM_UNREADABLE))
    out = mem_guard.system_reading()
    assert out["ok"] is True
    assert out["commit_mb"] is None
    assert out["commit_limit_mb"] is None


@pytest.mark.skipif(os.name != "nt", reason="ctypes 兜底只在 Windows 有意义")
def test_system_reading_falls_back_when_psutil_missing(monkeypatch):
    """把 sys.modules['psutil'] 置 None 模拟未安装，system_reading 应走 ctypes。"""
    monkeypatch.setitem(sys.modules, "psutil", None)
    out = mem_guard.system_reading()
    assert out["ok"] is True
    assert out["total_mb"] is not None


# --------------------------------------------------------------------------
# 只读标定 CLI（全部注入 sampler / sleep / clock，不真睡、不依赖真实内存）
# --------------------------------------------------------------------------

def test_cli_probe_prints_values(capsys):
    """--probe 打印一次读数，读到数返回 0。"""
    sampler = ScriptedSampler([reading(123.0, 456.0)])
    rc = mem_guard.main(["--probe"], sampler=sampler, clock=FakeClock())
    out = capsys.readouterr().out
    assert rc == 0
    assert "rss_mb=123.00" in out
    assert "commit_mb=456.00" in out
    assert "available=True" in out
    assert "over=False" in out


def test_cli_probe_unavailable_returns_1(capsys):
    """读不到数：available=False、over=False，退出码 1（且绝不谎报超限）。"""
    rc = mem_guard.main(["--probe"], sampler=ScriptedSampler([UNREADABLE]), clock=FakeClock())
    out = capsys.readouterr().out
    assert rc == 1
    assert "available=False" in out
    assert "over=False" in out
    assert "unavailable" in out


def test_cli_probe_json_is_parseable(capsys):
    """--json 输出可被 json.loads 解析，便于脚本取 peak_rss_mb。"""
    import json

    mem_guard.main(["--probe", "--json"], sampler=ScriptedSampler([reading(12.5, 30.0)]),
                   clock=FakeClock())
    payload = json.loads(capsys.readouterr().out)
    assert payload["rss_mb"] == 12.5
    assert payload["commit_mb"] == 30.0
    assert payload["available"] is True


def test_cli_watch_reports_peak_without_sleeping(capsys):
    """--watch 回报峰值；注入 sleep，测试不真睡（秒级完成）。"""
    sampler = ScriptedSampler([reading(100.0, 200.0), reading(900.0, 1500.0), reading(50.0, 80.0)])
    slept: list[float] = []
    summary = mem_guard.watch(
        5.0, interval=1.0, sampler=sampler, sleep=slept.append, clock=FakeClock(step=2.0)
    )
    out = capsys.readouterr().out
    assert out == ""          # watch() 只返回，不打印；打印在 main 里
    assert summary["peak_rss_mb"] == 900.0
    assert summary["peak_commit_mb"] == 1500.0
    assert summary["samples"] >= 2
    assert slept               # 确实调用了注入的 sleep，而不是 time.sleep


def test_cli_watch_json_and_exit_code(capsys):
    """--watch --json 输出峰值；全程读不到数才返回 1。"""
    import json

    sampler = ScriptedSampler([reading(700.0, 800.0)])
    rc = mem_guard.main(
        ["--watch", "5", "--interval", "1", "--json"],
        sampler=sampler, sleep=lambda _s: None, clock=FakeClock(step=2.0),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["peak_rss_mb"] == 700.0
    assert payload["unavailable_samples"] == 0

    rc2 = mem_guard.main(
        ["--watch", "5", "--interval", "1"],
        sampler=ScriptedSampler([UNREADABLE]), sleep=lambda _s: None, clock=FakeClock(step=2.0),
    )
    capsys.readouterr()
    assert rc2 == 1


def test_cli_watch_counts_over_samples(capsys):
    """--watch 统计超限次数（limit 只用于统计，不触发任何动作）。"""
    sampler = ScriptedSampler([reading(2000.0, 3000.0)])
    summary = mem_guard.watch(
        4.0, interval=1.0, limit_mb=1500, sampler=sampler,
        sleep=lambda _s: None, clock=FakeClock(step=2.0),
    )
    assert summary["over_samples"] == summary["samples"]
    assert summary["limit_mb"] == 1500.0


def test_cli_requires_an_action(capsys):
    """不给 --probe/--watch 时打印帮助并返回 2，不静默什么都不做。"""
    rc = mem_guard.main([], sampler=ScriptedSampler([reading(1.0)]))
    err = capsys.readouterr().err
    assert rc == 2
    assert "--probe" in err


def test_cli_rejects_bad_arguments():
    """--watch/--interval 非正数直接参数错误（SystemExit 2），不进入死循环。"""
    with pytest.raises(SystemExit) as exc:
        mem_guard.main(["--watch", "0"], sampler=ScriptedSampler([reading(1.0)]))
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        mem_guard.main(["--watch", "5", "--interval", "-1"], sampler=ScriptedSampler([reading(1.0)]))
    assert exc.value.code == 2


def test_cli_mutually_exclusive_actions():
    """--probe 与 --watch 互斥。"""
    with pytest.raises(SystemExit) as exc:
        mem_guard.main(["--probe", "--watch", "5"], sampler=ScriptedSampler([reading(1.0)]))
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# --summarize：事后标定入口（读代打写下的 jsonl，给出峰值与建议阈值）
# --------------------------------------------------------------------------

def _jsonl(tmp_path, records, name="resource_guard.jsonl"):
    """把记录写成 jsonl（走 pytest tmp_path，不碰任何生产目录）。"""
    path = tmp_path / name
    payload = []
    for rec in records:
        payload.append(rec if isinstance(rec, str) else json.dumps(rec))
    path.write_text("\n".join(payload) + "\n", encoding="utf-8")
    return str(path)


def test_summarize_records_computes_peaks():
    """汇总出 rss/commit/guard 三个峰值、时长、阈值与熔断开关。"""
    records = [
        {"ts": 100.0, "rss_mb": 300.0, "commit_mb": 500.0, "peak_mb": 500.0,
         "limit_mb": 1500, "abort_enabled": False, "sys_available_mb": 8000.0},
        {"ts": 160.0, "rss_mb": 900.0, "commit_mb": 700.0, "peak_mb": 900.0,
         "limit_mb": 1500, "abort_enabled": False, "sys_available_mb": 200.0},
        {"ts": 220.0, "rss_mb": 400.0, "commit_mb": 600.0, "peak_mb": 900.0,
         "limit_mb": 1500, "abort_enabled": False},
    ]
    out = mem_guard.summarize_records(records)
    assert out["records"] == 3
    assert out["bad_records"] == 0
    assert out["peak_rss_mb"] == 900.0
    assert out["peak_commit_mb"] == 700.0
    assert out["peak_guard_mb"] == 900.0
    assert out["min_sys_available_mb"] == 200.0      # 系统谷值
    assert out["duration_s"] == 120.0
    assert out["limit_mb"] == 1500.0
    assert out["abort_enabled"] is False
    assert out["suggested_limit_mb"] == 1350.0       # 900 * 1.5


def test_summarize_accepts_iso_timestamps():
    """ts 是 ISO 8601 字符串（带 +08:00）时也要算出时长，不能渲染成 "Nones"。"""
    records = [
        {"ts": "2026-10-26T09:40:38.291271+08:00", "rss_mb": 420.0, "peak_mb": 500.0},
        {"ts": "2026-10-26T09:40:43.291271+08:00", "rss_mb": 742.0, "peak_mb": 910.0},
        {"ts": "2026-10-26T09:40:48.291271+08:00", "rss_mb": 610.0, "peak_mb": 910.0},
    ]
    out = mem_guard.summarize_records(records)
    assert out["records"] == 3
    assert out["duration_s"] is not None
    assert abs(out["duration_s"] - 10.0) < 1e-3      # 首尾相差 10 秒
    assert out["peak_rss_mb"] == 742.0


def test_parse_ts_handles_numeric_iso_z_and_garbage():
    """_parse_ts：数值 / ISO(+08:00) / ISO(Z) / 垃圾值 都要有确定行为，不抛异常。"""
    parse = mem_guard._parse_ts
    assert parse(1000.0) == 1000.0
    assert parse("1000.0") == 1000.0
    assert parse("2026-10-26T09:40:38+08:00") is not None
    assert parse("2026-10-26T01:40:38Z") is not None
    assert parse(None) is None
    assert parse("") is None
    assert parse("not a timestamp") is None
    assert parse({"a": 1}) is None
    assert parse(True) is None


def test_cli_summarize_prints_n_a_not_nones(tmp_path, capsys):
    """没有可用时间戳时，时长显示 n/a 而不是 "Nones"。"""
    path = _jsonl(tmp_path, [
        {"rss_mb": 100.0, "peak_mb": 200.0},
        {"rss_mb": 300.0, "peak_mb": 400.0},
    ], name="nots.jsonl")
    rc = mem_guard.main(["--summarize", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nones" not in out
    assert "/ n/a" in out


def test_system_sampler_must_be_injected_separately():
    """【踩过的坑】只注入 sampler 时，系统级读数仍走真实机器——两条通道彼此独立。"""
    fake_process = {"rss_mb": 10.0, "commit_mb": 20.0, "available": True, "reason": "",
                    "sys_available_mb": 9000.0}     # 塞在进程 sampler 里也不生效
    guard_only_process = ResourceGuard(
        sampler=lambda: dict(fake_process), system_sampler=None, clock=FakeClock()
    )
    snap = guard_only_process.sample()
    # 系统级来自真实机器（可能是 None/真值），绝不是注入的 9000
    assert snap["rss_mb"] == 10.0                    # 进程级确实是注入值
    assert snap["sys_available_mb"] != 9000.0

    guard_both = ResourceGuard(
        sampler=lambda: dict(fake_process),
        system_sampler=lambda: {"total_mb": 32768.0, "available_mb": 9000.0,
                                "used_percent": 70.0, "commit_mb": None,
                                "commit_limit_mb": None, "ok": True},
        clock=FakeClock(),
    )
    assert guard_both.sample()["sys_available_mb"] == 9000.0   # 正确姿势：两个都注入


def test_summarize_skips_bad_records_without_crashing():
    """坏行/非 dict/空记录只计数跳过，绝不抛异常。"""
    records = [
        {"ts": 1.0, "rss_mb": 100.0},
        "not a dict",
        None,
        {},                                  # 没有任何可识别字段 → 坏行
        {"ts": 2.0, "rss_mb": 250.0},
        "{ broken json as string",
    ]
    out = mem_guard.summarize_records(records)
    assert out["records"] == 2
    assert out["bad_records"] == 4
    assert out["peak_rss_mb"] == 250.0


def test_summarize_file_and_cli(tmp_path, capsys):
    """CLI：--summarize 读文件打印峰值与建议阈值；--json 可解析。"""
    path = _jsonl(tmp_path, [
        {"ts": 1.0, "rss_mb": 100.0, "commit_mb": 200.0, "peak_mb": 200.0,
         "limit_mb": 1500, "abort_enabled": False},
        {"ts": 3.0, "rss_mb": 800.0, "commit_mb": 900.0, "peak_mb": 900.0,
         "limit_mb": 1500, "abort_enabled": False},
    ])
    rc = mem_guard.main(["--summarize", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "peak_rss_mb=800.00" in out
    assert "peak_guard_mb=900.00" in out
    assert "suggested_limit_mb=1350.00" in out
    assert "不是标定结论" in out            # 明确标注这只是起步建议

    rc2 = mem_guard.main(["--summarize", path, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert payload["peak_rss_mb"] == 800.0
    assert payload["readable"] is True


def test_summarize_exit_codes(tmp_path, capsys):
    """文件不存在 → 2；文件在但无有效记录 → 1。"""
    assert mem_guard.main(["--summarize", str(tmp_path / "nope.jsonl")]) == 2
    capsys.readouterr()

    empty = _jsonl(tmp_path, ["not json", "", "{oops"], name="empty.jsonl")
    assert mem_guard.main(["--summarize", empty]) == 1
    capsys.readouterr()


def test_summarize_is_mutually_exclusive():
    """--summarize 与 --probe/--watch 互斥。"""
    with pytest.raises(SystemExit) as exc:
        mem_guard.main(["--probe", "--summarize", "x.jsonl"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# 默认采样器的兜底路径（唯一一处碰真实读数：只验证"能不能读到"）
# --------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="ctypes 兜底只在 Windows 有意义")
def test_ctypes_fallback_can_read_without_psutil():
    """psutil 缺失时的 ctypes 兜底是否真的跑通（不依赖具体数值）。"""
    out = mem_guard._ctypes_read()
    assert out["available"] is True
    assert out["rss_mb"] is not None and out["rss_mb"] > 0
    assert out["commit_mb"] is not None and out["commit_mb"] > 0


@pytest.mark.skipif(os.name != "nt", reason="ctypes 兜底只在 Windows 有意义")
def test_default_reading_falls_back_when_psutil_missing(monkeypatch):
    """把 sys.modules['psutil'] 置 None 模拟未安装，default_reading 应走 ctypes。"""
    monkeypatch.setitem(sys.modules, "psutil", None)
    out = mem_guard.default_reading()
    assert out["available"] is True
    assert out["rss_mb"] is not None
