"""Counterexample tests for the fail-closed Tea-2 evidence builder."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


BOT_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = BOT_DIR / "tools"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_tea2_evidence_manifest as manifest  # noqa: E402
from recognition_audit import append_frame_event, append_review, build_frame_event  # noqa: E402


def _png(path: Path, marker: int = 1, *, size=(1600, 900)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = marker % 50
    image = Image.new("RGB", size, (25 + safe, 70, 45))
    draw = ImageDraw.Draw(image)
    x0 = 50 + (marker * 37) % max(1, size[0] - 380)
    draw.rectangle((x0, 120, min(x0 + 280, size[0] - 1), min(650, size[1] - 1)),
                   fill=(220, 30 + (safe * 3) % 210, 90))
    draw.ellipse((max(0, size[0] - 420 - (marker * 11) % 250), 180,
                  size[0] - 80, min(780, size[1] - 1)), fill=(40, 160, 230))
    image.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def _mode(*, source: str = "screen_rule_text", verified: bool = True) -> dict:
    return {"player_count": 2, "source": source, "verified": verified}


def _identity(round_seq: int = 1) -> dict:
    return {"mode": "tea_2p", "mode_evidence": _mode(), "room_id": "228087",
            "match_id": "table-228087-game-1", "round_seq": round_seq,
            "caishen": 5}


def _hand(offset: int = 0) -> list[int]:
    tiles = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 25, 26, 27]
    return tiles[offset:] + tiles[:offset]


def _review(digest: str, *, verdict: str = "correct", corrected=None,
            source: str | None = "human", reviewer: str | None = "operator-1",
            timestamp: str | None = "2026-10-27T02:01:00+08:00") -> dict:
    row = {"schema": "recognition_review/v1", "sha256": digest,
           "verdict": verdict}
    if source is not None:
        row["source"] = source
    if reviewer is not None:
        row["reviewer"] = reviewer
    if timestamp is not None:
        row["at"] = timestamp
    if corrected is not None:
        row["corrected_hand"] = corrected
    return row


def test_recognition_gold_requires_all_provenance_and_physical_label(tmp_path):
    frame = tmp_path / "session" / "frame.png"
    digest = _png(frame, 1)
    audit = {"at": "2026-10-27T02:00:00+08:00", "frame": str(frame),
             "sha256": digest, "hand_ids": _hand(), **_identity()}
    _write_jsonl(tmp_path / "session" / "recognition_audit.jsonl", [audit])
    _write_jsonl(tmp_path / "session" / "recognition_reviews.jsonl", [_review(digest)])

    result = manifest.build(tmp_path)

    assert len(result["evidence"]) == 1
    row = result["evidence"][0]
    assert row["recognition_label"] == _hand()
    assert row["human_review"] == "correct"
    assert row["image"]["declared_hash_verified"] is True
    assert row["label_usability"]["recognition"] is True
    assert row["strategy_label_usable"] is False


def test_live_producer_review_and_manifest_consumer_are_schema_compatible(tmp_path):
    session = tmp_path / "session"
    frame = session / "frame.png"
    digest = _png(frame, 41)
    result = {
        "status": "advise", "hand_ids": _hand(), "hand": [],
        "device_serial": "emulator-5554", **_identity(round_seq=4),
    }
    event = build_frame_event(frame.read_bytes(), frame, result, mode="tea_2p")
    assert event["sha256"] == digest
    append_frame_event(session, event)
    append_review(session, event, "correct", reviewer="operator-1")

    built = manifest.build(tmp_path)

    assert len(built["evidence"]) == 1
    evidence = built["evidence"][0]
    assert evidence["human_review"] == "correct"
    assert evidence["recognition_label"] == _hand()
    assert evidence["split_group"] == "228087|table-228087-game-1"
    assert evidence["strategy_label_usable"] is False


@pytest.mark.parametrize(("mode", "label", "review_kwargs", "reason"), [
    (_mode(source="latched"), _hand(), {}, "explicit_tea2_screen_evidence_missing"),
    (_mode(verified=False), _hand(), {}, "explicit_tea2_screen_evidence_missing"),
    (_mode(), [], {}, "recognition_label_invalid_or_wrong_physical_count"),
    (_mode(), _hand(), {"source": None}, "human_review_provenance_missing"),
    (_mode(), _hand(), {"timestamp": None}, "human_review_timestamp_missing_or_invalid"),
])
def test_recognition_counterexamples_are_quarantined(
        tmp_path, mode, label, review_kwargs, reason):
    frame = tmp_path / "session" / "frame.png"
    digest = _png(frame, 2)
    audit = {"frame": str(frame), "sha256": digest, "hand_ids": label,
             **_identity(), "mode_evidence": mode}
    _write_jsonl(tmp_path / "session" / "recognition_audit.jsonl", [audit])
    _write_jsonl(tmp_path / "session" / "recognition_reviews.jsonl",
                 [_review(digest, **review_kwargs)])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    assert reason in result["quarantine"][0]["quarantine_reasons"]
    assert result["quarantine"][0].get("recognition_label") is None or label


def test_wrong_size_and_bad_declared_hash_are_quarantined(tmp_path):
    frame = tmp_path / "session" / "small.png"
    real_digest = _png(frame, 3, size=(800, 450))
    row = {"frame": str(frame), "sha256": "0" * 64, "hand_ids": _hand(), **_identity()}
    _write_jsonl(tmp_path / "session" / "recognition_audit.jsonl", [row])
    _write_jsonl(tmp_path / "session" / "recognition_reviews.jsonl", [_review("0" * 64)])

    result = manifest.build(tmp_path)

    reasons = set(result["quarantine"][0]["quarantine_reasons"])
    assert "source_not_native_1600x900" in reasons
    assert "declared_sha256_not_verified" in reasons
    assert real_digest != "0" * 64


def test_bot_transition_requires_fresh_same_round_and_caishen(tmp_path):
    session = tmp_path / "session"
    before, after = session / "before.png", session / "after.png"
    _png(before, 4)
    _png(after, 5)
    header = {"kind": "session_header", "session": str(session),
              "serial": "emulator-5554", "mode": "tea_2p", "match_id": "table-228087-game-1"}
    action = {"event": "action", "ts": "2026-10-27T02:00:02+08:00",
              "frame": str(before), "after_frame": str(after), "action": "discard",
              "action_source": "bot", "executed": True, "semantic_verified": True,
              "fresh": True, "after_round_seq": 2, "after_caishen": 5,
              **_identity(round_seq=2)}
    _write_jsonl(session / "actions.jsonl", [header, action])

    result = manifest.build(tmp_path)

    assert len(result["evidence"]) == 1
    transition = result["evidence"][0]
    assert transition["transition"]["same_session"] is True
    assert transition["transition"]["same_round"] is True
    assert transition["transition"]["same_caishen"] is True
    assert transition["label_usability"]["transition"] is True


def test_plain_discard_without_after_round_and_caishen_is_quarantined(tmp_path):
    session = tmp_path / "session"
    before, after = session / "missing-before.png", session / "missing-after.png"
    _png(before, 70)
    _png(after, 71)
    action = {"event": "action", "frame": str(before), "after_frame": str(after),
              "action": "discard", "action_source": "bot", "verified": True,
              **_identity(round_seq=2)}
    _write_jsonl(session / "actions.jsonl", [action])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    reasons = set(result["quarantine"][0]["quarantine_reasons"])
    assert "round_changed_or_unknown" in reasons
    assert "caishen_changed_or_unknown" in reasons


def _follow_row(session: Path, marker: int, round_seq: int, *,
                followup: str = "valid") -> tuple[dict, list[Path]]:
    before = session / f"claim-before-{marker}.png"
    after = session / f"claim-after-{marker}.png"
    discard_before = session / f"discard-before-{marker}.png"
    discard_after = session / f"discard-after-{marker}.png"
    for index, path in enumerate((before, after, discard_before, discard_after), marker):
        _png(path, index)
    chain = {"post_state": {"round_seq": round_seq, "caishen": 5}}
    if followup != "missing":
        after_round = round_seq + 1 if followup == "cross_round" else round_seq
        chain["followup_discard"] = {
            "actor": "bot", "executed": True, "semantic_verified": True, "fresh": True,
            "before_frame": str(discard_before), "after_frame": str(discard_after),
            "round_seq": round_seq, "after_round_seq": after_round,
            "caishen": 5, "after_caishen": 5,
        }
    row = {"event": "action", "at": f"2026-10-27T02:{round_seq:02d}:00+08:00",
           "frame": str(before), **_identity(round_seq),
           "decision": {"actor": "bot", "claim": {
               "action": "chi", "actor": "bot",
               "auto_click": {"executed": True, "semantic_verified": True, "fresh": True,
                              "after_frame": str(after),
                              "post_state": {"round_seq": round_seq, "caishen": 5}},
               "chi_chain": chain}}}
    return row, [before, after, discard_before, discard_after]


def test_claim_requires_real_verified_followup_frames_and_same_round(tmp_path):
    session = tmp_path / "session"
    header = {"kind": "session_header", "session": str(session),
              "serial": "emulator-5556", "mode": "tea_2p", "match_id": "table-228087-game-1"}
    valid, _ = _follow_row(session, 6, 3, followup="valid")
    missing, _ = _follow_row(session, 12, 4, followup="missing")
    cross_round, _ = _follow_row(session, 18, 5, followup="cross_round")
    _write_jsonl(session / "follow_session.jsonl", [header, valid, missing, cross_round])

    result = manifest.build(tmp_path)

    valid_rows = [row for row in result["evidence"] if row["record_type"] == "transition"]
    assert len(valid_rows) == 1
    followup = valid_rows[0]["action"]["followup_discard"]
    assert followup["before"]["native_1600x900"] is True
    assert followup["after"]["stable_read"] is True
    assert valid_rows[0]["action"]["full_chain_verified"] is True
    bad = [set(row.get("quarantine_reasons", [])) for row in result["quarantine"]]
    assert any("claim_followup_verified_discard_missing" in reasons for reasons in bad)
    assert any("claim_followup_round_changed_or_unknown" in reasons for reasons in bad)


def _production_claim_rows(session: Path, *, action_id: str = "claim-1",
                           continuation_id: str | None = None,
                           claim_round: int = 8, continuation_round: int = 8,
                           continuation_caishen: int = 5,
                           continuation_session: str | None = None,
                           marker: int = 30) -> tuple[dict, dict]:
    claim_before = session / f"prod-claim-before-{marker}.png"
    claim_after = session / f"prod-claim-after-{marker}.png"
    discard_before = session / f"prod-discard-before-{marker}.png"
    discard_after = session / f"prod-discard-after-{marker}.png"
    for offset, frame in enumerate(
            (claim_before, claim_after, discard_before, discard_after)):
        _png(frame, marker + offset)
    claim = {
        "event": "action", "action_kind": "claim", "action_id": action_id,
        "action_source": "bot", "frame": str(claim_before),
        **_identity(claim_round),
        "claim": {"action": "peng", "frame": str(claim_before),
                  "auto_click": {"executed": True, "semantic_verified": True,
                                 "after_frame": str(claim_after)}},
    }
    continuation = {
        "event": "action", "action_kind": "discard", "action_source": "bot",
        "continuation_of": continuation_id or action_id, "verified": True,
        "frame": str(discard_before), "after_frame": str(discard_after),
        **_identity(continuation_round), "caishen": continuation_caishen,
        "after_round_seq": continuation_round,
        "after_caishen": continuation_caishen,
    }
    if continuation_session is not None:
        continuation["session"] = continuation_session
    return claim, continuation


def test_production_two_line_claim_and_continuation_build_one_transition(tmp_path):
    session = tmp_path / "session"
    header = {"kind": "session_header", "session": str(session),
              "mode": "tea_2p", "serial": "emulator-5554",
              "match_id": "table-228087-game-1"}
    claim, continuation = _production_claim_rows(session)
    _write_jsonl(session / "actions.jsonl", [header, claim, continuation])

    result = manifest.build(tmp_path)

    assert len(result["evidence"]) == 1
    row = result["evidence"][0]
    assert row["action"]["kind"] == "peng"
    assert row["action"]["action_id"] == "claim-1"
    assert row["action"]["full_chain_verified"] is True
    assert row["action"]["followup_discard"]["after"]["stable_read"] is True


def test_production_continuation_without_after_identity_is_quarantined(tmp_path):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, marker=35)
    continuation.pop("after_round_seq")
    continuation.pop("after_caishen")
    _write_jsonl(session / "actions.jsonl", [claim, continuation])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    reasons = set(result["quarantine"][0]["quarantine_reasons"])
    assert "claim_followup_round_changed_or_unknown" in reasons
    assert "claim_followup_caishen_changed_or_unknown" in reasons


def test_noncontinuation_claim_requires_explicit_auto_click_after_identity(tmp_path):
    session = tmp_path / "session"
    before, after = session / "hu-before.png", session / "hu-after.png"
    _png(before, 36)
    _png(after, 37)

    def hu_row(action_id: str, *, with_post_state: bool) -> dict:
        auto_click = {"executed": True, "semantic_verified": True,
                      "after_frame": str(after)}
        if with_post_state:
            auto_click["post_state"] = {"round_seq": 8, "caishen": 5}
        return {"event": "action", "action_kind": "claim", "action_id": action_id,
                "action_source": "bot", "frame": str(before), **_identity(8),
                "claim": {"action": "hu", "frame": str(before),
                          "auto_click": auto_click}}

    _write_jsonl(session / "actions.jsonl", [hu_row("hu-good", with_post_state=True)])
    valid = manifest.build(tmp_path)
    _write_jsonl(session / "actions.jsonl", [hu_row("hu-bad", with_post_state=False)])
    invalid = manifest.build(tmp_path)

    assert len(valid["evidence"]) == 1
    assert valid["evidence"][0]["action"]["kind"] == "hu"
    assert invalid["evidence"] == []
    reasons = set(invalid["quarantine"][0]["quarantine_reasons"])
    assert "round_changed_or_unknown" in reasons
    assert "caishen_changed_or_unknown" in reasons


def test_production_claim_missing_and_wrong_continuations_are_quarantined(tmp_path):
    session = tmp_path / "session"
    claim_missing, _ = _production_claim_rows(session, action_id="missing", marker=40)
    claim_wrong, wrong = _production_claim_rows(
        session, action_id="expected", continuation_id="other", marker=45)
    _write_jsonl(session / "actions.jsonl", [claim_missing, claim_wrong, wrong])

    result = manifest.build(tmp_path)
    reasons = [set(row.get("quarantine_reasons", [])) for row in result["quarantine"]]

    assert any("unmatched_claim_continuation" in item for item in reasons)
    assert sum("claim_continuation_missing_at_end_of_file" in item
               for item in reasons) == 2


def test_production_duplicate_continuation_is_quarantined(tmp_path):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, marker=50)
    duplicate = dict(continuation)
    duplicate["after_frame"] = str(session / "duplicate-after.png")
    _png(Path(duplicate["after_frame"]), 55)
    _write_jsonl(session / "actions.jsonl", [claim, continuation, duplicate])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    duplicate_rows = [row for row in result["quarantine"]
                      if "duplicate_claim_continuation" in
                      row.get("quarantine_reasons", [])]
    assert len(duplicate_rows) == 2


def test_claim_to_non_discard_continuation_revokes_both_sides(tmp_path):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, marker=54)
    continuation["action_kind"] = "mystery"
    _write_jsonl(session / "actions.jsonl", [claim, continuation])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    invalid = [row for row in result["quarantine"]
               if "claim_continuation_action_not_discard" in
               row.get("quarantine_reasons", [])]
    assert len(invalid) == 2


def test_claim_continuation_explicit_falsey_kind_is_not_default_discard(tmp_path):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, marker=55)
    continuation["action_kind"] = 0
    _write_jsonl(session / "actions.jsonl", [claim, continuation])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    invalid = [row for row in result["quarantine"]
               if "claim_continuation_action_not_discard" in
               row.get("quarantine_reasons", [])]
    assert len(invalid) == 2


def test_duplicate_claim_after_completed_chain_revokes_original_evidence(tmp_path):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, action_id="reused", marker=56)
    duplicate_claim = dict(claim)
    duplicate_claim["claim"] = dict(claim["claim"])
    _write_jsonl(session / "actions.jsonl", [claim, continuation, duplicate_claim])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    duplicate_rows = [row for row in result["quarantine"]
                      if "duplicate_claim_action_id" in
                      row.get("quarantine_reasons", [])]
    assert len(duplicate_rows) == 2


def test_unknown_production_claim_and_flat_action_are_quarantined(tmp_path):
    session = tmp_path / "session"
    before, after = session / "unknown-before.png", session / "unknown-after.png"
    _png(before, 57)
    _png(after, 58)
    unknown_claim = {
        "event": "action", "action_kind": "claim", "action_id": "typo-claim",
        "action_source": "bot", "frame": str(before), **_identity(8),
        "claim": {"action": "pnge", "frame": str(before), "auto_click": {
            "executed": True, "semantic_verified": True, "after_frame": str(after),
            "post_state": {"round_seq": 8, "caishen": 5}}},
    }
    unknown_flat = {
        "event": "action", "action": "mystery", "action_source": "bot",
        "verified": True, "frame": str(before), "after_frame": str(after),
        "after_round_seq": 8, "after_caishen": 5, **_identity(8),
    }
    _write_jsonl(session / "actions.jsonl", [unknown_claim, unknown_flat])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    reasons = [set(row.get("quarantine_reasons", [])) for row in result["quarantine"]]
    assert any("production_claim_action_not_allowed" in item for item in reasons)
    assert any("flat_action_kind_not_allowed" in item for item in reasons)


def test_flat_action_explicit_falsey_kind_is_not_default_discard(tmp_path):
    session = tmp_path / "session"
    before, after = session / "falsey-before.png", session / "falsey-after.png"
    _png(before, 59)
    _png(after, 60)
    row = {"event": "action", "action": 0, "action_source": "bot",
           "verified": True, "frame": str(before), "after_frame": str(after),
           "after_round_seq": 8, "after_caishen": 5, **_identity(8)}
    _write_jsonl(session / "actions.jsonl", [row])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    assert "flat_action_kind_not_allowed" in \
        result["quarantine"][0]["quarantine_reasons"]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"continuation_round": 9}, "claim_followup_round_changed_or_unknown"),
    ({"continuation_caishen": 6}, "claim_followup_caishen_changed_or_unknown"),
    ({"continuation_session": "other-session"},
     "claim_followup_session_changed_or_unknown"),
])
def test_production_continuation_identity_mismatch_is_quarantined(
        tmp_path, changes, reason):
    session = tmp_path / "session"
    claim, continuation = _production_claim_rows(session, marker=60, **changes)
    _write_jsonl(session / "actions.jsonl", [claim, continuation])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    assert any(reason in row.get("quarantine_reasons", [])
               for row in result["quarantine"])


def test_split_keeps_dual_serial_complete_16_round_match_together():
    records = []
    for serial in ("emulator-5554", "emulator-5556"):
        for round_seq in range(1, 17):
            digest = hashlib.sha256(f"{serial}-{round_seq}".encode()).hexdigest()
            records.append({"split_group": "228087|match-shared", "serial": serial,
                            "round_seq": round_seq, "started_at": f"different-{serial}",
                            "image": {"sha256": digest, "phash64": digest[:16]}})

    split = manifest._assign_splits(records)

    assert len({row["split"] for row in records}) == 1
    assert split["groups"] == {"228087|match-shared": records[0]["split"]}
    assert {row["round_seq"] for row in records} == set(range(1, 17))


def test_phash_hamming_neighbors_share_contamination_cluster_and_split():
    records = [
        {"split_group": "room-a|match-a", "image": {"sha256": "a" * 64,
                                                       "phash64": "0000000000000000"}},
        {"split_group": "room-b|match-b", "image": {"sha256": "b" * 64,
                                                       "phash64": "0000000000000001"}},
    ]

    manifest._assign_splits(records)

    assert records[0]["contamination_cluster"] == records[1]["contamination_cluster"]
    assert records[0]["split"] == records[1]["split"]


def test_path_escape_is_rejected_without_hashing_outside_file(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside.png"
    digest = _png(outside, 25)
    row = {"frame": str(outside), "sha256": digest, "hand_ids": _hand(), **_identity()}
    _write_jsonl(root / "session" / "recognition_audit.jsonl", [row])
    _write_jsonl(root / "session" / "recognition_reviews.jsonl", [_review(digest)])

    result = manifest.build(root)

    assert result["evidence"] == []
    escaped = result["quarantine"][0]
    assert "path_outside_root" in escaped["quarantine_reasons"]
    assert escaped["image"]["sha256"] is None
    assert result["report"]["png_files_inspected"] == 0


@pytest.mark.parametrize("forbidden", [65, 66])
def test_recognition_rejects_ids_outside_production_target_ids(tmp_path, forbidden):
    frame = tmp_path / "session" / f"forbidden-{forbidden}.png"
    digest = _png(frame, forbidden)
    label = _hand()
    label[-1] = forbidden
    row = {"frame": str(frame), "sha256": digest, "hand_ids": label, **_identity()}
    _write_jsonl(tmp_path / "session" / "recognition_audit.jsonl", [row])
    _write_jsonl(tmp_path / "session" / "recognition_reviews.jsonl", [_review(digest)])

    result = manifest.build(tmp_path)

    assert forbidden not in manifest.VALID_TEA_TILE_IDS
    assert result["evidence"] == []
    assert "recognition_label_invalid_or_wrong_physical_count" in \
        result["quarantine"][0]["quarantine_reasons"]


def test_recognition_combines_concealed_and_meld_physical_counts(tmp_path):
    frame = tmp_path / "session" / "meld-overflow.png"
    digest = _png(frame, 67)
    hand = [1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 21]
    row = {"frame": str(frame), "sha256": digest, "hand_ids": hand,
           "meld_count": 1, "melds": [{"kind": "peng", "tile_ids": [1, 1, 1]}],
           **_identity()}
    _write_jsonl(tmp_path / "session" / "recognition_audit.jsonl", [row])
    _write_jsonl(tmp_path / "session" / "recognition_reviews.jsonl", [_review(digest)])

    result = manifest.build(tmp_path)

    assert result["evidence"] == []
    assert "recognition_label_invalid_or_wrong_physical_count" in \
        result["quarantine"][0]["quarantine_reasons"]


@pytest.mark.parametrize(("header_dev", "row_dev", "eligible"), [
    (True, None, False),
    (None, True, False),
    (False, False, True),
    ("true", "true", True),
])
def test_development_flag_requires_literal_boolean_true(
        tmp_path, header_dev, row_dev, eligible):
    session = tmp_path / "session"
    frame = session / "dev.png"
    digest = _png(frame, 68)
    header = {"kind": "session_header", "mode": "tea_2p",
              "match_id": "table-228087-game-1"}
    if header_dev is not None:
        header["dev"] = header_dev
    row = {"frame": str(frame), "sha256": digest, "hand_ids": _hand(), **_identity()}
    if row_dev is not None:
        row["dev"] = row_dev
    _write_jsonl(session / "recognition_audit.jsonl", [header, row])
    _write_jsonl(session / "recognition_reviews.jsonl", [_review(digest)])

    result = manifest.build(tmp_path)

    assert bool(result["evidence"]) is eligible
    if not eligible:
        assert any("development_session_not_corpus_eligible" in
                   item.get("quarantine_reasons", []) for item in result["quarantine"])


def test_transition_burst_key_keeps_same_before_for_different_actions():
    base = {"schema": manifest.SCHEMA, "record_type": "transition",
            "timestamp": "2026-10-27T02:00:00+08:00", "split_group": "r|m",
            "frame_role": "before_after", "source_log": "actions.jsonl"}
    rows = []
    for line, action, after, followup in (
            (1, "peng", "2" * 16, "3" * 16),
            (2, "chi", "4" * 16, "5" * 16)):
        rows.append({**base, "source_line": line,
                     "action": {"kind": action, "followup_discard": {
                         "after": {"phash64": followup}}},
                     "transition": {"before": {"sha256": "a" * 64,
                                                "phash64": "1" * 16},
                                    "after": {"sha256": after * 4,
                                              "phash64": after}}})

    kept, duplicates = manifest._deduplicate_evidence(rows)

    assert len(kept) == 2
    assert duplicates == []


def test_stall_action_stall_flushes_into_two_negative_events(tmp_path):
    session = tmp_path / "session"
    frame = session / "stall.png"
    _png(frame, 26)
    base = {"event": "idle", "status": "hu", "reason": "claim_wait",
            "frame": str(frame), **_identity(round_seq=7)}
    rows = []
    for second in (0, 5, 10):
        rows.append({**base, "ts": f"2026-10-27T02:00:{second:02d}+08:00"})
    rows.append({"event": "action", "ts": "2026-10-27T02:00:15+08:00",
                 "action": "discard", "actor": "bot", **_identity(round_seq=7)})
    for second in (20, 25, 30):
        rows.append({**base, "ts": f"2026-10-27T02:00:{second:02d}+08:00"})
    _write_jsonl(session / "actions.jsonl", rows)

    result = manifest.build(tmp_path)

    stalls = [row for row in result["evidence"] if row["record_type"] == "stall_negative"]
    assert len(stalls) == 2
    assert [row["stall"]["poll_rows"] for row in stalls] == [3, 3]


def test_default_does_not_traverse_or_hash_orphan_png(tmp_path):
    orphan = tmp_path / "session" / "orphan.png"
    _png(orphan, 27)

    result = manifest.build(tmp_path)

    assert result["report"]["orphan_scan_enabled"] is False
    assert result["report"]["png_files_inspected"] == 0
    assert result["quarantine"] == []


def test_bounded_preflight_reports_limits_without_changing_full_build_default(tmp_path):
    for index in range(2):
        rows = [{"event": "idle", "ts": f"2026-10-27T02:00:0{row}+08:00",
                 "status": "waiting", **_identity()}
                for row in range(3)]
        _write_jsonl(tmp_path / f"session-{index}" / "actions.jsonl", rows)

    bounded = manifest.build(tmp_path, max_jsonl=1, max_rows=2,
                             since="2026-10-27T02:00:00+08:00")
    complete = manifest.build(tmp_path)

    assert bounded["report"]["scan_mode"] == "bounded_preflight"
    assert bounded["report"]["complete_scan"] is False
    assert bounded["report"]["jsonl_files_discovered"] == 2
    assert bounded["report"]["jsonl_files"] == 1
    assert bounded["report"]["limits"]["max_rows"] == 2
    assert complete["report"]["scan_mode"] == "complete"
    assert complete["report"]["complete_scan"] is True


def test_cli_refuses_to_write_a_bounded_preflight(tmp_path):
    with pytest.raises(SystemExit):
        manifest.main(["--root", str(tmp_path), "--write", "--max-rows", "1"])


def test_write_is_explicit_bundle_existing_fails_and_force_replaces(tmp_path, capsys):
    orphan = tmp_path / "session" / "orphan.png"
    _png(orphan, 28)
    output = tmp_path / "manifests"

    assert manifest.main(["--root", str(tmp_path), "--out-dir", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not output.exists()

    result = manifest.build(tmp_path, include_orphan_pngs=True)
    paths = manifest.write_manifests(result, output)
    assert {path.name for path in paths} == set(manifest.OUTPUT_NAMES)
    with pytest.raises(FileExistsError):
        manifest.write_manifests(result, output)
    replaced = manifest.write_manifests(result, output, force=True)
    assert {path.name for path in replaced} == set(manifest.OUTPUT_NAMES)
    quarantine = [json.loads(line) for line in
                  (output / "tea2_quarantine_manifest.jsonl").read_text(
                      encoding="utf-8").splitlines()]
    assert quarantine[0]["record_type"] == "unindexed_png"
    assert "png_not_referenced_by_supported_jsonl" in quarantine[0]["quarantine_reasons"]


def test_output_path_must_stay_below_capture_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    result = manifest.build(root)
    with pytest.raises(ValueError, match="outside_root"):
        manifest.write_manifests(result, tmp_path / "outside")
