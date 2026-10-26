# -*- coding: utf-8 -*-
"""本机故障包守护：窗口内手点观察 + 增量自动复盘。

它不修改代码、不点击游戏、不调用外部模型。Codex/自动巡检只需读取
``incident_root/index.json`` 中 open 项即可取得首故障证据包。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from auto_review import review_session
from human_input_monitor import (
    HumanInputMonitor,
    resolve_ldplayer_target,
)

__all__ = ["IncidentWatchdog", "find_latest_session"]

BOT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BOT_DIR.parents[1]
DEFAULT_LOGS_ROOT = BOT_DIR.parent / "采集" / "dev_auto_play"
DEFAULT_INCIDENT_ROOT = PROJECT_ROOT / "_incidents" / "tea2"


def _header_serial(session_dir: Path) -> Optional[str]:
    path = session_dir / "actions.jsonl"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(10):
                line = handle.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    value = item.get("serial") or item.get("device_serial")
                    if isinstance(value, str) and value:
                        return value
    except OSError:
        return None
    return None


def find_latest_session(root: str | Path, serial: str) -> Optional[Path]:
    candidates: list[Path] = []
    for ledger in Path(root).glob("*/actions.jsonl"):
        if _header_serial(ledger.parent) == serial:
            candidates.append(ledger.parent)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (
        (path / "actions.jsonl").stat().st_mtime_ns, str(path)))


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


class IncidentWatchdog:
    def __init__(
        self,
        logs_root: str | Path,
        incident_root: str | Path,
        serials: Sequence[str],
        *,
        ldconsole: str | Path,
        interval_s: float = 2.0,
        input_resolve_interval_s: float = 1.0,
    ) -> None:
        self.logs_root = Path(logs_root)
        self.incident_root = Path(incident_root)
        self.serials = tuple(dict.fromkeys(str(item) for item in serials if item))
        self.ldconsole = Path(ldconsole)
        self.interval_s = max(0.5, float(interval_s))
        self.input_resolve_interval_s = max(
            0.1, float(input_resolve_interval_s))
        self.stop_event = threading.Event()
        self._seen: dict[str, tuple[str, int, int]] = {}
        self._monitor_threads: list[threading.Thread] = []
        self._monitor_status: dict[str, dict] = {}

    def _human_path(self, serial: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)
        return self.incident_root / "human-input" / f"{safe}.jsonl"

    def start_input_monitors(self) -> None:
        for serial in self.serials:
            self._set_monitor_status(serial, "resolving", hwnd=None)
            thread = threading.Thread(
                target=self._run_input_monitor, args=(serial,), daemon=True,
                name=f"human-input-{serial}")
            thread.start()
            self._monitor_threads.append(thread)

    def _set_monitor_status(self, serial: str, status: str, **details) -> None:
        self._monitor_status[serial] = {"status": status, **details}

    def _run_input_monitor(self, serial: str) -> None:
        """Keep one observer bound to the serial's current live LDPlayer HWND."""
        monitor = None
        next_resolve_at = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            target_lost = monitor is not None and not monitor.target_valid()
            if target_lost:
                self._set_monitor_status(
                    serial, "window_lost", hwnd=monitor.target.hwnd)
                monitor = None
                next_resolve_at = 0.0

            if monitor is None or now >= next_resolve_at:
                target = resolve_ldplayer_target(serial, self.ldconsole)
                next_resolve_at = now + self.input_resolve_interval_s
                if target is None:
                    old_hwnd = monitor.target.hwnd if monitor is not None else None
                    self._set_monitor_status(
                        serial, "mapping_unknown", hwnd=old_hwnd)
                    monitor = None
                elif monitor is None or target != monitor.target:
                    try:
                        monitor = HumanInputMonitor(
                            target, self._human_path(serial))
                    except (OSError, RuntimeError, ValueError) as exc:
                        self._set_monitor_status(
                            serial, "monitor_unavailable", hwnd=target.hwnd,
                            error_type=type(exc).__name__)
                        monitor = None
                    else:
                        self._set_monitor_status(
                            serial, "observing", hwnd=target.hwnd,
                            mapping_basis=target.mapping_basis)
                elif monitor is not None:
                    # The resolver also verifies the LDPlayer row is running
                    # with a positive process id, so retain observing only after
                    # the periodic serial/process re-check succeeds.
                    self._set_monitor_status(
                        serial, "observing", hwnd=target.hwnd,
                        mapping_basis=target.mapping_basis)

            if monitor is not None:
                try:
                    monitor.poll_once()
                except OSError as exc:
                    self._set_monitor_status(
                        serial, "write_failed", hwnd=monitor.target.hwnd,
                        error_type=type(exc).__name__)
                    monitor = None
                    next_resolve_at = 0.0
                    self.stop_event.wait(
                        min(0.25, self.input_resolve_interval_s))
                    continue
                except Exception as exc:  # noqa: BLE001 - keep observer alive
                    self._set_monitor_status(
                        serial, "monitor_unavailable",
                        hwnd=monitor.target.hwnd,
                        error_type=type(exc).__name__)
                    monitor = None
                    next_resolve_at = 0.0
                    self.stop_event.wait(
                        min(0.25, self.input_resolve_interval_s))
                    continue
                wait_s = monitor.poll_s
            else:
                wait_s = min(0.25, self.input_resolve_interval_s)
            self.stop_event.wait(wait_s)

    def process_once(self) -> list[dict]:
        outcomes: list[dict] = []
        for serial in self.serials:
            session = find_latest_session(self.logs_root, serial)
            if session is None:
                outcomes.append({"serial": serial, "status": "no_session"})
                continue
            ledger = session / "actions.jsonl"
            try:
                stat = ledger.stat()
            except OSError:
                outcomes.append({"serial": serial, "status": "ledger_missing"})
                continue
            signature = (str(session), int(stat.st_size), int(stat.st_mtime_ns))
            if self._seen.get(serial) == signature:
                outcomes.append({"serial": serial, "status": "unchanged",
                                 "session": str(session)})
                continue
            try:
                summary = review_session(
                    session, incident_root=self.incident_root,
                    human_input_path=self._human_path(serial))
            except Exception as exc:  # noqa: BLE001 - keep watchdog alive and retry
                outcomes.append({"serial": serial, "status": "review_failed",
                                 "error_type": type(exc).__name__})
                continue
            incident_rows = summary.get("incidents")
            if not isinstance(incident_rows, list):
                incident_rows = [summary.get("incident")]
            if any(isinstance(item, dict)
                   and item.get("status") == "write_failed"
                   for item in incident_rows):
                outcomes.append({
                    "serial": serial, "status": "review_failed",
                    "error_type": "IncidentWriteFailed"})
                continue
            # A signature is acknowledged only after the review and incident
            # write complete.  Transient parse/I/O failures must retry even if
            # the ledger itself has not changed meanwhile.
            self._seen[serial] = signature
            outcomes.append({
                "serial": serial, "status": "reviewed",
                "session": str(session), "issue_count": summary["issue_count"],
                "incident": summary.get("incident"),
            })
        _atomic_json(self.incident_root / "watchdog_status.json", {
            "updated_at": datetime.now().astimezone().isoformat(),
            "input_monitors": self._monitor_status,
            "outcomes": outcomes,
        })
        return outcomes

    def run(self) -> None:
        self.start_input_monitors()
        while not self.stop_event.is_set():
            self.process_once()
            self.stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self._monitor_threads:
            thread.join(timeout=2.0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="温州麻将只读卡壳/人工点击故障包守护")
    parser.add_argument("--logs-root", default=str(DEFAULT_LOGS_ROOT))
    parser.add_argument("--incident-root", default=str(DEFAULT_INCIDENT_ROOT))
    parser.add_argument("--serial", action="append", required=True)
    parser.add_argument("--ldconsole", default=r"D:\leidian\LDPlayer14\ldconsole.exe")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    watcher = IncidentWatchdog(
        args.logs_root, args.incident_root, args.serial,
        ldconsole=args.ldconsole, interval_s=args.interval)
    try:
        if args.once:
            watcher.process_once()
        else:
            watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
