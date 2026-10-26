"""自动复盘与疑似问题筛查（离线、无头）。

产品口径（2026-10-25 定）：自动留证据 → 自动复盘 → 自动筛出疑似
问题 → 复盘报告（``auto_review.json`` / ``auto_review.md``）以及脱敏、
原子写入的 incident 包。本模块只读台账并写派生报告；不训练、不改代码、
不触发任何游戏执行/部署动作，也不接外部 LLM/API。

与既有模块的分工：

- ``recognition_review.py`` 是 Tk 人工逐帧复核窗口（线B 的 UI，
  读 ``recognition_audit.jsonl`` 做交互式人工标注），与本模块的
  无头台账筛查不是同一职责，故筛查核心独立成本模块，不往 UI
  模块里塞无头逻辑。
- 台账解析容错口径与 ``session_watch.load_ledger_events`` 一致
  （坏行/非 dict 行跳过）；本模块每条问题必须带行号引用，故自带
  带行号的读取器（坏行另计 ``corrupt_lines`` 供复盘看数据质量）。
- 弃权段口径直接复用 ``session_watch`` 的 ``StallPolicy`` 与
  ``_abstain_key``、会话头判据复用 ``_is_header``，不另立标准。
- ``actions.jsonl`` 是主台账；``follow_session.jsonl`` 是同进程的
  镜像调试日志，actions 存在时只用它兜底会话头，actions 缺失时
  才作主台账筛查，避免同一停手在两份日志里重复计数。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional

from human_input_monitor import associate_human_click, load_human_clicks
from incident_inbox import create_incident, read_repo_version
from session_watch import (StallPolicy, _abstain_key, _is_header,
                           derive_progress_suspicions)

__all__ = ["review_session", "review_root", "find_session_dirs",
           "render_review_md"]

ACTIONS_FILE = "actions.jsonl"
FOLLOW_FILE = "follow_session.jsonl"
FLAGS_FILE = "user_flags.jsonl"
HUMAN_INPUT_FILE = "human_input.jsonl"
REPORT_JSON = "auto_review.json"
REPORT_MD = "auto_review.md"

RECOGNITION = "recognition"
DECISION = "decision"

# 与 session_watch 熔断口径一致：同类 abstain 连续满 5 帧才记一段。
ABSTAIN_RUN_MIN = StallPolicy().abstain_run_min
# 财神未知段收录门槛：计数型（ticks 递增）连续未知 ≥2 帧，或整段以
# gate_unknown_caishen 停手收尾，或冻结型（ticks 不增长）长达 ≥5 行。
CAISHEN_TICKS_MIN = 2
CAISHEN_SEGMENT_MIN_LEN = 5

_SETTLE_KEYS = ("settle_samples", "fallback_pass_settle_samples")
_VERDICT_KEYS = ("semantic_verified", "fallback_pass_verified", "verified")
_REASON_KEYS = ("semantic_reason", "fallback_pass_reason", "reason")
_SIGNATURE_KEYS = ("concealed", "meld_groups", "total",
                   "claim_buttons", "settled", "selection_panel")


# ---------------------------------------------------------------------------
# 读取（带行号；容错口径同 session_watch.load_ledger_events）
# ---------------------------------------------------------------------------

def _load_rows(path: Path) -> tuple[list[tuple[int, dict]], int]:
    """逐行读 JSONL → ([(行号, dict)], 损坏行数)。文件缺失返回 ([], 0)。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    rows: list[tuple[int, dict]] = []
    corrupt = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if isinstance(item, dict):
            rows.append((line_no, item))
        else:
            corrupt += 1
    return rows, corrupt


def _find_header(rows: list[tuple[int, dict]]) -> Optional[dict]:
    for _line_no, item in rows:
        if _is_header(item):
            return item
    return None


def _ts_of(item: Mapping[str, Any]) -> Optional[str]:
    return item.get("ts") or item.get("at")


def _frame_of(item: Mapping[str, Any]) -> Optional[str]:
    for key in ("frame", "after_frame"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return Path(value).name
    return None


def _issue(category: str, klass: str, source: str, line_no: int,
           item: Optional[Mapping[str, Any]], excerpt: dict) -> dict:
    return {
        "category": category,
        "class": klass,
        "source": source,
        "line": line_no,
        "ts": _ts_of(item) if item else None,
        "frame": _frame_of(item) if item else None,
        "excerpt": excerpt,
    }


# ---------------------------------------------------------------------------
# 规则 1：stop 事件（gate + reason 原样引用）
# ---------------------------------------------------------------------------

_DECISION_STOP_GATES = {"max_actions"}


def _stop_class(gate: Optional[str]) -> str:
    if gate and (gate in _DECISION_STOP_GATES or "policy" in gate):
        return DECISION
    return RECOGNITION


def _screen_stops(rows, source: str) -> list[dict]:
    issues = []
    for line_no, item in rows:
        if item.get("event") != "stop":
            continue
        gate = item.get("gate")
        if gate is None and isinstance(item.get("gates"), Mapping):
            gate = item["gates"].get("stop_kind")  # follow 镜像的停手类别
        reason = item.get("reason")
        if reason is None:
            for holder in ("vision", "decision"):
                sub = item.get(holder)
                if isinstance(sub, Mapping) and sub.get("reason"):
                    reason = sub["reason"]
                    break
        excerpt: dict[str, Any] = {"gate": gate, "reason": reason}
        claim = item.get("claim")
        if isinstance(claim, Mapping):
            excerpt["claim_action"] = claim.get("action")
        if item.get("guard"):
            excerpt["guard"] = item.get("guard")
        issues.append(_issue("stop", _stop_class(gate), source,
                             line_no, item, excerpt))
    return issues


# ---------------------------------------------------------------------------
# 规则 2：claim 点后验证异常（settle_samples 未收敛 / 最终不符）
#
# 样本由 auto_play_loop._verify_claim_settled 逐帧落盘，位置：
# claim.auto_click.settle_samples（主 claim）、claim.chi_chain.
# settle_samples 与 claim.chi_chain.fallback_pass_settle_samples
# （吃链）。轮询语义：任一样本 verified=True 即成功返回，故样本
# 非空且无一 verified=True ⇔ 未收敛或落定后最终不符。
# ---------------------------------------------------------------------------

def _find_settle_lists(node: Any, path: str = "", depth: int = 0):
    found = []
    if depth > 5 or not isinstance(node, Mapping):
        return found
    for key in _SETTLE_KEYS:
        value = node.get(key)
        if isinstance(value, list) and value:
            found.append(((path + "." + key).strip("."), value, node))
    for key, value in node.items():
        if isinstance(value, Mapping):
            found.extend(_find_settle_lists(
                value, (path + "." + key).strip("."), depth + 1))
    return found


def _sample_signature(sample: Any) -> Optional[tuple]:
    if not isinstance(sample, Mapping) or "error" in sample:
        return None  # 异常样本是唯一哨兵，永不算落定
    return tuple(sample.get(key) for key in _SIGNATURE_KEYS)


def _screen_settle(rows, source: str) -> list[dict]:
    issues = []
    for line_no, item in rows:
        for path, samples, container in _find_settle_lists(item):
            if any(isinstance(s, Mapping) and s.get("verified") is True
                   for s in samples):
                continue
            signatures = [_sample_signature(s) for s in samples]
            settled_mismatch = (len(signatures) >= 2
                                and signatures[-1] is not None
                                and signatures[-1] == signatures[-2])
            verdict = next((container.get(k) for k in _VERDICT_KEYS
                            if container.get(k) is not None), None)
            reason = next((container.get(k) for k in _REASON_KEYS
                           if container.get(k)), None)
            last = samples[-1] if isinstance(samples[-1], Mapping) else {}
            excerpt = {
                "samples_path": path,
                "sample_count": len(samples),
                "verdict": verdict,
                "reason": reason,
                "detail": ("落定后最终不符" if settled_mismatch
                           else "未收敛（轮询内无样本通过验证）"),
                "last_sample": {k: last.get(k) for k in
                                ("attempt", "concealed", "meld_groups",
                                 "total", "verified", "reason", "error")
                                if k in last},
            }
            issue = _issue("claim_settle_anomaly", RECOGNITION, source,
                           line_no, item, excerpt)
            last_frame = last.get("frame")
            if isinstance(last_frame, str) and last_frame:
                issue["frame"] = Path(last_frame).name
            issues.append(issue)
    return issues


# ---------------------------------------------------------------------------
# 规则 3：面板恢复尝试及其成败（panel_recovery 标记）
# ---------------------------------------------------------------------------

def _screen_panel_recovery(rows, source: str) -> list[dict]:
    issues = []
    for line_no, item in rows:
        marker = item.get("panel_recovery")
        if not isinstance(marker, Mapping):
            continue
        outcome = marker.get("outcome")
        excerpt = {
            "outcome": outcome,
            "result": ("success" if outcome == "recovered"
                       else "planned" if outcome == "dry_run_ready"
                       else "not_recovered"),
            "decision": marker.get("decision"),
            "reason": marker.get("reason"),
            "tile_id": marker.get("tile_id"),
            "case": marker.get("case"),
        }
        issues.append(_issue("panel_recovery", RECOGNITION, source,
                             line_no, item, excerpt))
    return issues


# ---------------------------------------------------------------------------
# 规则 4：连续 abstain 段 ≥5（口径与 session_watch 熔断一致：
# status == "abstain"、以 _abstain_key 分组、会话头与 stop/error 打断）
# ---------------------------------------------------------------------------

def _screen_abstain_runs(rows, source: str) -> list[dict]:
    issues: list[dict] = []
    run_key: Optional[str] = None
    run: list[tuple[int, dict]] = []

    def flush() -> None:
        nonlocal run_key, run
        if run_key is not None and len(run) >= ABSTAIN_RUN_MIN:
            first_no, first = run[0]
            last_no = run[-1][0]
            excerpt = {"key": run_key, "run_length": len(run),
                       "line_start": first_no, "line_end": last_no,
                       "reason": first.get("reason")}
            issue = _issue("abstain_run", DECISION, source,
                           first_no, first, excerpt)
            issue["line_end"] = last_no
            issues.append(issue)
        run_key, run = None, []

    for line_no, item in rows:
        if _is_header(item) or item.get("event") in ("stop", "error"):
            flush()
            continue
        if item.get("status") == "abstain":
            key = _abstain_key(item)
            if key == run_key:
                run.append((line_no, item))
            else:
                flush()
                run_key, run = key, [(line_no, item)]
            continue
        flush()
    flush()
    return issues


# ---------------------------------------------------------------------------
# 规则 5：旁路卡住 / 外部推进疑似事件
# ---------------------------------------------------------------------------

def _screen_progress_suspicions(rows, source: str, *, clicks=(),
                                serial=None) -> list[dict]:
    """把 session_watch 的纯派生事件映射回原 JSONL 行号。"""
    if not rows:
        return []
    items = [item for _line_no, item in rows]
    issues: list[dict] = []
    for event in derive_progress_suspicions(items):
        index = event.get("event_index")
        if not isinstance(index, int) or not (0 <= index < len(rows)):
            continue
        line_no, item = rows[index]
        excerpt = {key: value for key, value in event.items()
                   if key not in ("event_index", "start_index", "end_index")}
        if event.get("type") == "external_advance_suspected":
            human = associate_human_click(event.get("ts"), clicks, serial)
            if human is not None:
                event = dict(event)
                event["type"] = "human_action_likely"
                event["base_type"] = "external_advance_suspected"
                event["human_click"] = human
                event["reason"] = (
                    "可信本家牌河推进前后存在同 serial、已核验雷电窗口内的"
                    "鼠标按下，且中间无 BOT action；高度疑似人工手点，但仍不"
                    "把单次鼠标观察当作游戏动作真值")
                excerpt = {key: value for key, value in event.items()
                           if key not in ("event_index", "start_index", "end_index")}
        if event.get("type") == "stuck_suspected":
            start_index = event.get("start_index")
            end_index = event.get("end_index")
            if isinstance(start_index, int) and 0 <= start_index < len(rows):
                # 同一持续卡壳的主行固定锚定 occurrence 起点；否则
                # session_watch 随新帧更新 event_index 会令 incident 指纹漂移。
                line_no = rows[start_index][0]
                excerpt["line_start"] = line_no
            if isinstance(end_index, int) and 0 <= end_index < len(rows):
                # 末行与末帧仍作为 latest_failure 的动态证据保留，但不参与
                # stuck_suspected 的稳定指纹。
                excerpt["line_end"] = rows[end_index][0]
                item = rows[end_index][1]
        issue = _issue(str(event.get("type")), DECISION, source,
                       line_no, item, excerpt)
        if "line_end" in excerpt:
            issue["line_end"] = excerpt["line_end"]
        issues.append(issue)
    return issues


# ---------------------------------------------------------------------------
# 规则 6：财神未知段（unknown_caishen_ticks 连续段）
# ---------------------------------------------------------------------------

def _screen_caishen(rows, source: str) -> list[dict]:
    issues: list[dict] = []
    segment: list[tuple[int, dict]] = []

    def flush() -> None:
        nonlocal segment
        if segment:
            ticks = [it.get("unknown_caishen_ticks") for _n, it in segment]
            ticks = [t for t in ticks if isinstance(t, int)]
            max_ticks = max(ticks) if ticks else 0
            last_item = segment[-1][1]
            ends_stop = (last_item.get("event") == "stop"
                         and last_item.get("gate") == "gate_unknown_caishen")
            if (max_ticks >= CAISHEN_TICKS_MIN or ends_stop
                    or len(segment) >= CAISHEN_SEGMENT_MIN_LEN):
                frozen = sum(1 for _n, it in segment
                             if "计数冻结" in str(it.get("reason") or ""))
                excerpt = {
                    "max_ticks": max_ticks,
                    "budget": segment[0][1].get("unknown_caishen_budget"),
                    "segment_length": len(segment),
                    "frozen_rows": frozen,
                    "ends_with_stop": ends_stop,
                    "line_start": segment[0][0],
                    "line_end": segment[-1][0],
                    "reason_first": segment[0][1].get("reason"),
                }
                issue = _issue("caishen_unknown_run", RECOGNITION, source,
                               segment[0][0], segment[0][1], excerpt)
                issue["line_end"] = segment[-1][0]
                issues.append(issue)
        segment = []

    for line_no, item in rows:
        if "unknown_caishen_ticks" in item:
            segment.append((line_no, item))
        else:
            flush()
    flush()
    return issues


# ---------------------------------------------------------------------------
# 规则 7：人工标记（user_flags.jsonl 全量列入）
# ---------------------------------------------------------------------------

def _trim(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _screen_user_flags(rows, source: str) -> list[dict]:
    issues = []
    for line_no, item in rows:
        klass = item.get("class")
        if klass not in (RECOGNITION, DECISION):
            klass = DECISION  # 人工标记默认进决策类审阅队列，行内自带 class 时以行内为准
        excerpt = {key: _trim(value) for key, value in item.items()}
        issues.append(_issue("user_flag", klass, source,
                             line_no, item, excerpt))
    return issues


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def review_session(session_dir, *, incident_root=None, human_input_path=None) -> dict:
    """筛查一个会话目录，写 auto_review.json/.md，返回摘要 dict。"""
    session_dir = Path(session_dir)
    actions_rows, actions_corrupt = _load_rows(session_dir / ACTIONS_FILE)
    follow_rows, follow_corrupt = _load_rows(session_dir / FOLLOW_FILE)
    flag_rows, flag_corrupt = _load_rows(session_dir / FLAGS_FILE)
    human_source = (Path(human_input_path) if human_input_path is not None
                    else session_dir / HUMAN_INPUT_FILE)
    human_clicks = load_human_clicks(human_source)

    header = _find_header(actions_rows) or _find_header(follow_rows) or {}
    mode = header.get("mode")
    dev = header.get("dev")
    serial = header.get("serial") or header.get("device_serial")

    if actions_rows:
        primary_rows, primary_name = actions_rows, ACTIONS_FILE
    else:
        primary_rows, primary_name = follow_rows, FOLLOW_FILE

    issues: list[dict] = []
    if primary_rows:
        issues += _screen_stops(primary_rows, primary_name)
        issues += _screen_settle(primary_rows, primary_name)
        issues += _screen_panel_recovery(primary_rows, primary_name)
        issues += _screen_abstain_runs(primary_rows, primary_name)
        issues += _screen_progress_suspicions(
            primary_rows, primary_name, clicks=human_clicks, serial=serial)
        issues += _screen_caishen(primary_rows, primary_name)
    issues += _screen_user_flags(flag_rows, FLAGS_FILE)
    issues.sort(key=lambda i: (i["source"], i["line"]))

    # Rule 8: an action event without a confirmed post-click transition is
    # never a success, even when the loop did not emit a terminal stop.
    for line_no, item in primary_rows:
        if item.get("event") == "action" and item.get("verified") is not True:
            issues.append(_issue(
                "action_unverified", DECISION, primary_name,
                line_no, item, {
                    "verified": item.get("verified"),
                    "before_count": item.get("before_count"),
                    "after_count": item.get("after_count"),
                    "reason": item.get("reason"),
                    "action_id": item.get("action_id"),
                    "action_source": item.get("action_source") or item.get("source"),
                }))
    issues.sort(key=lambda i: (i["source"], i["line"]))

    by_category = dict(Counter(i["category"] for i in issues))
    by_class = dict(Counter(i["class"] for i in issues))

    stop_gates = [i["excerpt"].get("gate") for i in issues
                  if i["category"] == "stop"]
    blockers: list[str] = []
    if dev is True:
        blockers.append(
            "开发局会话（dev=true）：按数据隔离口径，开发局数据单独"
            "标记、不进训练语料")
    elif dev is not False:
        blockers.append("dev 未明确标记为 false：来源未知，禁止自动入训练语料")
    if actions_corrupt or follow_corrupt or flag_corrupt:
        blockers.append(
            f"台账损坏：actions={actions_corrupt} follow={follow_corrupt} "
            f"flags={flag_corrupt}，须复核原始记录")
    if not primary_rows:
        blockers.append("无有效会话事件：无法审查完整性")
    if not serial:
        blockers.append("会话头缺少设备 serial，无法追溯来源")
    if issues:
        blockers.append(
            f"自动复盘检出 {len(issues)} 条未人工结案问题：仅入待复核池")
    if not mode:
        blockers.append("会话头缺少 mode：无法确认玩法口径，不进训练语料")
    if stop_gates:
        gates_text = ", ".join(str(g) for g in stop_gates)
        blockers.append(
            f"存在未决 stop 事件 {len(stop_gates)} 个（gate：{gates_text}）："
            "台账无结案标记，人工结案前不进训练语料")
    corpus_eligible = not blockers

    summary: dict[str, Any] = {
        "session_dir": str(session_dir),
        "mode": mode,
        "dev": dev,
        "serial": serial,
        "corpus_eligible": corpus_eligible,
        "corpus_status": "candidate_review" if corpus_eligible else "blocked",
        "train_approved": False,
        "train_approval_note": "语料候选不等于训练授权；须独立金标和人工审核",
        "corpus_blockers": blockers,
        "issue_count": len(issues),
        "by_category": by_category,
        "by_class": by_class,
        "issues": issues,
        "sources": {
            ACTIONS_FILE: {"rows": len(actions_rows),
                           "corrupt_lines": actions_corrupt},
            FOLLOW_FILE: {"rows": len(follow_rows),
                          "corrupt_lines": follow_corrupt},
            FLAGS_FILE: {"rows": len(flag_rows),
                         "corrupt_lines": flag_corrupt},
            HUMAN_INPUT_FILE: {"rows": len(human_clicks),
                               "corrupt_lines": 0},
        },
        "primary_ledger": primary_name if primary_rows else None,
        "report_json": str(session_dir / REPORT_JSON),
        "report_md": str(session_dir / REPORT_MD),
        "generator": "auto_review/v1",
    }
    incident_categories = {
        "stop", "action_unverified", "stuck_suspected",
        "external_advance_suspected", "human_action_likely",
    }
    actionable_failures = [
        issue for issue in issues
        if issue.get("category") in incident_categories]
    root = (Path(incident_root) if incident_root is not None
            else session_dir / "incidents")
    repo_root = Path(__file__).resolve().parents[2]
    repo_version = read_repo_version(repo_root)
    incident_summaries: list[dict] = []
    for failure in actionable_failures:
        failure_line = int(failure.get("line", 1))
        row_index = next(
            (index for index, (line, _item) in enumerate(primary_rows)
             if line == failure_line),
            max(0, failure_line - 1))
        recent = [item for _line, item in primary_rows[
            max(0, row_index - 15):row_index + 15]]
        try:
            incident = create_incident(
                failure, session_dir, recent, incident_root=root,
                serial=serial, repo_version=repo_version,
                process_info=header)
            incident_summaries.append({
                "incident_id": incident.get("incident_id"),
                "status": incident.get("status"),
                "category": failure.get("category"),
                "line": failure.get("line"),
                "path": str(root / str(incident.get("incident_id"))),
            })
        except OSError as exc:
            incident_summaries.append({
                "status": "write_failed", "reason": type(exc).__name__,
                "category": failure.get("category"),
                "line": failure.get("line"),
            })
    summary["incidents"] = incident_summaries
    summary["incident"] = next(
        (item for item in incident_summaries if item.get("status") == "open"),
        incident_summaries[0] if incident_summaries else None)
    try:
        (session_dir / REPORT_JSON).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (session_dir / REPORT_MD).write_text(
            render_review_md(summary), encoding="utf-8")
    except OSError:
        summary["report_json"] = None
        summary["report_md"] = None
    return summary


def find_session_dirs(root) -> list[Path]:
    """根目录下所有含台账文件的会话目录（根目录本身是会话目录时返回它）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    names = (ACTIONS_FILE, FOLLOW_FILE, FLAGS_FILE)
    if any((root / name).exists() for name in names):
        return [root]
    return sorted(path for path in root.rglob("*")
                  if path.is_dir()
                  and any((path / name).exists() for name in names))


def review_root(root) -> list[dict]:
    """批量复盘根目录下全部会话目录，逐个返回摘要。"""
    return [review_session(d) for d in find_session_dirs(root)]


# ---------------------------------------------------------------------------
# Markdown 证据包
# ---------------------------------------------------------------------------

_CATEGORY_TITLES = {
    "stop": "硬停手 stop",
    "claim_settle_anomaly": "claim 点后验证异常",
    "abstain_run": "连续弃权段",
    "caishen_unknown_run": "财神未知段",
    "panel_recovery": "面板恢复尝试",
    "user_flag": "人工标记",
    "stuck_suspected": "卡壳疑似",
    "external_advance_suspected": "外部推进疑似",
    "human_action_likely": "高度疑似人工手点",
    "action_unverified": "BOT 动作未验证",
}

_FOLLOWUP_TEXT = {
    "stop": "出现硬停手：人工回看停手帧与 reason 原文，判定属识别证据"
            "不足还是门禁口径过严；结案前该场不进语料。",
    "claim_settle_anomaly": "claim 点后落定样本未收敛/最终不符：模式与"
                            "「压暗/动画帧计数误读」相符，也可能是点击未"
                            "实际生效；人工对照样本帧逐帧判定。",
    "abstain_run": "同类弃权连续成段：可能为行动窗口静默堵住（如 claim "
                   "窗口无人应答）或回合门长期不放行；人工回看该段首尾帧。",
    "caishen_unknown_run": "财神未知连续段：先按发生时段区分等候室/局间"
                           "过渡（预期行为）与局中真丢失（疑似指示牌模板"
                           "缺失或读数异常）。",
    "panel_recovery": "面板恢复尝试：核对 outcome 与当时帧，确认恢复放行"
                      "口径是否过严（弃权过多）或过松。",
    "user_flag": "人工标记：按标记原文逐条归因，确认属识别类还是决策类"
                 "后再决定后续。",
    "stuck_suspected": "按 stall_class 与 threshold_s 定位首个长期不变状态；"
                       "先修第一原因，再重新实机回归。",
    "external_advance_suspected": "可信本家牌河已推进但附近没有 BOT action；"
                                  "只能认定外部推进，不能猜具体操作者。",
    "human_action_likely": "同 serial 雷电窗口内鼠标按下与可信推进时间相邻；"
                           "高度疑似人工手点，仍需结合帧证据确认。",
    "action_unverified": "BOT 发出动作但点后迁移未验证；按动作 ID 回看前后帧，"
                         "结案前不得视为成功。",
}


def render_review_md(summary: dict) -> str:
    name = Path(summary["session_dir"]).name
    by_class = summary["by_class"]
    lines = [f"# 自动复盘 · {name}", ""]
    lines.append(f"- 会话目录：`{summary['session_dir']}`")
    lines.append(f"- serial：{summary.get('serial')} · mode："
                 f"{summary.get('mode')} · dev：{summary.get('dev')}")
    if summary["corpus_eligible"]:
        lines.append("- 训练语料准入：✅ 可进语料（无拦截项）")
    else:
        lines.append("- 训练语料准入：❌ 不可进语料")
        for blocker in summary["corpus_blockers"]:
            lines.append(f"  - 拦截：{blocker}")
    src = summary["sources"]
    lines.append(
        f"- 台账行数：actions {src[ACTIONS_FILE]['rows']} / follow "
        f"{src[FOLLOW_FILE]['rows']} / user_flags {src[FLAGS_FILE]['rows']}"
        f"（损坏行：actions {src[ACTIONS_FILE]['corrupt_lines']} / follow "
        f"{src[FOLLOW_FILE]['corrupt_lines']} / user_flags "
        f"{src[FLAGS_FILE]['corrupt_lines']}）")
    lines.append(f"- 疑似问题：{summary['issue_count']} 条"
                 f"（识别类 {by_class.get(RECOGNITION, 0)} / "
                 f"决策类 {by_class.get(DECISION, 0)}）")
    lines.append("")
    lines.append("## 按类别计数")
    if summary["by_category"]:
        for category, count in sorted(summary["by_category"].items()):
            title = _CATEGORY_TITLES.get(category, category)
            lines.append(f"- {title}（{category}）：{count}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 问题明细")
    for klass, title in ((RECOGNITION, "识别类 recognition"),
                         (DECISION, "决策类 decision")):
        group = [i for i in summary["issues"] if i["class"] == klass]
        lines.append(f"### {title}（{len(group)}）")
        if not group:
            lines.append("- 无")
        for issue in group:
            head = (f"- [{_CATEGORY_TITLES.get(issue['category'], issue['category'])}] "
                    f"{issue['source']} 第 {issue['line']} 行")
            if issue.get("line_end"):
                head += f"–{issue['line_end']}"
            if issue.get("ts"):
                head += f" · {issue['ts']}"
            if issue.get("frame"):
                head += f" · 帧 {issue['frame']}"
            lines.append(head)
            lines.append(f"  - 摘录：{json.dumps(issue['excerpt'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 候选后续（仅供人工审核）")
    present = [c for c in _FOLLOWUP_TEXT if summary["by_category"].get(c)]
    if present:
        for category in present:
            lines.append(f"- （{category} ×{summary['by_category'][category]}）"
                         f"{_FOLLOWUP_TEXT[category]}")
    else:
        lines.append("- 本场未筛出疑似问题，无候选后续。")
    lines.append("")
    lines.append("> 本报告由 auto_review 自动生成：只筛查疑似问题并整理"
                 "证据，不含任何自动训练、自动改代码或自动上线动作。")
    return "\n".join(lines) + "\n"
