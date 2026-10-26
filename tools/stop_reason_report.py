#!/usr/bin/env python3
"""停手/弃权原因统计报告（只读）。

扫描工作目录下所有 ``actions.jsonl``，只保留**真实代打会话**，按 ``gate``
归并、按归一化后的 ``reason`` 聚合，回答三个问题：

1. 停手原因 Top5 各占多少；
2. 其中多少属于「单帧识牌不稳」这同一个病根；
3. 多少停手是本帧已识别正确、只是决策链没接上。

输出：控制台摘要 + ``_takeover/stop_reason_report.md`` +
``_takeover/stop_reason_stats.json``。报告里每个数字都由本脚本现算，无手填。

真实代打会话的判定（三条同时满足）：

* 路径命中白名单 :data:`SESSION_WHITELIST`，且不含任何黑名单段
  :data:`BLOCKED_SEGMENT_PATTERNS`（pytest 夹具、临时目录、工程探针目录）；
* 会话头 ``serial`` 属于真实模拟器 :data:`REAL_SERIALS`（``emulator-test``
  等测试桩一律排除）；
* 会话头 ``dry_run`` 不是 ``True``。

硬链接/目录联接造成的重复文件按 ``(st_dev, st_ino)`` 去重，避免把同一批
数据算两遍。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

ACTIONS_NAME = "actions.jsonl"
REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 会话筛选规则（改这里就能改口径，规则本身也会打印进报告）
# ---------------------------------------------------------------------------

#: 白名单（一）：路径段序列必须连续出现在会话目录里。
SESSION_WHITELIST: tuple[tuple[str, ...], ...] = (
    ("_takeover", "wb_live"),
    ("采集", "dev_auto_play"),
)

#: 白名单（二）：``_takeover`` 下的单局实录目录，如 ``live_5556`` / ``play7_5558``。
#: 这些是逐局挑出来的真机录像，没有子目录，靠目录名识别。
LIVE_DIR_PATTERN = re.compile(r"^(?:live|play\d*)_\d{4}$")

#: 黑名单：``(分组名, 段名正则)``。命中即排除，同一分组会被聚合统计。
#: 覆盖 pytest 夹具、临时工作目录、工程探针。
BLOCKED_SEGMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("临时目录 .tmp*", r"^\.tmp"),          # .tmp / .tmp_bind / .tmp_cs ...
        ("pytest 输出 .pytest*", r"^\.pytest"),  # .pytest_tmp / .pytest_cache
        ("codex 临时目录", r"^\.codex_tmp"),
        ("验收夹具 _acc_bt*", r"^_acc_bt"),      # _acc_bt / _acc_bt3 / _acc_bt4
        ("备份目录 _backups", r"^_backups$"),
        ("测试夹具 test_*", r"^test_"),
        ("审计探针 wb_audit*", r"^wb_audit"),
        ("lead 探针 wb_lead*", r"^wb_lead"),
        ("工程探针 wb_eng*", r"^wb_eng"),
        ("基准测试 bt_*", r"^bt_"),              # bt_rel1 / bt_full1 / bt_hint1
        ("联接副本 scratch_*", r"^scratch_"),     # scratch_kong = 采集目录的硬链接
        ("隔离 UI 临时目录", r"^gpt_ui_final_isolated_tmp$"),
        ("test_out 输出", r"^test_out$"),
    )
)

#: 真实模拟器序列号；``emulator-test`` 是测试桩，``offline-demo`` 是离线演示。
REAL_SERIALS = frozenset({"emulator-5556", "emulator-5558"})

#: 视为「弃权/不动作」的 idle 状态；``wait`` 是正常等牌，不计入停手。
STOPPISH_STATUSES = frozenset({"abstain", "guard"})

#: claim.action 有值即代表行动行识别成功。
CLAIM_ACTIONS = frozenset({"chi", "peng", "gang", "hu", "pass"})

# ---------------------------------------------------------------------------
# reason 归一化
# ---------------------------------------------------------------------------

_BRACKET = re.compile(r"\[[^\]]*\]")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_MS = re.compile(r"\d+(?:\.\d+)?\s*ms")
_FLOAT = re.compile(r"-?\d+\.\d+")
_INT = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")
_MULTI_PUNCT = re.compile(r"[，,]{2,}")


def normalize_reason(reason: Any) -> str:
    """把易变片段替换成占位符，让同类原因能合并计数。

    ``[]`` 里通常是坐标/牌面列表，整体换成 ``<LIST>``；其余依次处理
    十六进制、毫秒、浮点、整数。归一化只用于**聚合分组**，报告里同时
    给出原始样例，避免丢掉可读性。
    """
    if reason is None or str(reason).strip() == "":
        return "(未记录 reason)"
    text = str(reason)
    text = _BRACKET.sub("<LIST>", text)
    text = _HEX.sub("<HEX>", text)
    text = _MS.sub("<MS>", text)
    text = _FLOAT.sub("<F>", text)
    text = _INT.sub("<N>", text)
    text = _SPACES.sub(" ", text).strip()
    text = _MULTI_PUNCT.sub("，", text)
    return text


# ---------------------------------------------------------------------------
# 病根分类
# ---------------------------------------------------------------------------

#: 严格口径：任务点名的四类单帧识牌问题。顺序即优先级，越具体越靠前
#: （「多亮白名单」的 reason 里也含「识牌不完整」字样，必须先匹配）。
VISION_CORE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("多亮白名单未确认",
     re.compile(r"多亮白名单未确认|bright_subset_unconfirmed")),
    ("圆盘未变化/未检出",
     re.compile(r"未检出任何行动圆盘|圆盘[^，。]*?(未变化|没有生效|仍实测存在)"
                r"|画面未变化|行动圆盘仍有")),
    ("压暗少读/暗手张数不守恒",
     re.compile(r"压暗|少读|暗手\s*\d+\s*→|暗手应为\s*\d+\s*或")),
    ("识牌不完整/漏检",
     re.compile(r"识牌不完整|明牌第\d+组仅\d+张|明牌第\d+组牌型非法"
                r"|疑似漏检|手牌张数不守恒")),
)

#: 扩展口径：同样是「一帧没读稳」，但表现为身份/稳定性维度。
#: 「财神未知…等候室/局间过渡」是正常等下一局，显式排除。
VISION_EXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("财神未知（多帧）", re.compile(r"财神[^，。]*?(未知|不可读)")),
    ("帧未稳定", re.compile(r"画面尚未稳定|连续一致\s*\d+\s*帧")),
    ("局身份不可读", re.compile(r"局身份[^，。]*?不可读|房号局数[^，。]*?不可读")),
    ("非茶2人局未确认", re.compile(r"未确认茶\s*\d+\s*人局")),
    ("点击前复核失败", re.compile(r"连续\s*\d+\s*帧复核失败")),
    ("点后复核不符", re.compile(r"副露组数\s*\d+\s*→")),
)

#: 正常等待类，不算病根（局间过渡/换局帧/听牌提示）。
NORMAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("局间过渡/等候室", re.compile(r"等候室|局间过渡")),
    ("换局帧跳过", re.compile(r"换局帧跳过|检测到换局|手选财神须重新确认"
                              r"|手选财神与当前画面不一致")),
    ("听牌提示面板", re.compile(r"疑似胡提示面板")),
    ("限时/限次退出", re.compile(r"达到运行时长|达到 --max-actions")),
    ("结算/模态弹窗", re.compile(r"牌局已结束/结算或模态弹窗")),
)


#: 既不是识牌也不是正常等待的其它归因（决策/点击验证/环境）。顺序即优先级。
OTHER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("环境/系统故障",
     re.compile(r"PermissionError|ADB 调用失败|页面文件太小|WinError"
                r"|unhandled|循环未捕获异常")),
    ("行动盘已识别但 L2 否决",
     re.compile(r"皆不可行|L2 否决[^。]*不可行")),
    ("来牌未知，L2 无法验证", re.compile(r"来牌未知")),
    ("行动界面挡住点击", re.compile(r"点击前检测到[^。]*行动界面")),
    ("点击后验证未通过",
     re.compile(r"点击或点后验证未通过|点击未通过|点击结果未知|没有生效")),
    ("行动界面暂停（无 claim 证据）", re.compile(r"行动界面")),
)


def classify_vision(reason: Any) -> tuple[str | None, str | None]:
    """返回 (口径, 命中标签)；不在识牌类里则返回 (None, None)。

    口径是 ``core``（任务点名的四类）、``ext``（扩展的识别不稳表现）
    或 ``normal``（正常等待，不计病根）。
    """
    if reason is None:
        return None, None
    text = str(reason)
    for label, pattern in VISION_CORE_RULES:
        if pattern.search(text):
            return "core", label
    for label, pattern in NORMAL_RULES:
        if pattern.search(text):
            return "normal", label
    for label, pattern in VISION_EXT_RULES:
        if pattern.search(text):
            return "ext", label
    return None, None


def classify_row(row: "Row") -> tuple[str | None, str | None]:
    """给一行停手/弃权归因，返回 (口径, 标签)。

    口径：``core``（单帧识牌不稳，严格四类）、``ext``（识牌不稳的其它
    表现）、``other``（决策/点击验证，不是识牌）、``normal``（正常等待）。
    ``claim.blocked_reason`` 比 ``reason`` 更细，优先用它细分 guard 行。
    """
    blocked = row.claim_blocked_reason
    # 只有 idle/guard 行才谈「识别成功却没记账」；stop 行按自己的 gate 归因。
    if (row.event == "idle" and row.claim_status == "ready"
            and row.claim_action):
        return "other", "识别成功但未走 action 记账"
    if blocked:
        if "张数不守恒" in blocked:
            return "core", "压暗少读/暗手张数不守恒"
        if "皆不可行" in blocked:
            return "other", "行动盘已识别但 L2 否决"
        if "来牌未知" in blocked:
            return "other", "来牌未知，L2 无法验证"
    bucket, label = classify_vision(row.reason)
    if bucket:
        return bucket, label
    text = str(row.reason or "")
    for label, pattern in OTHER_RULES:
        if pattern.search(text):
            return "other", label
    return None, None


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """一条事件行（只保留统计要用的字段）。"""

    event: str
    status: str | None
    gate: str | None
    reason: str | None
    guard: str | None
    tick: int | None
    claim_action: str | None
    claim_status: str | None
    claim_executable: bool | None
    claim_auto_executed: bool | None
    claim_blocked_reason: str | None = None
    claim_reason: str | None = None
    ts: datetime | None = None

    @property
    def stoppish(self) -> bool:
        """停手行或弃权行。"""
        if self.event == "stop":
            return True
        return self.event == "idle" and (self.status in STOPPISH_STATUSES)

    @property
    def gate_key(self) -> str:
        """归并用的 gate 键：显式 gate 优先，其次按事件/状态给隐式名。"""
        if self.gate:
            return str(self.gate)
        if self.event == "stop":
            return "stop:<no gate>"
        if self.event == "idle" and self.status == "guard":
            return f"guard:{self.guard or 'unknown'}"
        if self.event == "idle" and self.status == "abstain":
            return "abstain:<implicit>"
        return f"{self.event}:<no gate>"


@dataclass
class Session:
    """一个代打会话。"""

    path: Path
    serial: str
    rows: list[Row] = field(default_factory=list)
    line_count: int = 0

    @property
    def actions(self) -> int:
        return sum(1 for row in self.rows if row.event == "action")

    @property
    def stops(self) -> int:
        return sum(1 for row in self.rows if row.event == "stop")

    @property
    def stoppish(self) -> int:
        return sum(1 for row in self.rows if row.stoppish)


# ---------------------------------------------------------------------------
# 发现与解析
# ---------------------------------------------------------------------------


def _segments(path: Path) -> tuple[str, ...]:
    return tuple(part for part in path.parts if part not in (path.anchor, ""))


def blocked_reason(path: Path) -> str | None:
    """路径若该被排除，返回原因；否则 None。

    只检查**会话锚点之后**的目录名（``_takeover`` / ``采集`` 到会话之间的
    那几级），外加会话目录及其上一级。若对整个绝对路径逐段匹配，pytest 自己
    的临时目录（名字常以 ``test_`` 开头，位于锚点之外）会被误判成夹具。
    """
    candidates: list[str] = [path.parent.name, path.parent.parent.name]
    segments = _segments(path.parent)
    for anchor in ("_takeover", "采集"):
        if anchor in segments:
            index = len(segments) - 1 - segments[::-1].index(anchor)
            candidates.extend(segments[index + 1:])
            break
    for name in candidates:
        if not name:
            continue
        for label, pattern in BLOCKED_SEGMENT_PATTERNS:
            if pattern.search(name):
                return label
    if not _matches_whitelist(path):
        # 兜底：不在白名单的路径再看一眼完整路径，尽量给出「为什么被排除」
        # 的实际归类（例如 .tmp_bind/... 本身也含夹具段）。
        for name in _segments(path.parent):
            for label, pattern in BLOCKED_SEGMENT_PATTERNS:
                if pattern.search(name):
                    return label
        return "不在白名单"
    return None


def _matches_whitelist(path: Path) -> bool:
    segments = _segments(path)
    for wanted in SESSION_WHITELIST:
        size = len(wanted)
        for index in range(len(segments) - size + 1):
            if segments[index:index + size] == wanted:
                return True
    parent = path.parent
    if parent.parent.name == "_takeover" and LIVE_DIR_PATTERN.match(parent.name):
        return True
    return False


def iter_action_files(root: Path) -> Iterator[Path]:
    """递归找 ``actions.jsonl``，跳过 ``.git``。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        if ACTIONS_NAME in filenames:
            yield Path(dirpath) / ACTIONS_NAME


@dataclass
class Discovery:
    """发现结果，便于把「扫了多少、为什么排除」写进报告。"""

    sessions: list[Path]
    scanned: int
    skipped: Counter[str]
    duplicates: int


def discover_sessions(root: Path, settle_before: float | None = None) -> Discovery:
    """按白名单/黑名单/inode 去重挑出真实代打会话文件。

    ``settle_before`` 是时间戳阈值：mtime 晚于它的文件视为「BOT 可能仍在写」，
    整体跳过。正在直播的会话会被后续追加，逐行统计必然抖动，宁可先不统计。
    """
    scanned = 0
    skipped: Counter[str] = Counter()
    duplicates = 0
    chosen: list[Path] = []
    seen_inode: set[tuple[int, int]] = set()

    for path in iter_action_files(root):
        scanned += 1
        why = blocked_reason(path)
        if why:
            skipped[why] += 1
            continue
        try:
            stat = path.stat()
        except OSError:
            skipped["stat 失败"] += 1
            continue
        if settle_before is not None and stat.st_mtime > settle_before:
            skipped["仍在写入（未封盘）"] += 1
            continue
        inode = (stat.st_dev, stat.st_ino)
        if inode in seen_inode:
            duplicates += 1
            continue
        seen_inode.add(inode)
        chosen.append(path)

    return Discovery(chosen, scanned, skipped, duplicates)


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _claim_fields(raw: dict[str, Any]) -> tuple[str | None, str | None,
                                             bool | None, bool | None,
                                             str | None, str | None]:
    claim = raw.get("claim")
    if not isinstance(claim, dict):
        return None, None, None, None, None, None
    action = claim.get("action")
    action = action if isinstance(action, str) and action else None
    status = claim.get("status")
    status = status if isinstance(status, str) else None
    executable = _as_bool(claim.get("executable"))
    auto = claim.get("auto_click")
    executed = None
    if isinstance(auto, dict):
        executed = _as_bool(auto.get("executed"))
    blocked = claim.get("blocked_reason")
    blocked = blocked if isinstance(blocked, str) and blocked else None
    claim_reason = claim.get("claim_reason")
    claim_reason = claim_reason if isinstance(claim_reason, str) and claim_reason else None
    return action, status, executable, executed, blocked, claim_reason


def _as_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def parse_row(raw: dict[str, Any]) -> Row | None:
    """把一行 JSON 转成 :class:`Row`；会话头（无 event）返回 None。"""
    event = raw.get("event")
    if not isinstance(event, str):
        return None
    action, claim_status, executable, executed, blocked, claim_reason = _claim_fields(raw)
    tick = raw.get("tick")
    return Row(
        event=event,
        status=raw.get("status") if isinstance(raw.get("status"), str) else None,
        gate=raw.get("gate") if isinstance(raw.get("gate"), str) else None,
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
        guard=raw.get("guard") if isinstance(raw.get("guard"), str) else None,
        tick=tick if isinstance(tick, int) else None,
        claim_action=action,
        claim_status=claim_status,
        claim_executable=executable,
        claim_auto_executed=executed,
        claim_blocked_reason=blocked,
        claim_reason=claim_reason,
        ts=_as_time(raw.get("ts")),
    )


def load_session(path: Path, as_of: datetime | None = None) -> Session | None:
    """读一个会话文件。跳过测试桩 serial 与 dry-run，返回 None。

    ``as_of`` 只保留 ``ts`` 不晚于该时刻的行；这样即使同一个文件之后又被
    BOT 追加，重跑仍能精确复现当天的快照。
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    header: dict[str, Any] = {}
    rows: list[Row] = []
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        if index == 0 and "event" not in raw:
            header = raw
            continue
        row = parse_row(raw)
        if row is None:
            continue
        if as_of is not None and row.ts is not None and row.ts > as_of:
            continue
        rows.append(row)

    serial = header.get("serial")
    if not isinstance(serial, str) or serial not in REAL_SERIALS:
        return None
    if header.get("dry_run") is True:
        return None
    return Session(path=path, serial=serial, rows=rows, line_count=len(lines))


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

#: 停手前连续 idle 帧数的分桶边界（下限含，上限不含）。
IDLE_STREAK_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0 帧", 0, 1),
    ("1 帧", 1, 2),
    ("2 帧", 2, 3),
    ("3-5 帧", 3, 6),
    ("6-10 帧", 6, 11),
    ("11-20 帧", 11, 21),
    ("21-50 帧", 21, 51),
    ("51+ 帧", 51, None),
)


def _bucket_streak(value: int) -> str:
    for label, low, high in IDLE_STREAK_BUCKETS:
        if value >= low and (high is None or value < high):
            return label
    return IDLE_STREAK_BUCKETS[-1][0]


def idle_streaks(session: Session) -> list[tuple[int, Row]]:
    """每个停手/弃权点前紧邻的连续 idle 帧数（不含该行自身）。"""
    out: list[tuple[int, Row]] = []
    streak = 0
    for row in session.rows:
        if row.event == "idle":
            if row.stoppish:
                out.append((streak, row))
            streak += 1
            continue
        if row.stoppish:
            out.append((streak, row))
        streak = 0
    return out


def tail_idle(session: Session) -> int:
    """会话结尾连续 idle 帧数：日志写完还没恢复动作就是「卡住了」。"""
    count = 0
    for row in reversed(session.rows):
        if row.event != "idle":
            break
        count += 1
    return count


def build_stats(sessions: Sequence[Session]) -> dict[str, Any]:
    """汇总所有统计量，返回可直接 JSON 序列化的字典。"""
    gate_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    reason_raw: dict[str, Counter[str]] = {}
    reason_gates: dict[str, Counter[str]] = {}
    event_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    vision_core: Counter[str] = Counter()
    vision_ext: Counter[str] = Counter()
    other_counts: Counter[str] = Counter()
    normal_counts: Counter[str] = Counter()
    streak_buckets_stop: Counter[str] = Counter()
    streak_buckets_all: Counter[str] = Counter()
    tail_buckets: Counter[str] = Counter()
    stop_gate_counts: Counter[str] = Counter()
    guard_breakdown: Counter[str] = Counter()

    stoppish_total = 0
    stop_total = 0
    claim_ready_idle = 0
    claim_ready_idle_unexecuted = 0
    claim_ready_idle_executed = 0
    decision_blocked = 0
    decision_blocked_detail: Counter[str] = Counter()

    for session in sessions:
        for row in session.rows:
            event_counts[row.event] += 1
            if row.event == "idle" and row.status:
                status_counts[row.status] += 1

            # 「识别对了但没接上」A：行动行已判出 action，事件却仍是 idle。
            if (row.event == "idle" and row.status == "guard"
                    and row.claim_status == "ready"
                    and row.claim_action in CLAIM_ACTIONS):
                claim_ready_idle += 1
                if row.claim_auto_executed is False:
                    claim_ready_idle_unexecuted += 1
                else:
                    claim_ready_idle_executed += 1

            # 「识别对了但没接上」B：圆盘已识别，却被 L2 挡在 action 之外。
            if (row.claim_status == "blocked"
                    and row.claim_reason
                    and "检出" in row.claim_reason):
                decision_blocked += 1
                decision_blocked_detail[
                    normalize_reason(row.claim_blocked_reason)] += 1

            if not row.stoppish:
                continue
            stoppish_total += 1
            if row.event == "stop":
                stop_total += 1
                stop_gate_counts[row.gate_key] += 1
            gate = row.gate_key
            gate_counts[gate] += 1
            normalized = normalize_reason(row.reason)
            reason_counts[normalized] += 1
            reason_raw.setdefault(normalized, Counter())[
                (row.reason or "")[:120]] += 1
            reason_gates.setdefault(normalized, Counter())[gate] += 1
            bucket, label = classify_row(row)
            if bucket == "core" and label:
                vision_core[label] += 1
            elif bucket == "ext" and label:
                vision_ext[label] += 1
            elif bucket == "other" and label:
                other_counts[label] += 1
            elif bucket == "normal" and label:
                normal_counts[label] += 1
            else:
                other_counts["未归类"] += 1
            if normalized.startswith("检测到吃碰杠胡"):
                guard_breakdown[label or "未归类"] += 1

        for streak, streak_row in idle_streaks(session):
            bucket = _bucket_streak(streak)
            streak_buckets_all[bucket] += 1
            if streak_row.event == "stop":
                streak_buckets_stop[bucket] += 1
        tail_buckets[_bucket_streak(tail_idle(session))] += 1

    action_counts = [session.actions for session in sessions]
    zero_action = sum(1 for value in action_counts if value == 0)
    per_session = [
        {
            "path": str(session.path.resolve()),
            "serial": session.serial,
            "lines": session.line_count,
            "events": len(session.rows),
            "actions": session.actions,
            "stops": session.stops,
            "stoppish": session.stoppish,
            "tail_idle": tail_idle(session),
            "top_stop_reason": (
                Counter(normalize_reason(row.reason)
                        for row in session.rows if row.stoppish).most_common(1)
                or [(None, 0)]
            )[0][0],
        }
        for session in sessions
    ]
    per_session.sort(key=lambda item: (-item["actions"], item["path"]))

    core_total = sum(vision_core.values())
    ext_total = sum(vision_ext.values())

    def pct(value: int) -> float:
        return round(100.0 * value / stoppish_total, 2) if stoppish_total else 0.0

    gate_rows = [
        {"gate": gate, "count": count, "pct": pct(count)}
        for gate, count in gate_counts.most_common()
    ]
    reason_rows = [
        {
            "reason": reason,
            "count": count,
            "pct": pct(count),
            "gates": dict(reason_gates.get(reason, {})),
            "samples": [text for text, _n in reason_raw.get(reason, Counter()).most_common(2)],
        }
        for reason, count in reason_counts.most_common()
    ]

    return {
        "generated_by": "tools/stop_reason_report.py",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rules": {
            "whitelist": ["/".join(parts) for parts in SESSION_WHITELIST],
            "whitelist_dir_pattern": LIVE_DIR_PATTERN.pattern,
            "blacklist_segment_patterns": [
                f"{label} → {pattern.pattern}"
                for label, pattern in BLOCKED_SEGMENT_PATTERNS
            ],
            "real_serials": sorted(REAL_SERIALS),
            "stoppish_statuses": sorted(STOPPISH_STATUSES),
            "stoppish_definition": (
                "event=stop 或 (event=idle 且 status∈{abstain,guard})；"
                "status=wait（等摸牌）不算停手"
            ),
        },
        "totals": {
            "sessions": len(sessions),
            "rows": sum(len(session.rows) for session in sessions),
            "events": dict(event_counts),
            "idle_statuses": dict(status_counts),
            "actions": sum(action_counts),
            "stops": stop_total,
            "stoppish": stoppish_total,
            "mean_actions_per_session": (
                round(statistics.fmean(action_counts), 2) if action_counts else 0.0
            ),
            "median_actions_per_session": (
                statistics.median(action_counts) if action_counts else 0
            ),
            "zero_action_sessions": zero_action,
            "zero_action_pct": (
                round(100.0 * zero_action / len(sessions), 2) if sessions else 0.0
            ),
        },
        "gates": gate_rows,
        "stop_gates": [
            {"gate": gate, "count": count,
             "pct_of_stops": round(100.0 * count / stop_total, 2) if stop_total else 0.0}
            for gate, count in stop_gate_counts.most_common()
        ],
        "reasons": reason_rows,
        "vision": {
            "core": dict(vision_core),
            "core_total": core_total,
            "core_pct": pct(core_total),
            "ext_extra": dict(vision_ext),
            "ext_extra_total": ext_total,
            "ext_total": core_total + ext_total,
            "ext_pct": pct(core_total + ext_total),
        },
        "attribution": {
            "vision_core": dict(vision_core),
            "vision_ext": dict(vision_ext),
            "other": dict(other_counts),
            "normal": dict(normal_counts),
            "unclassified": (
                stoppish_total - core_total - ext_total
                - sum(other_counts.values()) - sum(normal_counts.values())
            ),
        },
        "guard_breakdown": [
            {"cause": cause, "count": count, "pct_of_guard": (
                round(100.0 * count / sum(guard_breakdown.values()), 2)
                if guard_breakdown else 0.0)}
            for cause, count in guard_breakdown.most_common()
        ],
        "idle_streak_buckets": [
            {
                "bucket": label,
                "stop_points": streak_buckets_stop.get(label, 0),
                "all_stop_points": streak_buckets_all.get(label, 0),
                "session_tails": tail_buckets.get(label, 0),
            }
            for label, _low, _high in IDLE_STREAK_BUCKETS
        ],
        "claim_ready_but_idle": {
            "count": claim_ready_idle,
            "pct_of_stoppish": pct(claim_ready_idle),
            "already_tapped": claim_ready_idle_executed,
            "never_tapped": claim_ready_idle_unexecuted,
            "definition": (
                "event=idle 且 status=guard 且 claim.status=ready 且 "
                "claim.action∈{chi,peng,gang,hu,pass}"
            ),
        },
        "decision_blocked_after_detection": {
            "count": decision_blocked,
            "pct_of_stoppish": pct(decision_blocked),
            "detail": dict(decision_blocked_detail),
            "definition": (
                "claim.status=blocked 且 claim.claim_reason 含「检出」"
                "（圆盘已识别成功，但 L2 把它挡在 action 之外）"
            ),
        },
        "recognized_but_not_acted": {
            "count": claim_ready_idle + decision_blocked,
            "pct_of_stoppish": pct(claim_ready_idle + decision_blocked),
        },
        "sessions": per_session,
    }


def pick_failure_samples(sessions: Sequence[Session], limit: int = 3) -> list[dict[str, Any]]:
    """挑典型失败会话：优先「动作少 + 停手多 + 拖尾 idle 长」。"""
    scored: list[tuple[float, Session, int, int]] = []
    for session in sessions:
        if not session.rows:
            continue
        score = (session.stoppish * 2.0
                 + tail_idle(session) * 1.5
                 - session.actions * 1.2
                 + len(session.rows) * 0.02)
        scored.append((score, session, session.actions, tail_idle(session)))
    scored.sort(key=lambda item: item[0], reverse=True)

    samples: list[dict[str, Any]] = []
    used_reasons: set[str] = set()
    for score, session, actions, tail in scored:
        counters = Counter(
            (row.gate_key, normalize_reason(row.reason))
            for row in session.rows if row.stoppish
        )
        top_gate, top_reason = counters.most_common(1)[0][0] if counters else ("-", "-")
        if top_reason in used_reasons and len(samples) < limit:
            # 尽量让样本覆盖不同病根
            if len(samples) + (limit - len(samples)) > len(scored):
                pass
            continue
        used_reasons.add(top_reason)
        samples.append({
            "path": str(session.path.resolve()),
            "serial": session.serial,
            "events": len(session.rows),
            "actions": actions,
            "stops": session.stops,
            "stoppish": session.stoppish,
            "tail_idle": tail,
            "score": round(score, 2),
            "top_gate": top_gate,
            "top_reason": top_reason,
        })
        if len(samples) >= limit:
            break

    if len(samples) < limit:  # 病根种类不够多时用高分兜底
        for score, session, actions, tail in scored:
            path = str(session.path.resolve())
            if any(sample["path"] == path for sample in samples):
                continue
            counters = Counter(
                (row.gate_key, normalize_reason(row.reason))
                for row in session.rows if row.stoppish
            )
            top_gate, top_reason = counters.most_common(1)[0][0] if counters else ("-", "-")
            samples.append({
                "path": path, "serial": session.serial,
                "events": len(session.rows), "actions": actions,
                "stops": session.stops, "stoppish": session.stoppish,
                "tail_idle": tail, "score": round(score, 2),
                "top_gate": top_gate, "top_reason": top_reason,
            })
            if len(samples) >= limit:
                break
    return samples


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def _short(text: Any, limit: int = 70) -> str:
    value = str(text).replace("|", "\\|")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def render_markdown(stats: dict[str, Any], discovery: Discovery,
                    samples: Sequence[dict[str, Any]], root: Path) -> str:
    totals = stats["totals"]
    vision = stats["vision"]
    claim = stats["claim_ready_but_idle"]
    reasons = stats["reasons"]
    gates = stats["gates"]

    denominator = totals["stoppish"] or 1

    def share(value: int) -> str:
        return f"{round(100.0 * value / denominator, 2)}%"

    lines: list[str] = []
    add = lines.append
    add("# 代打停手原因统计报告")
    add("")
    add(f"- 扫描根目录：`{root.resolve()}`")
    add(f"- 生成时刻：{stats['generated_at']}（数据是**快照**；`_takeover/wb_live` "
        f"等目录若仍在写入，重跑会得到略大的数字）")
    add("- 生成方式：`python tools/stop_reason_report.py`（数字全部现算，无手填）")
    skipped = discovery.skipped
    live = skipped.get("仍在写入（未封盘）", 0)
    fixture = sum(skipped.values()) - live
    add(f"- 发现 `actions.jsonl` {discovery.scanned} 份，"
        f"命中真实代打会话 **{totals['sessions']}** 份"
        f"（排除夹具/临时目录 {fixture} 份，"
        f"仍在写入未封盘 {live} 份，inode 重复 {discovery.duplicates} 份）")
    if stats.get("discovery", {}).get("as_of"):
        add(f"- 快照截止：`{stats['discovery']['as_of']}`；复现命令："
            f"`python tools/stop_reason_report.py --as-of "
            f"{stats['discovery']['as_of']} --settle "
            f"{stats['discovery'].get('settle') or 0}`")
    if skipped:
        add("")
        add("被排除的文件（按规则分组）：")
        add("")
        lines.extend(_table(
            ["排除规则", "文件数"],
            [[label, count] for label, count in skipped.most_common(10)],
        ))
    add("")
    add("## 会话筛选规则")
    add("")
    add("必须同时满足：")
    add("")
    add("1. 路径命中白名单：" +
        "、".join(f"`{item}`" for item in stats["rules"]["whitelist"]) +
        "，或 `_takeover` 下的单局实录目录（目录名匹配 `" +
        stats["rules"]["whitelist_dir_pattern"] + "`）")
    add("2. 路径不含黑名单段（正则按路径段匹配）：")
    patterns = stats["rules"]["blacklist_segment_patterns"]
    add("   " + "、".join(f"`{p}`" for p in patterns))
    add("3. 会话头 `serial` ∈ " +
        "、".join(f"`{s}`" for s in stats["rules"]["real_serials"]) +
        " 且 `dry_run` 非 true")
    add("4. 硬链接/目录联接按 `(st_dev, st_ino)` 去重")
    add("")
    add("停手/弃权行定义：" + stats["rules"]["stoppish_definition"])
    add("")
    add("## 总览")
    add("")
    lines.extend(_table(
        ["指标", "数值"],
        [
            ["会话数", totals["sessions"]],
            ["事件行数", totals["rows"]],
            ["正常出牌行（event=action）", totals["actions"]],
            ["真停手行（event=stop）", totals["stops"]],
            ["停手/弃权行合计", totals["stoppish"]],
            ["单会话平均动作数", totals["mean_actions_per_session"]],
            ["单会话动作数中位数", totals["median_actions_per_session"]],
            ["动作数 = 0 的会话", f"{totals['zero_action_sessions']} "
                                f"（{totals['zero_action_pct']}%）"],
        ],
    ))
    add("")
    add("事件分布：" + "、".join(
        f"`{name}`={count}" for name, count in
        sorted(totals["events"].items(), key=lambda item: -item[1])))
    add("")
    add("## 问题一：停手原因 Top5")
    add("")
    top5 = reasons[:5]
    lines.extend(_table(
        ["#", "停手原因（归一化）", "次数", "占停手/弃权行", "主要 gate", "原始样例"],
        [
            [index, _short(item["reason"], 60), item["count"], f"{item['pct']}%",
             _short(",".join(item["gates"].keys()), 30),
             _short(item["samples"][0] if item["samples"] else "", 46)]
            for index, item in enumerate(top5, 1)
        ],
    ))
    add("")
    top5_sum = sum(item["count"] for item in top5)
    add(f"Top5 合计 {top5_sum} 次，占停手/弃权行的 "
        f"{share(top5_sum)}。")
    add("")
    add("## 问题二：同一病根（单帧识牌不稳）占比")
    add("")
    add("严格口径 = 任务点名的四类：识牌不完整 / 多亮白名单未确认 / "
        "圆盘未变化 / 压暗少读（含手牌张数不守恒，本质是漏读多读）。")
    add("")
    core_rows = [[label, count,
                  share(count)]
                 for label, count in sorted(vision["core"].items(),
                                            key=lambda item: -item[1])]
    core_rows.append(["**小计（严格口径）**", f"**{vision['core_total']}**",
                      f"**{vision['core_pct']}%**"])
    lines.extend(_table(["病根分类", "次数", "占停手/弃权行"], core_rows))
    add("")
    if vision["ext_extra"]:
        add("扩展口径再加以下「一帧没读稳」的表现（同样是识别问题，"
            "只是落在身份/稳定性维度，不重复计算已命中的严格四类）：")
        add("")
        ext_rows = [[label, count,
                     share(count)]
                    for label, count in sorted(vision["ext_extra"].items(),
                                               key=lambda item: -item[1])]
        ext_rows.append(["**小计（扩展口径）**", f"**{vision['ext_total']}**",
                         f"**{vision['ext_pct']}%**"])
        lines.extend(_table(["扩展分类", "次数", "占停手/弃权行"], ext_rows))
        add("")
    add(f"**结论**：单帧识牌不稳导致 {vision['core_total']} 次停手，占 "
        f"{vision['core_pct']}%（严格口径）；放宽到身份/稳定性维度后为 "
        f"{vision['ext_total']} 次、占 {vision['ext_pct']}%。")
    add("")
    add("注：任务点名的第 3 类「圆盘未变化」在本批数据里只有 "
        f"{vision['core'].get('圆盘未变化/未检出', 0)} 次——圆盘相关问题主要不是"
        "「点了没变」，而是「盘已识别、判定后不动作」，落在下面的"
        "「决策/点击验证」里（见 P1）。")
    add("")
    attribution = stats["attribution"]
    add("全量归因（四类口径加总应等于停手/弃权行总数）：")
    add("")
    lines.extend(_table(
        ["归因大类", "次数", "占停手/弃权行"],
        [
            ["单帧识牌不稳（严格四类）", vision["core_total"],
             f"{vision['core_pct']}%"],
            ["单帧识牌不稳（扩展）", vision["ext_extra_total"],
             share(vision["ext_extra_total"])],
            ["决策/点击验证（非识牌）", sum(attribution["other"].values()),
             share(sum(attribution["other"].values()))],
            ["正常等待（不计病根）", sum(attribution["normal"].values()),
             share(sum(attribution["normal"].values()))],
            ["未归类", attribution["unclassified"],
             share(attribution["unclassified"])],
        ],
    ))
    add("")
    if attribution["other"]:
        add("「决策/点击验证」明细：")
        add("")
        lines.extend(_table(
            ["原因", "次数"],
            [[label, count] for label, count in
             sorted(attribution["other"].items(), key=lambda item: -item[1])],
        ))
        add("")
    if attribution["normal"]:
        add("「正常等待」明细（这些不该算病根）：")
        add("")
        lines.extend(_table(
            ["原因", "次数"],
            [[label, count] for label, count in
             sorted(attribution["normal"].items(), key=lambda item: -item[1])],
        ))
        add("")
    if stats["guard_breakdown"]:
        guard_reason_count = next(
            (item["count"] for item in reasons
             if item["reason"].startswith("检测到吃碰杠胡")), 0)
        add("### 「行动界面暂停」这一条的内部拆解")
        add("")
        add(f"该 reason 共 {guard_reason_count} 次，但内部是几类完全不同的情况，"
            f"不能当成同一种病根：")
        add("")
        lines.extend(_table(
            ["内部原因", "次数", "占该 reason"],
            [[item["cause"], item["count"], f"{item['pct_of_guard']}%"]
             for item in stats["guard_breakdown"]],
        ))
        add("")
    add("## 问题三：本帧识别正确、只是决策没接上")
    add("")
    blocked = stats["decision_blocked_after_detection"]
    combined = stats["recognized_but_not_acted"]
    add("按任务定义（`claim.action` 已识别但 `event=idle`）："
        f"`{claim['definition']}`。")
    add("")
    lines.extend(_table(
        ["指标", "数值"],
        [
            ["命中行数", claim["count"]],
            ["占停手/弃权行", f"{claim['pct_of_stoppish']}%"],
            ["其中已实际点击（auto_click.executed=true）", claim["already_tapped"]],
            ["其中完全没点（executed=false）", claim["never_tapped"]],
        ],
    ))
    add("")
    add("但这批行里「已点击」居多，说明它是**记账断点**：动作发出去了，"
        "事件却仍记 `idle/guard`，正常 `event=action` 台账缺了一行。")
    add("")
    add("另一半更严重：圆盘已经识别成功、L2 判定后却不落任何动作，"
        f"`{blocked['definition']}`。")
    add("")
    lines.extend(_table(
        ["指标", "数值"],
        [
            ["圆盘已识别但被 L2 挡住", blocked["count"]],
            ["占停手/弃权行", f"{blocked['pct_of_stoppish']}%"],
        ],
    ))
    add("")
    if blocked["detail"]:
        add("被挡住的具体理由：")
        add("")
        lines.extend(_table(
            ["L2 否决理由（归一化）", "次数"],
            [[_short(reason, 76), count] for reason, count in
             sorted(blocked["detail"].items(), key=lambda item: -item[1])],
        ))
        add("")
    add(f"**结论**：识别成功却没落动作合计 **{combined['count']}** 次，占停手/弃权行的 "
        f"**{combined['pct_of_stoppish']}%**（其中 {claim['count']} 次是记账断点、"
        f"{blocked['count']} 次是决策链阻断）。")
    add("")
    add("## gate 归并")
    add("")
    lines.extend(_table(
        ["gate", "次数", "占停手/弃权行"],
        [[f"`{item['gate']}`", item["count"], f"{item['pct']}%"] for item in gates],
    ))
    add("")
    add("仅 `event=stop` 的 gate 分布（真停机）：")
    add("")
    lines.extend(_table(
        ["gate", "次数", "占真停手"],
        [[f"`{item['gate']}`", item["count"], f"{item['pct_of_stops']}%"]
         for item in stats["stop_gates"]],
    ))
    add("")
    add("## 停手前连续 idle 帧数分布")
    add("")
    add("「真停手点」只数 `event=stop`；「含弃权帧」把每个 `idle/abstain|guard` "
        "也当成一个停手点，因此一条长弃权序列会被重复计数。"
        "「会话尾部」是每个会话日志末尾的连续 idle 帧数——这是 BOSS 说的"
        "「打着打着就停」最直接的形态。")
    add("")
    lines.extend(_table(
        ["连续 idle 帧数", "真停手点", "含弃权帧的全部停手点", "会话尾部"],
        [[item["bucket"], item["stop_points"], item["all_stop_points"],
          item["session_tails"]] for item in stats["idle_streak_buckets"]],
    ))
    add("")
    add("## 典型失败会话（复现样本）")
    add("")
    lines.extend(_table(
        ["#", "会话绝对路径", "动作", "stop", "停手/弃权", "尾部 idle", "主要 gate"],
        [[index, f"`{item['path']}`", item["actions"], item["stops"],
          item["stoppish"], item["tail_idle"], f"`{item['top_gate']}`"]
         for index, item in enumerate(samples, 1)],
    ))
    add("")
    add("## 结论与修复优先级（按数据排序）")
    add("")
    incomplete = vision["core"].get("识牌不完整/漏检", 0)
    bright = vision["core"].get("多亮白名单未确认", 0)
    l2_reject = attribution["other"].get("行动盘已识别但 L2 否决", 0)
    bookkeeping = attribution["other"].get("识别成功但未走 action 记账", 0)
    add(f"1. **P0｜单帧识牌不稳**：合计 {vision['core_total']} 次（{vision['core_pct']}%），"
        f"是停手的第一大病根。其中「识牌不完整/漏检」{incomplete} 次、"
        f"「多亮白名单未确认」{bright} 次。前者内部又含「明牌第 N 组仅 N 张，"
        f"疑似漏检」这类可定位的漏读，说明问题在**牌面取证**而不是决策规则；"
        f"建议把「漏检」与「真不可读」分开处理：版面可读但张数不足时应重拍，"
        f"而不是直接 abstain。")
    add(f"2. **P1｜行动盘已识别但 L2 否决后不落动作**：{l2_reject} 次"
        f"（{share(l2_reject)}）。金盘/胡盘已经识别成功，L2 判定不可行后既不点「过」"
        f"也不记 action，画面就停在 guard。建议否决后必须落一个明确的「过」动作。")
    add(f"3. **P2｜记账断点**：{bookkeeping} 次（{share(bookkeeping)}）。claim 已判出 "
        f"pass/chi/hu 且实际点了，但事件仍记 idle/guard，导致台账里看不到这次动作，"
        f"复盘时会误判成「没反应」。")
    add("")
    add("## 归一化 reason 完整排行")
    add("")
    lines.extend(_table(
        ["#", "归一化 reason", "次数", "占比", "gate"],
        [[index, _short(item["reason"], 78), item["count"], f"{item['pct']}%",
          _short(",".join(item["gates"].keys()), 26)]
         for index, item in enumerate(reasons, 1)],
    ))
    add("")
    return "\n".join(lines)


def render_console(stats: dict[str, Any], discovery: Discovery) -> str:
    totals = stats["totals"]
    vision = stats["vision"]
    claim = stats["claim_ready_but_idle"]
    out = [
        f"会话 {totals['sessions']} 份 / 事件 {totals['rows']} 行 / "
        f"动作 {totals['actions']} / 停手弃权 {totals['stoppish']}",
        f"单会话平均动作 {totals['mean_actions_per_session']}，"
        f"0 动作会话 {totals['zero_action_sessions']} 份（{totals['zero_action_pct']}%）",
        "Top5 停手原因：",
    ]
    for index, item in enumerate(stats["reasons"][:5], 1):
        out.append(f"  {index}. {item['count']:>5} 次 {item['pct']:>6}%  {item['reason'][:64]}")
    out.append(f"单帧识牌不稳（严格四类）：{vision['core_total']} 次 / "
               f"{vision['core_pct']}%   扩展口径：{vision['ext_total']} 次 / {vision['ext_pct']}%")
    blocked = stats["decision_blocked_after_detection"]
    combined = stats["recognized_but_not_acted"]
    out.append(f"识别对了但决策没接上：{claim['count']} 次（已点击 "
               f"{claim['already_tapped']} / 未点击 {claim['never_tapped']}）")
    out.append(f"圆盘已识别但被 L2 挡住：{blocked['count']} 次 / {blocked['pct_of_stoppish']}%"
               f"  合计未落动作：{combined['count']} 次 / {combined['pct_of_stoppish']}%")
    out.append(f"排除夹具/临时目录 {sum(discovery.skipped.values())} 份，"
               f"inode 重复 {discovery.duplicates} 份")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def collect(root: Path, as_of: datetime | None = None,
            settle_seconds: float | None = None
            ) -> tuple[list[Session], Discovery, Counter[str]]:
    """发现 + 解析，返回会话、发现结果、以及被 header 规则刷掉的数量。"""
    settle_before = None
    if settle_seconds is not None:
        # 锚定在 as_of 上，保证「同一 as_of + 同一 settle」永远得到同一批文件。
        anchor = as_of.timestamp() if as_of is not None else datetime.now().timestamp()
        settle_before = anchor - settle_seconds
    discovery = discover_sessions(root, settle_before)
    sessions: list[Session] = []
    rejected: Counter[str] = Counter()
    for path in discovery.sessions:
        session = load_session(path, as_of)
        if session is None:
            rejected["serial/dry_run 不合格"] += 1
            continue
        if not session.rows:
            rejected["as_of 之前无事件"] += 1
            continue
        sessions.append(session)
    return sessions, discovery, rejected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="代打停手原因统计（只读）")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="扫描根目录，默认仓库根")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="报告输出目录，默认 <root>/_takeover")
    parser.add_argument("--as-of", default=None,
                        help="只统计 ts 不晚于该 ISO 时刻的行，用于复现快照")
    parser.add_argument("--settle", type=float, default=None,
                        help="跳过最近 N 秒内仍被写入的会话文件（未封盘），"
                             "直播进行时用它换取稳定数字")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    out_dir = (args.out_dir or (root / "_takeover")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.astimezone()

    sessions, discovery, rejected = collect(root, as_of, args.settle)
    stats = build_stats(sessions)
    stats["discovery"] = {
        "scanned": discovery.scanned,
        "kept_files": len(discovery.sessions),
        "parsed_sessions": len(sessions),
        "skipped": dict(discovery.skipped),
        "duplicates": discovery.duplicates,
        "rejected_by_header": dict(rejected),
        "as_of": as_of.isoformat() if as_of else None,
        "settle": args.settle,
    }
    samples = pick_failure_samples(sessions)
    stats["failure_samples"] = samples

    json_path = out_dir / "stop_reason_stats.json"
    md_path = out_dir / "stop_reason_report.md"
    json_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_markdown(stats, discovery, samples, root),
                       encoding="utf-8")

    print(render_console(stats, discovery))
    print(f"\n报告：{md_path}\n明细：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
