"""adb 可执行文件与 adb server 的单点治理（只读优先，默认不杀进程）。

背景（2026-10-26 外部审查结论，两个真实故障）：

1. **两套 adb 抢服务**。代码里一处用 ``D:/leidian/LDPlayer14/adb.exe``
   （雷电模拟器自带），另一处用 ``采集/scrcpy/adb.exe``（采集工具链自带）。
   两个版本/来源不同的 adb server 会在 5037 端口互相踢，表现为
   ``adb shell dumpsys window windows`` 随机崩溃（退出码 1，或
   3221225773 = 0xC0000005 访问违例），进而让"前台包名门禁"误判成
   "无法确认前台应用"而停手——表面像识别问题，根因是 adb 抢端口。

   本模块把"到底用哪个 adb"收敛成**唯一一处**决策：
   ``adb_candidates()`` 定顺序、``resolve_adb_path()`` 做探测挑选，
   其余模块一律调用本模块，不许再各自拼路径。

2. **进程内存失控**（全量测试与代打并发时 OpenCV OOM）。
   见 ``mem_guard.py``。

设计红线
--------
* ``adb_inventory()`` 只读：只发 ``version`` / ``devices``，绝不重启服务。
* ``ensure_single_server()`` **默认只读**，
  ``allow_restart=False``（缺省）时**绝不**执行 ``kill-server``；
  只有调用方显式传 ``allow_restart=True`` 才允许 kill/start。
  无人值守脚本乱杀 adb server 会把正在采集的会话一起踢掉。
* 所有子进程调用都不用 ``shell=True``，Windows 下显式带
  ``CREATE_NO_WINDOW``，避免弹控制台窗口卡住无人值守流程。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 本文件所在目录（bot/）。
BOT_DIR = Path(__file__).resolve().parent
#: bot/ 的上级目录，采集工具链（采集/scrcpy/adb.exe）挂在这里。
PROJECT_DIR = BOT_DIR.parent

#: 雷电模拟器自带 adb（D 盘为首发，C 盘为备选）。
LEIDIAN_ADB_D = "D:/leidian/LDPlayer14/adb.exe"
LEIDIAN_ADB_C = "C:/leidian/LDPlayer14/adb.exe"
#: 采集工具链自带 adb（与雷电版可能不同源，是"两套 adb 抢服务"的另一半）。
SCRCPY_ADB = str(PROJECT_DIR / "采集" / "scrcpy" / "adb.exe")

#: 探测单个 adb 是否可用时的超时（秒）。版本探测应当很快，慢即不可用。
RESOLVE_TIMEOUT_S = 3.0
#: 普通只读命令（version / devices）的超时（秒）。
DEFAULT_TIMEOUT_S = 5.0
#: server 级命令（kill-server / start-server）的超时（秒）。
SERVER_TIMEOUT_S = 10.0

#: ``adb version`` 正常输出的判别串。
ADB_VERSION_MARKER = "Android Debug Bridge"

#: 子进程被异常/超时打断时使用的哨兵返回码（不等于 0，所以一律按失败处理）。
EXCEPTION_RETURNCODE = -1

#: Windows 下避免弹出控制台窗口。
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

__all__ = [
    "adb_candidates",
    "resolve_adb_path",
    "resolve_adb_path_cached",
    "adb_inventory",
    "ensure_single_server",
]


# --------------------------------------------------------------------------
# 内部执行器（测试可整体替换成假 runner，从而完全不碰真实 adb）
# --------------------------------------------------------------------------

def _run_cmd(cmd: list[str], timeout: float = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """执行一条 adb/系统命令，**永不抛异常**。

    Args:
        cmd: 完整参数列表，首个元素为可执行文件路径。绝不使用 shell。
        timeout: 超时秒数。

    Returns:
        ``subprocess.CompletedProcess``。超时或 OSError 时返回
        ``returncode=EXCEPTION_RETURNCODE``（非 0，调用方按失败处理），
        并把异常信息塞进 ``stderr`` 便于排查。
    """
    argv = [str(part) for part in cmd]
    kwargs: dict = {
        "args": argv,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        return subprocess.run(**kwargs)  # type: ignore[arg-type]
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, EXCEPTION_RETURNCODE, stdout="", stderr=f"TimeoutExpired after {timeout}s: {exc}"
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            argv, EXCEPTION_RETURNCODE, stdout="", stderr=f"FileNotFoundError: {exc}"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv, EXCEPTION_RETURNCODE, stdout="", stderr=f"OSError[{exc.errno}]: {exc}"
        )
    except ValueError as exc:
        return subprocess.CompletedProcess(
            argv, EXCEPTION_RETURNCODE, stdout="", stderr=f"ValueError: {exc}"
        )


def _raw_candidates() -> list[str]:
    """返回未去重的原始候选列表（含可能为空、可能重复的项）。

    拆分出这个内部函数是为了让测试可以整段替换候选来源。
    """
    return [
        LEIDIAN_ADB_D,
        LEIDIAN_ADB_C,
        SCRCPY_ADB,
        shutil.which("adb") or "",
    ]


def _dedupe(paths: list[str]) -> list[str]:
    """去重（按规范化后的路径，Windows 大小写不敏感）并保持原有顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        if not raw:
            continue
        try:
            key = os.path.normcase(os.path.normpath(str(raw)))
        except (TypeError, ValueError):
            key = str(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(str(raw))
    return out


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------

def adb_candidates() -> list[str]:
    """按优先级返回候选 adb 路径。

    顺序：雷电 D 盘 → 雷电 C 盘 → ``<bot 上级>/采集/scrcpy/adb.exe`` →
    ``shutil.which("adb")``（PATH 里的）。

    只做去重 + 保序，**不检查文件是否存在**（存在性由
    :func:`resolve_adb_path` 用真实的 ``version`` 探测来裁定）。

    Returns:
        list[str]: 去重保序后的绝对路径列表，可能为空列表。
    """
    return _dedupe(_raw_candidates())


def resolve_adb_path() -> str:
    """返回第一个可用的 adb 绝对路径。

    可用性判定：对候选执行 ``[path, "version"]``（超时 3 秒），
    返回码为 0 **且** stdout 含 ``"Android Debug Bridge"`` 才算通过。
    任何异常（不存在、超时、拒绝执行、输出错乱）都吞掉并继续下一个。

    Returns:
        str: 可用的 adb 绝对路径；全部不可用时返回空串 ``""``。
    """
    for path in adb_candidates():
        proc = _run_cmd([path, "version"], timeout=RESOLVE_TIMEOUT_S)
        if proc.returncode == 0 and ADB_VERSION_MARKER in (proc.stdout or ""):
            return path
    return ""


#: 进程内缓存：只缓存**非空**结果，失败允许下次重试（避免一次性抖动被永久记住）。
_ADB_PATH_CACHE: dict[str, str] = {}


def resolve_adb_path_cached(refresh: bool = False) -> str:
    """:func:`resolve_adb_path` 的进程内缓存版。

    每次调用 :func:`resolve_adb_path` 都要起 1~N 个子进程（每个候选最多 3 秒）。
    在 import 期或 Tk 启动路径上反复探测会明显拖慢启动，多开实例时更糟，
    所以给调用方一个缓存入口。

    Args:
        refresh: True 时丢弃缓存重新探测（例如用户手动指定了 adb 路径之后）。

    Returns:
        str: 与 :func:`resolve_adb_path` 同义；全不可用时为 ``""``。
        空结果**不会**写入缓存，下次调用仍会重试。
    """
    if refresh:
        _ADB_PATH_CACHE.pop("path", None)
    cached = _ADB_PATH_CACHE.get("path", "")
    if cached:
        return cached
    path = resolve_adb_path()
    if path:
        _ADB_PATH_CACHE["path"] = path
    return path


def _parse_devices(text: str) -> list[str]:
    """解析 ``adb devices`` 输出，只收状态为 ``device`` 的序列号。

    Args:
        text: 命令 stdout，形如 ``"List of devices attached\\nemulator-5556\\tdevice\\n"``。

    Returns:
        list[str]: 在线设备序列号列表（``offline`` / ``unauthorized`` / ``no permissions``
        一律不收，避免把掉线设备当成可用靶机）。
    """
    serials: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("list of devices"):
            continue
        if line.startswith("*") or line.lower().startswith("adb server"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            serials.append(serial)
    return serials


def _adb_server_pids() -> list[int]:
    """找出所有 ``adb.exe`` 进程的 PID（只读）。

    优先用 ``psutil``（装了的话）；没装就退回 Windows 的 ``tasklist``。
    两条路都拿不到时返回空列表，**绝不抛异常**——拿不到只是体检信息缺失，
    不能让体检本身崩掉。

    Returns:
        list[int]: 升序 PID 列表；拿不到则为空列表。
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            pids: list[int] = []
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").strip().lower()
                if name == "adb.exe":
                    pids.append(int(proc.info.get("pid") or proc.pid))
            return sorted(set(pids))
        except Exception:
            # psutil 迭代中途进程消失等，静默降级到 tasklist。
            pass

    if os.name != "nt":
        return []

    import csv as _csv

    proc = _run_cmd(["tasklist", "/FI", "IMAGENAME eq adb.exe", "/FO", "CSV", "/NH"],
                    timeout=DEFAULT_TIMEOUT_S)
    if proc.returncode != 0:
        return []
    pids_out: list[int] = []
    for row in _csv.reader((proc.stdout or "").splitlines()):
        if len(row) < 2:
            continue
        if row[0].strip().lower() != "adb.exe":
            continue
        try:
            pids_out.append(int(row[1].strip()))
        except ValueError:
            continue
    return sorted(set(pids_out))


def adb_inventory() -> dict:
    """只读体检：当前到底用的哪个 adb、有几个 adb server、挂着哪些设备。

    **只读**：只发 ``version`` 与 ``devices``，不会 kill/start 任何 server。

    Returns:
        dict::

            {
              "chosen": str,   # resolve_adb_path() 的结果，全不可用为 ""
              "found": [str],  # adb_candidates() 的全部候选（去重保序）
              "version": str,  # `adb version` 首行；探测失败为 ""
              "servers": [int],# 所有 adb.exe 的 PID
              "devices": [str],# 状态为 device 的序列号
              "notes": [str],  # 人类可读的补充说明/降级原因
            }
    """
    notes: list[str] = []
    found = adb_candidates()
    chosen = resolve_adb_path()
    if not chosen:
        notes.append("所有 adb 候选均不可用（version 探测未通过），未做任何 server 改动。")

    version = ""
    if chosen:
        proc = _run_cmd([chosen, "version"], timeout=DEFAULT_TIMEOUT_S)
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if line.strip():
                    version = line.strip()
                    break
        else:
            notes.append(f"version 探测失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:200]}")

    try:
        servers = _adb_server_pids()
    except Exception as exc:  # noqa: BLE001 —— 体检本身绝不能因为进程枚举失败而崩
        servers = []
        notes.append(f"枚举 adb.exe 进程失败（{type(exc).__name__}: {exc}），servers 记为空列表。")
    if not servers:
        notes.append("未发现 adb.exe 进程（psutil/tasklist 均不可用，或当前确实没有 server 在跑）。")
    elif len(servers) > 1:
        notes.append(
            f"发现 {len(servers)} 个 adb.exe 进程（PID={servers}）：这正是'两套 adb 抢服务'的典型特征，"
            "需用 resolve_adb_path() 统一入口收敛到同一个 adb。"
        )

    devices: list[str] = []
    if chosen:
        proc = _run_cmd([chosen, "devices"], timeout=DEFAULT_TIMEOUT_S)
        if proc.returncode == 0:
            devices = _parse_devices(proc.stdout or "")
            if not devices:
                notes.append("devices 探测成功但没有状态为 device 的在线设备。")
        else:
            notes.append(f"devices 探测失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:200]}")
    else:
        notes.append("chosen 为空，跳过 devices 探测。")

    return {
        "chosen": chosen,
        "found": found,
        "version": version,
        "servers": servers,
        "devices": devices,
        "notes": notes,
    }


def ensure_single_server(chosen: str, *, allow_restart: bool = False) -> dict:
    """确保只有一个 adb server 在跑。**默认只读，默认绝不杀进程。**

    Args:
        chosen: 要使用的 adb 绝对路径（通常来自 :func:`resolve_adb_path`）。
        allow_restart: 是否允许执行 ``kill-server`` / ``start-server``。
            **缺省 False**：只做 ``devices`` 探测，把结论报给调用方，
            由人来决定要不要重启。置 True 才会真正 kill + start。

    Returns:
        dict::

            {
              "chosen": str,
              "restarted": bool,          # 是否真的执行了 kill/start
              "ok": bool,                 # 探测成功 / 重启全流程成功
              "reason": str,              # 人类可读结论
              "steps": [{"cmd": [...], "returncode": int}],
              "devices": [str],           # 探测到的在线设备
              "kill_returncode": int|None,# 仅 allow_restart=True 时存在
              "start_returncode": int|None,
            }

        超时按失败处理：超时命令的 returncode 为 -1（非 0）。
    """
    chosen_str = str(chosen or "")
    result: dict = {
        "chosen": chosen_str,
        "restarted": False,
        "ok": False,
        "reason": "",
        "steps": [],
        "devices": [],
        "kill_returncode": None,
        "start_returncode": None,
    }

    if not chosen_str:
        result["reason"] = "chosen 为空：无法探测，且绝不杀任何 adb 进程。"
        return result

    probe = _run_cmd([chosen_str, "devices"], timeout=DEFAULT_TIMEOUT_S)
    result["steps"].append({"cmd": [chosen_str, "devices"], "returncode": probe.returncode})
    result["devices"] = _parse_devices(probe.stdout or "")

    if probe.returncode == 0:
        result["ok"] = True
        result["reason"] = (
            f"devices 探测成功（{len(result['devices'])} 台在线）。"
            "本次为只读检查，未改动任何 adb server 状态。"
        )
        return result

    detail = (probe.stderr or "").strip()[:200]
    if not allow_restart:
        result["ok"] = False
        result["reason"] = (
            f"devices 探测失败（rc={probe.returncode}）{('：' + detail) if detail else ''}。"
            "allow_restart=False，本模块拒绝执行 kill-server。"
        )
        return result

    # ---- 以下仅在调用方显式授权后才执行，会短暂中断所有 adb 连接 ----
    kill = _run_cmd([chosen_str, "kill-server"], timeout=SERVER_TIMEOUT_S)
    result["steps"].append({"cmd": [chosen_str, "kill-server"], "returncode": kill.returncode})
    result["kill_returncode"] = kill.returncode

    start = _run_cmd([chosen_str, "start-server"], timeout=SERVER_TIMEOUT_S)
    result["steps"].append({"cmd": [chosen_str, "start-server"], "returncode": start.returncode})
    result["start_returncode"] = start.returncode

    result["restarted"] = True
    result["ok"] = kill.returncode == 0 and start.returncode == 0

    after = _run_cmd([chosen_str, "devices"], timeout=DEFAULT_TIMEOUT_S)
    result["steps"].append({"cmd": [chosen_str, "devices"], "returncode": after.returncode})
    if after.returncode == 0:
        result["devices"] = _parse_devices(after.stdout or "")

    result["reason"] = (
        f"已授权重启：kill-server rc={kill.returncode}、start-server rc={start.returncode}，"
        f"重启后在线设备 {len(result['devices'])} 台。"
    )
    return result
