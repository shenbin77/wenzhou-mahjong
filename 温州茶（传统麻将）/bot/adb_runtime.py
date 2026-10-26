"""adb 可执行文件与 adb server 的单点治理（只读优先，默认不杀进程）。

本模块存在的唯一理由：**把"到底用哪个 adb"收敛成一处决策**。
**它不解决、也不需要解决"两套 adb 抢端口"——那条推测已被实测推翻。**

背景（2026-10-26 重写；上一版注释已被实测推翻，并被证明会持续把排查带偏）
------------------------------------------------------------------------

【已推翻】旧版注释声称：一处用雷电 adb、另一处用采集工具链 adb，两个 server
会在 5037 端口互踢，导致 ``adb shell dumpsys window windows`` 随机崩溃。
实测证据（来源为下方报告，第 1~3 条均为该报告标注【实测】的结果）：

1. 本机此刻只有 **1 个** ``adb.exe`` 进程（PID 18368，启动于 2026-10-26 08:04:17）；
2. ``C:/leidian/LDPlayer14/adb.exe`` 与 ``D:/leidian/LDPlayer14/adb.exe``
   **字节完全相同**：同为 1.0.41、5,916,440 字节，MD5
   ``F1F479BBA21298E758FC22D8D98F8E48``；
3. ``采集/scrcpy/adb.exe`` **磁盘上并不存在**（它只是一条候选路径，不是一个进程）。

第 2、3 条的字节比对由团队执行并在报告中标注【实测】，**本次改动未重跑该比对**，
所以这里只作引用，不额外宣称实测。

【真实故障】宿主机内存/提交额度耗尽 → 子进程起不来 → dumpsys 失败 →
被误判成"无法确认前台应用"而停手。台账里真实存在的 adb 失败只有 2 次
（台账原文照抄）：

    ```
    2026-10-25T18:45:49.463471+08:00  emulator-5558  tick=87
    gate=gate_foreground  reason="无法确认前台应用: ADB 调用失败: [WinError 1455] 页面文件太小，无法完成操作。"

    2026-10-25T18:45:52.127547+08:00  emulator-5556  tick=87
    gate=gate_foreground  reason="无法确认前台应用: ADB 调用失败: Command '['C:\\leidian\\LDPlayer14\\adb.exe', '-s', 'emulator-5556', 'shell', 'dumpsys', 'window', 'windows']' returned non-zero exit status 3221225794."
    ```

两台机相隔 3 秒、同一个 tick 同时失败 → 宿主机级资源故障，**不是 adb 的错**。
崩溃前 1.2 秒（18:45:50.881）同一会话已先出现
``capture_failed: OpenCV !buf.empty()``（screencap 返回空缓冲）——顺序自洽。
这三个退出码一律交给 :func:`describe_exit_code` 翻译，**不要人肉换算**：

* ``3221225477`` = ``0xC0000005`` STATUS_ACCESS_VIOLATION —— 访问违例（真崩了）；
* ``3221225794`` = ``0xC0000142`` STATUS_DLL_INIT_FAILED —— 进程起不来（典型：无内存可提交）；
* ``3221225773`` = ``0xC000012D`` STATUS_COMMITMENT_LIMIT —— 提交额度/页面文件不足。
  旧注释把它错写成 "0xC0000005 访问违例"，正是把人带向"adb 崩溃"而非
  "内存耗尽"的那一行，2026-10-26 已订正。

【推断，未确认】到底是谁把提交额度打满。报告第 ④ 节第 1 名疑犯是**进程内存
失控**（``mem_guard.py`` 头部记录的"第二个真实故障：OpenCV OOM"是同一真因的
另一个面），但崩溃时刻没有 RSS/commit 读数（当时看门狗还没接进
``auto_play_loop``），所以这一步只是推断，不要当成结论传播。

报告全文：``D:/温州麻将/SPECS/ADB-FORENSICS-20261026-dumpsys-timeout.md``
（含 adb 调用点全表、并发压测数据、以及"崩溃瞬间该抓哪些证据"的命令清单）。

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
import re
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

#: 已收录的 Windows/NTSTATUS 退出码 -> (名称, 人话)。
#:
#: 只收**实测见过**的退出码。收进来之前必须有真凭实据（台账原文 / 亲自换算），
#: 绝不从数字长相去猜：**猜错一次就把整条排查带偏**（前车之鉴见模块头注释）。
#:
#: 十进制值与十六进制的换算可在 Python 里复核：``hex(3221225794)``。
EXIT_CODE_TABLE: dict[int, tuple[str, str]] = {
    # 真·访问违例：进程自己崩了。
    3221225477: ("STATUS_ACCESS_VIOLATION", "访问违例（真崩了）"),
    # 进程根本没起来：通常是 DLL 初始化拿不到可提交的内存/桌面堆。
    3221225794: ("STATUS_DLL_INIT_FAILED", "进程起不来（典型：无内存可提交）"),
    # WinError 1455 / 0xC000012D 是同一件事的两个面：提交额度（内存+页面文件）被打满。
    3221225773: ("STATUS_COMMITMENT_LIMIT", "提交额度/页面文件不足"),
}

#: 从 ``subprocess.CalledProcessError`` 的文本里抠十进制退出码。
#: 典型句式：``Command '[...]' returned non-zero exit status 3221225794.``
_EXIT_STATUS_DECIMAL_RE = re.compile(r"exit\s+status\s+(-?\d+)", re.IGNORECASE)
#: 兜底：日志里直接写十六进制的句式（``0xC0000142``）。
_EXIT_STATUS_HEX_RE = re.compile(r"0x([0-9a-fA-F]{1,16})\b")

__all__ = [
    "adb_candidates",
    "resolve_adb_path",
    "resolve_adb_path_cached",
    "adb_inventory",
    "ensure_single_server",
    "describe_exit_code",
    "extract_exit_code",
]


# --------------------------------------------------------------------------
# 退出码翻译（真源：不许再有人肉换算）
# --------------------------------------------------------------------------

def describe_exit_code(code: object) -> str:
    """把子进程退出码（含 Windows NTSTATUS）翻译成人话。

    存在理由：2026-10-26 之前靠人肉换算，把 ``3221225773`` 错写成
    ``0xC0000005 访问违例``，实际它是 ``0xC000012D STATUS_COMMITMENT_LIMIT``
    ——与台账里那条 ``WinError 1455`` 是同一件事的两个面。错一次就足以把
    整轮排查从"内存耗尽"带向"adb 崩溃"，所以换算必须留在代码里。

    Args:
        code: 退出码。接受 int / 可 int() 的字符串 / None / 任意对象。
            负数按 32 位无符号补码归一化后再查表
            （``-1073741819`` 与 ``3221225477`` 是同一个 0xC0000005）。

    Returns:
        str: 统一格式 ``退出码 <十进制> (0x<8 位十六进制> <名称>)：<人话>``。
        未收录的码返回中性串 ``退出码 <十进制> (0x<十六进制>)``，
        **不猜、不套到已知码上**；``0`` 单独说明为正常退出；
        拿不到整数的一律返回 ``退出码不可用：<repr>``，**绝不抛异常**
        （本函数本身要在异常处理路径里被调用）。
    """
    try:
        value = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"退出码不可用：{code!r}"
    if value == 0:
        return "退出码 0 (0x00000000)：子进程正常退出"
    unsigned = value & 0xFFFFFFFF
    entry = EXIT_CODE_TABLE.get(unsigned)
    if entry is None:
        return f"退出码 {unsigned} (0x{unsigned:08X})"
    name, plain = entry
    signed_note = f"（有符号 {value}）" if value != unsigned else ""
    return f"退出码 {unsigned}{signed_note} (0x{unsigned:08X} {name}）：{plain}"


def extract_exit_code(text: object) -> int | None:
    """从异常/日志文本里抠出退出码，抠不到返回 ``None``。

    Args:
        text: 任意对象；非字符串先 ``str()``。为 ``None`` 时按空串处理。

    Returns:
        int | None: 抠到的退出码（十进制 int）；
        文本里没有 ``exit status <n>`` 也没有 ``0x<hex>`` 时返回 ``None``。
    """
    raw = "" if text is None else str(text)
    match = _EXIT_STATUS_DECIMAL_RE.search(raw)
    if match is not None:
        try:
            return int(match.group(1))
        except ValueError:            # pragma: no cover - 正则已保证是数字
            return None
    match = _EXIT_STATUS_HEX_RE.search(raw)
    if match is not None:
        try:
            return int(match.group(1), 16)
        except ValueError:            # pragma: no cover - 超长溢出
            return None
    return None


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
        # 措辞须精确：多 server 会让"用哪个 adb"产生分歧，需收敛到统一入口；
        # **不等于**"抢端口"——后者是早期推测，2026-10-26 已被推翻
        # （本机常态只有 1 个 adb.exe；两路径 adb 字节完全相同）。详见模块头注释。
        notes.append(
            f"发现 {len(servers)} 个 adb.exe 进程（PID={servers}）：adb 入口出现分歧，"
            "需用 resolve_adb_path() 收敛到同一个 adb（注意这不是'抢端口'，见模块头注释）。"
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
