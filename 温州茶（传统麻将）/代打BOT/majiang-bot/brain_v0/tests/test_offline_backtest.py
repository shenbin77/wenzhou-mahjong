# -*- coding: utf-8 -*-
"""HandRecord schema 加载 + 无 actual 回测 + 固定报告必填键 + frame_diff。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BRAIN = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BRAIN / "schemas" / "hand_record.v0.json"
REPORT_SCHEMA_PATH = BRAIN / "schemas" / "backtest_report.v0.json"

import sys
sys.path.insert(0, str(BRAIN / "offline_tools"))
from offline_backtest import (  # noqa: E402
    SCHEMA_ID,
    REPORT_SCHEMA_ID,
    REPORT_REQUIRED_KEYS,
    TARGET_IDS,
    build_fixed_report,
    decisions_jsonl_to_hand_record,
    evaluate_decision,
    hand_record_from_analyze,
    infer_actual_by_frame_diff,
    load_hand_record,
    load_json_schema,
    multiset_missing_one,
    round_from_meta,
    run_backtest,
    write_report,
)


def test_schema_file_loads_and_marks_v0():
    schema = load_json_schema(SCHEMA_PATH)
    assert schema["title"] == "HandRecord v0"
    assert "hand_record.v0" in schema.get("$id", "")
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert raw["properties"]["schema"]["const"] == SCHEMA_ID
    dp = raw["$defs"]["decision_point"]["properties"]
    assert "actual_discard" in dp
    assert "actual_discard_source" in dp
    assert set(dp["actual_discard_source"]["enum"]) == {
        "manual_gold",
        "frame_diff",
        "none",
    }
    assert "suggested_discard" in dp
    # 识牌对齐：seats / user_seat / dealer_name
    rp = raw["properties"]["round"]["properties"]
    assert "seats" in rp
    assert "user_seat" in rp
    assert "dealer_name" in rp
    # 副露过渡仅 unknown
    meld_kind = dp["open_melds"]["items"]["properties"]["kind"]
    assert meld_kind.get("const") == "unknown"
    assert 55 in TARGET_IDS
    assert set(TARGET_IDS) >= set(range(1, 10)) | set(range(21, 30)) | set(range(41, 50)) | {55}


def test_backtest_report_schema_required_keys():
    raw = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert raw["properties"]["schema"]["const"] == REPORT_SCHEMA_ID
    assert list(raw["required"]) == list(REPORT_REQUIRED_KEYS)


def test_decisions_adapt_marks_actual_missing(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        json.dumps({
            "kind": "decision",
            "hand14": [1, 2, 4, 5, 6, 8, 21, 22, 24, 26, 41, 43, 45, 55],
            "played_counts": {},
            "turn_no": 1,
            "open_meld_count": 0,
            "suggested_discard": 8,
            "shanten": 2,
        }, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    rec = decisions_jsonl_to_hand_record(path, user_account="晓红")
    assert rec["schema"] == SCHEMA_ID
    assert rec["round"]["user_account"] == "晓红"
    assert len(rec["decisions"]) == 1
    dp = rec["decisions"][0]
    assert dp["actual_missing"] is True
    assert dp["actual_discard"] is None
    assert dp["actual_discard_source"] == "none"
    assert dp["suggested_discard"] == 8
    assert dp["joker"] == 55
    assert dp["is_hand14"] is True
    assert dp["open_melds"] == []


def test_run_backtest_no_actual_does_not_crash_and_rate_na(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    hand = [1, 2, 4, 5, 6, 8, 21, 22, 24, 26, 41, 43, 45, 55]
    path.write_text(
        "\n".join([
            json.dumps({
                "kind": "decision",
                "hand14": hand,
                "played_counts": {},
                "turn_no": 1,
                "open_meld_count": 0,
                "suggested_discard": 8,
                "shanten": 2,
            }, ensure_ascii=False),
            json.dumps({
                "kind": "decision",
                "hand14": hand,
                "played_counts": {},
                "turn_no": 1,
                "open_meld_count": 0,
                "suggested_discard": 8,
                "shanten": 2,
            }, ensure_ascii=False),
        ])
        + "\n",
        encoding="utf-8",
    )
    rec = load_hand_record(path, user_account="晓红")
    result = run_backtest(rec)
    s = result["summary"]
    assert s["n_decisions"] == 2
    assert s["n_actual_missing"] == 2
    assert s["n_comparable"] == 0
    assert s["agreement_rate"] is None
    assert "actual_missing" in (s["agreement_rate_note"] or "")
    assert all(r["bot_action"] and r["bot_action"]["type"] == "discard" for r in result["rows"])
    assert all(r["agree"] is None for r in result["rows"])
    assert all(r["error"] is None for r in result["rows"])
    jp, mp = write_report(result, tmp_path, "bt_test")
    assert jp.is_file() and mp.is_file()
    assert "N/A" in mp.read_text(encoding="utf-8")
    report = json.loads(jp.read_text(encoding="utf-8"))
    assert report["schema"] == REPORT_SCHEMA_ID
    for k in REPORT_REQUIRED_KEYS:
        assert k in report


def test_fixed_report_required_keys_locked():
    fake = {
        "summary": {
            "schema": SCHEMA_ID,
            "generated_at": "t",
            "round_uid": "r",
            "user_account": "晓红",
            "n_decisions": 0,
            "n_errors": 0,
            "n_actual_missing": 0,
            "n_comparable": 0,
            "n_agree": 0,
            "agreement_rate": None,
            "agreement_rate_note": "N/A",
            "shanten_after_bot_avg": None,
            "shanten_after_actual_avg": None,
            "ukeire_count_bot_avg": None,
            "ukeire_count_actual_avg": None,
            "ting_count_avg": None,
        },
        "rows": [],
        "record_notes": None,
    }
    report = build_fixed_report(
        data_sources=["unit"],
        round_results=[fake],
        user_account="晓红",
    )
    for k in REPORT_REQUIRED_KEYS:
        assert k in report
    assert report["n_rounds"] == 1
    assert report["agree_rate_display"] == "N/A"
    assert isinstance(report["blockers"], list)
    assert isinstance(report["points"], list)


def test_evaluate_with_actual_computes_agree():
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
    row = evaluate_decision({
        "turn_no": 2,
        "hand": hand,
        "open_meld_count": 0,
        "played_counts": {},
        "is_hand14": True,
        "joker": 55,
        "actual_discard": 25,
        "actual_discard_source": "manual_gold",
        "actual_missing": False,
    })
    assert row["error"] is None
    assert row["bot_action"]["tile"] in (25, 47)
    assert row["agree"] in (True, False)
    assert row["shanten_after_bot"] == 0
    assert row["shanten_after_actual"] == 0
    assert row["ukeire_count_bot"] is not None


def test_multiset_missing_one_and_frame_diff():
    assert multiset_missing_one([1, 2, 2, 3], [1, 2, 3]) == 2
    assert multiset_missing_one([1, 2, 3], [1, 2, 3]) is None
    assert multiset_missing_one([1, 2, 3, 4], [1, 2, 5]) is None
    prev = {
        "complete": True,
        "gameplay": True,
        "open_meld_count": 0,
        "concealed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47],
    }
    nxt = {
        "complete": True,
        "gameplay": True,
        "open_meld_count": 0,
        "concealed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 47],
    }
    tile, src = infer_actual_by_frame_diff(prev, nxt)
    assert tile == 25 and src == "frame_diff"
    # open_meld 变化 → none
    nxt2 = dict(nxt, open_meld_count=1)
    assert infer_actual_by_frame_diff(prev, nxt2) == (None, "none")
    # 禁止：非 gameplay
    assert infer_actual_by_frame_diff(dict(prev, gameplay=False), nxt) == (None, "none")


def test_round_from_meta_seats_and_dealer():
    meta = {
        "round_uid": "302805-2026-10-14-r02",
        "user_account": "晓红",
        "replay_code": "72321963",
        "dealer": "晓红",
        "seats": {"bottom": "晓红", "left": "李哥", "top": "dkl", "right": "啊泽哥"},
        "user_seat": "bottom (晓红)",
        "players": ["李哥", "dkl", "啊泽哥", "晓红"],
        "status": "captured",
    }
    r = round_from_meta(meta)
    assert r["dealer_name"] == "晓红"
    assert r["dealer_seat"] == "bottom"
    assert r["user_seat"] == "bottom"
    assert r["replay_code"] == "72321963"
    assert r["seats"]["top"] == "dkl"


def test_hand_record_from_analyze_applies_frame_diff():
    meta = {
        "round_uid": "test-r1",
        "user_account": "晓红",
        "status": "captured",
        "frames": [],
    }
    analyze = {
        "frames": [
            {
                "name": "a.png",
                "complete": True,
                "gameplay": True,
                "is_hand14": True,
                "is_hand13": False,
                "open_meld_count": 0,
                "concealed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47],
                "melds": [],
                "shanten": 0,
                "hongzhong": 0,
                "advise": {"suggested": 25},
            },
            {
                "name": "b.png",
                "complete": True,
                "gameplay": True,
                "is_hand14": False,
                "is_hand13": True,
                "open_meld_count": 0,
                "concealed": [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 47],
                "melds": [],
                "shanten": 0,
                "hongzhong": 0,
                "advise": None,
            },
        ]
    }
    rec = hand_record_from_analyze(meta, analyze)
    assert rec["schema"] == SCHEMA_ID
    assert len(rec["decisions"]) == 2
    d0 = rec["decisions"][0]
    assert d0["is_hand14"] is True
    assert d0["actual_discard"] == 25
    assert d0["actual_discard_source"] == "frame_diff"
    assert d0["actual_missing"] is False
    d1 = rec["decisions"][1]
    assert d1["actual_missing"] is True
    # frames 只含 bottom
    assert rec["frames"][0]["seats"][0]["seat"] == "bottom"
