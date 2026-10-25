"""Corpus admission rejects unknown provenance and damaged ledgers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_review import review_session


def _write_session(tmp_path, header, *, bad=False):
    target = tmp_path / "session"
    target.mkdir()
    rows = [json.dumps(header, ensure_ascii=False),
            json.dumps({"event": "idle", "status": "advise",
                        "ts": "2026-10-25T11:00:00+08:00"})]
    if bad:
        rows.append("{bad json")
    (target / "actions.jsonl").write_text("\n".join(rows)+"\n", encoding="utf-8")
    return target


def test_missing_dev_flag_is_not_approved(tmp_path):
    path = _write_session(tmp_path, {
        "mode": "tea_2p", "serial": "emulator-test", "started_at": "2026-10-25T11:00:00"})
    result = review_session(path)
    assert result["corpus_eligible"] is False
    assert any("dev 未明确" in x for x in result["corpus_blockers"])


def test_corrupt_input_blocks_corpus(tmp_path):
    path = _write_session(tmp_path, {
        "dev": False, "mode": "tea_2p", "serial": "emulator-test",
        "started_at": "2026-10-25T11:00:00"}, bad=True)
    result = review_session(path)
    assert result["corpus_eligible"] is False
    assert any("台账损坏" in x for x in result["corpus_blockers"])


def test_clean_explicitly_nondev_is_only_candidate(tmp_path):
    path = _write_session(tmp_path, {
        "dev": False, "mode": "tea_2p", "serial": "emulator-test",
        "started_at": "2026-10-25T11:00:00"})
    result = review_session(path)
    assert result["corpus_eligible"] is True
    assert result["corpus_status"] == "candidate_review"
    assert result["train_approved"] is False


def test_unverified_action_is_flagged_and_blocks_corpus(tmp_path):
    path = _write_session(tmp_path, {
        "dev": False, "mode": "tea_2p", "serial": "emulator-test",
        "started_at": "2026-10-25T11:00:00"})
    with (path / "actions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "action", "verified": False,
                            "before_count": 17, "after_count": 17,
                            "ts": "2026-10-25T11:01:00+08:00"}) + "\n")
    result = review_session(path)
    assert result["corpus_eligible"] is False
    assert any(issue["category"] == "action_unverified"
               for issue in result["issues"])
