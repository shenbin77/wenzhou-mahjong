"""进程内存看门狗（带迟滞，避免单次抖动误杀长任务）。

背景（2026-10-26 外部审查结论）
------------------------------
今晨第二个真实故障：**OpenCV 内存不足** —— 全量测试与代打并发跑时，
图像流水线把 RSS 顶到系统上限后 OOM 崩溃，且崩溃点离真正的泄漏点很远，
事后只能看到"某处 cv2 调用炸了"，定位成本极高。

本模块只做一件事：给长任务一个**廉价的自我体检开关**，让调用方在
每轮循环问一句"我还能继续吗"，从而在 OOM 前有序收敛/报警，而不是
被系统一刀切。

阈值说明（重要，勿误读）
------------------------
``limit_mb=1500`` 是**初值**，不是标定结论。它必须按实测 RSS 重新标定：
先用真实代打 + 真实采集各跑一轮，记录峰值 RSS/commit，再回头改这个数。
**不得因为加了本模块就声称"OOM 已解决"** —— 看门狗只负责在踩线时
给出信号，不回收任何内存，也不替代对真实泄漏点的定位。

设计红线
--------
* 读数不可用时（没装 psutil 且 ctypes 兜底也失败、采样器抛异常），
  一律 ``available=False`` / ``over=False`` / ``reason="unavailable"``。
  **绝不因为读不到就误报超限** —— 误报会直接杀死正常的代打任务，
  比漏报危害更大。
* ``should_abort()`` 要求**连续** ``consecutive`` 次超限才返回 True
  （迟滞）。单次尖峰（一次大图解码、一次 GC 前的瞬时值）不该杀任务。
* 采样按 ``interval_s`` 节流：一次循环里问十次也只真采一次，
  避免看门狗自己成为热点。

标定入口（只读 CLI）
--------------------
``limit_mb=1500`` 是未标定初值。要把它变成有依据的数，下一场真实代打时
在旁边挂一条（**只读**，不改任何东西、不杀任何进程）：

    python mem_guard.py --probe              # 打一次当前 rss/commit
    python mem_guard.py --watch 600          # 采 10 分钟，结束时报峰值
    python mem_guard.py --watch 600 --interval 2 --json   # 机器可读

跑完拿 ``peak_rss_mb`` 去定阈值（建议先取 peak + 50% 余量起步）。
**没有实测峰值之前，不得声称 OOM 已解决。**
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from typing import Callable, Optional

__all__ = [
    "ResourceGuard", "default_reading", "system_reading",
    "summarize_records", "summarize_file", "probe", "watch", "main",
]

_BYTES_PER_MB = 1024.0 * 1024.0

#: 系统级读数拿不到时的空壳（_shape 固定，避免调用方 KeyError）。
_EMPTY_SYSTEM: dict = {
    "total_mb": None,
    "available_mb": None,
    "used_percent": None,
    "commit_mb": None,
    "commit_limit_mb": None,
    "ok": False,
}


# --------------------------------------------------------------------------
# 读数来源
# --------------------------------------------------------------------------

def _to_float_or_none(value: object) -> Optional[float]:
    """把采样值转成 float；非数字/None/负数一律返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number < 0:
        return None
    return number


def _mb(size_bytes: Optional[float]) -> Optional[float]:
    """字节转 MB，保留两位小数；None 原样返回。"""
    if size_bytes is None:
        return None
    return round(float(size_bytes) / _BYTES_PER_MB, 2)


def _psutil_read() -> dict:
    """用 psutil 读当前进程内存。

    Returns:
        dict: ``{"rss_mb": float|None, "commit_mb": float|None,
        "available": bool, "reason": str}``。

    Raises:
        ImportError: psutil 未安装（调用方负责兜底）。
        Exception: psutil 自身的任何异常（调用方负责兜底）。
    """
    import psutil  # noqa: WPS433 —— 故意延迟导入，psutil 是可选依赖

    info = psutil.Process().memory_info()
    rss = _to_float_or_none(getattr(info, "rss", None))
    # commit（私有提交内存）：Windows 上 psutil 给 private/pagefile 两个近似量。
    commit = _to_float_or_none(getattr(info, "private", None))
    if commit is None:
        commit = _to_float_or_none(getattr(info, "pagefile", None))
    return {
        "rss_mb": _mb(rss),
        "commit_mb": _mb(commit),
        "available": True,
        "reason": "",
    }


def _ctypes_read() -> dict:
    """Windows 兜底：用 ctypes 直接调 ``GetProcessMemoryInfo``。

    Returns:
        dict: 与 :func:`_psutil_read` 同构。非 Windows 或调用失败时
        ``available=False``（**不抛异常**）。
    """
    unavailable = {"rss_mb": None, "commit_mb": None, "available": False, "reason": "unavailable"}
    if os.name != "nt":
        return unavailable

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        """``PROCESS_MEMORY_COUNTERS_EX``（psapi.h）。"""

        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        psapi = ctypes.windll.psapi  # type: ignore[attr-defined]

        # 必须显式声明签名。实测（本机 Python 3.11 / Win x64）：
        # * 不声明时 GetCurrentProcess 的默认 restype 是 c_int，返回的伪句柄
        #   0xFFFFFFFFFFFFFFFF 会被截断成 -1 的 c_int，随后
        #   GetProcessMemoryInfo 静默返回 0（失败），读数永远是 unavailable；
        # * 只声明 restype=c_void_p 而 argtypes 仍为默认 c_int，则直接抛
        #   "OverflowError: int too long to convert"。
        # 因此两者都必须显式给定，才能真的读到数。
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetCurrentProcess.argtypes = []
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]

        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.c_ulong(ctypes.sizeof(counters))
        )
        if not ok:
            return unavailable
        rss = _to_float_or_none(counters.WorkingSetSize)
        commit = _to_float_or_none(counters.PrivateUsage)
        return {
            "rss_mb": _mb(rss),
            "commit_mb": _mb(commit),
            "available": True,
            "reason": "",
        }
    except Exception:
        return unavailable


def _ctypes_system_read() -> dict:
    """Windows 兜底：``GlobalMemoryStatusEx`` 读系统级内存。

    能拿到 psutil 拿不到的**真实提交内存**（commit charge）：
    ``ullTotalPageFile - ullAvailPageFile``，以及提交上限 ``ullTotalPageFile``。

    Returns:
        dict: 与 :func:`_psutil_system_read` 同构。非 Windows 或失败时
        ``ok=False``（**不抛异常**）。
    """
    broken = {
        "total_mb": None, "available_mb": None, "used_percent": None,
        "commit_mb": None, "commit_limit_mb": None, "ok": False,
    }
    if os.name != "nt":
        return broken

    class MEMORYSTATUSEX(ctypes.Structure):
        """``MEMORYSTATUSEX``（winbase.h）。"""

        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # 同 _ctypes_read() 的教训：restype/argtypes 必须显式声明，
        # 否则返回值会被按默认 c_int 截断，调用静默失败。
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        ok = kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        if not ok:
            return broken

        total = _to_float_or_none(stat.ullTotalPhys)
        avail = _to_float_or_none(stat.ullAvailPhys)
        commit_limit = _to_float_or_none(stat.ullTotalPageFile)
        avail_page = _to_float_or_none(stat.ullAvailPageFile)
        commit = None
        if commit_limit is not None and avail_page is not None:
            commit = commit_limit - avail_page
        used_percent = _to_float_or_none(stat.dwMemoryLoad)
        if used_percent is not None and total:
            # dwMemoryLoad 是 0~100 的整数百分比，这里换算成与 psutil 同口径
            used_percent = round(used_percent, 2)
        return {
            "total_mb": _mb(total),
            "available_mb": _mb(avail),
            "used_percent": used_percent,
            "commit_mb": _mb(commit),
            "commit_limit_mb": _mb(commit_limit),
            "ok": True,
        }
    except Exception:
        return broken


def _psutil_system_read() -> dict:
    """用 psutil 读系统级内存。

    Returns:
        dict: ``{"total_mb", "available_mb", "used_percent",
        "commit_mb", "commit_limit_mb", "ok"}``。

    Raises:
        ImportError: psutil 未安装。
        Exception: psutil 自身异常。

    commit_mb / commit_limit_mb 一律置 None，由 :func:`system_reading` 用
    ctypes 补权威值。原因见下（本机实测，勿删）：
    psutil 的 ``swap_memory().used`` 在 Windows 上是**页面文件已用掉的量**
    （实测 172.23MB），而 ctypes ``GlobalMemoryStatusEx`` 给的是**真实提交
    内存**（实测 28390.33MB）。两者差 165 倍，语义完全不同，绝不能放在
    同一个键下让"有没有装 psutil"来决定读到的含义。
    """
    import psutil  # noqa: WPS433 —— 故意延迟导入，psutil 是可选依赖

    vm = psutil.virtual_memory()
    return {
        "total_mb": _mb(_to_float_or_none(getattr(vm, "total", None))),
        "available_mb": _mb(_to_float_or_none(getattr(vm, "available", None))),
        "used_percent": _to_float_or_none(getattr(vm, "percent", None)),
        "commit_mb": None,          # 见 docstring：psutil 给不了真实提交内存
        "commit_limit_mb": None,
        "ok": True,
    }


def system_reading() -> dict:
    """系统级内存采样。

    策略：``total_mb`` / ``available_mb`` / ``used_percent`` 用 psutil（有就用，
    跨平台）；``commit_mb`` / ``commit_limit_mb`` **只能用 ctypes**
    ``GlobalMemoryStatusEx`` 拿真实提交内存——psutil 给不了这个量，而它的
    ``swap_memory().used`` 是另一个语义（页面文件已用量），实测差 165 倍，
    不能混用。都拿不到时 ``ok=False``，其余字段为 None，**不抛异常**。

    Returns:
        dict: ``{"total_mb", "available_mb", "used_percent",
        "commit_mb", "commit_limit_mb", "ok"}``。
    """
    base: Optional[dict] = None
    try:
        base = _psutil_system_read()
    except Exception:
        base = None
    if base is None:
        return _ctypes_system_read()

    # psutil 拿到了物理内存三件套；提交内存只能问 ctypes。
    precise = _ctypes_system_read()
    if precise["ok"] and precise["commit_mb"] is not None:
        base["commit_mb"] = precise["commit_mb"]
        base["commit_limit_mb"] = precise["commit_limit_mb"]
    return base


def default_reading() -> dict:
    """默认采样器：psutil 优先，ctypes 兜底。

    Returns:
        dict: ``{"rss_mb", "commit_mb", "available", "reason"}``。
        两条路都失败时 ``available=False``、``reason="unavailable"``。
    """
    try:
        return _psutil_read()
    except Exception:
        pass
    return _ctypes_read()


# --------------------------------------------------------------------------
# 看门狗
# --------------------------------------------------------------------------

class ResourceGuard:
    """带迟滞的进程内存看门狗。

    Args:
        limit_mb: 超限阈值（MB）。**初值 1500，需按实测 RSS 标定**，
            不得据此声称已解决 OOM。
        interval_s: 采样节流间隔（秒）。距上次真实采样不足该值时，
            :meth:`sample` 直接返回上一次结果。
        consecutive: 连续超限多少次才让 :meth:`should_abort` 返回 True。
            任一次回落到限值以下即清零计数。
        sampler: 注入式采样器（**进程级**），返回 ``{"rss_mb":..., "commit_mb":...,
            "available":..., "reason":...}``。缺省用 :func:`default_reading`。
            注入的 sampler 抛异常或返回非 dict，一律按"读数不可用"处理。
        system_sampler: 注入式采样器（**系统级**），返回
            ``{"total_mb":..., "available_mb":..., "used_percent":...,
            "commit_mb":..., "commit_limit_mb":..., "ok":...}``。
            缺省用 :func:`system_reading`。系统级读数**只观测、不参与
            :meth:`should_abort` 判定**——熔断依据只有自身 RSS/commit。

            ⚠ **注入测试必须同时给 `sampler` 和 `system_sampler` 两个**。
            只给 ``sampler`` 的话，进程级读数是你注入的、**系统级读数仍会走
            真实机器**（这是实测踩过的坑：有人往 `sampler` 里塞了
            `sys_available_mb`，结果读到的是真实值 15758 而不是注入的 9000）。
            两条通道彼此独立，不会互相覆盖。
        clock: 注入式单调时钟（便于测试），缺省 ``time.monotonic``。

    Example:
        >>> guard = ResourceGuard(limit_mb=1500, interval_s=5.0, consecutive=2)
        >>> if guard.should_abort():
        ...     ...  # 收敛/报警，而不是硬崩
    """

    def __init__(
        self,
        limit_mb: int = 1500,
        interval_s: float = 5.0,
        consecutive: int = 2,
        sampler: Optional[Callable[[], dict]] = None,
        system_sampler: Optional[Callable[[], dict]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.limit_mb: Optional[float] = (
            float(limit_mb) if limit_mb is not None else None
        )
        self.interval_s: float = float(interval_s)
        self.consecutive: int = max(1, int(consecutive))
        self._sampler: Callable[[], dict] = sampler if sampler is not None else default_reading
        self._system_sampler: Callable[[], dict] = (
            system_sampler if system_sampler is not None else system_reading
        )
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

        self._last: Optional[dict] = None
        self._last_ts: Optional[float] = None
        self._over_streak: int = 0
        self._streak_ts: Optional[float] = None
        #: 会话内观测到的峰值（MB），用于**标定** limit_mb。reset() 不会清除它。
        self._peak_mb: Optional[float] = None
        #: 采样器最近一次失败的原因（仅用于排查，不参与判定）。
        self.last_error: str = ""
        #: 系统级采样器最近一次失败的原因（仅用于排查，不参与判定）。
        self.last_system_error: str = ""
        #: 会话内观测到的**系统可用内存谷值**（MB），用于判断"被别人挤爆"。
        self._min_sys_available_mb: Optional[float] = None

    # ---------------------------------------------------------------- 采样

    def _read(self) -> dict:
        """调一次采样器并归一化结果，任何失败都归为"读数不可用"。"""
        self.last_error = ""
        raw: object
        try:
            raw = self._sampler()
        except Exception as exc:  # noqa: BLE001 —— 采样器不可信，一律吞掉
            self.last_error = f"sampler raised {type(exc).__name__}: {exc}"
            raw = None

        if not isinstance(raw, dict):
            if not self.last_error:
                self.last_error = f"sampler returned {type(raw).__name__}, expected dict"
            return {"rss_mb": None, "commit_mb": None, "available": False, "reason": "unavailable"}

        rss = _to_float_or_none(raw.get("rss_mb"))
        commit = _to_float_or_none(raw.get("commit_mb"))
        if rss is None and commit is None:
            if not self.last_error:
                self.last_error = "sampler returned no usable rss_mb/commit_mb"
            return {"rss_mb": None, "commit_mb": None, "available": False, "reason": "unavailable"}
        return {"rss_mb": rss, "commit_mb": commit, "available": True, "reason": ""}

    def _read_system(self) -> dict:
        """采一次系统级内存并归一化；任何失败都归为"读不到"，**不抛异常**。

        系统级读数只用于观测（判断"是不是被别人的进程挤爆了"），
        **绝不参与** :meth:`should_abort` 的判定。
        """
        self.last_system_error = ""
        raw: object
        try:
            raw = self._system_sampler()
        except Exception as exc:  # noqa: BLE001 —— 采样器不可信，一律吞掉
            self.last_system_error = f"system sampler raised {type(exc).__name__}: {exc}"
            raw = None

        if not isinstance(raw, dict):
            if not self.last_system_error:
                self.last_system_error = f"system sampler returned {type(raw).__name__}, expected dict"
            return _EMPTY_SYSTEM

        total = _to_float_or_none(raw.get("total_mb"))
        avail = _to_float_or_none(raw.get("available_mb"))
        percent = _to_float_or_none(raw.get("used_percent"))
        commit = _to_float_or_none(raw.get("commit_mb"))
        commit_limit = _to_float_or_none(raw.get("commit_limit_mb"))
        # ok 由"有没有真的读到数"决定，不信采样器自报的 ok 标志。
        ok = total is not None or avail is not None
        if not ok:
            if not self.last_system_error:
                self.last_system_error = "system sampler returned no usable total_mb/available_mb"
            return _EMPTY_SYSTEM
        return {
            "total_mb": total,
            "available_mb": avail,
            "used_percent": percent,
            "commit_mb": commit,
            "commit_limit_mb": commit_limit,
            "ok": True,
        }

    def sample(self) -> dict:
        """取一次内存快照（按 ``interval_s`` 节流）。

        Returns:
            dict::

                {
                  "rss_mb": float|None,   # 工作集；读不到为 None
                  "commit_mb": float|None,# 私有提交内存；读不到为 None
                  "over": bool,           # 是否超限（读不到时恒为 False）
                  "reason": str,          # "ok" / "over-limit..." / "unavailable"
                  "ts": float,            # 本次（或节流命中的上次）采样时刻
                  "available": bool,      # 进程级读数是否可用
                  # ↓ 以下为系统级观测键，只观测，不参与 should_abort() 判定
                  "sys_reading_ok": bool,
                  "sys_total_mb": float|None,
                  "sys_available_mb": float|None,
                  "sys_used_percent": float|None,
                  "sys_commit_mb": float|None,
                  "sys_commit_limit_mb": float|None,
                }
        """
        now = float(self._clock())
        if (
            self._last is not None
            and self._last_ts is not None
            and (now - self._last_ts) < self.interval_s
        ):
            # 节流命中：返回上一次结果的副本（ts 保持为上次真实采样时刻）。
            return dict(self._last)

        reading = self._read()
        system = self._read_system()
        sys_keys = {
            "sys_reading_ok": system["ok"],
            "sys_total_mb": system["total_mb"],
            "sys_available_mb": system["available_mb"],
            "sys_used_percent": system["used_percent"],
            "sys_commit_mb": system["commit_mb"],
            "sys_commit_limit_mb": system["commit_limit_mb"],
        }
        if system["available_mb"] is not None:
            if (
                self._min_sys_available_mb is None
                or system["available_mb"] < self._min_sys_available_mb
            ):
                self._min_sys_available_mb = system["available_mb"]

        if not reading["available"]:
            snapshot = {
                "rss_mb": None,
                "commit_mb": None,
                "over": False,
                "reason": "unavailable",
                "ts": now,
                "available": False,
            }
        else:
            values = [
                value
                for value in (reading["rss_mb"], reading["commit_mb"])
                if value is not None
            ]
            peak = max(values)
            if self._peak_mb is None or peak > self._peak_mb:
                self._peak_mb = peak
            # limit_mb 为 None = 未设阈值（未标定阶段的"纯观测"模式）：
            # 永不判超限，只记录。这样调用方可以直接把"未配置"透传进来，
            # 不必塞一个假的大数字（假的阈值写进遥测会看起来像已标定）。
            if self.limit_mb is None:
                over = False
                reason = "no-limit（未设阈值，纯观测）"
            else:
                over = peak > self.limit_mb
                reason = (
                    "ok" if not over
                    else f"over-limit: peak {peak:.1f}MB > limit {self.limit_mb:.1f}MB"
                )
            snapshot = {
                "rss_mb": reading["rss_mb"],
                "commit_mb": reading["commit_mb"],
                "over": over,
                "reason": reason,
                "ts": now,
                "available": True,
            }

        snapshot.update(sys_keys)
        self._last = dict(snapshot)
        self._last_ts = now
        return dict(snapshot)

    # ---------------------------------------------------------------- 判定

    def should_abort(self) -> bool:
        """是否应当收敛/停手：连续 ``consecutive`` 次超限才为 True。

        任一次采样未超限（含读数不可用）即清零连续计数，防止误杀。

        Returns:
            bool: True 表示已连续超限达到阈值。
        """
        snapshot = self.sample()
        if not snapshot["available"] or not snapshot["over"]:
            self._over_streak = 0
            self._streak_ts = None
            return False

        # 节流命中的快照（ts 与上次计数时刻相同）不重复计数，
        # 否则一次超限 + 高频轮询会立刻凑满 consecutive。
        if self._streak_ts is not None and self._streak_ts == snapshot["ts"]:
            return self._over_streak >= self.consecutive

        self._over_streak += 1
        self._streak_ts = snapshot["ts"]
        return self._over_streak >= self.consecutive

    # ---------------------------------------------------------------- 辅助

    @property
    def over_streak(self) -> int:
        """当前连续超限计数（仅供观测/日志）。"""
        return self._over_streak

    @property
    def peak_mb(self) -> Optional[float]:
        """会话内观测到的内存峰值（MB），用于标定 ``limit_mb``。

        ``limit_mb=1500`` 是未经标定的初值。正确用法是先开着本看门狗
        跑一轮真实代打/采集（先只记录、不 abort），读这里的峰值，
        再据此把 ``limit_mb`` 改成有依据的数。``reset()`` **不会**清除
        峰值——它是标定证据，不该被中途清零。
        """
        return self._peak_mb

    @property
    def min_sys_available_mb(self) -> Optional[float]:
        """会话内**系统可用内存的谷值**（MB）。

        这是判断"是不是被别的进程挤爆了"的关键量：自身 RSS 可能全程健康，
        但系统可用内存被并发的全量测试吃到见底。与 :attr:`peak_mb` 一样，
        ``reset()`` 不清除它。
        """
        return self._min_sys_available_mb

    def reset(self) -> None:
        """清零连续计数与缓存快照（例如任务切换、手动 GC 之后）。

        注意：观测峰值 :attr:`peak_mb` 与系统谷值 :attr:`min_sys_available_mb`
        保留不清——它们是标定证据，不该被中途清零。
        """
        self._over_streak = 0
        self._streak_ts = None
        self._last = None
        self._last_ts = None


# --------------------------------------------------------------------------
# 只读标定 CLI
# --------------------------------------------------------------------------

def _fmt(value: Optional[float]) -> str:
    """把可能为 None 的读数格式化成人类可读串（None → "n/a"，绝不撒谎成 0）。"""
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def probe(
    limit_mb: float = 1500,
    sampler: Optional[Callable[[], dict]] = None,
    system_sampler: Optional[Callable[[], dict]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict:
    """采一次内存快照（只读，不做任何动作）。

    Args:
        limit_mb: 仅用于计算 ``over`` 标志，**不触发任何动作**。
        sampler: 注入式进程级采样器（测试用），缺省 :func:`default_reading`。
        system_sampler: 注入式系统级采样器（测试用），缺省 :func:`system_reading`。
        clock: 注入式时钟，缺省 ``time.monotonic``。

    Returns:
        dict: :meth:`ResourceGuard.sample` 的同构结果（含系统级观测键）。
    """
    guard = ResourceGuard(
        limit_mb=limit_mb, interval_s=0.0, consecutive=1,
        sampler=sampler, system_sampler=system_sampler, clock=clock,
    )
    return guard.sample()


def watch(
    seconds: float,
    interval: float = 5.0,
    limit_mb: float = 1500,
    sampler: Optional[Callable[[], dict]] = None,
    system_sampler: Optional[Callable[[], dict]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict:
    """按 ``interval`` 连续采样若干秒，回报峰值（只读，不做任何动作）。

    这是把 ``limit_mb`` 从"拍脑袋"变成"有依据"的取证工具：真实代打时
    在旁边挂一条，跑完读 ``peak_rss_mb``。

    Args:
        seconds: 观测总时长（秒），必须 > 0。
        interval: 采样间隔（秒），必须 > 0。
        limit_mb: 仅用于统计 ``over_samples``，**不触发任何动作**。
        sampler: 注入式进程级采样器（测试用）。
        system_sampler: 注入式系统级采样器（测试用）。
        sleep: 注入式 sleep（测试用，避免真睡），缺省 ``time.sleep``。
        clock: 注入式时钟，缺省 ``time.monotonic``。

    Returns:
        dict::

            {"samples": int, "peak_rss_mb": float|None,
             "peak_commit_mb": float|None, "over_samples": int,
             "unavailable_samples": int, "elapsed_s": float,
             "min_sys_available_mb": float|None,   # 系统可用内存谷值
             "sys_reading_ok": bool, "limit_mb": float}
    """
    do_sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
    tick: Callable[[], float] = clock if clock is not None else time.monotonic
    guard = ResourceGuard(
        limit_mb=limit_mb, interval_s=interval, consecutive=1,
        sampler=sampler, system_sampler=system_sampler, clock=tick,
    )

    start = tick()
    summary: dict = {
        "samples": 0,
        "peak_rss_mb": None,
        "peak_commit_mb": None,
        "over_samples": 0,
        "unavailable_samples": 0,
        "elapsed_s": 0.0,
        "min_sys_available_mb": None,
        "sys_reading_ok": False,
        "limit_mb": float(limit_mb),
    }

    while True:
        snap = guard.sample()
        summary["samples"] += 1
        if not snap["available"]:
            summary["unavailable_samples"] += 1
        else:
            if snap["over"]:
                summary["over_samples"] += 1
            for key, peak_key in (("rss_mb", "peak_rss_mb"), ("commit_mb", "peak_commit_mb")):
                value = snap[key]
                if value is None:
                    continue
                if summary[peak_key] is None or value > summary[peak_key]:
                    summary[peak_key] = value
        if snap.get("sys_reading_ok"):
            summary["sys_reading_ok"] = True
        # guard 自己维护系统可用内存谷值，每轮同步即可
        summary["min_sys_available_mb"] = guard.min_sys_available_mb
        elapsed = tick() - start
        if elapsed >= seconds:
            break
        do_sleep(interval)

    summary["elapsed_s"] = round(elapsed, 3)
    return summary


def _parse_ts(value: object) -> Optional[float]:
    """把时间戳解析成 float 秒。

    支持两种形态（代打遥测里都出现过）：
    * 数值 epoch 秒（``1000.0``、``"1000.0"``）；
    * ISO 8601 字符串（``"2026-10-26T09:40:38.291271+08:00"``、``...Z``）。

    Args:
        value: 任意对象，类型不对返回 None，**不抛异常**。

    Returns:
        float | None: epoch 秒；解析不了返回 None。
    """
    number = _to_float_or_none(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        from datetime import datetime

        # Python 3.11 的 fromisoformat 已能直接吃 "+08:00"；"Z" 仍需手动换。
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return float(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def summarize_records(records) -> dict:
    """把一批采样记录（每行一个 dict）汇总成标定结论。

    这是**事后**标定入口：代打进程把每次采样的 ``rss_mb`` / ``peak_mb``
    写进 jsonl，跑完用 ``python mem_guard.py --summarize <file>`` 读峰值，
    省得人去翻文件。纯函数，便于测试。

    Args:
        records: 可迭代的 dict。缺字段/类型不对的记录会被跳过并计入
            ``bad_records``，**不抛异常**。

    Returns:
        dict::

            {"records": int, "bad_records": int,
             "peak_rss_mb": float|None,      # max(rss_mb)
             "peak_commit_mb": float|None,   # max(commit_mb)
             "peak_guard_mb": float|None,    # max(peak_mb)，即 guard 自报峰值
             "min_sys_available_mb": float|None,
             "limit_mb": float|None,         # 记录里最后见到的阈值
             "abort_enabled": bool|None,     # 最后见到的熔断开关
             "first_ts": float|None, "last_ts": float|None,
             "duration_s": float|None,
             "suggested_limit_mb": float|None}
    """
    out: dict = {
        "records": 0, "bad_records": 0,
        "peak_rss_mb": None, "peak_commit_mb": None, "peak_guard_mb": None,
        "min_sys_available_mb": None, "limit_mb": None, "abort_enabled": None,
        "first_ts": None, "last_ts": None, "duration_s": None, "suggested_limit_mb": None,
    }

    for record in records:
        if not isinstance(record, dict):
            out["bad_records"] += 1
            continue
        try:
            rss = _to_float_or_none(record.get("rss_mb"))
            commit = _to_float_or_none(record.get("commit_mb"))
            guard_peak = _to_float_or_none(record.get("peak_mb"))
            sys_avail = _to_float_or_none(record.get("sys_available_mb"))
            ts = _parse_ts(record.get("ts"))
            if ts is None:
                ts = _parse_ts(record.get("timestamp"))
            limit = _to_float_or_none(record.get("limit_mb"))
            abort = record.get("abort_enabled")
        except Exception:  # noqa: BLE001 —— 记录来自外部文件，一律容错
            out["bad_records"] += 1
            continue

        if rss is None and commit is None and guard_peak is None and ts is None:
            out["bad_records"] += 1
            continue

        out["records"] += 1
        for key, value in (
            ("peak_rss_mb", rss), ("peak_commit_mb", commit), ("peak_guard_mb", guard_peak),
        ):
            if value is not None and (out[key] is None or value > out[key]):
                out[key] = value
        if sys_avail is not None and (
            out["min_sys_available_mb"] is None or sys_avail < out["min_sys_available_mb"]
        ):
            out["min_sys_available_mb"] = sys_avail
        if ts is not None:
            if out["first_ts"] is None or ts < out["first_ts"]:
                out["first_ts"] = ts
            if out["last_ts"] is None or ts > out["last_ts"]:
                out["last_ts"] = ts
        if limit is not None:
            out["limit_mb"] = limit
        if isinstance(abort, bool):
            out["abort_enabled"] = abort

    if out["first_ts"] is not None and out["last_ts"] is not None:
        out["duration_s"] = round(out["last_ts"] - out["first_ts"], 3)

    # 标定建议：以 guard 判定口径（max(rss, commit)）的峰值为基准 +50% 余量。
    # 这只是**起步建议**，不是标定结论——真值要按真实代打场景复核。
    basis = out["peak_guard_mb"]
    if basis is None:
        basis = out["peak_rss_mb"]
    if basis is not None:
        out["suggested_limit_mb"] = round(basis * 1.5, 2)
    return out


def summarize_file(path: str) -> dict:
    """读一个 jsonl 采样文件并汇总；坏行跳过，**不抛异常**。

    Args:
        path: jsonl 路径，每行一个 JSON 对象。

    Returns:
        dict: 与 :func:`summarize_records` 同构，额外带 ``"path"`` 和
        ``"readable"``（文件是否成功打开）。
    """
    import io

    out = summarize_records([])
    out["path"] = str(path)
    out["readable"] = False
    lines: list[dict] = []
    try:
        with io.open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:  # noqa: BLE001 —— 坏行只计数
                    out["bad_records"] += 1
                    continue
                lines.append(parsed)
    except OSError:
        return out

    out["readable"] = True
    merged = summarize_records(lines)
    merged["bad_records"] += out["bad_records"]
    merged["path"] = str(path)
    merged["readable"] = True
    return merged


def _build_parser() -> argparse.ArgumentParser:
    """构造只读 CLI 的参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="mem_guard",
        description="只读内存标定工具：采 RSS/commit 峰值，不改任何东西、不杀任何进程。",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe", action="store_true", help="采一次快照并打印")
    group.add_argument("--watch", type=float, metavar="SECONDS",
                       help="按 --interval 连续采 SECONDS 秒，结束时打印峰值")
    group.add_argument("--summarize", metavar="JSONL",
                       help="汇总一次代打写下的采样 jsonl，直接给出峰值与建议阈值")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="--watch 的采样间隔（秒），默认 5.0")
    parser.add_argument("--limit-mb", type=float, default=1500.0,
                        help="仅用于计算 over 标志，不触发任何动作，默认 1500（未标定初值）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于脚本取 peak_rss_mb）")
    return parser


def main(
    argv: Optional[list[str]] = None,
    sampler: Optional[Callable[[], dict]] = None,
    system_sampler: Optional[Callable[[], dict]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> int:
    """只读 CLI 入口。

    Args:
        argv: 参数列表，缺省 ``sys.argv[1:]``。
        sampler / system_sampler / sleep / clock: 注入式替身，仅供测试，
            生产调用不传。

    Returns:
        int: 0 = 读到了数（**超限也返回 0**，本工具只测量不当门禁）；
        1 = 读数不可用；2 = 参数错误。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.probe and args.watch is None and args.summarize is None:
        parser.print_help(sys.stderr)
        return 2
    if args.watch is not None and args.watch <= 0:
        parser.error("--watch 必须 > 0")
    if args.interval <= 0:
        parser.error("--interval 必须 > 0")

    if args.summarize is not None:
        summary = summarize_file(args.summarize)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False))
        elif not summary["readable"]:
            print(f"summarize: 读不到文件 {args.summarize}")
            return 2
        elif summary["records"] == 0:
            print(f"summarize: {args.summarize} 里没有有效采样记录（坏行 {summary['bad_records']} 行）")
            return 1
        else:
            duration = "n/a" if summary["duration_s"] is None else f"{summary['duration_s']}s"
            print(
                f"summarize: {summary['records']} records"
                f"（坏行 {summary['bad_records']}）"
                f" / {duration}\n"
                f"peak_rss_mb={_fmt(summary['peak_rss_mb'])} "
                f"peak_commit_mb={_fmt(summary['peak_commit_mb'])} "
                f"peak_guard_mb={_fmt(summary['peak_guard_mb'])}\n"
                f"min_sys_available_mb={_fmt(summary['min_sys_available_mb'])} "
                f"limit_mb={_fmt(summary['limit_mb'])} "
                f"abort_enabled={summary['abort_enabled']}\n"
                f"suggested_limit_mb={_fmt(summary['suggested_limit_mb'])}"
                "（peak + 50% 余量，仅起步建议，不是标定结论）"
            )
        return 0 if summary["records"] > 0 else 1

    if args.probe:
        snap = probe(
            limit_mb=args.limit_mb, sampler=sampler,
            system_sampler=system_sampler, clock=clock,
        )
        if args.json:
            print(json.dumps(snap, ensure_ascii=False))
        else:
            print(
                f"rss_mb={_fmt(snap['rss_mb'])} commit_mb={_fmt(snap['commit_mb'])} "
                f"available={snap['available']} over={snap['over']} "
                f"limit_mb={args.limit_mb:g} reason={snap['reason']}"
            )
            print(
                f"sys: available_mb={_fmt(snap['sys_available_mb'])} "
                f"total_mb={_fmt(snap['sys_total_mb'])} "
                f"used_percent={_fmt(snap['sys_used_percent'])} "
                f"commit_mb={_fmt(snap['sys_commit_mb'])}/"
                f"{_fmt(snap['sys_commit_limit_mb'])} "
                f"ok={snap['sys_reading_ok']}"
            )
        return 0 if snap["available"] else 1

    summary = watch(
        args.watch,
        interval=args.interval,
        limit_mb=args.limit_mb,
        sampler=sampler,
        system_sampler=system_sampler,
        sleep=sleep,
        clock=clock,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(
            f"watch: {summary['samples']} samples / {summary['elapsed_s']}s "
            f"(unavailable={summary['unavailable_samples']}, over={summary['over_samples']})\n"
            f"peak_rss_mb={_fmt(summary['peak_rss_mb'])} "
            f"peak_commit_mb={_fmt(summary['peak_commit_mb'])} "
            f"limit_mb={args.limit_mb:g}\n"
            f"sys: min_available_mb={_fmt(summary['min_sys_available_mb'])} "
            f"ok={summary['sys_reading_ok']}\n"
            "用法：拿 peak_rss_mb 标定 limit_mb（建议 peak + 50% 余量起步）；"
            "min_available_mb 见底说明是被别的进程挤的，不是自己泄漏。"
        )
    # 全程读不到数才算失败；读到了就返回 0（哪怕超限——这里只测量，不当门禁）。
    return 0 if summary["samples"] > summary["unavailable_samples"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
