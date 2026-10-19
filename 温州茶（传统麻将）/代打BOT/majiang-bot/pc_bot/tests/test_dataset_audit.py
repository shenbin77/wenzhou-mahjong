"""Audit must preserve unknowns and never alter source evidence or databases."""
import base64
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataset_audit import RULE_REQUIREMENTS, audit_dataset, sha256_file


class DatasetAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"test source bytes; no actual gameplay facts inferred")
        self.frame = self.root / "frame.png"
        self.frame.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jh3cAAAAASUVORK5CYII="
        ))

    def complete_record(self):
        return {
            "hand": [1,2,3,4,5,6,7,8,9,21,22,23,25,47], "open_meld_count": 0,
            "rivers": {}, "actual_discard": 47, "gamelog_id": "sourcegame_R01_P001",
            "timestamp_seconds": 12.0, "source_clip": "source.mp4",
            "source_sha256": sha256_file(self.source), "reviewer": "human", "evidence_frame": "frame.png",
            "rules": dict(RULE_REQUIREMENTS),
            "result": {"win_type": "zimo", "winner": 0, "estimated": False},
        }

    def jsonl(self, records, name="samples.jsonl"):
        path = self.root / name
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        return path

    def audit(self, records):
        return audit_dataset(jsonl_paths=[self.jsonl(records)])

    def test_explicit_evidence_can_pass_and_sources_remain_byte_identical(self):
        path = self.jsonl([self.complete_record()])
        before = {p: p.read_bytes() for p in (path, self.source, self.frame)}
        report = audit_dataset(jsonl_paths=[path])
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["summary"]["technical_valid"], 1)
        self.assertTrue(report["sources"][0]["unchanged"])
        for p, content in before.items():
            self.assertEqual(p.read_bytes(), content)

    def test_missing_outcome_source_and_context_are_not_gold(self):
        report = self.audit([{"ts": 1, "round_no": 1, "hands": {"bottom": [1,2,3,4,5,6,7,8,9,21,22,23,25]}}])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["manual_review"], 1)
        for code in ("source_missing", "open_meld_context_missing", "outcome_missing_or_incomplete", "target_rules_unverified"):
            self.assertIn(code, report["issue_counts"])

    def test_malformed_and_unknown_records_do_not_crash_or_become_gold(self):
        path = self.root / "broken.jsonl"
        path.write_text('{bad json\nnull\n[]\n{"random":"unknown"}\n', encoding="utf-8")
        before = path.read_bytes()
        report = audit_dataset(jsonl_paths=[path])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["rejected"], 3)
        self.assertEqual(report["summary"]["manual_review"], 1)
        self.assertEqual(report["sources"][0]["source_issues"][0]["code"], "malformed_json")
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_field_types_are_flagged_without_type_errors(self):
        record = self.complete_record()
        record.update(data_origin={}, review_status=[], plays=[{"tile_id": None, "seq": [], "player": []}])
        record["result"] = {"win_type": {}, "winner": [], "fangpao_by": {}}
        report = self.audit([record])
        self.assertEqual(report["summary"]["rejected"], 1)
        self.assertIn("invalid_play_sequence", report["issue_counts"])
        self.assertIn("unknown_player_seat", report["issue_counts"])

    def test_target_ids_multiplicity_and_explicit_meld_count(self):
        records = []
        for hand, context in [([1]*5 + [2]*3 + [3]*3 + [4]*3, 0),
                              ([1,2,3,4,5,6,7,8,9,21,22,23,25,61], 0),
                              ([1,2,3,4,5,6,7,8,9,21,22], 0),
                              ([1,2,3,4,5,6,7,8,9,21,22,23,25,None], 0)]:
            record = self.complete_record()
            record.update(hand=hand, open_meld_count=context, actual_discard=None)
            records.append(record)
        report = self.audit(records)
        self.assertEqual(report["summary"]["accepted"], 0)
        for code in ("tile_over_four", "out_of_target_tile", "hand_count_meld_mismatch", "unknown_tile"):
            self.assertIn(code, report["issue_counts"])

    def test_known_exposed_meld_count_accepts_short_target_hand(self):
        record = self.complete_record()
        record.update(hand=[1,2,3,4,5,6,7,8,9,25,47], open_meld_count=1)
        report = self.audit([record])
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_duplicate_frame_and_game_record_ids_exclude_both_occurrences(self):
        record = self.complete_record()
        report = self.audit([record, copy.deepcopy(record)])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["rejected"], 2)
        self.assertEqual(report["issue_counts"]["duplicate_source_frame"], 2)
        self.assertEqual(report["issue_counts"]["duplicate_game_record_id"], 2)

    def test_duplicate_game_ids_on_round_records_are_not_counted_twice(self):
        records = []
        for timestamp in (1, 2):
            record = self.complete_record()
            record["hands"] = {"bottom": record.pop("hand")}
            record.update(ts=timestamp, round_no=1, game_id="explicit-game-id")
            records.append(record)
        report = self.audit(records)
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["issue_counts"]["duplicate_game_id"], 2)

    def test_synthetic_estimated_and_unverified_records_never_count_as_gold(self):
        for field, value, expected_code in [("synthetic", True, "synthetic_record"),
                                             ("verified", False, "explicitly_unverified"),
                                             ("reviewer", "agent", "frame_backed_human_review_missing")]:
            with self.subTest(field=field):
                record = self.complete_record()
                record[field] = value
                report = self.audit([record])
                self.assertEqual(report["summary"]["accepted"], 0)
                self.assertIn(expected_code, report["issue_counts"])
        record = self.complete_record()
        record["result"]["estimated"] = True
        self.assertIn("estimated_or_inferred_data", self.audit([record])["issue_counts"])

    def test_hash_mismatch_and_visible_conservation_are_rejected(self):
        record = self.complete_record()
        record["source_sha256"] = "0" * 64
        record["rivers"] = {"top": [1,1,1,1]}
        report = self.audit([record])
        self.assertEqual(report["summary"]["rejected"], 1)
        self.assertIn("source_hash_mismatch", report["issue_counts"])
        self.assertIn("visible_tile_conservation", report["issue_counts"])

    def make_storage_db(self, name="games.db", invalid_tile=False):
        path = self.root / name
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript('''
                CREATE TABLE rounds(id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT, self_score INTEGER,
                  is_win INTEGER, result TEXT, rounds_played INTEGER, note TEXT, win_type TEXT, winner_seat TEXT, losing_seat TEXT, deltas TEXT);
                CREATE TABLE plays(id INTEGER PRIMARY KEY, round_id INTEGER, seq INTEGER, tile_id INTEGER,
                  tile_name TEXT, player TEXT, turn_no INTEGER, is_caishen INTEGER, timestamp TEXT);
                INSERT INTO rounds(id,result,win_type,winner_seat) VALUES(1,'win_zimo','zimo','bottom');
            ''')
            connection.execute("INSERT INTO plays VALUES(1,1,1,?,'sample','bottom',1,0,'time')", (61 if invalid_tile else 1,))
            connection.commit()
        return path

    def test_actual_sqlite_storage_schema_is_readonly_and_incomplete_not_gold(self):
        path = self.make_storage_db()
        before = path.read_bytes()
        names_before = set(self.root.iterdir())
        report = audit_dataset(sqlite_paths=[path])
        self.assertEqual(report["sources"][0]["detected_schema"], "storage")
        self.assertEqual(report["summary"]["manual_review"], 1)
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertIn("hand_snapshot_missing", report["issue_counts"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), names_before)

    def test_sqlite_invalid_tile_rejected_and_unsupported_schema_reported(self):
        path = self.make_storage_db(invalid_tile=True)
        report = audit_dataset(sqlite_paths=[path])
        self.assertEqual(report["summary"]["rejected"], 1)
        self.assertIn("out_of_target_tile", report["issue_counts"])
        unknown = self.root / "other.db"
        with closing(sqlite3.connect(unknown)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        report = audit_dataset(sqlite_paths=[unknown])
        self.assertEqual(report["summary"]["records_audited"], 0)
        self.assertEqual(report["sources"][0]["source_issues"][0]["code"], "unsupported_sqlite_schema")

    def test_actual_replay_schema_pending_review_remains_unknown(self):
        path = self.root / "replay.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript('''
                CREATE TABLE rounds(id INTEGER PRIMARY KEY, source_sha256 TEXT, source_clip TEXT, round_idx INTEGER,
                  take TEXT, started_at TEXT, ended_at TEXT, result TEXT, is_win INTEGER, note TEXT, gold_label TEXT,
                  review_status TEXT, evidence_path TEXT, classifier_version TEXT, created_at TEXT);
                CREATE TABLE plays(id INTEGER PRIMARY KEY, round_id INTEGER, seq INTEGER, tile_id INTEGER,
                  tile_name TEXT, action_type TEXT, player TEXT, turn_no INTEGER, timestamp_seconds REAL, is_caishen INTEGER);
                INSERT INTO rounds(id,source_clip,result,review_status) VALUES(1,'source.mp4','unknown','pending');
                INSERT INTO plays VALUES(1,1,1,1,'1万','discard','bottom',1,9.0,0);
            ''')
        before = path.read_bytes()
        report = audit_dataset(sqlite_paths=[path])
        self.assertEqual(report["sources"][0]["detected_schema"], "replay")
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["manual_review"], 1)
        self.assertIn("explicitly_unverified", report["issue_counts"])
        self.assertEqual(path.read_bytes(), before)

    def test_missing_sqlite_does_not_create_a_database(self):
        path = self.root / "not_existing.db"
        report = audit_dataset(sqlite_paths=[path])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["sources"][0]["source_issues"][0]["code"], "source_read_failed")
        self.assertFalse(path.exists())

    def test_nonempty_sqlite_wal_is_not_silently_ignored(self):
        path = self.make_storage_db()
        Path(str(path) + "-wal").write_bytes(b"unsnapshotted wal")
        report = audit_dataset(sqlite_paths=[path])
        self.assertEqual(report["summary"]["records_audited"], 0)
        self.assertEqual(report["sources"][0]["source_issues"][0]["code"], "sqlite_wal_requires_consistent_snapshot")

    def test_audit_bounds_are_explicit_and_do_not_claim_full_acceptance(self):
        path = self.jsonl([self.complete_record(), self.complete_record()])
        report = audit_dataset(jsonl_paths=[path], max_records=1)
        self.assertEqual(report["summary"]["records_audited"], 1)
        self.assertTrue(report["sources"][0]["truncated"])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertIn("audit_coverage_truncated", report["issue_counts"])

    def test_cli_cannot_overwrite_input(self):
        path = self.jsonl([self.complete_record()])
        before = path.read_bytes()
        script = Path(__file__).resolve().parents[1] / "tools" / "audit_dataset.py"
        completed = subprocess.run([sys.executable, str(script), "--jsonl", str(path), "--output", str(path)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("must not overwrite", completed.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_real_observation_recorder_output_is_structurally_valid_but_never_gold(self):
        import numpy as np
        from observation import ObservationRecorder

        recorder = ObservationRecorder(self.root / "capture", "no-device-used", interval=0)
        frame = np.zeros((90, 160, 3), np.uint8)
        record = recorder.record(
            frame, boxes=[(5,60,15,85),(16,60,26,85),(27,60,37,85)],
            predictions=[], recognized=[1,2,3], complete=True,
            rounds_started=1, rounds_done=0, now=1,
        )
        png = recorder.output / record["source_frame"]
        before = {path: path.read_bytes() for path in (recorder.log_path, png)}
        report = audit_dataset(jsonl_paths=[recorder.log_path])
        self.assertEqual(report["records"][0]["schema"], "observation/v1")
        self.assertEqual(report["summary"]["technical_valid"], 1)
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["manual_review"], 1)
        self.assertEqual(report["coverage"]["source_verified"], 1)
        self.assertEqual(report["coverage"]["outcome_known"], 0)
        self.assertIn("observation_predictions_are_not_gold_labels", report["issue_counts"])
        self.assertIn("pixel_hash_declared_not_recomputed", report["issue_counts"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_observation_metadata_does_not_override_unverified_capture_scope(self):
        record = self.complete_record()
        record.update(schema="observation/v1", session_id="session", serial="no-device", timestamp="2026-10-14T00:00:00Z",
                      source_frame="frame.png", source_sha256=sha256_file(self.frame), frame_pixel_sha256="1"*64,
                      shape=[1,1,3], detected_boxes=[[0,0,1,1]], predictions=[], recognized_tiles=[1],
                      all_candidates_classified=True, candidate_round_event=None, candidate_rounds_started=1,
                      candidate_rounds_done=0, retention_reason="candidate_tile_row", gameplay_variant="unverified",
                      gold_labels=None, review_status="unreviewed", outcome=None)
        report = self.audit([record])
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertIn("observation_predictions_are_not_gold_labels", report["issue_counts"])
        self.assertEqual(report["coverage"]["outcome_known"], 0)
        self.assertEqual(report["coverage"]["human_review_verified"], 0)
        self.assertEqual(report["coverage"]["target_rules_verified"], 0)


if __name__ == "__main__":
    unittest.main()
