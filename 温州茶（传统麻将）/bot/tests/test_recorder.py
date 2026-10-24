"""Frozen SPEC-002 v4 recorder contract tests."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from recorder import (AssistRecorder, decision_id, infer_rule_gate,  # noqa: E402
                      normalize_candidates, normalize_tiles, semantic_key)
from tile_vision import TILE_IDS  # noqa: E402


NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone(timedelta(hours=8)))
SHU_HAND = [1, 1, 6, 6, 8, 41, 46, 23, 23, 24, 25, 26, 29, 29]
SHU_OPTIONS = [
    {"tile": 41, "name": "1条", "final_score": 40 - index,
     "shanten_after": 1, "ukeire_count": 9 - index, "extra": "drop"}
    for index in range(6)
]
TEA_HAND = [
    TILE_IDS[label] for label in
    "w2 w4 w5 w7 s1 s1 s3 s6 s6 s7 s7 t1 t2 t8 t8 t8 t5".split()
]
REAL_TEA_FRAME = (BOT_DIR.parent / "采集" / "2026-10-16" / "740195" /
                  "r08" / "frame_02.png")


def _frame(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(("frame:" + name).encode("utf-8"))
    return path


def _tiles(ids):
    return [{"id": value, "score": .99 if value is not None else .2,
             "margin": .2 if value is not None else .01,
             "reason": "ok" if value is not None else "ambiguous"}
            for value in ids]


def _shu_result(frame: Path, hand=None, *, status="advise", options=None):
    hand = list(SHU_HAND if hand is None else hand)
    return {
        "mode": "shu_4p", "status": status, "image": str(frame),
        "screen_rule_text": "放炮麻将 红中 4人", "gate_ok": True,
        "record_tiles": _tiles(hand), "hand_ids": hand, "shanten": 1,
        "meld_count": 0, "options": SHU_OPTIONS if options is None else options,
    }


def _tea_candidates():
    from tea_bot import vision_to_engine
    from brain_v0.engine.tea.shanten import best_discard_by_efficiency

    engine_hand = [vision_to_engine(tile) for tile in TEA_HAND]
    return best_discard_by_efficiency(
        engine_hand, caishen_id=vision_to_engine(TILE_IDS["s7"]),
    )


def _tea_result(frame: Path, *, caishen_id, hand=None):
    hand = list(TEA_HAND if hand is None else hand)
    return {
        "mode": "tea_2p", "status": "advise", "image": str(frame),
        "screen_rule_text": "温州茶 2人 无中发", "gate_ok": True,
        "record_tiles": _tiles(hand), "hand_ids": hand, "shanten": 2,
        "caishen_id": caishen_id, "candidates": _tea_candidates(),
    }


def test_state_machine_stability_successful_diff_and_single_close_write(tmp_path):
    decision = _frame(tmp_path, "decision.png")
    result_frame = _frame(tmp_path, "result.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="account-1", room_id="room-9")

    first = _shu_result(decision)
    shuffled = _shu_result(decision, list(reversed(SHU_HAND)))
    assert recorder.observe(first, NOW) is None
    assert recorder.state == "detected" and not output.exists()
    assert recorder.observe(shuffled, NOW + timedelta(seconds=1)) is None
    assert recorder.state == "suggested"
    assert recorder.active.transitions == ["detected", "suggested"]
    assert not output.exists(), "suggested must not be persisted"

    remaining = list(SHU_HAND)
    remaining.remove(41)
    assert recorder.observe(_shu_result(result_frame, remaining, status="wait", options=[]),
                            NOW + timedelta(seconds=2)) is None
    record = recorder.observe(
        _shu_result(result_frame, list(reversed(remaining)), status="wait", options=[]),
        NOW + timedelta(seconds=3),
    )
    assert record["status"] == "resolved"
    assert set(record) == {
        "schema", "decision_id", "timestamp", "account", "room_id", "mode",
        "screen_rule_text", "gate_ok", "round", "tiles", "caishen_id",
        "melds", "shanten", "bot_suggest", "bot_candidates",
        "human_discard", "agreed", "status", "abstain_reason", "diff_note",
        "decision_frame", "result_frame",
    }
    assert record["human_discard"] == 41 and record["agreed"] is True
    assert record["diff_note"] is None and record["abstain_reason"] is None
    assert record["result_frame"]["sha256"] == hashlib.sha256(result_frame.read_bytes()).hexdigest()
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == record


def test_semantic_key_decision_id_and_closed_key_rearm_rules(tmp_path):
    key_a = semantic_key("tea_2p", [3, 1, 3], 1, None)
    key_b = semantic_key("tea_2p", [3, 3, 1], 1, None)
    assert key_a == key_b and len(key_a) == 64
    identifier = decision_id("tea_2p", key_a)
    assert identifier == f"tea_2p_{key_a[:16]}"
    assert re.fullmatch(r"tea_2p_[0-9a-f]{16}", identifier)

    frame = _frame(tmp_path, "tea.png")
    recorder = AssistRecorder(tmp_path / "records.jsonl", account="a")
    unset = _tea_result(frame, caishen_id=None)
    assert recorder.observe(unset, NOW) is None
    assert recorder.state == "detected"
    closed = recorder.observe(unset, NOW + timedelta(seconds=1))
    assert closed["status"] == "abstained" and closed["diff_note"] == "caishen_unset"
    assert recorder.observe(unset, NOW + timedelta(seconds=2)) is None
    assert recorder.active is None

    selected = _tea_result(frame, caishen_id=TILE_IDS["s7"])
    assert recorder.observe(selected, NOW + timedelta(seconds=3)) is None
    assert recorder.state == "detected", "same hand must rearm when caishen changes"
    recorder.next_round()
    assert recorder.round == 2 and recorder.active is None
    assert recorder.observe(selected, NOW + timedelta(seconds=4)) is None
    assert recorder.state == "detected", "round change must rearm"


def test_timeout_after_suggested_has_null_agreement_and_no_result_frame(tmp_path):
    frame = _frame(tmp_path, "decision.png")
    recorder = AssistRecorder(tmp_path / "records.jsonl", account="a")
    recorder.observe(_shu_result(frame), NOW)
    recorder.observe(_shu_result(frame), NOW + timedelta(seconds=1))
    assert recorder.tick(NOW + timedelta(seconds=30)) is None
    record = recorder.tick(NOW + timedelta(seconds=31))
    assert record["status"] == "timeout"
    assert record["agreed"] is None and record["human_discard"] is None
    assert record["result_frame"] is None and record["abstain_reason"] is None


def test_diff_failed_field_rules(tmp_path):
    decision = _frame(tmp_path, "decision.png")
    changed = _frame(tmp_path, "changed.png")
    recorder = AssistRecorder(tmp_path / "records.jsonl", account="a")
    recorder.observe(_shu_result(decision), NOW)
    recorder.observe(_shu_result(decision), NOW + timedelta(seconds=1))
    invalid = list(SHU_HAND)
    invalid.remove(41)
    invalid.append(42)
    recorder.observe(_shu_result(changed, invalid), NOW + timedelta(seconds=2))
    record = recorder.observe(_shu_result(changed, invalid), NOW + timedelta(seconds=3))
    assert record["status"] == "resolved" and record["diff_note"] == "diff_failed"
    assert record["human_discard"] is None and record["agreed"] is None
    assert record["abstain_reason"] is None and record["result_frame"] is not None


def test_caishen_unset_and_gate_abstentions_follow_full_lifecycle(tmp_path, monkeypatch):
    frame = _frame(tmp_path, "frame.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="a")
    transitions = []
    original_append = recorder._append

    def capture_transitions(record):
        transitions.append(list(recorder.active.transitions))
        original_append(record)

    monkeypatch.setattr(recorder, "_append", capture_transitions)
    unset = _tea_result(frame, caishen_id=None)
    assert recorder.observe(unset, NOW) is None
    assert recorder.state == "detected" and not output.exists()
    record = recorder.observe(unset, NOW + timedelta(seconds=1))
    assert record["status"] == "abstained"
    assert record["abstain_reason"] is None and record["diff_note"] == "caishen_unset"
    assert record["bot_suggest"] is None and record["bot_candidates"] == []
    assert transitions[-1] == ["detected", "suggested", "abstained"]

    recorder.next_round()
    unreadable = _shu_result(frame)
    unreadable.update(screen_rule_text=None, gate_ok=False)
    assert recorder.observe(unreadable, NOW + timedelta(seconds=2)) is None
    assert recorder.state == "detected"
    record = recorder.observe(unreadable, NOW + timedelta(seconds=3))
    assert record["status"] == "abstained"
    assert record["abstain_reason"] == "gate_unreadable" and record["diff_note"] is None
    assert transitions[-1] == ["detected", "suggested", "abstained"]


def test_detected_decision_lock_rejects_different_semantic_key(tmp_path):
    frame = _frame(tmp_path, "frame.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="a")
    first = _shu_result(frame)
    different_hand = list(SHU_HAND)
    different_hand[-1] = 28
    different = _shu_result(frame, different_hand)

    assert recorder.observe(first, NOW) is None
    locked_key = recorder.active.key
    assert recorder.active.transitions == ["detected"]
    assert recorder.observe(different, NOW + timedelta(seconds=1)) is None
    assert recorder.observe(different, NOW + timedelta(seconds=2)) is None
    assert recorder.active.key == locked_key
    assert recorder.active.stable_count == 1
    assert recorder.active.transitions == ["detected"]
    assert not output.exists()

    assert recorder.observe(first, NOW + timedelta(seconds=3)) is None
    assert recorder.state == "suggested"
    record = recorder.tick(NOW + timedelta(seconds=34))
    assert record["status"] == "timeout"
    assert recorder.active is None


def test_tea_rule_gate_requires_readable_matching_mode_not_no_zhong_fa(monkeypatch):
    import mode_vision

    monkeypatch.setattr(mode_vision, "detect_shu_rule_text", lambda image: False)
    monkeypatch.setattr(mode_vision, "detect_tea_player_count", lambda image: 2)
    monkeypatch.setattr(mode_vision, "detect_tea_no_zhong_fa", lambda image: False)

    text, gate_ok = infer_rule_gate(REAL_TEA_FRAME, "tea_2p")
    assert text == "温州茶 2人" and gate_ok is True
    _, wrong_mode_ok = infer_rule_gate(REAL_TEA_FRAME, "tea_4p")
    assert wrong_mode_ok is False


def test_candidate_normalization_for_tea_and_shu_is_top5_and_exact_shape():
    tea = normalize_candidates(
        "tea_2p", _tea_candidates(), hand_ids=TEA_HAND,
        caishen_id=TILE_IDS["s7"],
    )
    shu = normalize_candidates("shu_4p", list(reversed(SHU_OPTIONS)))
    required = {"tile", "final_score", "shanten_after", "ukeire_count"}
    assert len(tea) == 5 and len(shu) == 5
    assert all(set(row) == required for row in tea + shu)
    assert [row["final_score"] for row in tea] == sorted(
        (row["final_score"] for row in tea), reverse=True,
    )
    assert [row["final_score"] for row in shu] == [40, 39, 38, 37, 36]
    assert all("name" not in row and "extra" not in row for row in shu)


def test_abstain_reasons_and_tile_reason_vocabulary(tmp_path):
    frame = _frame(tmp_path, "frame.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="a")

    wrong_count = _shu_result(frame, SHU_HAND[:-2])
    assert recorder.observe(wrong_count, NOW) is None
    record = recorder.observe(wrong_count, NOW + timedelta(seconds=1))
    assert record["abstain_reason"] == "wrong_count"
    recorder.next_round()

    unknown = _shu_result(frame, [None] + SHU_HAND[1:])
    assert recorder.observe(unknown, NOW + timedelta(seconds=2)) is None
    record = recorder.observe(unknown, NOW + timedelta(seconds=3))
    assert record["abstain_reason"] == "unrecognized" and record["shanten"] is None
    recorder.next_round()

    source = {
        "vision_diagnostics": [
            {"tile_id": 1, "score": .9, "margin": .1, "accepted": True,
             "reason": "accepted"},
            {"tile_id": None, "score": .4, "margin": .1, "accepted": False,
             "reason": "low_score"},
            {"tile_id": None, "score": .9, "margin": .01, "accepted": False,
             "reason": "ambiguous"},
            {"tile_id": None, "score": 0, "margin": 0, "accepted": False,
             "reason": "no_glyph"},
        ],
    }
    assert [tile["reason"] for tile in normalize_tiles(source)] == [
        "ok", "low_score", "ambiguous", "no_box",
    ]


def test_every_appended_line_is_independently_json_loadable(tmp_path):
    frame = _frame(tmp_path, "frame.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="a")
    for round_no in range(1, 4):
        bad = _shu_result(frame)
        bad.update(screen_rule_text="温州茶 2人 无中发", gate_ok=False)
        offset = round_no * 2
        assert recorder.observe(bad, NOW + timedelta(seconds=offset - 1)) is None
        record = recorder.observe(bad, NOW + timedelta(seconds=offset))
        assert record["abstain_reason"] == "gate_mismatch"
        if round_no < 3:
            recorder.next_round()
    parsed = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(parsed) == 3
    assert all(row["schema"] == "assist_record/v4" for row in parsed)


def test_semantic_key_never_repeats_within_same_round(tmp_path):
    frame = _frame(tmp_path, "frame.png")
    output = tmp_path / "records.jsonl"
    recorder = AssistRecorder(output, account="a")
    first = _shu_result(frame)
    first.update(screen_rule_text="温州茶 2人 无中发", gate_ok=False)
    assert recorder.observe(first, NOW) is None
    first_record = recorder.observe(first, NOW + timedelta(seconds=1))

    different = _shu_result(frame, SHU_HAND[:-1])
    different.update(screen_rule_text="温州茶 2人 无中发", gate_ok=False)
    assert recorder.observe(different, NOW + timedelta(seconds=2)) is None
    recorder.observe(different, NOW + timedelta(seconds=3))
    assert recorder.observe(first, NOW + timedelta(seconds=4)) is None
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert len({record["decision_id"] for record in records}) == 2
    assert first_record["decision_id"] == records[0]["decision_id"]


def test_window_analysis_path_disables_old_per_frame_json(tmp_path):
    from assist_tea import import_and_analyze

    session = tmp_path / "session"
    results = [
        import_and_analyze(
            REAL_TEA_FRAME, session_dir=session, caishen_label="s9",
            confirmed_two_player=True, write_sidecar=False,
        )
        for _ in range(2)
    ]
    assert all("record" not in result for result in results)
    assert not list(session.glob("*.json"))

    recorder = AssistRecorder(session / "assist_record.jsonl", account="a")
    for offset, result in enumerate(results):
        result["caishen_id"] = TILE_IDS["s9"]
        assert recorder.observe(result, NOW + timedelta(seconds=offset)) is None
    assert recorder.state == "suggested"
    assert not recorder.output_path.exists(), "suggested stage must not write JSONL"
