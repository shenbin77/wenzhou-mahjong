"""adb_runtime 的单元测试。

纪律：**全部使用假 runner / 假候选，绝不碰真实 adb，绝不发点击，
绝不 kill/start 任何真实 server。** 所有对外的 subprocess 调用都通过
替换模块级 ``adb_runtime._run_cmd`` 拦截。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import adb_runtime  # noqa: E402


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------

class FakeRunner:
    """记录每一次调用并按脚本返回结果的假命令执行器。

    Args:
        script: 关键字 -> CompletedProcess 的映射。关键字按 ``adb_runtime``
            发出的命令参数依次匹配（``"version"`` / ``"devices"`` /
            ``"kill-server"`` / ``"start-server"``）。未命中的关键字
            默认返回一个 rc=1 的失败结果。
    """

    def __init__(self, script: dict | None = None) -> None:
        self.script: dict = script or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float = adb_runtime.DEFAULT_TIMEOUT_S):
        self.calls.append([str(part) for part in cmd])
        for key, completed in self.script.items():
            if key in cmd:
                return completed
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fake: unscripted command")

    @property
    def argv_tail(self) -> list[str]:
        """每次调用的最后一个参数（子命令名）列表。"""
        return [c[-1] for c in self.calls if c]

    def called_with(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)


def ok_version() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["adb", "version"], 0, stdout="Android Debug Bridge version 1.0.41\nVersion 34.0.5\n", stderr=""
    )


def bad_version() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["adb", "version"], 1, stdout="", stderr="not an adb")


# --------------------------------------------------------------------------
# adb_candidates
# --------------------------------------------------------------------------

def test_candidates_dedupe_preserves_order(monkeypatch):
    """候选去重、保序、丢弃空串。"""
    monkeypatch.setattr(
        adb_runtime,
        "_raw_candidates",
        lambda: [
            "D:/a/adb.exe",
            "C:/b/adb.exe",
            "D:/a/adb.exe",          # 完全重复
            r"D:\a\adb.exe",         # 同一路径的反斜杠写法
            "",                      # 空串（which 没找到时的占位）
            "C:/b/adb.exe",
            "D:/c/adb.exe",
        ],
    )
    got = adb_runtime.adb_candidates()
    assert got == ["D:/a/adb.exe", "C:/b/adb.exe", "D:/c/adb.exe"]


def test_candidates_priority_order_is_leidian_then_scrcpy():
    """真实候选顺序：雷电 D 盘 → 雷电 C 盘 → 采集/scrcpy。"""
    got = adb_runtime.adb_candidates()
    assert got[0] == "D:/leidian/LDPlayer14/adb.exe"
    assert got[1] == "C:/leidian/LDPlayer14/adb.exe"
    assert got[2].endswith("采集/scrcpy/adb.exe".replace("/", "\\"))
    # 去重生效：不存在重复项
    assert len(got) == len(set(got))


def test_candidates_does_not_check_existence(monkeypatch, tmp_path):
    """候选列表本身不做存在性检查（存在性由 resolve 用 version 探测裁定）。"""
    missing = str(tmp_path / "definitely_not_here" / "adb.exe")
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: [missing])
    assert adb_runtime.adb_candidates() == [missing]


# --------------------------------------------------------------------------
# resolve_adb_path
# --------------------------------------------------------------------------

def test_resolve_skips_unusable_candidates(monkeypatch):
    """前面的候选 version 探测不过，跳过；返回第一个真正可用的。"""
    monkeypatch.setattr(
        adb_runtime, "_raw_candidates", lambda: ["D:/bad/adb.exe", "C:/bad/adb.exe", "D:/good/adb.exe"]
    )
    recorder = FakeRunner()  # 只用来记录调用，返回由 scripted 决定

    def scripted(cmd, timeout=adb_runtime.DEFAULT_TIMEOUT_S):
        recorder.calls.append([str(p) for p in cmd])
        if "D:/good/adb.exe" in cmd:
            return ok_version()
        return bad_version()

    monkeypatch.setattr(adb_runtime, "_run_cmd", scripted)
    assert adb_runtime.resolve_adb_path() == "D:/good/adb.exe"
    assert len(recorder.calls) == 3


def test_resolve_all_unusable_returns_empty(monkeypatch):
    """全部候选都不可用时返回空串。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/x/adb.exe", "C:/y/adb.exe"])
    monkeypatch.setattr(adb_runtime, "_run_cmd", lambda cmd, timeout=0.0: bad_version())
    assert adb_runtime.resolve_adb_path() == ""


def test_resolve_rejects_zero_rc_without_adb_marker(monkeypatch):
    """rc=0 但 stdout 不含 'Android Debug Bridge' 不算可用（防假 adb）。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/fake/adb.exe"])
    monkeypatch.setattr(
        adb_runtime,
        "_run_cmd",
        lambda cmd, timeout=0.0: subprocess.CompletedProcess(cmd, 0, stdout="hello world\n", stderr=""),
    )
    assert adb_runtime.resolve_adb_path() == ""


def test_resolve_swallows_timeout_and_exception(monkeypatch):
    """探测超时/异常一律吞掉并继续下一个候选，绝不向上抛。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/boom/adb.exe", "D:/good/adb.exe"])

    def flaky(cmd, timeout=adb_runtime.DEFAULT_TIMEOUT_S):
        if "boom" in cmd[0]:
            return subprocess.CompletedProcess(cmd, adb_runtime.EXCEPTION_RETURNCODE, stdout="", stderr="TimeoutExpired")
        return ok_version()

    monkeypatch.setattr(adb_runtime, "_run_cmd", flaky)
    assert adb_runtime.resolve_adb_path() == "D:/good/adb.exe"


# --------------------------------------------------------------------------
# ensure_single_server —— 最重要的红线：默认绝不 kill-server
# --------------------------------------------------------------------------

def test_ensure_single_server_never_kills_by_default(monkeypatch):
    """allow_restart=False（缺省）时，调用记录里绝不能出现 kill-server。"""
    runner = FakeRunner({"devices": subprocess.CompletedProcess(
        ["adb", "devices"], 0, stdout="List of devices attached\nemulator-5556\tdevice\n", stderr=""
    )})
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server("D:/leidian/LDPlayer14/adb.exe")

    assert runner.called_with("kill-server") is False
    assert runner.called_with("start-server") is False
    assert runner.argv_tail == ["devices"]
    assert out["restarted"] is False
    assert out["ok"] is True
    assert out["devices"] == ["emulator-5556"]


def test_ensure_single_server_no_kill_even_when_probe_fails(monkeypatch):
    """探测失败也不许自作主张杀服务：只报错，等调用方授权。"""
    runner = FakeRunner({"devices": subprocess.CompletedProcess(
        ["adb", "devices"], 3221225773, stdout="", stderr="access violation"
    )})
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server("D:/leidian/LDPlayer14/adb.exe")

    assert runner.called_with("kill-server") is False
    assert out["restarted"] is False
    assert out["ok"] is False
    assert "kill-server" in out["reason"]


def test_ensure_single_server_empty_path_does_nothing(monkeypatch):
    """chosen 为空时一个子进程都不启动。"""
    runner = FakeRunner()
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server("")

    assert runner.calls == []
    assert out["restarted"] is False
    assert out["ok"] is False


def test_ensure_single_server_allow_restart_runs_kill_and_start(monkeypatch):
    """只有 allow_restart=True 才 kill-server + start-server，并回报两次 returncode。"""
    runner = FakeRunner({
        "devices": subprocess.CompletedProcess(
            ["adb", "devices"], 1, stdout="", stderr="cannot connect to daemon"
        ),
        "kill-server": subprocess.CompletedProcess(["adb", "kill-server"], 0, stdout="", stderr=""),
        "start-server": subprocess.CompletedProcess(["adb", "start-server"], 0, stdout="* daemon started successfully", stderr=""),
    })
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server(
        "D:/leidian/LDPlayer14/adb.exe", allow_restart=True
    )

    assert runner.called_with("kill-server") is True
    assert runner.called_with("start-server") is True
    assert out["restarted"] is True
    assert out["kill_returncode"] == 0
    assert out["start_returncode"] == 0
    assert out["ok"] is True
    # steps 里两次 returncode 都要在
    rcs = [s["returncode"] for s in out["steps"] if s["cmd"][-1] in ("kill-server", "start-server")]
    assert rcs == [0, 0]


def test_ensure_single_server_restart_timeout_counts_as_failure(monkeypatch):
    """超时按失败处理：returncode 非 0 → ok=False。"""
    runner = FakeRunner({
        "devices": subprocess.CompletedProcess(["adb", "devices"], 1, stdout="", stderr="down"),
        "kill-server": subprocess.CompletedProcess(
            ["adb", "kill-server"], adb_runtime.EXCEPTION_RETURNCODE, stdout="", stderr="TimeoutExpired"
        ),
        "start-server": subprocess.CompletedProcess(["adb", "start-server"], 0, stdout="", stderr=""),
    })
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server("D:/x/adb.exe", allow_restart=True)

    assert out["kill_returncode"] == adb_runtime.EXCEPTION_RETURNCODE
    assert out["start_returncode"] == 0
    assert out["ok"] is False


def test_ensure_single_server_still_probes_when_allow_restart_true(monkeypatch):
    """探测本来就成功时，即使 allow_restart=True 也不该动 server。"""
    runner = FakeRunner({"devices": subprocess.CompletedProcess(
        ["adb", "devices"], 0, stdout="List of devices attached\nemulator-5556\tdevice\n", stderr=""
    )})
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)

    out = adb_runtime.ensure_single_server("D:/x/adb.exe", allow_restart=True)

    assert runner.called_with("kill-server") is False
    assert out["restarted"] is False
    assert out["ok"] is True


# --------------------------------------------------------------------------
# resolve_adb_path_cached
# --------------------------------------------------------------------------

def test_resolve_cached_probes_only_once(monkeypatch):
    """缓存版只在首次真探测，后续复用结果（避免 import 期/启动路径反复起子进程）。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/good/adb.exe"])
    calls: list[list[str]] = []

    def scripted(cmd, timeout=adb_runtime.DEFAULT_TIMEOUT_S):
        calls.append([str(p) for p in cmd])
        return ok_version()

    monkeypatch.setattr(adb_runtime, "_run_cmd", scripted)
    adb_runtime._ADB_PATH_CACHE.clear()

    assert adb_runtime.resolve_adb_path_cached() == "D:/good/adb.exe"
    assert adb_runtime.resolve_adb_path_cached() == "D:/good/adb.exe"
    assert len(calls) == 1

    adb_runtime.resolve_adb_path_cached(refresh=True)
    assert len(calls) == 2


def test_resolve_cached_does_not_cache_empty(monkeypatch):
    """探测失败（空串）不写缓存，下次仍重试，避免一次性抖动被永久记住。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/bad/adb.exe"])
    calls: list[list[str]] = []

    def scripted(cmd, timeout=adb_runtime.DEFAULT_TIMEOUT_S):
        calls.append([str(p) for p in cmd])
        return bad_version()

    monkeypatch.setattr(adb_runtime, "_run_cmd", scripted)
    adb_runtime._ADB_PATH_CACHE.clear()

    assert adb_runtime.resolve_adb_path_cached() == ""
    assert adb_runtime.resolve_adb_path_cached() == ""
    assert len(calls) == 2


# --------------------------------------------------------------------------
# adb_inventory —— 只读体检
# --------------------------------------------------------------------------

def test_inventory_is_read_only_and_parses_devices(monkeypatch):
    """体检只发 version/devices，解析出 device 状态的序列号。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/good/adb.exe"])
    runner = FakeRunner({
        "version": ok_version(),
        "devices": subprocess.CompletedProcess(
            ["adb", "devices"],
            0,
            stdout=(
                "List of devices attached\n"
                "emulator-5556\tdevice\n"
                "emulator-5554\toffline\n"
                "127.0.0.1:5555\tunauthorized\n"
            ),
            stderr="",
        ),
    })
    monkeypatch.setattr(adb_runtime, "_run_cmd", runner)
    monkeypatch.setattr(adb_runtime, "_adb_server_pids", lambda: [1001, 1002])

    inv = adb_runtime.adb_inventory()

    assert runner.called_with("kill-server") is False
    assert inv["chosen"] == "D:/good/adb.exe"
    assert inv["devices"] == ["emulator-5556"]
    assert inv["servers"] == [1001, 1002]
    assert "Android Debug Bridge" in inv["version"]
    assert set(inv) == {"chosen", "found", "version", "servers", "devices", "notes"}
    # 多个 adb.exe 时要给出"抢服务"提示
    assert any("adb.exe" in n for n in inv["notes"])


def test_inventory_reports_empty_when_no_adb(monkeypatch):
    """没有可用 adb 时 chosen 为空、devices 为空，并给出说明，不抛异常。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: [])
    monkeypatch.setattr(adb_runtime, "_run_cmd", lambda cmd, timeout=0.0: bad_version())
    monkeypatch.setattr(adb_runtime, "_adb_server_pids", lambda: [])

    inv = adb_runtime.adb_inventory()

    assert inv["chosen"] == ""
    assert inv["devices"] == []
    assert inv["servers"] == []
    assert inv["notes"]


def test_inventory_survives_server_pid_failure(monkeypatch):
    """PID 拿不到时返回空列表 + note，绝不抛异常。"""
    monkeypatch.setattr(adb_runtime, "_raw_candidates", lambda: ["D:/good/adb.exe"])
    monkeypatch.setattr(
        adb_runtime,
        "_run_cmd",
        lambda cmd, timeout=0.0: (
            ok_version() if "version" in cmd
            else subprocess.CompletedProcess(cmd, 0, stdout="List of devices attached\n", stderr="")
        ),
    )

    def boom():
        raise RuntimeError("tasklist exploded")

    monkeypatch.setattr(adb_runtime, "_adb_server_pids", boom)

    # 内部要能兜住；若真的漏出异常，本用例即失败
    inv = adb_runtime.adb_inventory()
    assert inv["chosen"] == "D:/good/adb.exe"


# --------------------------------------------------------------------------
# _run_cmd 本身
# --------------------------------------------------------------------------

def test_run_cmd_never_uses_shell(monkeypatch):
    """所有子进程调用都不许 shell=True（且 Windows 下带 CREATE_NO_WINDOW）。"""
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(kwargs.get("args", []), 0, stdout="", stderr="")

    monkeypatch.setattr(adb_runtime.subprocess, "run", fake_run)
    adb_runtime._run_cmd(["adb", "devices"], timeout=2.0)

    assert captured["shell"] is False
    assert captured["timeout"] == 2.0
    assert captured["capture_output"] is True
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW")


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(["adb"], 1.0),
    FileNotFoundError("no such file"),
    OSError(5, "access denied"),
])
def test_run_cmd_swallows_exceptions(monkeypatch, exc):
    """超时/缺文件/OSError 都转成 rc=-1 的 CompletedProcess，不抛出。"""
    def fake_run(**kwargs):
        raise exc

    monkeypatch.setattr(adb_runtime.subprocess, "run", fake_run)
    proc = adb_runtime._run_cmd(["adb", "version"])
    assert proc.returncode == adb_runtime.EXCEPTION_RETURNCODE
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# describe_exit_code —— 退出码翻译真源（2026-10-26）
#
# 起因：adb_runtime 旧注释把 3221225773 写成了 "0xC0000005 访问违例"，
# 实为 0xC000012D STATUS_COMMITMENT_LIMIT（提交额度不足，与 WinError 1455
# 同源）。人肉换算错一次就把整轮排查从"内存耗尽"带向"adb 崩溃"，
# 所以三个已实测见过的码必须用**逐字相等**钉死在测试里。
# --------------------------------------------------------------------------

#: (退出码, 十六进制串, NTSTATUS 名, 人话片段)
MEASURED_EXIT_CODES = [
    (3221225477, "0xC0000005", "STATUS_ACCESS_VIOLATION", "访问违例（真崩了）"),
    (3221225794, "0xC0000142", "STATUS_DLL_INIT_FAILED",
     "进程起不来（典型：无内存可提交）"),
    (3221225773, "0xC000012D", "STATUS_COMMITMENT_LIMIT", "提交额度/页面文件不足"),
]


@pytest.mark.parametrize("code,hex_text,name,plain", MEASURED_EXIT_CODES)
def test_describe_exit_code_is_exact_for_measured_codes(code, hex_text, name, plain):
    """逐字相等：改掉表里任意一个字符本用例都会变红（供变异验证）。"""
    assert adb_runtime.describe_exit_code(code) == (
        f"退出码 {code} ({hex_text} {name}）：{plain}")


def test_exit_code_table_covers_exactly_the_three_measured_codes():
    """表里不许多收（不猜），也不许漏收，且三个名字/人话互不重复。"""
    assert set(adb_runtime.EXIT_CODE_TABLE) == {3221225477, 3221225794, 3221225773}
    names = [entry[0] for entry in adb_runtime.EXIT_CODE_TABLE.values()]
    plains = [entry[1] for entry in adb_runtime.EXIT_CODE_TABLE.values()]
    assert len(names) == len(set(names))
    assert len(plains) == len(set(plains))


@pytest.mark.parametrize("code", [
    1, 2, 255, 3221225476, 3221225478, 3221225793, 3221225795, 4294967295,
])
def test_describe_exit_code_never_guesses_an_unknown_code(code):
    """未收录的码只给中性串：不许套到任何已知码的名字/人话上。"""
    got = adb_runtime.describe_exit_code(code)
    assert got == f"退出码 {code} (0x{code:08X})"
    for name, plain in adb_runtime.EXIT_CODE_TABLE.values():
        assert name not in got
        assert plain not in got


def test_describe_exit_code_handles_zero_signed_and_garbage():
    """0 是正常退出；负数按补码归一；拿不到整数时不抛异常。"""
    assert adb_runtime.describe_exit_code(0) == "退出码 0 (0x00000000)：子进程正常退出"
    # -1073741819 与 3221225477 是同一个 0xC0000005 的有符号/无符号写法。
    assert "STATUS_ACCESS_VIOLATION" in adb_runtime.describe_exit_code(-1073741819)
    # 本模块自己的哨兵（EXCEPTION_RETURNCODE = -1）必须安全退化。
    assert adb_runtime.describe_exit_code(adb_runtime.EXCEPTION_RETURNCODE) == (
        "退出码 4294967295 (0xFFFFFFFF)")
    # 垃圾输入一律返回 str，绝不抛。
    for junk in (None, "abc", "", object(), 1.5):
        got = adb_runtime.describe_exit_code(junk)
        assert isinstance(got, str)
    assert adb_runtime.describe_exit_code(None).startswith("退出码不可用")
    assert adb_runtime.describe_exit_code("abc").startswith("退出码不可用")


def test_extract_exit_code_reads_the_subprocess_message():
    """台账原文那条：exit status 3221225794 → 抠出来 → 翻译成人话。"""
    text = (
        "ADB 调用失败: Command '['C:\\leidian\\LDPlayer14\\adb.exe', '-s', "
        "'emulator-5556', 'shell', 'dumpsys', 'window', 'windows']' "
        "returned non-zero exit status 3221225794.")
    code = adb_runtime.extract_exit_code(text)
    assert code == 3221225794
    assert "0xC0000142 STATUS_DLL_INIT_FAILED" in adb_runtime.describe_exit_code(code)


def test_extract_exit_code_returns_none_when_there_is_no_code():
    """抠不到就是抠不到：WinError 1455 那条台账原文里本来就没有退出码。"""
    assert adb_runtime.extract_exit_code(
        "ADB 调用失败: [WinError 1455] 页面文件太小，无法完成操作。") is None
    assert adb_runtime.extract_exit_code("") is None
    assert adb_runtime.extract_exit_code(None) is None


def test_extract_exit_code_falls_back_to_the_hex_form():
    """日志里直接写 0x 形式时也要认得。"""
    assert adb_runtime.extract_exit_code("adb 炸了 0xC0000142 收尾") == 3221225794
