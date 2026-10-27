"""Synthetic log-contract tests only; no pixels, bot imports or device IO."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/analyze_cycle_metrics.py"
spec = importlib.util.spec_from_file_location("cycle_metrics_under_test", SCRIPT)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
A = r"D:\synthetic\20261027_120000_000000_before_a.png"
B = r"D:\synthetic\20261027_120002_000000_before_b.png"


def write_log(tmp_path, rows, name="actions.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_same_frame_idle_cannot_steal_action_cycle(tmp_path):
    path = write_log(tmp_path, [
        {"event": "action", "frame": A, "elapsed_ms": 800},
        {"event": "idle", "frame": A},
        {"event": "idle", "frame": B},
    ])
    report = metrics.action_metrics(path)
    assert report["action_elapsed"]["n"] == 1
    assert report["capture_return_proxy_after_timed_action"]["p50_ms"] == 2000
    assert report["mixed_anchor_residual_not_sleep"]["p50_ms"] == 1200
    assert report["idle_to_idle_capture_return_proxy"]["n"] == 0
    assert report["representative_pairs"][0]["action_line"] == 1


def test_multiple_actions_on_one_frame_are_explicitly_ambiguous(tmp_path):
    report = metrics.action_metrics(write_log(tmp_path, [
        {"event": "action", "frame": A, "elapsed_ms": 800},
        {"event": "action", "frame": A, "elapsed_ms": 900},
        {"event": "idle", "frame": B},
    ]))
    assert report["action_elapsed"]["n"] == 2
    assert report["capture_return_proxy_after_timed_action"]["n"] == 0
    assert report["grouping_diagnostics"]["ambiguous_action_frame_groups"] == 1
    assert report["idle_to_idle_capture_return_proxy"]["n"] == 0


def test_invalid_elapsed_consistent_in_counts_pairs_and_json(tmp_path):
    invalid = [False, True, float("nan"), float("inf"), float("-inf"), "800", -1]
    rows = [{"event": "action", "elapsed_ms": value, "claim": {"action": "pass"}} for value in invalid]
    rows += [{"event": "action"}, {"event": "action", "elapsed_ms": None},
             {"event": "action", "frame": A, "elapsed_ms": 3000}, {"event": "idle", "frame": B}]
    report = metrics.action_metrics(write_log(tmp_path, rows))
    assert report["action_elapsed"]["records"] == 10
    assert report["action_elapsed"]["n"] == 1
    assert report["action_elapsed"]["missing"] == 2
    assert report["action_elapsed"]["invalid"] == 7
    assert report["claim_action_elapsed"]["invalid"] == 7
    assert report["mixed_anchor_residual_not_sleep"]["p50_ms"] == -1000
    assert report["mixed_anchor_residual_not_sleep"]["negative_samples"] == 1
    json.dumps(report, allow_nan=False)
    assert metrics.stats([None, False, True, float("nan")])["n"] == 0


@pytest.mark.parametrize("boundary", [
    {"event": "stop"}, {"event": "error", "error": "capture_failed"},
    {"event": "round_reset"}, {"session": "new", "started_at": "now"},
    {"event": "idle"}, {"event": "idle", "frame": "invalid.png"},
    {"event": "idle", "frame": A, "round_signal": {"state": "round_suspect"}},
])
def test_no_proxy_cycle_across_missing_capture_or_runtime_boundary(tmp_path, boundary):
    report = metrics.action_metrics(write_log(tmp_path, [
        {"event": "action", "frame": A, "elapsed_ms": 800}, boundary,
        {"event": "idle", "frame": B},
    ]))
    assert report["capture_return_proxy_after_timed_action"]["n"] == 0
    assert report["mixed_anchor_residual_not_sleep"]["n"] == 0


def test_nonforward_wall_clock_is_preserved_as_anomaly(tmp_path):
    report = metrics.action_metrics(write_log(tmp_path, [
        {"event": "action", "frame": B, "elapsed_ms": 800},
        {"event": "idle", "frame": A},
    ]))
    assert report["capture_return_proxy_after_timed_action"]["n"] == 0
    assert report["clock_anomalies"][0]["wall_cycle_ms"] == -2000
    assert report["grouping_diagnostics"]["nonforward_wall_cycles"] == 1


def test_latency_invalid_values_and_producer_unknown(tmp_path):
    path = write_log(tmp_path, [
        {"capture_ms": False, "total_ms": float("nan"), "period_ms": 0},
        {"capture_ms": 20, "total_ms": 25, "period_ms": "not a duration"},
    ])
    report = metrics.latency_metrics(path)
    assert report["fields"]["capture_ms"]["n"] == 1
    assert report["fields"]["capture_ms"]["invalid"] == 1
    assert report["fields"]["total_ms"]["invalid"] == 1
    assert report["fields"]["period_ms"]["invalid"] == 1
    assert report["fields"]["period_ms"]["excluded_zero"] == 1
    assert "not assumed" in report["total_ms_semantics"]
    json.dumps(report, allow_nan=False)


def test_visual_timer_never_becomes_current_action_budget():
    rows = [
        (1, {"table_state": {"countdown_status": "present", "countdown_seconds": 8}}),
        (2, {"table_state": {"countdown_status": "low", "countdown_seconds": 1,
                            "countdown_evidence": {"source": "central_timer_native", "source_verified": True}}}),
    ]
    report = metrics.timer_layers(rows)
    assert report["unverified_source_records"] == 1
    assert report["source_verified_visual_timer"]["minimum_displayed_seconds"] == 1
    assert report["current_action_budget"]["minimum_seconds"] is None
    assert report["current_action_budget"]["ownership"] == "unknown"


def test_optional_countdown_evidence_keeps_splits_and_limits(tmp_path):
    records = []
    for split, seconds in [("calibration", 1), ("validation", 2)]:
        records.append({"split": split, "stage": {
            "countdown_status": "present", "countdown_seconds": seconds,
            "countdown_evidence": {"source": "central_timer_native", "source_verified": True}}})
    path = tmp_path / "timer.json"
    raw = json.dumps({"schema": "countdown_source_evidence/v1", "records": records,
                      "limits": ["not blinded", "final calibration frame changed player"]}).encode()
    path.write_bytes(raw)
    report = metrics.countdown_evidence(path)
    assert report["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["calibration"]["source_verified_visual_timer"]["minimum_displayed_seconds"] == 1
    assert report["independent_validation"]["source_verified_visual_timer"]["minimum_displayed_seconds"] == 2
    assert report["limits"] == ["not blinded", "final calibration frame changed player"]
    assert report["current_action_budget_minimum_seconds"] is None


def test_cli_works_without_stage_paths_and_never_overwrites(tmp_path, monkeypatch):
    path = write_log(tmp_path, [{"event": "action", "frame": A, "elapsed_ms": 800},
                               {"event": "idle", "frame": B}])
    output = tmp_path / "report.json"
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    assert metrics.main(["--actions-log", str(path), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["actions"][0]["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert report["countdown_evidence"]["status"] == "not_provided"
    assert report["countdown_evidence"]["independent_validation"] is None
    assert report["actual_end_to_end_action_p95_ms"] is None
    assert report["actual_end_to_end_action_p99_ms"] is None
    saved = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        metrics.main(["--actions-log", str(path), "--output", str(output)])
    assert exc.value.code == 2 and output.read_bytes() == saved and path.read_bytes() == before


def test_cli_requires_explicit_logs_and_rejects_duplicate_inputs(tmp_path):
    with pytest.raises(SystemExit):
        metrics.main(["--output", str(tmp_path / "report.json")])
    path = write_log(tmp_path, [])
    with pytest.raises(SystemExit):
        metrics.main(["--actions-log", str(path), "--actions-log", str(path),
                      "--output", str(tmp_path / "report.json")])
    assert not (tmp_path / "report.json").exists()
