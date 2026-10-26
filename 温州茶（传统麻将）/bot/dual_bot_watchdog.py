"""Keep the two authorised tea-2p BOT processes alive without bypassing taps.

The watchdog only restarts a child that crashed before writing a terminal safety
event, or one that stopped after three *pre-click* anomalies.  Any ambiguous
post-click/safety gate remains halted for human/code review; restarting such a
child could repeat an already-issued action and is deliberately forbidden.

A small set of *named* pre-click races is exempted one reason-text at a time
(see ``restart_verdict``): they abort before any input tap is emitted, so a
fresh child that re-reads the frame cannot duplicate an action.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time


BOT_DIR = Path(__file__).resolve().parent
DEV_ROOT = BOT_DIR.parent / "采集" / "dev_auto_play"
SERIALS = ("emulator-5556", "emulator-5558")
RESTART_WINDOW_S = 60.0
RESTARTS_PER_WINDOW = 3
# 停机报告只留最后这些行子进程输出，够看清「最后一张帧为什么没出牌」即可。
HALT_REPORT_TAIL_LINES = 20
# 台账单行可能塞了整帧证据（几十 KB），报告里截断到人能读的长度。
HALT_REPORT_LEDGER_CHARS = 2000

# These outcomes prove no uncertain physical click needs to be reconciled.
SAFE_RESTART_GATES = {
    "",                         # consecutive pre-click analysis anomalies
    "unhandled_exception",      # process/code crash; next process starts fresh
    "duration",
    "max_actions",
}
NEVER_RESTART_GATES = {
    "tap_aborted", "tap_failed", "claim_unverified",
    "claim_tap_aborted", "claim_tap_failed",
    "chi_group_tap_aborted", "chi_group_tap_failed",
    "selection_cancel_unverified", "invitation_cancel_aborted",
    "invitation_cancel_failed", "gate_foreground", "user_stop",
    "match_complete",
}


def _latest_event(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1]) if lines else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def tail_lines(path: Path, count: int = HALT_REPORT_TAIL_LINES) -> list[str]:
    """取文件末尾若干行；文件不存在/读不了就当没有（报告里如实标注）。"""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-count:]


def last_ledger_line(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        lines = [line for line in
                 path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if line.strip()]
    except OSError:
        return ""
    if not lines:
        return ""
    line = lines[-1]
    if len(line) > HALT_REPORT_LEDGER_CHARS:
        line = line[:HALT_REPORT_LEDGER_CHARS] + "…（已截断）"
    return line


def halt_section(serial: str, session: Path | None, returncode: int | None,
                 gate: str, reason: str, restarts: int) -> str:
    """一段停机说明：最后 20 行子进程输出 + 最后 1 条台账行 + 停机 gate。"""
    session = session if session is not None else Path(".")
    out_lines = tail_lines(session / "stdout.log")
    err_lines = tail_lines(session / "stderr.log")
    ledger = last_ledger_line(session / "actions.jsonl")
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    body: list[str] = [
        f"## {serial}",
        "",
        f"- 停机时间: {stamp}",
        f"- 停机 gate: `{gate or '(空)'}`",
        f"- 看门狗判定: {reason}",
        f"- 子进程 returncode: {returncode}",
        f"- 已重启次数: {restarts}",
        f"- 会话目录: `{session}`",
        "",
        f"### 最后 {HALT_REPORT_TAIL_LINES} 行 stdout",
        "",
        "```text",
        *(out_lines or ["（stdout.log 为空或不存在）"]),
        "```",
        "",
        f"### 最后 {HALT_REPORT_TAIL_LINES} 行 stderr",
        "",
        "```text",
        *(err_lines or ["（stderr.log 为空或不存在）"]),
        "```",
        "",
        "### 最后 1 条台账行",
        "",
        "```json",
        ledger or "（actions.jsonl 为空或不存在）",
        "```",
        "",
    ]
    return "\n".join(body)


def write_halted_report(root: Path, sections: dict[str, str]) -> Path:
    """把各端停机说明原子写到 <root>/halted_report.md。"""
    path = root / "halted_report.md"
    header = [
        "# 双端看门狗停机报告",
        "",
        "> 由 dual_bot_watchdog.py 在 halted 时自动生成；"
        "gate 不在可证明「未发出点击」的白名单里时，看门狗**不会**自动重启。",
        "",
    ]
    text = "\n".join(header + [""]) + "\n".join(
        sections[serial] for serial in sorted(sections))
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return path


def write_status_json(path: Path, payload: dict) -> None:
    """周期性把看门狗自身状态原子落一份，供 live_status/人肉排查读取。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def restart_verdict(event: dict, returncode: int | None) -> tuple[bool, str]:
    """Pure classification; never converts a post-click uncertainty to retry."""
    if not event:
        return True, f"进程退出且没有终止台账（returncode={returncode}）"
    if event.get("event") != "stop":
        return True, f"进程异常退出，末条台账不是stop（returncode={returncode}）"
    gate = str(event.get("gate") or "")
    reason = str(event.get("reason") or "")
    if (gate == "tap_aborted"
            and "第一次点击后手牌计数在" in reason
            and "禁止第二击" in reason):
        return True, (
            "可恢复待二击：执行器仅完成第一次选牌并已持久化恢复状态；"
            "新进程仍须fresh帧证明同一抬起牌后才可二击")
    if (gate == "claim_tap_aborted"
            and "没有实测到任何行动圆盘" in reason):
        return True, (
            "可恢复点击前中止：行动窗口在点击前已消失，未发出点击；"
            "新进程须从fresh帧重新read_claims再决定")
    if (gate == "tap_aborted"
            and "点击前检测到吃碰杠胡/过的行动界面" in reason):
        return True, (
            "可恢复点击前中止：点击前发现行动界面，未发出点击，"
            "交由新进程先处理行动窗口")
    if gate in NEVER_RESTART_GATES:
        return False, f"安全停机 {gate}: {reason}"
    if gate in SAFE_RESTART_GATES:
        if gate == "" and not reason.startswith("连续 3 次异常"):
            return False, f"无分类终止原因，不自动重启: {reason}"
        return True, f"可恢复停机 {gate or 'preclick_anomaly'}: {reason}"
    return False, f"未列入可恢复白名单的安全门禁 {gate}: {reason}"


class Child:
    def __init__(self, serial: str, root: Path, args) -> None:
        self.serial = serial
        self.root = root
        self.args = args
        self.process: subprocess.Popen | None = None
        self.session: Path | None = None
        self.restarts: deque[float] = deque()
        self.generation = 0
        self.halted = False
        self.halt_gate = ""
        self.halt_reason = ""
        self.returncode: int | None = None
        self.last_event: dict = {}

    def start(self) -> None:
        self.generation += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session = self.root / f"{self.serial}_{stamp}_g{self.generation}"
        self.session.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(BOT_DIR / "run_auto_play_dev.py"),
            "--serial", self.serial,
            "--mode", "tea_2p", "--caishen", "auto",
            "--enable", "--no-dry-run", "--claim-auto-click", "--claim-chi",
            "--max-actions", str(self.args.max_actions),
            "--duration", str(self.args.child_duration),
            "--interval", str(self.args.interval),
            "--session", str(self.session),
        ]
        out = (self.session / "stdout.log").open("ab")
        err = (self.session / "stderr.log").open("ab")
        self.process = subprocess.Popen(
            command, cwd=str(BOT_DIR), stdout=out, stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def inspect_exit(self) -> tuple[bool, str]:
        assert self.process is not None and self.session is not None
        event = self.last_event = _latest_event(self.session / "actions.jsonl")
        return restart_verdict(event, self.process.returncode)

    def restart_budget_ok(self, now: float) -> bool:
        while self.restarts and now - self.restarts[0] > RESTART_WINDOW_S:
            self.restarts.popleft()
        if len(self.restarts) >= RESTARTS_PER_WINDOW:
            return False
        self.restarts.append(now)
        return True

    def status(self) -> dict:
        """单端状态快照（只读台账末条，不改变任何重启判定）。"""
        event = _latest_event(self.session / "actions.jsonl") if self.session else {}
        alive = None if self.process is None else self.process.poll() is None
        return {
            "serial": self.serial,
            "pid": None if self.process is None else self.process.pid,
            "alive": alive,
            "generation": self.generation,
            "restarts": max(0, self.generation - 1),
            "session": None if self.session is None else str(self.session),
            "halted": self.halted,
            "halt_gate": self.halt_gate,
            "halt_reason": self.halt_reason,
            "returncode": self.returncode,
            "last_event": str(event.get("event") or ""),
            "last_gate": str(event.get("gate") or ""),
            "last_reason": str(event.get("reason") or "")[:200],
            "last_ts": event.get("ts"),
            "tick": event.get("tick"),
        }


def _append(path: Path, payload: dict) -> None:
    payload = {"ts": datetime.now().astimezone().isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="温州茶双端代打安全看门狗")
    parser.add_argument("--root", default="")
    parser.add_argument("--duration", type=float, default=43200.0)
    parser.add_argument("--child-duration", type=float, default=43200.0)
    parser.add_argument("--max-actions", type=int, default=5000)
    parser.add_argument("--interval", type=float, default=1.2)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--status-json", default="",
                        help="周期性落一份看门狗自身状态（JSON，原子写）")
    parser.add_argument("--status-every", type=float, default=5.0,
                        help="状态 JSON 落盘间隔秒")
    return parser


def build_status_payload(root: Path, children: list, began: float,
                         duration: float, event: str,
                         last_halt: dict | None = None) -> dict:
    return {
        "schema": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "watchdog_pid": os.getpid(),
        "root": str(root),
        "event": event,
        "elapsed_s": round(time.monotonic() - began, 1),
        "duration_s": duration,
        "restart_policy": {
            "safe_gates": sorted(SAFE_RESTART_GATES),
            "never_restart_gates": sorted(NEVER_RESTART_GATES),
        },
        "last_halt": last_halt or {},
        "children": [child.status() for child in children],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (Path(args.root) if args.root else
            DEV_ROOT / ("dual_watchdog_" + datetime.now().strftime("%Y%m%d_%H%M%S")))
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "watchdog.jsonl"
    status_path = Path(args.status_json) if args.status_json else None
    children = [Child(serial, root, args) for serial in SERIALS]
    for child in children:
        child.start()
        _append(ledger, {"event": "started", "serial": child.serial,
                         "pid": child.process.pid, "session": str(child.session)})
    began = time.monotonic()
    halt_sections: dict[str, str] = {}
    last_halt: dict = {}
    last_status_at = 0.0

    def flush_status(event: str) -> None:
        if status_path is None:
            return
        try:
            write_status_json(status_path,
                              build_status_payload(root, children, began,
                                                   args.duration, event, last_halt))
        except OSError:
            pass  # 状态文件写不进去不能影响看门狗本身

    flush_status("running")
    try:
        while time.monotonic() - began < max(0.0, args.duration):
            for child in children:
                if child.halted or child.process is None:
                    continue
                code = child.process.poll()
                if code is None:
                    continue
                allowed, reason = child.inspect_exit()
                if not allowed:
                    child.halted = True
                    child.returncode = code
                    child.halt_gate = str(child.last_event.get("gate") or "")
                    child.halt_reason = reason
                    _append(ledger, {"event": "halted", "serial": child.serial,
                                     "returncode": code, "reason": reason})
                    halt_sections[child.serial] = halt_section(
                        child.serial, child.session, code, child.halt_gate,
                        reason, max(0, child.generation - 1))
                    try:
                        write_halted_report(root, halt_sections)
                    except OSError:
                        pass
                    last_halt = {"serial": child.serial, "gate": child.halt_gate,
                                 "reason": reason, "ts": datetime.now()
                                 .astimezone().isoformat(timespec="seconds")}
                    flush_status("halted")
                    continue
                now = time.monotonic()
                if not child.restart_budget_ok(now):
                    child.halted = True
                    child.returncode = code
                    child.halt_gate = str(child.last_event.get("gate") or "")
                    child.halt_reason = f"重启预算耗尽：{reason}"
                    _append(ledger, {"event": "restart_budget_exhausted",
                                     "serial": child.serial, "reason": reason})
                    halt_sections[child.serial] = halt_section(
                        child.serial, child.session, code, child.halt_gate,
                        child.halt_reason, max(0, child.generation - 1))
                    try:
                        write_halted_report(root, halt_sections)
                    except OSError:
                        pass
                    last_halt = {"serial": child.serial, "gate": child.halt_gate,
                                 "reason": child.halt_reason, "ts": datetime.now()
                                 .astimezone().isoformat(timespec="seconds")}
                    flush_status("halted")
                    continue
                old_session = str(child.session)
                child.start()
                _append(ledger, {"event": "restarted", "serial": child.serial,
                                 "pid": child.process.pid, "from": old_session,
                                 "session": str(child.session), "reason": reason})
            if status_path is not None and (
                    time.monotonic() - last_status_at >= max(1.0, args.status_every)):
                last_status_at = time.monotonic()
                flush_status("running")
            time.sleep(max(0.2, args.poll))
    except KeyboardInterrupt:
        _append(ledger, {"event": "watchdog_stopped", "reason": "KeyboardInterrupt"})
        flush_status("watchdog_stopped")
        return 130
    flush_status("finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
