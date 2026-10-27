"""Offline cycle metrics from explicit logs; no bot or device imports.

Frame filenames are capture-return wall-clock proxies. They do not provide
physical tap, game-change, or final-verification timestamps.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re


BOUNDARIES = {"stop", "error", "round_reset", "round_transition", "left_table",
              "session_start", "session_end"}
LATENCY_FIELDS = ("capture_ms", "recognize_ms", "advise_ms", "cycle_ms",
                  "capture_to_render_ms", "total_ms", "render_ms", "poll_wait_ms", "period_ms")
LIMITS = [
    "action elapsed is the producer's tick-work metric, not physical first-tap to final verification; many claim records omit it.",
    "before filenames are host wall time after screenshot-provider return, before PNG saving; they are not tick-start or device timestamps.",
    "Cycles pair consecutive observed frame groups inside one input file, never across explicit boundaries or missing capture observations.",
    "cycle minus elapsed is a mixed-anchor residual, not measured sleep; negative residuals remain unchanged.",
    "Non-forward filename time is a clock anomaly, retained separately and excluded from normal cycle distributions.",
    "UI total_ms is producer-specific. assist_window._finish_frame normally records poll_wait + capture_to_render; this CLI cannot verify that producer from a generic log.",
    "A source-verified visual timer is only a logged visual reading. Current player, phase, window and observation-age ownership remain unproven.",
    "Actual end-to-end action p95/p99 remain null without complete same-transaction monotonic anchors.",
]


def number(value, *, nonnegative=False):
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "boolean" if isinstance(value, bool) else "non_numeric"
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None, "non_finite"
    if not math.isfinite(result):
        return None, "non_finite"
    if nonnegative and result < 0:
        return None, "negative"
    return result, None


def stats(values):
    ordered = sorted(n for value in values for n, reason in [number(value)] if reason is None)
    if not ordered:
        return {"n": 0, "min_ms": None, "p50_ms": None, "p95_ms": None,
                "p99_ms": None, "max_ms": None}
    def quantile(p):
        index = p * (len(ordered) - 1)
        lo = int(index)
        return round(ordered[lo] * (1 - (index - lo))
                     + ordered[min(lo + 1, len(ordered) - 1)] * (index - lo), 6)
    return {"n": len(ordered), "min_ms": round(ordered[0], 6), "p50_ms": quantile(.5),
            "p95_ms": quantile(.95), "p99_ms": quantile(.99), "max_ms": round(ordered[-1], 6)}


def timing(rows, key, *, positive=False):
    values, invalid, missing, excluded_zero = [], Counter(), 0, 0
    for _, row in rows:
        value, reason = number(row.get(key), nonnegative=True)
        if reason == "missing":
            missing += 1
        elif reason:
            invalid[reason] += 1
        elif positive and value == 0:
            excluded_zero += 1
        else:
            values.append(value)
    return {**stats(values), "records": len(rows), "missing": missing,
            "invalid": sum(invalid.values()), "invalid_reasons": dict(invalid),
            "excluded_zero": excluded_zero}


def read_jsonl(path):
    # Hash and parse the same bytes; never re-open evidence just for its hash.
    raw = path.read_bytes()
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: expected a JSON object")
        rows.append((line_no, value))
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "lines": len(raw.splitlines()), "records": len(rows)}, rows


def frame_stamp(frame):
    if not isinstance(frame, str):
        return None
    match = re.match(r"^(\d{8}_\d{6}_\d{6})_before_", PureWindowsPath(frame).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S_%f")
    except ValueError:
        return None


def frame_groups(rows):
    groups, diagnostics = [], Counter()
    for line_no, row in rows:
        signal = row.get("round_signal")
        boundary = (row.get("event") in BOUNDARIES
                    or ("started_at" in row and "session" in row)
                    or (isinstance(signal, dict) and signal.get("state") not in (None, "same")))
        stamp = frame_stamp(row.get("frame"))
        missing_observation = (stamp is None and
                               (row.get("frame") is not None or row.get("event") in ("action", "idle")))
        if boundary or missing_observation:
            diagnostics["explicit_boundaries" if boundary else "unassociated_capture_rows"] += 1
            if groups and groups[-1] is not None:
                groups.append(None)
            continue
        if stamp is None:  # Non-observation metadata does not fabricate a capture.
            continue
        if groups and groups[-1] is not None and groups[-1]["frame"] == row["frame"]:
            groups[-1]["rows"].append((line_no, row))
            diagnostics["same_frame_additional_rows"] += 1
        else:
            groups.append({"frame": row["frame"], "stamp": stamp, "rows": [(line_no, row)]})
    return groups, diagnostics


def is_idle(group):
    return all(row.get("event") == "idle" for _, row in group["rows"])


def timer_layers(rows):
    statuses, raw_seconds, values, source_verified = Counter(), Counter(), [], 0
    count = 0
    for _, row in rows:
        state = row.get("table_state")
        if not isinstance(state, dict) or "countdown_status" not in state:
            continue
        count += 1
        statuses[str(state.get("countdown_status"))] += 1
        seconds = state.get("countdown_seconds")
        raw_seconds[str(seconds)] += 1
        evidence = state.get("countdown_evidence")
        verified = (isinstance(evidence, dict) and evidence.get("source") == "central_timer_native"
                    and evidence.get("source_verified") is True)
        source_verified += verified
        if verified and isinstance(seconds, int) and not isinstance(seconds, bool) and seconds >= 0:
            values.append(seconds)
    return {
        "raw_logged": {"records": count, "status_counts": dict(statuses),
                       "seconds_counts_unverified": dict(raw_seconds)},
        "source_verified_visual_timer": {
            "scope": "as explicitly logged, without image re-evaluation",
            "records": source_verified, "readable_records": len(values),
            "seconds_counts": dict(Counter(str(value) for value in values)),
            "minimum_displayed_seconds": min(values) if values else None,
        },
        "unverified_source_records": count - source_verified,
        "current_action_budget": {"minimum_seconds": None, "ownership": "unknown",
                                  "phase": "unknown", "window": "unknown", "observation_age": "unknown"},
    }


def action_metrics(path):
    source, rows = read_jsonl(path)
    actions = [(line, row) for line, row in rows if row.get("event") == "action"]
    claims = [(line, row) for line, row in actions if isinstance(row.get("claim"), dict)]
    groups, diagnostics = frame_groups(rows)
    cycles, residuals, idle_cycles, examples, anomalies = [], [], [], [], []
    for index, group in enumerate(groups):
        if group is None:
            continue
        following = groups[index + 1] if index + 1 < len(groups) else None
        if following is None:
            diagnostics["unpaired_frame_groups"] += 1
            continue
        delta = (following["stamp"] - group["stamp"]).total_seconds() * 1000
        action_rows = [(line, row) for line, row in group["rows"] if row.get("event") == "action"]
        if delta <= 0:
            diagnostics["nonforward_wall_cycles"] += 1
            if len(anomalies) < 20:
                anomalies.append({"from_line": group["rows"][0][0],
                                  "to_line": following["rows"][0][0], "wall_cycle_ms": delta})
            continue
        if len(action_rows) > 1:
            diagnostics["ambiguous_action_frame_groups"] += 1
            continue
        if action_rows:
            line, row = action_rows[0]
            elapsed, reason = number(row.get("elapsed_ms"), nonnegative=True)
            if reason:
                diagnostics["action_groups_without_valid_elapsed"] += 1
                continue
            residual = delta - elapsed
            cycles.append(delta)
            residuals.append(residual)
            if len(examples) < 3 or (residual < 0 and sum(e["mixed_anchor_residual_ms"] < 0 for e in examples) < 3):
                examples.append({"action_line": line, "next_frame_line": following["rows"][0][0],
                                 "before_frame": group["frame"], "next_before_frame": following["frame"],
                                 "elapsed_ms": elapsed, "capture_return_proxy_cycle_ms": delta,
                                 "mixed_anchor_residual_ms": residual})
        elif is_idle(group) and is_idle(following):
            idle_cycles.append(delta)
    return {
        "source": source, "statistical_unit": "action records; cadence pairs consecutive distinct frame groups",
        "action_elapsed": timing(actions, "elapsed_ms"),
        "claim_action_elapsed": timing(claims, "elapsed_ms"),
        "capture_return_proxy_after_timed_action": stats(cycles),
        "mixed_anchor_residual_not_sleep": {**stats(residuals),
                                           "negative_samples": sum(v < 0 for v in residuals), "clipped": False},
        "idle_to_idle_capture_return_proxy": stats(idle_cycles),
        "grouping_diagnostics": dict(diagnostics), "clock_anomalies": anomalies,
        "representative_pairs": examples, "countdown": timer_layers(rows),
    }


def latency_metrics(path):
    source, rows = read_jsonl(path)
    return {"source": source, "scope": "reported latency fields; producer semantics not verified",
            "total_ms_semantics": "producer-specific; not assumed to include poll wait",
            "fields": {key: timing(rows, key, positive=(key == "period_ms")) for key in LATENCY_FIELDS}}


def countdown_evidence(path):
    if path is None:
        return {"status": "not_provided", "calibration": None, "independent_validation": None,
                "limits": [], "current_action_budget_minimum_seconds": None}
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema") != "countdown_source_evidence/v1":
        raise ValueError(f"{path}: unsupported countdown evidence schema")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{path}: countdown evidence records missing")
    split_rows = {"calibration": [], "validation": [], "unclassified": []}
    for line, record in enumerate(records, 1):
        if not isinstance(record, dict) or not isinstance(record.get("stage"), dict):
            raise ValueError(f"{path}: invalid countdown evidence record {line}")
        split = record.get("split")
        split_rows[split if split in split_rows else "unclassified"].append(
            (line, {"table_state": record["stage"]}))
    limits = data.get("limits", [])
    if not isinstance(limits, list) or any(not isinstance(item, str) for item in limits):
        raise ValueError(f"{path}: invalid countdown limits")
    return {"status": "provided",
            "source": {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                       "lines": len(raw.splitlines()), "records": len(records)},
            "calibration": timer_layers(split_rows["calibration"]),
            "independent_validation": timer_layers(split_rows["validation"]),
            "unclassified": timer_layers(split_rows["unclassified"]),
            "limits": limits, "current_action_budget_minimum_seconds": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions-log", type=Path, action="append", default=[])
    parser.add_argument("--latency-log", type=Path, action="append", default=[])
    parser.add_argument("--countdown-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="New JSON report; existing output is never overwritten")
    args = parser.parse_args(argv)
    if not args.actions_log and not args.latency_log:
        parser.error("provide at least one --actions-log or --latency-log")
    inputs = args.actions_log + args.latency_log + ([args.countdown_evidence] if args.countdown_evidence else [])
    if len({path.resolve() for path in inputs}) != len(inputs):
        parser.error("each input path may be supplied only once")
    try:
        report = {
            "schema": "cycle_metrics/v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "quantile_method": "sorted linear interpolation p*(n-1); milliseconds",
            "actions": [action_metrics(path) for path in args.actions_log],
            "latency_logs": [latency_metrics(path) for path in args.latency_log],
            "countdown_evidence": countdown_evidence(args.countdown_evidence),
            "actual_end_to_end_action_p95_ms": None, "actual_end_to_end_action_p99_ms": None,
            "limitations": LIMITS,
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    print(str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
