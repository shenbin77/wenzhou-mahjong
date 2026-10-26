# -*- coding: utf-8 -*-
"""双端代打运行状态视图（live status）——只读、只统计、不动进程。

为什么需要它（2026-10-25 联调痛点）：BOSS 看到的是「停住了」，而事后翻
日志才知道停在哪一层、停了多久。看门狗只对「能证明没发出点击」的原因重启
（见 bot/dual_bot_watchdog.py 的 SAFE_RESTART_GATES），点击后不确定一律
halted——策略本身是对的，缺的只是一个一眼能看懂的状态视图。

本工具的硬边界：
- **只读**：只读 <root>/**/actions.jsonl、*.log、*ledger*.jsonl；
  不碰 ADB、不发点击、不启停任何进程（唯一写的文件是状态 JSON 自身）。
- **读不准就说读不准**：解析失败/时间缺失一律给 None，不猜、不脑补。
- **增量解析**：按字节偏移只读文件新增部分，12 小时长跑也不会每 3 秒重读
  几 MB 台账；文件被截断/轮转时自动从头重来。

判定口径（STALL_SECONDS 默认 90 秒）：
- ``silent``  —— 台账 90 秒没有任何新行：循环真的冻住了（最严重）。
- ``stalled`` —— 还在 tick，但 90 秒没有任何 action，且末条原因不属于
  已知的正常等待（换局/结算/邀请/大厅/等待下一张清晰画面）。
  这就是「人在牌桌上坐着不出牌、4 分钟被踢」的那一类。
- ``halted``  —— 台账尾部是 stop 事件且 gate 不在「可证明没点过」的集合里，
  或者本来就是安全停机：停是停住了，但停得干净，等人看。
- ``dead``    —— 进程不在了、台账也没有 stop：崩了或被 kill。
- ``waiting`` —— 属于正常等待（换局/结算/非我回合）。
- ``running`` —— 有 action 且在阈值内。

用法：
    python tools/live_status.py --once                 # 单次，打印一行摘要
    python tools/live_status.py --once --json          # 单次，打印完整 JSON
    python tools/live_status.py --interval 3           # 常驻，每 3 秒刷一行
    python tools/live_status.py --root <run> --once    # 指定会话根目录
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

SCHEMA = 1
STALL_SECONDS = 90.0
SILENT_SECONDS = 90.0
TAIL_REASONS = 3
REASON_CHARS = 80

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_ROOT = REPO_ROOT / "温州茶（传统麻将）" / "采集" / "dev_auto_play"
DEFAULT_OUT = REPO_ROOT / "_takeover" / "live_status.json"

SERIAL_RE = re.compile(r"emulator[-_ ]?(\d{4})")
# 联调里同一个端可能被开成 d1_5556 / 5556_g2 这类名字，末尾四位数字就是 serial。
BARE_SERIAL_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
DEFAULT_SERIALS = ("5556", "5558")

# 台账尾部出现这些字样时，属于「等」而不是「卡」。
BENIGN_WAIT_MARKERS = (
    "换局", "结算", "邀请", "大厅", "等待下一张", "局间", "等待下一局",
)
# 尾部是 stop 且 gate 落在这里 = 可证明没发出点击的安全停机（仅用于排版标注，
# 不参与任何重启决策；重启决策只有 dual_bot_watchdog.restart_verdict 一处）。
SAFE_GATE_LABEL = {"", "unhandled_exception", "duration", "max_actions"}


# ---------------------------------------------------------------- 时间工具


def parse_ts(text) -> datetime | None:
    """解析台账里的 ISO 时间；无时区按本机时区解释。解析失败返回 None。"""
    if not text or not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def _clamped_age(stamp: datetime | None, now: datetime,
                 mtime: float | None) -> float | None:
    """时间戳可信就用它；缺失或明显快于本机（时钟漂移）就退回文件 mtime。

    子进程台账的 ts 偶尔会比本机钟快几秒；如果直接 clamp 到 0，停顿判定会
    永远看不到「已经 90 秒没出牌」，所以这里显式退回 mtime。
    """
    if stamp is not None and (stamp - now).total_seconds() <= 2.0:
        return max(0.0, (now - stamp).total_seconds())
    if mtime:
        return max(0.0, now.timestamp() - mtime)
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


# ---------------------------------------------------------------- 增量台账


class LedgerStats:
    """一个 actions.jsonl 的累计统计（跨轮询保留，按字节偏移增量读）。"""

    __slots__ = ("path", "offset", "ticks", "actions", "round_seq",
                 "last_ts", "last_event", "last_gate", "last_status",
                 "last_turn_state", "last_reason", "reasons",
                 "last_action_ts", "lines", "mtime")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.ticks = 0
        self.actions = 0
        self.round_seq = 0
        self.last_ts: str | None = None
        self.last_event = ""
        self.last_gate = ""
        self.last_status = ""
        self.last_turn_state = ""
        self.last_reason = ""
        self.reasons: deque[str] = deque(maxlen=TAIL_REASONS)
        self.last_action_ts: str | None = None
        self.lines = 0
        self.mtime = 0.0

    def reset(self) -> None:
        self.offset = 0
        self.ticks = 0
        self.actions = 0
        self.round_seq = 0
        self.last_ts = None
        self.last_event = ""
        self.last_gate = ""
        self.last_status = ""
        self.last_turn_state = ""
        self.last_reason = ""
        self.reasons.clear()
        self.last_action_ts = None
        self.lines = 0

    def _consume(self, record: dict) -> None:
        if not isinstance(record, dict):
            return
        if "ts" not in record:
            return  # 首行配置头，不算 tick
        self.lines += 1
        self.ticks += 1
        self.last_ts = record.get("ts") or self.last_ts
        event = str(record.get("event") or "")
        if event:
            self.last_event = event
        if event == "action":
            self.actions += 1
            self.last_action_ts = record.get("ts") or self.last_action_ts
        gate = record.get("gate")
        if gate:
            self.last_gate = str(gate)
        status = record.get("status")
        if status:
            self.last_status = str(status)
        gate_state = record.get("turn_gate")
        if isinstance(gate_state, dict) and gate_state.get("state"):
            self.last_turn_state = str(gate_state["state"])
        reason = record.get("reason")
        if reason:
            text = str(reason).replace("\n", " ")
            self.last_reason = text
            if not self.reasons or self.reasons[-1] != text:
                self.reasons.append(text)
        seq = record.get("round_seq")
        if isinstance(seq, int) and seq > self.round_seq:
            self.round_seq = seq

    def poll(self, now: datetime) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return
        self.mtime = stat.st_mtime
        if stat.st_size < self.offset:
            self.reset()
        if stat.st_size == self.offset:
            return
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return
        if not chunk:
            return
        head, sep, _tail = chunk.rpartition(b"\n")
        if not sep:
            return  # 还没写完整行，等下次
        consumed = head + sep
        for raw in consumed.split(b"\n"):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            self._consume(record)
        self.offset += len(consumed)


class LedgerTail:
    """*ledger*.jsonl 的末行读数（局数/输赢进度）。行数很少，整读即可。"""

    __slots__ = ("path", "hands", "last", "mtime")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.hands = 0
        self.last: dict = {}
        self.mtime = 0.0

    def poll(self) -> None:
        try:
            stat = self.path.stat()
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        self.mtime = stat.st_mtime
        hands = 0
        last: dict = {}
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or "ts" not in record:
                continue
            hands += 1
            last = record
        self.hands = hands
        self.last = _squash(last)


def _squash(record: dict, limit: int = 14) -> dict:
    """把一条台账压成可读摘要：只留标量、截断长值。"""
    out: dict = {}
    for key, value in record.items():
        if len(out) >= limit:
            break
        if isinstance(value, (dict, list)):
            if isinstance(value, list) and value and all(
                    isinstance(item, (int, float, str)) for item in value):
                out[key] = value[:8]
            continue
        if isinstance(value, str) and len(value) > 120:
            value = value[:120] + "…"
        out[key] = value
    return out


# ---------------------------------------------------------------- 目录发现


def serial_key(name: str, known: tuple[str, ...] = DEFAULT_SERIALS) -> str:
    match = SERIAL_RE.search(name)
    if match:
        return match.group(1)
    for serial in known:
        if serial in name:
            return serial
    return name


def _has_actions(path: Path) -> bool:
    return (path / "actions.jsonl").is_file()


def discover_sessions(root: Path,
                      proc_sessions: dict[str, str] | None = None
                      ) -> dict[str, Path]:
    """找各端的会话目录：进程 --session 优先，其次按目录名里的 serial。

    root 既可能是「一轮联调」目录（子目录是 emulator-5556_xxx / d1_5556），
    也可能是「会话总目录」（子目录是 dual_watchdog_xxx / ui_xxx）。两种都支持。
    """
    proc_sessions = proc_sessions or {}
    candidates: list[Path] = []
    if _has_actions(root):
        candidates.append(root)
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if _has_actions(child):
                candidates.append(child)
                continue
            for grand in child.iterdir():
                if grand.is_dir() and _has_actions(grand):
                    candidates.append(grand)
    watchdog_only = [c for c in candidates if "dual_watchdog" in str(c)]
    if watchdog_only:
        candidates = watchdog_only

    newest: dict[str, tuple[float, Path]] = {}
    owner = {_resolve_path(Path(raw)): serial
             for serial, raw in proc_sessions.items()}
    for path in candidates:
        key = owner.get(_resolve_path(path))
        if key is None:
            name = path.name
            key = serial_key(name)
            if key == name:  # 名字里认不出 serial：归档/单端目录，不参与双端视图
                continue
        try:
            stamp = (path / "actions.jsonl").stat().st_mtime
        except OSError:
            continue
        if key not in newest or stamp > newest[key][0]:
            newest[key] = (stamp, path)
    # 进程指出来的会话是权威的（本轮真正在写的那一个），排在前面。
    for serial, raw in proc_sessions.items():
        path = Path(raw)
        if path.is_dir() and _has_actions(path) and _is_under(path, root):
            newest[serial] = (float("inf"), path)
    return {key: value[1] for key, value in sorted(newest.items())}


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_path(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _logs_freshness(session: Path) -> tuple[float | None, str | None]:
    """最近一次 *.log 的 mtime（证明子进程还在写日志/还活着）。"""
    latest: float | None = None
    name: str | None = None
    for pattern in ("*.log", "logs/*.log"):
        for path in session.glob(pattern):
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or stamp > latest:
                latest, name = stamp, path.name
    return latest, name


def _find_ledger(session: Path, root: Path) -> Path | None:
    best: tuple[float, Path] | None = None
    for base in (session, root):
        if not base.is_dir():
            continue
        for pattern in ("*ledger*.jsonl", "logs/*ledger*.jsonl"):
            for path in base.glob(pattern):
                try:
                    stamp = path.stat().st_mtime
                except OSError:
                    continue
                if best is None or stamp > best[0]:
                    best = (stamp, path)
    return best[1] if best else None


# ---------------------------------------------------------------- 进程存活


def process_index() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """索引「代打子进程」命令行（只读，绝不 kill）。

    返回 (serial -> 进程列表, serial -> 该进程 --session 目录)。
    """
    index: dict[str, list[dict]] = {}
    sessions: dict[str, str] = {}
    try:
        import psutil  # 本地可选依赖；缺失时状态里 process_alive 一律 None
    except ImportError:
        return index, sessions
    try:
        iterator = psutil.process_iter(["pid", "name", "cmdline"])
    except Exception:  # pragma: no cover - 极端环境
        return index, sessions
    for proc in iterator:
        try:
            cmdline = [str(part) for part in (proc.info.get("cmdline") or [])]
        except Exception:
            continue
        text = " ".join(cmdline)
        if "run_auto_play_dev" not in text:
            continue
        match = SERIAL_RE.search(text)
        key = match.group(1) if match else ""
        session = ""
        if "--session" in cmdline:
            position = cmdline.index("--session")
            if position + 1 < len(cmdline):
                session = cmdline[position + 1]
        index.setdefault(key, []).append({
            "pid": proc.info.get("pid"),
            "session": session,
            "cmd": text[:200],
        })
        if key and session:
            sessions[key] = session
    return index, sessions


# ---------------------------------------------------------------- 判定


def classify(record: dict, now: datetime) -> tuple[str, bool]:
    """(state, stalled)。纯函数，便于回归测试与口径复核。

    record 是 :func:`end_state` 组装出来的单端读数。
    """
    if not record.get("session"):
        return "no_session", False
    silent = record.get("silent_s")
    since_action = record.get("since_action_s")
    last_event = str(record.get("last_event") or "")
    reason = str(record.get("last_reason") or "")
    turn_state = str(record.get("last_turn_state") or "")
    gate = str(record.get("gate") or "")

    if last_event == "stop":
        return ("halted" if gate not in SAFE_GATE_LABEL else "stopped"), False
    if record.get("process_alive") is False:
        return "dead", False
    if silent is not None and silent > SILENT_SECONDS:
        return "silent", True
    if since_action is not None and since_action > STALL_SECONDS:
        waiting = (gate in SAFE_GATE_LABEL
                   and any(marker in reason for marker in BENIGN_WAIT_MARKERS))
        if not waiting:
            return "stalled", True
        return "waiting", False
    if turn_state == "wait" and last_event != "action":
        return "waiting", False
    return "running", False


def end_state(name: str, session: Path, stats: LedgerStats,
              ledger: LedgerTail | None, procs: dict[str, list[dict]],
              now: datetime) -> dict:
    key = serial_key(name)
    last_ts = parse_ts(stats.last_ts)
    action_ts = parse_ts(stats.last_action_ts)
    age = _clamped_age(last_ts, now, stats.mtime)
    since_action = _clamped_age(action_ts, now, stats.mtime)
    if since_action is None or action_ts is None:
        since_action = age  # 一次 action 都没有：按「距最后事件」算
    log_mtime, log_name = _logs_freshness(session)
    log_age = (None if log_mtime is None
               else max(0.0, now.timestamp() - log_mtime))
    alive_procs = procs.get(key, [])
    process_alive: bool | None = bool(alive_procs) if procs else None
    if process_alive is None:
        # psutil 不可用：用日志新鲜度兜一个保守判断（90 秒内写过 = 还活着）
        process_alive = None if log_age is None else log_age < SILENT_SECONDS

    record = {
        "end": key,
        "session": str(session),
        "session_name": session.name,
        "last_event_ts": stats.last_ts,
        "age_s": None if age is None else round(age, 1),
        "silent_s": None if age is None else round(age, 1),
        "since_action_s": None if since_action is None else round(since_action, 1),
        "ticks": stats.ticks,
        "actions": stats.actions,
        "gate": stats.last_gate,
        "last_event": stats.last_event,
        "last_status": stats.last_status,
        "last_turn_state": stats.last_turn_state,
        "last_reason": stats.last_reason[:REASON_CHARS],
        "reasons": [text[:REASON_CHARS] for text in stats.reasons],
        "round_seq": stats.round_seq,
        "log_age_s": None if log_age is None else round(log_age, 1),
        "log_name": log_name,
        "process_alive": process_alive,
        "pids": [item.get("pid") for item in alive_procs],
        "ledger": None if ledger is None or not ledger.last else {
            "path": str(ledger.path),
            "hands": ledger.hands,
            "last": ledger.last,
        },
    }
    state, stalled = classify(record, now)
    record["state"] = state
    record["stalled"] = stalled
    return record


# ---------------------------------------------------------------- 汇总


def render_line(status: dict) -> str:
    parts = []
    for key in sorted(status.get("ends", {})):
        item = status["ends"][key]
        label = {
            "running": "OK", "waiting": "WAIT", "stalled": "STALL",
            "silent": "SILENT", "halted": "HALT", "stopped": "STOP",
            "dead": "DEAD", "no_session": "NONE",
        }.get(item.get("state"), str(item.get("state") or "?").upper())
        age = item.get("since_action_s")
        age_text = "-" if age is None else f"{age:.0f}s"
        parts.append(
            f"{key} {label} {age_text} act={item.get('actions', 0)}"
            f" tick={item.get('ticks', 0)}"
            + (f" gate={item['gate']}" if item.get("gate") else "")
        )
    summary = status.get("summary", {})
    tail = (f"局={summary.get('round_seq', 0)}"
            f" 活={len(summary.get('live_ends') or [])}/{summary.get('active_ends', 0)}")
    if summary.get("stalled_ends"):
        tail += " 卡住:" + ",".join(summary["stalled_ends"])
    halted = summary.get("halted_ends") or []
    if halted:
        shown = ",".join(halted[:2]) + ("…" if len(halted) > 2 else "")
        tail += f" 停机:{shown}"
    stamp = (status.get("generated_at") or "")[11:19]
    return " ".join([stamp] + parts + [tail]).strip()


def build_status(root: Path, now: datetime | None = None,
                 cache: dict[str, LedgerStats] | None = None) -> dict:
    now = now or _now()
    procs, proc_sessions = process_index()
    sessions = discover_sessions(root, proc_sessions)
    cache = cache if cache is not None else {}
    live_paths = set()
    ends: dict[str, dict] = {}
    for key, session in sessions.items():
        ledger_file = session / "actions.jsonl"
        live_paths.add(str(ledger_file))
        stats = cache.get(str(ledger_file))
        if stats is None:
            stats = cache[str(ledger_file)] = LedgerStats(ledger_file)
        stats.poll(now)
        ledger_path = _find_ledger(session, root)
        ledger = None
        if ledger_path is not None:
            ledger = LedgerTail(ledger_path)
            ledger.poll()
        ends[key] = end_state(session.name, session, stats, ledger, procs, now)
    for stale in [path for path in cache if path not in live_paths]:
        cache.pop(stale, None)

    stalled = [k for k, v in ends.items() if v.get("stalled")]
    halted = [k for k, v in ends.items()
              if v.get("state") in ("halted", "stopped", "dead", "no_session")]
    rounds = max([v.get("round_seq") or 0 for v in ends.values()] or [0])
    hands = max([(v.get("ledger") or {}).get("hands") or 0 for v in ends.values()] or [0])
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "root": str(root),
        "stall_seconds": STALL_SECONDS,
        "ends": ends,
        "summary": {
            "active_ends": len(ends),
            "stalled_ends": stalled,
            "halted_ends": halted,
            "live_ends": [k for k, v in ends.items()
                          if v.get("state") in ("running", "waiting")],
            "round_seq": rounds,
            "hands": hands,
        },
    }


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def resolve_root(raw: str) -> Path:
    if raw:
        return Path(raw).resolve()
    if DEV_ROOT.is_dir():
        runs = sorted((p for p in DEV_ROOT.iterdir()
                       if p.is_dir() and p.name.startswith("dual_watchdog")),
                      key=lambda p: p.name)
        if runs:
            return runs[-1]
    # 没有看门狗运行目录时，跟着「正在跑的那两个子进程」的 --session 走，
    # 免得默认值永远指着一个旧的归档目录。
    _index, proc_sessions = process_index()
    parents = {str(Path(path).parent) for path in proc_sessions.values()}
    if len(parents) == 1:
        candidate = Path(parents.pop())
        if candidate.is_dir():
            return candidate
    return DEV_ROOT if DEV_ROOT.is_dir() else REPO_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="温州茶双端代打运行状态视图（只读）")
    parser.add_argument("--root", default="", help="联调会话根目录（缺省=最新 dual_watchdog_*）")
    parser.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="状态 JSON 路径")
    parser.add_argument("--once", action="store_true", help="只扫一次就退出")
    parser.add_argument("--json", action="store_true", help="打印完整 JSON 而不是单行摘要")
    parser.add_argument("--quiet", action="store_true", help="不打印终端摘要")
    parser.add_argument("--duration", type=float, default=0.0, help="常驻总时长秒（0=不限）")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    root = resolve_root(args.root)
    out = Path(args.out)
    began = time.monotonic()
    cache: dict[str, LedgerStats] = {}
    if not args.once:
        while True:
            status = build_status(root, cache=cache)
            write_atomic(out, status)
            if not args.quiet:
                print(render_line(status), flush=True)
            if args.duration and time.monotonic() - began >= args.duration:
                return 0
            time.sleep(max(0.5, args.interval))
    status = build_status(root, cache=cache)
    write_atomic(out, status)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=1))
    elif not args.quiet:
        print(render_line(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
