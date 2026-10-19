"""Read-only audit of actual storage/replay SQLite and existing game JSONL schemas.

Acceptance means frame-backed, human-reviewed target-game evidence with known
outcome and legal tile observations. Parseable logs are not automatically gold.
Optional explicit metadata (reviewer/evidence_frame/rules) is never invented.
SQLite is opened immutable and read-only; nonempty WAL requires a snapshot first.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

TARGET_IDS = set(range(1, 10)) | set(range(21, 30)) | set(range(41, 50)) | {55}
VALID_SEATS = {0, 1, 2, 3, "0", "1", "2", "3", "bottom", "top", "left", "right"}
OUTCOMES = {"zimo": "zimo", "自摸": "zimo", "point": "fangpao", "fangpao": "fangpao",
            "ron": "fangpao", "点炮": "fangpao", "接炮": "fangpao", "lose": "fangpao",
            "draw": "draw", "流局": "draw"}
RULE_REQUIREMENTS = {"tile_count": 112, "fixed_laizi": 55, "player_count": 4,
                     "can_fangpao": True, "can_qidui": True, "can_chi": False}
STORAGE_ROUNDS = {"id", "started_at", "ended_at", "self_score", "is_win", "result", "rounds_played", "note"}
STORAGE_PLAYS = {"id", "round_id", "seq", "tile_id", "tile_name", "player", "turn_no", "is_caishen", "timestamp"}
REPLAY_ROUNDS = {"id", "source_sha256", "source_clip", "round_idx", "take", "started_at", "ended_at",
                 "result", "is_win", "note", "gold_label", "review_status", "evidence_path", "classifier_version", "created_at"}
REPLAY_PLAYS = {"id", "round_id", "seq", "tile_id", "tile_name", "action_type", "player", "turn_no", "timestamp_seconds", "is_caishen"}
COVERAGE_KEYS = ("schema_known", "hand_observed", "hand_count_context", "source_verified",
                 "human_review_verified", "target_rules_verified", "outcome_known")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue(issues, code, field=None, *, error=False, detail=None):
    item = {"code": code, "severity": "error" if error else "warning"}
    if field:
        item["field"] = field
    if detail is not None:
        item["detail"] = detail
    issues.append(item)


def _path(value, source_path, evidence_root):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    base = Path(evidence_root or Path(source_path).parent).resolve()
    path = Path(value)
    candidate = (path if path.is_absolute() else base / path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _tiles(value, field, issues):
    if not isinstance(value, list):
        _issue(issues, "invalid_tile_list", field, error=True)
        return None
    valid = []
    for tile in value:
        if type(tile) is not int:
            _issue(issues, "unknown_tile", field, error=True)
        elif tile not in TARGET_IDS:
            _issue(issues, "out_of_target_tile", field, error=True, detail=tile)
        else:
            valid.append(tile)
    excess = {str(tile): count for tile, count in Counter(valid).items() if count > 4}
    if excess:
        _issue(issues, "tile_over_four", field, error=True, detail=excess)
    return valid if len(valid) == len(value) else None


def _hand(value, field, meld_count, issues):
    hand = _tiles(value, field, issues)
    if hand is None:
        return None
    if meld_count is None:
        _issue(issues, "open_meld_context_missing", field)
        if len(hand) not in {13 - 3 * m + extra for m in range(5) for extra in (0, 1)}:
            _issue(issues, "illegal_target_hand_count", field, error=True, detail=len(hand))
    elif type(meld_count) is not int or not 0 <= meld_count <= 4:
        _issue(issues, "invalid_open_meld_count", field, error=True)
    elif len(hand) not in (13 - 3 * meld_count, 14 - 3 * meld_count):
        _issue(issues, "hand_count_meld_mismatch", field, error=True,
               detail={"count": len(hand), "open_meld_count": meld_count})
    return hand


def _schema(record, schema_hint):
    if schema_hint in ("storage", "replay"):
        return "sqlite_" + schema_hint
    if record.get("schema") == "observation/v1":
        return "observation/v1"
    if "hand" in record and any(key in record for key in ("actual_discard", "gamelog_id", "source_clip")):
        return "decision_point"
    if "ts" in record and any(key in record for key in ("hands", "round_no", "win_type")):
        return "round_log"
    if record.get("type") == "decision" and "discard" in record and "turn" in record:
        return "technical_decision"
    return None


def _seat(value):
    return type(value) in (int, str) and value in VALID_SEATS


def _frame_exists(path):
    try:
        if path is None or not path.is_file():
            return False
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return False
    return header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")


def _observation(record, issues):
    """Validate the exact ObservationRecorder fields, without treating predictions as labels."""
    required = {"schema", "session_id", "timestamp", "serial", "source_frame", "source_sha256",
                "frame_pixel_sha256", "shape", "detected_boxes", "predictions", "recognized_tiles",
                "all_candidates_classified", "candidate_round_event", "candidate_rounds_started",
                "candidate_rounds_done", "retention_reason", "gameplay_variant", "gold_labels", "review_status", "outcome"}
    missing = sorted(required - record.keys())
    if missing:
        _issue(issues, "observation_fields_missing", error=True, detail=missing)
    for field in ("session_id", "serial", "timestamp"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            _issue(issues, "invalid_observation_identity", field, error=True)
    shape = record.get("shape")
    shape_valid = isinstance(shape, list) and len(shape) == 3 and all(type(value) is int and value > 0 for value in shape) and shape[2] == 3
    if not shape_valid:
        _issue(issues, "invalid_observation_shape", error=True)
    boxes = record.get("detected_boxes")
    if not isinstance(boxes, list):
        _issue(issues, "invalid_observation_boxes", error=True)
    elif shape_valid:
        height, width, _ = shape
        for box in boxes:
            if not (isinstance(box, list) and len(box) == 4 and all(type(value) is int for value in box)
                    and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                _issue(issues, "observation_box_out_of_frame", error=True)
    if not isinstance(record.get("predictions"), list):
        _issue(issues, "invalid_observation_predictions", error=True)
    recognized = _tiles(record.get("recognized_tiles"), "recognized_tiles_predictions", issues)
    complete = record.get("all_candidates_classified")
    if type(complete) is not bool:
        _issue(issues, "invalid_observation_complete_flag", error=True)
    elif complete and isinstance(boxes, list) and recognized is not None and len(recognized) != len(boxes):
        _issue(issues, "observation_complete_count_mismatch", error=True)
    for field in ("candidate_rounds_started", "candidate_rounds_done"):
        if type(record.get(field)) is not int or record[field] < 0:
            _issue(issues, "invalid_candidate_round_count", field, error=True)
    pixel_hash = record.get("frame_pixel_sha256")
    if not isinstance(pixel_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", pixel_hash) is None:
        _issue(issues, "invalid_declared_pixel_hash", error=True)
    else:
        _issue(issues, "pixel_hash_declared_not_recomputed")
    _issue(issues, "observation_predictions_are_not_gold_labels")


def audit_record(record, *, reference, source_path, evidence_root=None, schema_hint=None, hash_cache=None):
    issues = []
    coverage = {key: False for key in COVERAGE_KEYS}
    result = {"reference": reference, "schema": None, "issues": issues, "coverage": coverage}
    if not isinstance(record, dict):
        _issue(issues, "record_not_object", error=True)
        result.update(status="rejected", technical_valid=False)
        return result
    schema = result["schema"] = _schema(record, schema_hint)
    coverage["schema_known"] = schema is not None
    if schema is None:
        _issue(issues, "unsupported_record_schema")
    if schema == "observation/v1":
        _observation(record, issues)
    if record.get("synthetic") is True or record.get("data_origin") in ("synthetic", "simulated", "test"):
        _issue(issues, "synthetic_record", error=True)
    if "synthetic" in str(source_path).lower():
        _issue(issues, "filename_suggests_synthetic")
    if record.get("verified") is False or record.get("review_status") in ("pending", "unverified", "unreviewed", "rejected"):
        _issue(issues, "explicitly_unverified")
    for field in ("ts", "timestamp_seconds"):
        if field in record and (type(record[field]) not in (int, float) or not math.isfinite(record[field]) or record[field] < 0):
            _issue(issues, "invalid_timestamp", field, error=True)

    hands = []
    if "hand" in record:
        hands.append(("hand", record["hand"], record.get("open_meld_count")))
    elif isinstance(record.get("hands"), dict):
        contexts = record.get("open_meld_counts")
        for seat, value in record["hands"].items():
            if value is not None:
                context = contexts.get(seat) if isinstance(contexts, dict) else record.get("open_meld_count")
                hands.append(("hands." + str(seat), value, context))
    all_visible = []
    valid_hands = []
    for field, value, context in hands:
        hand = _hand(value, field, context, issues)
        if hand is not None:
            valid_hands.append(hand)
            all_visible.extend(hand)
    coverage["hand_observed"] = bool(valid_hands)
    coverage["hand_count_context"] = bool(hands) and all(type(context) is int and 0 <= context <= 4 for _, _, context in hands)
    if not hands:
        _issue(issues, "hand_snapshot_missing")
    rivers = record.get("rivers")
    if rivers is not None:
        if not isinstance(rivers, dict):
            _issue(issues, "invalid_rivers", error=True)
        else:
            for seat, value in rivers.items():
                river = _tiles(value, "rivers." + str(seat), issues)
                if river is not None:
                    all_visible.extend(river)
    if any(count > 4 for count in Counter(all_visible).values()):
        _issue(issues, "visible_tile_conservation", error=True)
    for field in ("actual_discard", "discard"):
        if record.get(field) is not None:
            observed = _tiles([record[field]], field, issues)
            if field == "actual_discard" and valid_hands and observed and observed[0] not in valid_hands[0]:
                _issue(issues, "discard_not_in_hand", field, error=True)
    if schema == "decision_point" and record.get("actual_discard") is None:
        _issue(issues, "actual_discard_missing")
    plays = record.get("plays")
    if plays is not None:
        if not isinstance(plays, list):
            _issue(issues, "invalid_plays", error=True)
        else:
            seen_seq = set()
            for index, play in enumerate(plays):
                if not isinstance(play, dict):
                    _issue(issues, "invalid_play_record", error=True)
                    continue
                _tiles([play.get("tile_id")], f"plays.{index}.tile_id", issues)
                if not _seat(play.get("player")):
                    _issue(issues, "unknown_player_seat", f"plays.{index}.player", error=True)
                sequence = play.get("seq")
                if type(sequence) is not int or sequence < 1:
                    _issue(issues, "invalid_play_sequence", error=True)
                elif sequence in seen_seq:
                    _issue(issues, "duplicate_play_sequence", error=True)
                else:
                    seen_seq.add(sequence)
            if not plays:
                _issue(issues, "plays_missing")

    if schema == "observation/v1":
        outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    else:
        outcome = record.get("result") if isinstance(record.get("result"), dict) else record
    outcome_type = outcome.get("win_type")
    win_type = OUTCOMES.get(outcome_type) if isinstance(outcome_type, str) else None
    winner = outcome.get("winner", outcome.get("winner_seat"))
    loser = outcome.get("fangpao_by", outcome.get("losing_seat"))
    coverage["outcome_known"] = win_type == "draw" or (win_type in {"zimo", "fangpao"} and _seat(winner)
                                                       and (win_type == "zimo" or _seat(loser)))
    if not coverage["outcome_known"]:
        _issue(issues, "outcome_missing_or_incomplete")
    if outcome.get("estimated") is True or record.get("discard_inferred") is True:
        _issue(issues, "estimated_or_inferred_data")
    deltas = outcome.get("deltas")
    if isinstance(deltas, str):
        try:
            deltas = json.loads(deltas)
        except (json.JSONDecodeError, TypeError):
            _issue(issues, "invalid_reported_deltas", error=True)
    if deltas:
        if not isinstance(deltas, dict) or len(deltas) != 4:
            _issue(issues, "reported_deltas_incomplete")
        elif all(type(value) in (int, float) and math.isfinite(value) for value in deltas.values()):
            if not math.isclose(sum(deltas.values()), 0, abs_tol=1e-6):
                _issue(issues, "nonzero_sum_reported_deltas", error=True)
        else:
            _issue(issues, "invalid_reported_deltas", error=True)

    source_value = record.get("source_frame") if schema == "observation/v1" else record.get("source_file") or record.get("source_clip")
    source_file = _path(source_value, source_path, evidence_root)
    expected_hash = record.get("source_sha256")
    if not source_value:
        _issue(issues, "source_missing")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None:
        _issue(issues, "source_hash_missing_or_invalid")
    elif source_file is None or not source_file.is_file():
        _issue(issues, "source_file_unavailable")
    else:
        try:
            stat = source_file.stat()
            key = (str(source_file.resolve()), stat.st_mtime_ns, stat.st_size)
            actual_hash = hash_cache.get(key) if hash_cache is not None else None
            if actual_hash is None:
                actual_hash = sha256_file(source_file)
                if hash_cache is not None:
                    hash_cache[key] = actual_hash
            if actual_hash.lower() != expected_hash.lower():
                _issue(issues, "source_hash_mismatch", error=True)
            else:
                coverage["source_verified"] = True
        except OSError:
            _issue(issues, "source_file_unreadable")
    evidence = _path(record.get("evidence_frame") or record.get("evidence_path"), source_path, evidence_root)
    coverage["human_review_verified"] = (record.get("reviewer") == "human" and _frame_exists(evidence)
                                         and record.get("review_status") not in ("pending", "unverified", "unreviewed", "rejected"))
    if not coverage["human_review_verified"]:
        _issue(issues, "frame_backed_human_review_missing")
    rules = record.get("rules")
    coverage["target_rules_verified"] = isinstance(rules, dict) and all(
        key in rules and type(rules[key]) is type(value) and rules[key] == value
        for key, value in RULE_REQUIREMENTS.items()
    )
    if schema == "observation/v1" and record.get("gameplay_variant") == "unverified":
        coverage["target_rules_verified"] = False
    if not coverage["target_rules_verified"]:
        _issue(issues, "target_rules_unverified")
    if schema == "technical_decision":
        _issue(issues, "technical_log_only")
    structural = [item for item in issues if item["severity"] == "error" and item["code"] != "synthetic_record"]
    result["technical_valid"] = bool(schema) and not structural
    result["status"] = "rejected" if any(item["severity"] == "error" for item in issues) else "manual_review" if issues else "accepted"
    return result


def _duplicate_keys(record, schema):
    keys = []
    if schema == "observation/v1" and record.get("source_sha256"):
        keys.append(("source_frame", str(record["source_sha256"]).lower()))
    if record.get("gamelog_id") is not None:
        keys.append(("game_record_id", str(record["gamelog_id"])))
    if record.get("game_id") is not None and schema != "decision_point":
        keys.append(("game_id", str(record["game_id"])))
    source = record.get("source_sha256") or record.get("source_file") or record.get("source_clip")
    if source:
        frame = record.get("source_frame_id", record.get("timestamp_seconds"))
        if frame is not None:
            keys.append(("source_frame", str(source), str(frame)))
        elif record.get("round_idx") is not None and schema != "decision_point":
            keys.append(("source_game", str(source), str(record["round_idx"]), str(record.get("take"))))
    return keys


def _load_jsonl(path, limit):
    records, errors = [], []
    truncated = False
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(records) >= limit:
                truncated = True
                break
            try:
                records.append((f"line:{line_number}", json.loads(line)))
            except json.JSONDecodeError:
                records.append((f"line:{line_number}", None))
                errors.append({"code": "malformed_json", "line": line_number})
    return records, {"format": "jsonl", "truncated": truncated, "source_issues": errors}


def _load_sqlite(path, limit, plays_limit):
    for suffix, code in (("-wal", "sqlite_wal_requires_consistent_snapshot"), ("-journal", "sqlite_journal_requires_consistent_snapshot")):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            return [], {"format": "sqlite", "source_issues": [{"code": code}], "truncated": True}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema = {name: [row["name"] for row in connection.execute(f'PRAGMA table_info("{name}")')]
                  for name in ("rounds", "plays") if name in tables}
        rounds, plays = set(schema.get("rounds", [])), set(schema.get("plays", []))
        kind = "replay" if REPLAY_ROUNDS <= rounds and REPLAY_PLAYS <= plays else "storage" if STORAGE_ROUNDS <= rounds and STORAGE_PLAYS <= plays else None
        info = {"format": "sqlite", "schema": schema, "detected_schema": kind, "source_issues": [], "truncated": False}
        if kind is None:
            info["source_issues"].append({"code": "unsupported_sqlite_schema"})
            return [], info
        total_rounds = connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        info["total_rounds"] = total_rounds
        info["total_plays"] = connection.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        rows = [dict(row) for row in connection.execute("SELECT * FROM rounds ORDER BY id LIMIT ?", (limit,))]
        selected_plays = [dict(row) for row in connection.execute(
            "SELECT * FROM plays WHERE round_id IN (SELECT id FROM rounds ORDER BY id LIMIT ?) ORDER BY round_id,seq,id LIMIT ?",
            (limit, plays_limit + 1))]
        info["plays_truncated"] = len(selected_plays) > plays_limit
        grouped = defaultdict(list)
        for play in selected_plays[:plays_limit]:
            grouped[play["round_id"]].append(play)
        info["truncated"] = total_rounds > limit or info["plays_truncated"]
        records = []
        for row in rows:
            row["plays"] = grouped[row["id"]]
            records.append((f"round:{row['id']}", row))
        return records, info


def audit_dataset(*, jsonl_paths=(), sqlite_paths=(), max_records=1000, max_plays=10000, evidence_root=None):
    """Audit bounded rows without importing write-capable storage/game modules."""
    if type(max_records) is not int or max_records <= 0 or type(max_plays) is not int or max_plays <= 0:
        raise ValueError("max_records and max_plays must be positive integers")
    sources, results, identities, hash_cache = [], [], defaultdict(list), {}
    for format_name, paths in (("jsonl", jsonl_paths), ("sqlite", sqlite_paths)):
        for supplied in paths:
            path = Path(supplied).resolve()
            metadata = {"path": str(path), "format": format_name, "source_issues": []}
            records = []
            try:
                before = sha256_file(path)
                loaded, info = _load_jsonl(path, max_records) if format_name == "jsonl" else _load_sqlite(path, max_records, max_plays)
                metadata.update(info, sha256_before=before)
                after = sha256_file(path)
                metadata.update(sha256_after=after, unchanged=before == after)
                if before != after:
                    metadata["source_issues"].append({"code": "source_changed_during_audit"})
                records = loaded
            except (OSError, UnicodeError, sqlite3.Error) as exc:
                metadata["source_issues"].append({"code": "source_read_failed", "detail": type(exc).__name__ + ": " + str(exc)})
            metadata["records_audited"] = len(records)
            sources.append(metadata)
            for locator, record in records:
                reference = str(path) + "#" + locator
                result = audit_record(record, reference=reference, source_path=path, evidence_root=evidence_root,
                                      schema_hint=metadata.get("detected_schema"), hash_cache=hash_cache)
                if metadata.get("truncated"):
                    _issue(result["issues"], "audit_coverage_truncated")
                if metadata.get("unchanged") is False:
                    _issue(result["issues"], "source_changed_during_audit", error=True)
                for key in _duplicate_keys(record, result["schema"]) if isinstance(record, dict) else []:
                    identities[key].append(len(results))
                results.append(result)
    for key, indexes in identities.items():
        if len(indexes) > 1:
            for index in indexes:
                _issue(results[index]["issues"], "duplicate_" + key[0], error=True,
                       detail={"occurrences": len(indexes)})
    for result in results:
        result["status"] = "rejected" if any(item["severity"] == "error" for item in result["issues"]) else "manual_review" if result["issues"] else "accepted"
    reasons = Counter(issue["code"] for result in results for issue in result["issues"])
    coverage = Counter({key: 0 for key in COVERAGE_KEYS})
    coverage.update(key for result in results for key, value in result["coverage"].items() if value)
    summary = {key: sum(result["status"] == key for result in results) for key in ("accepted", "rejected", "manual_review")}
    summary.update(records_audited=len(results), technical_valid=sum(result["technical_valid"] for result in results),
                   sources=len(sources), sources_with_issues=sum(bool(source["source_issues"]) for source in sources))
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            "acceptance_scope": "Frame-backed human-reviewed target-rule game evidence; no profit or win-rate inference",
            "limits": ["Acceptance checks declared review/rule metadata, source hashes and frame presence; it does not independently authenticate a human reviewer or verify visual labels.",
                       "Technical records, estimated outcomes and stored point proxies do not establish actual game counts, win rate, cash profit or loss.",
                       "For observation/v1, the PNG file SHA256 is verified; the separately declared pixel hash is syntax checked, not recomputed.",
                       "Relative evidence paths resolve only within the explicit evidence root or source file directory."],
            "summary": summary, "coverage": dict(coverage), "issue_counts": dict(reasons), "sources": sources, "records": results}
