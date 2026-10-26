#!/usr/bin/env python3
"""Build fail-closed Tea-2 evidence manifests without altering source evidence.

The default mode is a report-only dry run.  Only frames referenced by supported
JSONL rows are opened; ``--include-orphan-pngs`` explicitly enables the slower
orphan inventory.  ``--write`` publishes the three manifests as one validated
directory bundle and refuses to replace an existing bundle without ``--force``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import numpy as np
except ImportError:  # pragma: no cover - fail closed when unavailable
    np = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - fail closed when unavailable
    Image = None


def _production_target_ids() -> frozenset[int]:
    """Use the active recognizer's vocabulary; never invent production IDs."""
    bot_dir = Path(__file__).resolve().parents[1]
    inserted = False
    if str(bot_dir) not in sys.path:
        sys.path.insert(0, str(bot_dir))
        inserted = True
    try:
        from tile_vision import TARGET_IDS  # pylint: disable=import-outside-toplevel
        values = frozenset(TARGET_IDS)
        if not values or any(type(value) is not int for value in values):
            raise RuntimeError("tile_vision.TARGET_IDS is invalid")
        return values
    except Exception as exc:
        # Unit tests may exercise this standalone file without the production
        # recognizer import graph.  The fallback is exact, not an expansion.
        if "pytest" in sys.modules:
            return frozenset((*range(1, 10), *range(21, 30),
                              *range(41, 50), 61, 62, 63, 64, 67))
        raise RuntimeError("cannot load production tile_vision.TARGET_IDS") from exc
    finally:
        if inserted:
            sys.path.remove(str(bot_dir))


SCHEMA = "tea2-evidence-manifest/v1"
QUARANTINE_SCHEMA = "tea2-quarantine-manifest/v1"
SPLIT_SCHEMA = "tea2-split-manifest/v1"
JSONL_NAMES = {
    "actions.jsonl",
    "follow_session.jsonl",
    "recognition_audit.jsonl",
    "recognition_reviews.jsonl",
    "round_transition.jsonl",
}
ALLOWED_MODE_SOURCES = {"screen_rule_text", "image", "ocr"}
HUMAN_REVIEW_SOURCES = {"human", "manual", "user", "review_ui", "human_review"}
CLAIM_ACTIONS = {"chi", "peng", "kong", "gang", "ming_gang", "an_gang", "jia_gang"}
PRODUCTION_CLAIM_ACTIONS = {"chi", "peng", "kong", "hu", "pass"}
FLAT_ACTIONS = {"discard"}
VALID_TEA_TILE_IDS = _production_target_ids()
BURST_SECONDS = 10.0
STALL_POLL_GAP_SECONDS = 5.0
PHASH_NEIGHBOR_DISTANCE = 4
OUTPUT_NAMES = (
    "tea2_evidence_manifest.jsonl",
    "tea2_quarantine_manifest.jsonl",
    "tea2_split_manifest.json",
)
_DCT32 = None


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Counter):
        return dict(value)
    raise TypeError(type(value).__name__)


def _within(path: Path, root: Path) -> bool:
    """Resolve symlinks and reject every path that escapes the capture root."""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Stream complete JSONL rows; a partial live tail is quarantined."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                if not line.endswith("\n"):
                    yield number, None, "partial_jsonl_tail"
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    yield number, None, "malformed_json"
                    continue
                if not isinstance(value, dict):
                    yield number, None, "json_row_not_object"
                    continue
                yield number, value, None
    except (OSError, UnicodeError):
        yield 0, None, "jsonl_read_failed"


def _read_header(path: Path) -> dict[str, Any]:
    """Read only header-shaped rows; data rows are streamed in a second pass."""
    header: dict[str, Any] = {}
    for _, row, error in _read_jsonl(path):
        if error or not row:
            continue
        if row.get("kind") == "session_header" or (
                "started_at" in row and "event" not in row and "frame" not in row):
            header.update(row)
            continue
        # Production session headers are first.  Do not scan a potentially
        # huge live log merely to rediscover no later header.
        break
    return header


def _discover_jsonl(root: Path) -> list[Path]:
    """Find the five exact log names without walking every PNG in Python.

    Ripgrep is already part of the development environment and is materially
    faster on the existing capture tree.  The standard-library fallback keeps
    the tool portable and never follows directory symlinks.
    """
    rg = shutil.which("rg")
    if rg:
        command = [rg, "--files", "--hidden", "--no-ignore", "--no-messages"]
        for name in sorted(JSONL_NAMES):
            command.extend(("-g", name))
        command.append(str(root))
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="strict", timeout=30,
                                   check=False)
        if completed.returncode not in {0, 1}:
            raise RuntimeError(f"rg_log_discovery_failed:{completed.returncode}")
        paths = [Path(line).resolve(strict=False) for line in completed.stdout.splitlines()
                 if line.strip()]
        return sorted(path for path in paths if path.name in JSONL_NAMES and _within(path, root))
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames
                       if not Path(directory, name).is_symlink()]
        for name in filenames:
            if name in JSONL_NAMES:
                path = Path(directory, name).resolve(strict=False)
                if _within(path, root):
                    found.append(path)
    return sorted(found)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _nested(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(mapping: dict[str, Any], paths: Iterable[tuple[str, ...]]) -> Any:
    for path in paths:
        value = _nested(mapping, *path)
        if value not in (None, ""):
            return value
    return None


def _resolve_frame(value: Any, log_path: Path, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [log_path.parent / raw, root / raw]
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if not _within(resolved, root):
            return resolved  # ImageIndex records the escape without opening it.
        if resolved.exists():
            return resolved
    return candidates[0].resolve(strict=False)


def _dct32():
    global _DCT32
    if np is None:
        return None
    if _DCT32 is None:
        indexes = np.arange(32, dtype=np.float64)
        frequencies = indexes[:, None]
        matrix = np.cos(np.pi * (2.0 * indexes + 1.0) * frequencies / 64.0)
        matrix *= np.sqrt(2.0 / 32.0)
        matrix[0, :] /= np.sqrt(2.0)
        _DCT32 = matrix
    return _DCT32


def _phash64(raw: bytes) -> str | None:
    """Return a vectorized 64-bit DCT perceptual hash."""
    matrix = _dct32()
    if Image is None or matrix is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as source:
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            pixels = np.asarray(source.convert("L").resize((32, 32), resampling),
                                dtype=np.float64)
    except (OSError, ValueError):
        return None
    coefficients = matrix @ pixels @ matrix.T
    block = coefficients[:8, :8].reshape(-1)
    threshold = float(np.median(block[1:]))
    bits = 0
    for value in block:
        bits = (bits << 1) | int(value >= threshold)
    return f"{bits:016x}"


class ImageIndex:
    """Stable cached image inspection, confined to one resolved root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.by_path: dict[str, dict[str, Any]] = {}
        self.phash_by_sha: dict[str, str | None] = {}
        self.open_count = 0

    def inspect(self, path: Path | None, declared_sha: Any = None) -> dict[str, Any]:
        declared = str(declared_sha).lower() if isinstance(declared_sha, str) else None
        if path is None:
            return self._missing(None, declared, confined=True)
        resolved = path.resolve(strict=False)
        if not _within(resolved, self.root):
            return self._missing(resolved, declared, confined=False)
        key = str(resolved)
        cached = self.by_path.get(key)
        if cached is not None:
            result = dict(cached)
            result["declared_sha256"] = declared
            result["declared_hash_verified"] = bool(
                declared and result.get("stable_read") and declared == result.get("sha256"))
            return result
        try:
            before = resolved.stat()
            raw = resolved.read_bytes()
            after = resolved.stat()
            self.open_count += 1
        except OSError:
            return self._missing(resolved, declared, confined=True)
        stable = (before.st_size == after.st_size and
                  before.st_mtime_ns == after.st_mtime_ns and
                  len(raw) == after.st_size)
        digest = hashlib.sha256(raw).hexdigest()
        width = height = None
        if Image is not None:
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
            except (OSError, ValueError):
                pass
        if digest not in self.phash_by_sha:
            self.phash_by_sha[digest] = _phash64(raw)
        base = {
            "path": key,
            "path_confined": True,
            "exists": True,
            "bytes": len(raw),
            "mtime_ns": after.st_mtime_ns,
            "sha256": digest,
            "phash64": self.phash_by_sha[digest],
            "width": width,
            "height": height,
            "native_1600x900": (width, height) == (1600, 900),
            "stable_read": stable,
            "computed_hash_verified": stable,
        }
        self.by_path[key] = base
        result = dict(base)
        result["declared_sha256"] = declared
        result["declared_hash_verified"] = bool(declared and stable and declared == digest)
        return result

    @staticmethod
    def _missing(path: Path | None, declared: str | None, *, confined: bool) -> dict[str, Any]:
        return {
            "path": None if path is None else str(path),
            "path_confined": confined,
            "exists": False,
            "sha256": None,
            "phash64": None,
            "width": None,
            "height": None,
            "native_1600x900": False,
            "stable_read": False,
            "computed_hash_verified": False,
            "declared_sha256": declared,
            "declared_hash_verified": False,
        }


def _mode_evidence(row: dict[str, Any]) -> dict[str, Any] | None:
    candidates = (
        row.get("mode_evidence"),
        _nested(row, "table_state", "action_ui"),
        _nested(row, "turn_gate", "evidence", "action_ui"),
        _nested(row, "frame_facts", "mode_evidence"),
        _nested(row, "vision", "snapshot", "mode_evidence"),
    )
    for evidence in candidates:
        if not isinstance(evidence, dict):
            continue
        players = evidence.get("player_count", evidence.get("mode_players"))
        source = evidence.get("source", evidence.get("mode_source"))
        if players == 2 and source in ALLOWED_MODE_SOURCES and evidence.get("verified") is True:
            return {"player_count": 2, "source": source, "verified": True,
                    "score": evidence.get("score", evidence.get("mode_score"))}
    return None


def _identity(row: dict[str, Any], header: dict[str, Any], log_path: Path) -> dict[str, Any]:
    room_id = _first(row, (("room_id",), ("frame_facts", "room_id"),
                           ("vision", "room_id"), ("vision", "snapshot", "room_id")))
    if room_id is None:
        room_id = _first(header, (("room_id",),))
    round_seq = _first(row, (("round_seq",), ("round_no",),
                              ("frame_facts", "round_no"), ("vision", "round_no")))
    # Never use per-device/session timestamps as match identity.
    match_id = _first(row, (("match_id",), ("room_start_id",), ("match_fingerprint",)))
    if match_id is None:
        match_id = _first(header, (("match_id",), ("room_start_id",),
                                   ("match_fingerprint",)))
    session_id = _first(row, (("session",),)) or header.get("session") or str(log_path.parent)
    caishen = _first(row, (("caishen",), ("frame_facts", "caishen"),
                            ("table_state", "caishen"), ("vision", "caishen")))
    if isinstance(caishen, dict):
        caishen = caishen.get("label") or caishen.get("id")
    return {
        "session_id": str(session_id),
        "serial": _first(row, (("device_serial",), ("serial",))) or header.get("serial"),
        "timestamp": _first(row, (("ts",), ("at",), ("timestamp",))),
        "room_id": None if room_id in (None, "") else str(room_id),
        "match_id": None if match_id in (None, "") else str(match_id),
        "round_seq": round_seq,
        "caishen": caishen,
    }


def _split_group(identity: dict[str, Any]) -> str | None:
    """One complete table match is indivisible; serial and round are excluded."""
    if identity.get("room_id") is None or identity.get("match_id") is None:
        return None
    return f'{identity["room_id"]}|{identity["match_id"]}'


def _base_record(root: Path, log_path: Path, line: int, row: dict[str, Any],
                 header: dict[str, Any], record_type: str) -> dict[str, Any]:
    identity = _identity(row, header, log_path)
    development = header.get("dev") is True or row.get("dev") is True
    return {
        "schema": SCHEMA,
        "record_type": record_type,
        "source_log": str(log_path.resolve()),
        "source_log_rel": str(log_path.resolve().relative_to(root.resolve())),
        "source_line": line,
        "source_kind": log_path.name.removesuffix(".jsonl"),
        "mode": row.get("mode") or header.get("mode"),
        "mode_evidence": _mode_evidence(row),
        "development_session": development,
        **identity,
        "split_group": _split_group(identity),
        "label_usability": {"recognition": False, "transition": False,
                            "stall_negative": False, "strategy": False},
        "strategy_label_usable": False,
        "quarantine_reasons": (["development_session_not_corpus_eligible"]
                               if development else []),
    }


def _meld_tile_ids(row: dict[str, Any]) -> tuple[list[list[int]] | None, int]:
    raw_melds = row.get("melds")
    declared_count = row.get("meld_count")
    if raw_melds is None:
        if declared_count in (None, 0):
            return [], 0
        return None, -1
    if not isinstance(raw_melds, list):
        return None, -1
    groups: list[list[int]] = []
    for raw in raw_melds:
        kind = None
        if isinstance(raw, dict):
            kind = str(raw.get("kind") or raw.get("action") or raw.get("type") or "").lower()
            tiles = (raw.get("tile_ids") if raw.get("tile_ids") is not None
                     else raw.get("tiles") if raw.get("tiles") is not None
                     else raw.get("ids"))
        else:
            tiles = raw
        if not isinstance(tiles, (list, tuple)):
            return None, -1
        group = list(tiles)
        if (len(group) not in {3, 4} or
                any(type(tile) is not int or tile not in VALID_TEA_TILE_IDS
                    for tile in group)):
            return None, -1
        same = len(set(group)) == 1
        ordered = sorted(group)
        sequential = (len(group) == 3 and ordered[0] < 60 and
                      ordered[0] // 10 == ordered[2] // 10 and
                      ordered == list(range(ordered[0], ordered[0] + 3)))
        if kind in {"chi", "chow"} and not sequential:
            return None, -1
        if kind in {"peng", "pong"} and (len(group) != 3 or not same):
            return None, -1
        if kind in {"kong", "gang", "ming_gang", "an_gang", "jia_gang"} \
                and (len(group) != 4 or not same):
            return None, -1
        if not kind and not (same or sequential):
            return None, -1
        groups.append(group)
    if type(declared_count) is int and declared_count != len(groups):
        return None, -1
    return groups, len(groups)


def _valid_tile_label(value: Any, row: dict[str, Any]) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if any(type(tile) is not int or tile not in VALID_TEA_TILE_IDS for tile in value):
        return False
    melds, meld_count = _meld_tile_ids(row)
    if melds is None:
        return False
    if not 0 <= meld_count <= 5:
        return False
    counts = Counter(value)
    for group in melds:
        counts.update(group)
    if any(count > 4 for count in counts.values()):
        return False
    return len(value) in {16 - 3 * meld_count, 17 - 3 * meld_count}


def _review_status(review: dict[str, Any] | None,
                   predicted: Any, row: dict[str, Any]) -> tuple[str | None, list[int] | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(review, dict):
        return None, None, ["human_review_correct_or_corrected_missing"]
    verdict = str(review.get("verdict") or review.get("review_status") or "").lower()
    reviewer = review.get("reviewer") or review.get("reviewer_id")
    source = str(review.get("source") or review.get("review_source") or "").lower()
    stamp = review.get("reviewed_at") or review.get("at") or review.get("timestamp")
    if not isinstance(reviewer, str) or not reviewer.strip() or source not in HUMAN_REVIEW_SOURCES:
        reasons.append("human_review_provenance_missing")
    if _parse_time(stamp) is None:
        reasons.append("human_review_timestamp_missing_or_invalid")
    corrected = review.get("corrected_hand")
    if corrected is None:
        corrected = review.get("corrected")
    label = predicted if verdict == "correct" else corrected if verdict == "corrected" else None
    normalized = list(label) if isinstance(label, list) else None
    if verdict not in {"correct", "corrected"}:
        reasons.append("human_review_correct_or_corrected_missing")
    if not _valid_tile_label(normalized, row):
        reasons.append("recognition_label_invalid_or_wrong_physical_count")
        normalized = None
    status = "corrected" if verdict == "corrected" and normalized else verdict
    return status, normalized, reasons


def _image_reasons(image: dict[str, Any], *, declared: bool = False,
                   prefix: str = "source") -> list[str]:
    reasons: list[str] = []
    if not image.get("path_confined", True):
        reasons.append("path_outside_root")
    if not image.get("exists"):
        reasons.append(f"{prefix}_png_missing")
    elif not image.get("native_1600x900"):
        reasons.append(f"{prefix}_not_native_1600x900")
    if declared:
        if not image.get("declared_hash_verified"):
            reasons.append("declared_sha256_not_verified")
    elif not image.get("computed_hash_verified"):
        reasons.append(f"{prefix}_hash_not_stable")
    if image.get("phash64") is None:
        reasons.append(f"{prefix}_phash_unavailable")
    return reasons


def _recognition_record(root: Path, log_path: Path, line: int,
                        row: dict[str, Any], header: dict[str, Any],
                        reviews: dict[str, dict[str, Any]], images: ImageIndex,
                        referenced: set[str]) -> dict[str, Any]:
    record = _base_record(root, log_path, line, row, header, "recognition")
    frame = _resolve_frame(row.get("frame"), log_path, root)
    if frame is not None and _within(frame, root):
        referenced.add(str(frame.resolve()))
    image = images.inspect(frame, row.get("sha256"))
    review = reviews.get(str(row.get("sha256") or "").lower())
    review_status, label, review_reasons = _review_status(review, row.get("hand_ids"), row)
    record.update({
        "frame_role": "recognition",
        "image": image,
        "human_review": review_status,
        "human_review_provenance": None if not review else {
            "reviewer": review.get("reviewer") or review.get("reviewer_id"),
            "source": review.get("source") or review.get("review_source"),
            "timestamp": review.get("reviewed_at") or review.get("at") or review.get("timestamp"),
        },
        "recognition_label": label,
        "prediction": {"status": row.get("status"), "hand_ids": row.get("hand_ids"),
                       "flags": row.get("flags") or [], "verification": row.get("verification")},
    })
    reasons = record["quarantine_reasons"]
    reasons.extend(_image_reasons(image, declared=True))
    if record["mode"] != "tea_2p" or record["mode_evidence"] is None:
        reasons.append("explicit_tea2_screen_evidence_missing")
    reasons.extend(review_reasons)
    if record["split_group"] is None:
        reasons.append("complete_match_identity_missing")
    if record["round_seq"] is None:
        reasons.append("round_identity_missing")
    if not reasons:
        record["label_usability"]["recognition"] = True
    return record


def _same_session(before: Path | None, after: Path | None,
                  session_id: Any, log_path: Path, root: Path) -> bool:
    if before is None or after is None or not _within(before, root) or not _within(after, root):
        return False
    roots = [log_path.parent.resolve()]
    if isinstance(session_id, str) and session_id:
        candidate = Path(session_id)
        if candidate.is_absolute() and _within(candidate, root):
            roots.insert(0, candidate.resolve())
    for session_root in roots:
        try:
            before.resolve().relative_to(session_root)
            after.resolve().relative_to(session_root)
            return True
        except ValueError:
            continue
    return False


def _post_state(row: dict[str, Any], action: dict[str, Any] | None = None,
                chain: dict[str, Any] | None = None) -> dict[str, Any]:
    for candidate in ((action or {}).get("post_state"), row.get("post_state"),
                      (chain or {}).get("post_state")):
        if isinstance(candidate, dict):
            return candidate
    samples = (chain or {}).get("settle_samples") or (action or {}).get("settle_samples") or []
    return samples[-1] if samples and isinstance(samples[-1], dict) else {}


def _followup_discard(chain: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("followup_discard", "post_claim_discard", "post_meld_discard", "discard"):
        value = chain.get(key)
        if isinstance(value, dict):
            return value
    return None


def _fresh_pair(before: dict[str, Any], after: dict[str, Any], explicit: Any) -> bool:
    inferred = bool(before.get("sha256") and after.get("sha256") and
                    before["sha256"] != after["sha256"] and
                    int(after.get("mtime_ns") or 0) >= int(before.get("mtime_ns") or 0))
    return explicit is True or inferred


def _verified_followup_discard(discard: dict[str, Any] | None, *, root: Path,
                               log_path: Path, record: dict[str, Any],
                               images: ImageIndex, referenced: set[str]) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(discard, dict):
        return None, ["claim_followup_verified_discard_missing"]
    before_path = _resolve_frame(discard.get("before_frame"), log_path, root)
    after_path = _resolve_frame(discard.get("after_frame"), log_path, root)
    for path in (before_path, after_path):
        if path is not None and _within(path, root):
            referenced.add(str(path.resolve()))
    before = images.inspect(before_path, discard.get("before_sha256"))
    after = images.inspect(after_path, discard.get("after_sha256"))
    before_round = discard.get("round_seq", discard.get("before_round_seq"))
    after_round = discard.get("after_round_seq")
    post = discard.get("post_state") if isinstance(discard.get("post_state"), dict) else {}
    if after_round is None:
        after_round = post.get("round_seq", post.get("round_no"))
    before_caishen = discard.get("caishen", discard.get("before_caishen"))
    after_caishen = discard.get("after_caishen", post.get("caishen"))
    same_round = (before_round is not None and after_round is not None and
                  str(before_round) == str(after_round) == str(record.get("round_seq")))
    same_caishen = (before_caishen is not None and after_caishen is not None and
                    str(before_caishen) == str(after_caishen) == str(record.get("caishen")))
    same_session = _same_session(before_path, after_path, record.get("session_id"), log_path, root)
    declared_sessions = [discard.get("session"), discard.get("before_session"),
                         discard.get("after_session")]
    if any(value not in (None, "", record.get("session_id")) for value in declared_sessions):
        same_session = False
    fresh = _fresh_pair(before, after, discard.get("fresh") or discard.get("fresh_frame"))
    reasons: list[str] = []
    if discard.get("actor") != "bot":
        reasons.append("claim_followup_actor_not_bot")
    if discard.get("executed") is not True:
        reasons.append("claim_followup_not_executed")
    if discard.get("semantic_verified") is not True:
        reasons.append("claim_followup_semantic_not_verified")
    for image in (before, after):
        if not image.get("path_confined", True):
            reasons.append("path_outside_root")
    if not before.get("exists") or not after.get("exists"):
        reasons.append("claim_followup_before_or_after_png_missing")
    elif not before.get("native_1600x900") or not after.get("native_1600x900"):
        reasons.append("claim_followup_before_or_after_not_native_1600x900")
    if not before.get("computed_hash_verified") or not after.get("computed_hash_verified"):
        reasons.append("claim_followup_before_or_after_hash_not_stable")
    if not fresh:
        reasons.append("claim_followup_fresh_not_proven")
    if not same_session:
        reasons.append("claim_followup_session_changed_or_unknown")
    if not same_round:
        reasons.append("claim_followup_round_changed_or_unknown")
    if not same_caishen:
        reasons.append("claim_followup_caishen_changed_or_unknown")
    evidence = {
        "actor": discard.get("actor"), "executed": discard.get("executed") is True,
        "semantic_verified": discard.get("semantic_verified") is True,
        "before": before, "after": after, "fresh_before_after": fresh,
        "same_session": same_session, "same_round": same_round,
        "same_caishen": same_caishen,
    }
    if reasons:
        reasons.append("claim_followup_verified_discard_missing")
    return evidence, list(dict.fromkeys(reasons))


def _transition_record(root: Path, log_path: Path, line: int,
                       row: dict[str, Any], header: dict[str, Any],
                       images: ImageIndex, referenced: set[str], *,
                       action_kind: str, actor: Any, executed: bool,
                       semantic_verified: bool, before_value: Any,
                       after_value: Any, action_payload: dict[str, Any] | None = None,
                       chain: dict[str, Any] | None = None) -> dict[str, Any]:
    record = _base_record(root, log_path, line, row, header, "transition")
    before_path = _resolve_frame(before_value, log_path, root)
    after_path = _resolve_frame(after_value, log_path, root)
    for path in (before_path, after_path):
        if path is not None and _within(path, root):
            referenced.add(str(path.resolve()))
    before = images.inspect(before_path)
    after = images.inspect(after_path)
    post = _post_state(row, action_payload, chain)
    after_round = post.get("round_seq", post.get("round_no", row.get("after_round_seq")))
    after_caishen = post.get("caishen", row.get("after_caishen"))
    same_round = (record.get("round_seq") is not None and after_round is not None and
                  str(record["round_seq"]) == str(after_round))
    same_caishen = (record.get("caishen") is not None and after_caishen is not None and
                    str(record["caishen"]) == str(after_caishen))
    fresh = _fresh_pair(before, after, (action_payload or {}).get("fresh") or
                        row.get("fresh") or (action_payload or {}).get("fresh_frame") or
                        row.get("fresh_frame"))
    action = str(action_kind or "unknown").lower()
    followup = None
    followup_reasons: list[str] = []
    if action in CLAIM_ACTIONS:
        followup, followup_reasons = _verified_followup_discard(
            _followup_discard(chain or {}), root=root, log_path=log_path,
            record=record, images=images, referenced=referenced)
    record.update({
        "frame_role": "before_after",
        "action": {"kind": action, "actor": actor, "executed": bool(executed),
                   "semantic_verified": bool(semantic_verified),
                   "fresh_before_after": fresh,
                   "full_chain_verified": action not in CLAIM_ACTIONS or not followup_reasons,
                   "followup_discard": followup},
        "transition": {"before": before, "after": after,
                       "same_session": _same_session(before_path, after_path,
                                                     record.get("session_id"), log_path, root),
                       "same_round": same_round, "same_caishen": same_caishen,
                       "after_round_seq": after_round, "after_caishen": after_caishen},
    })
    reasons = record["quarantine_reasons"]
    if actor != "bot":
        reasons.append("actor_not_explicit_bot")
    if not executed:
        reasons.append("click_not_executed")
    if not semantic_verified:
        reasons.append("semantic_transition_not_verified")
    for image in (before, after):
        if not image.get("path_confined", True):
            reasons.append("path_outside_root")
    if not before.get("exists") or not after.get("exists"):
        reasons.append("before_or_after_png_missing")
    elif not before.get("native_1600x900") or not after.get("native_1600x900"):
        reasons.append("before_or_after_not_native_1600x900")
    if not before.get("computed_hash_verified") or not after.get("computed_hash_verified"):
        reasons.append("before_or_after_hash_not_stable")
    if before.get("phash64") is None or after.get("phash64") is None:
        reasons.append("before_or_after_phash_unavailable")
    if not fresh:
        reasons.append("fresh_before_after_not_proven")
    for key, reason in (("same_session", "session_changed_or_unknown"),
                        ("same_round", "round_changed_or_unknown"),
                        ("same_caishen", "caishen_changed_or_unknown")):
        if not record["transition"][key]:
            reasons.append(reason)
    reasons.extend(followup_reasons)
    if record["mode"] != "tea_2p" or record["mode_evidence"] is None:
        reasons.append("explicit_tea2_screen_evidence_missing")
    if record["split_group"] is None:
        reasons.append("complete_match_identity_missing")
    if record["round_seq"] is None:
        reasons.append("round_identity_missing")
    record["quarantine_reasons"] = list(dict.fromkeys(reasons))
    if not record["quarantine_reasons"]:
        record["label_usability"]["transition"] = True
    return record


def _stall_record(root: Path, log_path: Path, line: int,
                  first: dict[str, Any], last: dict[str, Any], count: int,
                  header: dict[str, Any], images: ImageIndex,
                  referenced: set[str]) -> dict[str, Any]:
    record = _base_record(root, log_path, line, first, header, "stall_negative")
    frame_path = _resolve_frame(first.get("frame"), log_path, root)
    if frame_path is not None and _within(frame_path, root):
        referenced.add(str(frame_path.resolve()))
    image = images.inspect(frame_path)
    start = _parse_time(first.get("ts") or first.get("at"))
    end = _parse_time(last.get("ts") or last.get("at"))
    duration = max(0.0, (end - start).total_seconds()) if start and end else 0.0
    status = first.get("status")
    actionable = bool(status in {"hu", "guard", "error"} or
                      _nested(first, "table_state", "action_discs_seen") or
                      _nested(first, "turn_gate", "state") in {"go", "guard"})
    record.update({"frame_role": "stall_start", "image": image,
                   "stall": {"status": status, "reason": first.get("reason"),
                             "duration_s": round(duration, 3), "poll_rows": count,
                             "actionable": actionable}})
    reasons = record["quarantine_reasons"]
    if duration < BURST_SECONDS:
        reasons.append("stall_burst_shorter_than_10s")
    if not actionable:
        reasons.append("actionable_turn_not_proven")
    reasons.extend(_image_reasons(image, prefix="stall"))
    if record["mode"] != "tea_2p" or record["mode_evidence"] is None:
        reasons.append("explicit_tea2_screen_evidence_missing")
    if record["split_group"] is None:
        reasons.append("complete_match_identity_missing")
    if record["round_seq"] is None:
        reasons.append("round_identity_missing")
    if not reasons:
        record["label_usability"]["stall_negative"] = True
    return record


def _quarantine_row(path: Path, line: int, reason: str) -> dict[str, Any]:
    return {"schema": QUARANTINE_SCHEMA, "source_log": str(path.resolve()),
            "source_line": line, "record_type": "malformed_jsonl",
            "strategy_label_usable": False, "quarantine_reasons": [reason]}


def _as_quarantine(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["schema"] = QUARANTINE_SCHEMA
    result["strategy_label_usable"] = False
    return result


def _load_reviews(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    reviews: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for path in paths:
        if path.name != "recognition_reviews.jsonl":
            continue
        for line, row, error in _read_jsonl(path):
            if error:
                failures.append(_quarantine_row(path, line, error))
                continue
            digest = str((row or {}).get("sha256") or "").lower()
            if len(digest) == 64:
                reviews[digest] = row or {}
    return reviews, failures


def _process_action_row(root: Path, path: Path, line: int, row: dict[str, Any],
                        header: dict[str, Any], images: ImageIndex,
                        referenced: set[str]) -> dict[str, Any]:
    action_kind, defaultable, valid_kind = _action_kind_fields(row)
    if not valid_kind:
        return _action_quarantine(
            root, path, line, row, header, "flat_action_kind_not_allowed")
    if defaultable:
        production_discard = (row.get("verified") is True and
                              isinstance(row.get("frame"), str) and
                              isinstance(row.get("after_frame"), str))
        action_kind = "discard" if production_discard else None
    if action_kind not in FLAT_ACTIONS:
        return _action_quarantine(
            root, path, line, row, header, "flat_action_kind_not_allowed")
    post = row.get("post_state") if isinstance(row.get("post_state"), dict) else {}
    payload = {"fresh": row.get("fresh"), "post_state": {
        "round_seq": row.get("after_round_seq", post.get("round_seq")),
        "caishen": row.get("after_caishen", post.get("caishen")),
    }}
    return _transition_record(
        root, path, line, row, header, images, referenced,
        action_kind=action_kind,
        actor=row.get("actor") or row.get("action_source"),
        executed=row.get("executed") is True or row.get("verified") is True,
        semantic_verified=(row.get("semantic_verified") is True or
                           row.get("verified") is True),
        before_value=row.get("frame"), after_value=row.get("after_frame"),
        action_payload=payload)


def _action_kind_fields(row: dict[str, Any]) -> tuple[str | None, bool, bool]:
    """Return normalized kind, whether true absence may default, and validity."""
    values = [row[key] for key in ("action", "action_kind") if key in row]
    if not values or all(value is None or value == "" for value in values):
        return None, True, True
    explicit = [value for value in values if value is not None and value != ""]
    if any(not isinstance(value, str) for value in explicit):
        return None, False, False
    normalized = [value.strip().lower() for value in explicit]
    if any(not value for value in normalized) or len(set(normalized)) != 1:
        return None, False, False
    return normalized[0], False, True


def _action_quarantine(root: Path, path: Path, line: int, row: dict[str, Any],
                       header: dict[str, Any], reason: str) -> dict[str, Any]:
    record = _base_record(root, path, line, row, header, "transition")
    record["action_id"] = row.get("action_id") or row.get("continuation_of")
    record["quarantine_reasons"].append(reason)
    record["quarantine_reasons"] = list(dict.fromkeys(record["quarantine_reasons"]))
    return _as_quarantine(record)


def _production_claim_transition(
        root: Path, path: Path, claim_line: int, claim_row: dict[str, Any],
        continuation_line: int | None, continuation_row: dict[str, Any] | None,
        header: dict[str, Any], images: ImageIndex,
        referenced: set[str]) -> dict[str, Any]:
    claim = claim_row.get("claim") if isinstance(claim_row.get("claim"), dict) else {}
    auto_click = claim.get("auto_click") if isinstance(claim.get("auto_click"), dict) else {}
    payload = dict(auto_click)
    after_value = auto_click.get("after_frame")
    if after_value is None:
        samples = auto_click.get("settle_samples") or []
        if samples and isinstance(samples[-1], dict):
            after_value = samples[-1].get("frame")
    chain: dict[str, Any] = {}
    if continuation_row is not None:
        follow_identity = _identity(continuation_row, header, path)
        # For chi/peng/kong the unique continuation's pre-discard identity is
        # the first independent proof of the claim click's after-state.
        payload["post_state"] = {
            "round_seq": follow_identity.get("round_seq"),
            "caishen": follow_identity.get("caishen"),
        }
        chain["followup_discard"] = {
            "actor": continuation_row.get("actor") or continuation_row.get("action_source"),
            "executed": continuation_row.get("verified") is True,
            "semantic_verified": continuation_row.get("verified") is True,
            "fresh": continuation_row.get("fresh"),
            "before_frame": continuation_row.get("frame"),
            "after_frame": continuation_row.get("after_frame"),
            "round_seq": follow_identity.get("round_seq"),
            "after_round_seq": continuation_row.get("after_round_seq"),
            "caishen": follow_identity.get("caishen"),
            "after_caishen": continuation_row.get("after_caishen"),
            "session": follow_identity.get("session_id"),
        }
    record = _transition_record(
        root, path, claim_line, claim_row, header, images, referenced,
        action_kind=str(claim.get("action") or "unknown"),
        actor=claim_row.get("actor") or claim_row.get("action_source"),
        executed=auto_click.get("executed") is True,
        semantic_verified=auto_click.get("semantic_verified") is True,
        before_value=claim.get("frame") or claim_row.get("frame"),
        after_value=after_value, action_payload=payload, chain=chain)
    record["action"]["action_id"] = claim_row.get("action_id")
    record["action"]["continuation_source_line"] = continuation_line
    if continuation_row is not None and continuation_row.get("dev") is True:
        record["quarantine_reasons"].append("development_session_not_corpus_eligible")
        record["label_usability"]["transition"] = False
    record["quarantine_reasons"] = list(dict.fromkeys(record["quarantine_reasons"]))
    return record


def _process_actions(root: Path, path: Path, header: dict[str, Any],
                     rows: Iterable[tuple[int, dict[str, Any] | None, str | None]],
                     images: ImageIndex, referenced: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    active: tuple[int, dict[str, Any], dict[str, Any], int, tuple[Any, ...]] | None = None
    pending_claims: dict[str, tuple[int, dict[str, Any]]] = {}
    consumed_claims: set[str] = set()
    completed_claims: dict[str, dict[str, Any]] = {}
    invalid_claims: set[str] = set()
    scanned = 0

    def flush() -> None:
        nonlocal active
        if active is not None:
            line, first, last, count, _ = active
            records.append(_stall_record(root, path, line, first, last, count,
                                         header, images, referenced))
            active = None

    for line, row, error in rows:
        if row is not None:
            scanned += 1
        if error:
            flush()
            malformed.append(_quarantine_row(path, line, error))
            continue
        if not row or row.get("kind") == "session_header" or (
                "started_at" in row and "event" not in row):
            flush()
            continue
        event = row.get("event")
        if event == "action":
            flush()  # A real action always ends a stall, even with the same key later.
            continuation_of = row.get("continuation_of")
            if continuation_of not in (None, ""):
                action_id = str(continuation_of)
                continuation_kind, defaultable, valid_kind = _action_kind_fields(row)
                if defaultable:
                    continuation_is_discard = (
                        row.get("verified") is True and
                        isinstance(row.get("frame"), str) and
                        isinstance(row.get("after_frame"), str))
                else:
                    continuation_is_discard = (
                        valid_kind and continuation_kind == "discard")
                if not continuation_is_discard:
                    pending = pending_claims.pop(action_id, None)
                    if pending is not None:
                        malformed.append(_action_quarantine(
                            root, path, pending[0], pending[1], header,
                            "claim_continuation_action_not_discard"))
                    completed = completed_claims.pop(action_id, None)
                    if completed is not None:
                        completed["quarantine_reasons"].append(
                            "claim_continuation_action_not_discard")
                        completed["label_usability"]["transition"] = False
                        malformed.append(_as_quarantine(completed))
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "claim_continuation_action_not_discard"))
                    invalid_claims.add(action_id)
                    consumed_claims.discard(action_id)
                    continue
                if action_id in consumed_claims:
                    completed = completed_claims.pop(action_id, None)
                    if completed is not None:
                        completed["quarantine_reasons"].append(
                            "duplicate_claim_continuation")
                        completed["label_usability"]["transition"] = False
                        malformed.append(_as_quarantine(completed))
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "duplicate_claim_continuation"))
                elif action_id in invalid_claims:
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "claim_action_id_invalid"))
                elif action_id not in pending_claims:
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "unmatched_claim_continuation"))
                else:
                    claim_line, claim_row = pending_claims.pop(action_id)
                    consumed_claims.add(action_id)
                    completed_claims[action_id] = _production_claim_transition(
                        root, path, claim_line, claim_row, line, row,
                        header, images, referenced)
                continue

            claim = row.get("claim") if isinstance(row.get("claim"), dict) else None
            if row.get("action_kind") == "claim" and claim is not None:
                action = str(claim.get("action") or "unknown").lower()
                if action not in PRODUCTION_CLAIM_ACTIONS:
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "production_claim_action_not_allowed"))
                    continue
                raw_action_id = row.get("action_id")
                if not isinstance(raw_action_id, str) or not raw_action_id.strip():
                    malformed.append(_action_quarantine(
                        root, path, line, row, header,
                        "claim_action_id_missing"))
                    continue
                action_id = raw_action_id.strip()
                if action in CLAIM_ACTIONS:
                    if (action_id in pending_claims or action_id in consumed_claims or
                            action_id in invalid_claims):
                        previous = pending_claims.pop(action_id, None)
                        if previous is not None:
                            malformed.append(_action_quarantine(
                                root, path, previous[0], previous[1], header,
                                "duplicate_claim_action_id"))
                        completed = completed_claims.pop(action_id, None)
                        if completed is not None:
                            completed["quarantine_reasons"].append(
                                "duplicate_claim_action_id")
                            completed["label_usability"]["transition"] = False
                            malformed.append(_as_quarantine(completed))
                        malformed.append(_action_quarantine(
                            root, path, line, row, header,
                            "duplicate_claim_action_id"))
                        invalid_claims.add(action_id)
                    else:
                        pending_claims[action_id] = (line, row)
                    continue
                records.append(_production_claim_transition(
                    root, path, line, row, None, None,
                    header, images, referenced))
                continue

            records.append(_process_action_row(
                root, path, line, row, header, images, referenced))
            continue
        is_stall = event in {"idle", "error"} and row.get("status") in {
            "abstain", "guard", "hu", "error"}
        if not is_stall:
            flush()
            continue
        identity = _identity(row, header, path)
        key = (identity.get("session_id"), identity.get("round_seq"), identity.get("caishen"),
               row.get("status"), row.get("reason"))
        if active is None:
            active = (line, row, row, 1, key)
            continue
        first_line, first, last, count, previous_key = active
        previous_time = _parse_time(last.get("ts") or last.get("at"))
        current_time = _parse_time(row.get("ts") or row.get("at"))
        gap = ((current_time - previous_time).total_seconds()
               if previous_time and current_time else None)
        if key != previous_key or gap is None or gap < 0 or gap > STALL_POLL_GAP_SECONDS:
            flush()
            active = (line, row, row, 1, key)
        else:
            active = (first_line, first, row, count + 1, key)
    flush()
    for action_id, (line, row) in pending_claims.items():
        record = _action_quarantine(
            root, path, line, row, header,
            "claim_continuation_missing_at_end_of_file")
        record["action_id"] = action_id
        malformed.append(record)
    records.extend(completed_claims.values())
    return records, malformed, scanned


def _process_follow(root: Path, path: Path, header: dict[str, Any],
                    rows: Iterable[tuple[int, dict[str, Any] | None, str | None]],
                    images: ImageIndex, referenced: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    scanned = 0
    for line, row, error in rows:
        if row is not None:
            scanned += 1
        if error:
            malformed.append(_quarantine_row(path, line, error))
            continue
        if not row or row.get("kind") == "session_header":
            continue
        decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
        claim = decision.get("claim") if isinstance(decision.get("claim"), dict) else None
        if not claim:
            continue
        action = decision.get("auto_click") or claim.get("auto_click")
        if not isinstance(action, dict):
            continue
        kind = str(claim.get("action") or "unknown")
        chain = claim.get(f"{kind}_chain") or claim.get("claim_chain") or {}
        if not isinstance(chain, dict):
            chain = {}
        after_value = action.get("after_frame")
        if after_value is None:
            samples = chain.get("settle_samples") or []
            if samples and isinstance(samples[-1], dict):
                after_value = samples[-1].get("frame")
        records.append(_transition_record(
            root, path, line, row, header, images, referenced,
            action_kind=kind, actor=decision.get("actor") or claim.get("actor") or row.get("action_source"),
            executed=action.get("executed") is True,
            semantic_verified=action.get("semantic_verified") is True,
            before_value=row.get("frame") or claim.get("frame"),
            after_value=after_value, action_payload=action, chain=chain))
    return records, malformed, scanned


def _round_transition_record(root: Path, path: Path, line: int,
                             row: dict[str, Any], header: dict[str, Any]) -> dict[str, Any]:
    record = _base_record(root, path, line, row, header, "round_transition")
    record["round_transition"] = {"status": row.get("status"),
                                  "clicked": row.get("clicked"),
                                  "reason": row.get("reason")}
    record["quarantine_reasons"] = ["round_transition_not_strategy_or_tile_gold"]
    return _as_quarantine(record)


def _orphan_record(path: Path, images: ImageIndex, seen_sha: dict[str, str]) -> dict[str, Any]:
    image = images.inspect(path)
    record = {"schema": QUARANTINE_SCHEMA, "record_type": "unindexed_png",
              "source_kind": "png", "source_path": str(path.resolve()), "image": image,
              "label_usability": {"recognition": False, "transition": False,
                                  "stall_negative": False, "strategy": False},
              "strategy_label_usable": False,
              "quarantine_reasons": ["png_not_referenced_by_supported_jsonl"]}
    digest = image.get("sha256")
    if digest and digest in seen_sha:
        record["quarantine_reasons"].append("duplicate_sha256")
        record["duplicate_of"] = seen_sha[digest]
    elif digest:
        seen_sha[digest] = str(path.resolve())
    record["quarantine_reasons"].extend(_image_reasons(image, prefix="orphan"))
    return record


def _record_id(record: dict[str, Any]) -> str:
    material = "|".join(str(value or "") for value in (
        record.get("record_type"), _nested(record, "image", "sha256"),
        _nested(record, "transition", "before", "sha256"),
        _nested(record, "transition", "after", "sha256"),
        _nested(record, "action", "kind"), record.get("source_log"), record.get("source_line")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _deduplicate_evidence(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    exact: dict[tuple[Any, ...], str] = {}
    burst: dict[tuple[Any, ...], tuple[str, datetime | None]] = {}
    ordered = sorted(records, key=lambda item: (str(item.get("timestamp") or ""),
                                                 str(item.get("source_log") or ""),
                                                 int(item.get("source_line") or 0)))
    for record in ordered:
        record["sample_id"] = _record_id(record)
        if record["record_type"] == "transition":
            key = ("transition", _nested(record, "action", "kind"),
                   _nested(record, "transition", "before", "sha256"),
                   _nested(record, "transition", "after", "sha256"))
        elif record["record_type"] == "stall_negative":
            # Identical pixels can describe two distinct stalls separated by a
            # successful action.  Preserve event boundaries established while
            # streaming the raw log; burst de-duplication below still catches
            # repeated polling inside one short interval.
            key = ("stall_negative", _nested(record, "image", "sha256"),
                   record.get("source_log"), record.get("source_line"))
        else:
            key = (record["record_type"], _nested(record, "image", "sha256"))
        if all(part is not None for part in key[1:]) and key in exact:
            copy = _as_quarantine(record)
            copy["quarantine_reasons"] = list(copy.get("quarantine_reasons") or []) + ["duplicate_sha256"]
            copy["duplicate_of"] = exact[key]
            duplicates.append(copy)
            continue
        exact[key] = record["sample_id"]
        phash = (_nested(record, "image", "phash64") or
                 _nested(record, "transition", "before", "phash64"))
        if record["record_type"] == "transition":
            burst_key = (
                "transition", _nested(record, "action", "kind"), phash,
                _nested(record, "transition", "after", "phash64"),
                _nested(record, "action", "followup_discard", "after", "phash64"),
                record.get("split_group"), record.get("frame_role"))
        else:
            burst_key = (record["record_type"], phash, record.get("split_group"),
                         record.get("frame_role"))
        stamp = _parse_time(record.get("timestamp"))
        previous = burst.get(burst_key) if phash else None
        if previous and stamp and previous[1] and abs((stamp - previous[1]).total_seconds()) <= BURST_SECONDS:
            copy = _as_quarantine(record)
            copy["quarantine_reasons"] = list(copy.get("quarantine_reasons") or []) + ["duplicate_phash_burst"]
            copy["duplicate_of"] = previous[0]
            duplicates.append(copy)
            continue
        burst[burst_key] = (record["sample_id"], stamp)
        kept.append(record)
    return kept, duplicates


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


class _BKTree:
    """Small metric index used to avoid all-pairs pHash comparisons."""

    def __init__(self) -> None:
        self.root: tuple[int, dict[int, Any]] | None = None

    def add(self, value: int) -> None:
        if self.root is None:
            self.root = (value, {})
            return
        node = self.root
        while True:
            current, children = node
            distance = (current ^ value).bit_count()
            if distance == 0:
                return
            child = children.get(distance)
            if child is None:
                children[distance] = (value, {})
                return
            node = child

    def query(self, value: int, radius: int) -> Iterator[int]:
        if self.root is None:
            return
        stack = [self.root]
        while stack:
            current, children = stack.pop()
            distance = (current ^ value).bit_count()
            if distance <= radius:
                yield current
            low, high = distance - radius, distance + radius
            stack.extend(child for edge, child in children.items() if low <= edge <= high)


def _fingerprints(record: dict[str, Any]) -> tuple[set[str], set[str]]:
    images = [record.get("image"), _nested(record, "transition", "before"),
              _nested(record, "transition", "after"),
              _nested(record, "action", "followup_discard", "before"),
              _nested(record, "action", "followup_discard", "after")]
    shas = {str(image.get("sha256")) for image in images
            if isinstance(image, dict) and image.get("sha256")}
    phashes = {str(image.get("phash64")) for image in images
               if isinstance(image, dict) and image.get("phash64")}
    return shas, phashes


def _assign_split(key: str) -> str:
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def _assign_splits(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Union whole matches and exact/near image contamination before splitting."""
    uf = _UnionFind(len(records))
    group_owner: dict[str, int] = {}
    sha_owner: dict[str, int] = {}
    phash_owner: dict[int, int] = {}
    tree = _BKTree()
    for index, record in enumerate(records):
        group = record.get("split_group")
        if group in group_owner:
            uf.union(index, group_owner[group])
        elif isinstance(group, str):
            group_owner[group] = index
        shas, phashes = _fingerprints(record)
        for digest in shas:
            if digest in sha_owner:
                uf.union(index, sha_owner[digest])
            else:
                sha_owner[digest] = index
        for encoded in phashes:
            value = int(encoded, 16)
            for neighbor in tree.query(value, PHASH_NEIGHBOR_DISTANCE):
                uf.union(index, phash_owner[neighbor])
            if value in phash_owner:
                uf.union(index, phash_owner[value])
            else:
                phash_owner[value] = index
                tree.add(value)

    components: dict[int, list[int]] = {}
    for index in range(len(records)):
        components.setdefault(uf.find(index), []).append(index)
    groups: dict[str, str] = {}
    clusters: dict[str, dict[str, Any]] = {}
    for members in components.values():
        component_groups = sorted({str(records[i]["split_group"]) for i in members})
        component_shas = sorted({sha for i in members for sha in _fingerprints(records[i])[0]})
        material = "|".join([*component_groups, *component_shas])
        cluster_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
        split = _assign_split(cluster_id)
        clusters[cluster_id] = {"split": split, "groups": component_groups,
                                "records": len(members)}
        for index in members:
            records[index]["contamination_cluster"] = cluster_id
            records[index]["split"] = split
        for group in component_groups:
            groups[group] = split
    return {
        "schema": SPLIT_SCHEMA,
        "grouping": "complete match: room_id|match_id (serial and round excluded)",
        "contamination": f"union exact SHA-256 and pHash Hamming <= {PHASH_NEIGHBOR_DISTANCE}",
        "assignment": "sha256(contamination_cluster) buckets: train 0-79, validation 80-89, test 90-99",
        "groups": dict(sorted(groups.items())),
        "clusters": dict(sorted(clusters.items())),
        "counts": dict(Counter(groups.values())),
    }


def _bounded_rows(path: Path, state: dict[str, Any]) -> Iterator[
        tuple[int, dict[str, Any] | None, str | None]]:
    for item in _read_jsonl(path):
        remaining = state.get("remaining")
        if remaining is not None:
            if remaining <= 0:
                state["rows_truncated"] = True
                return
            state["remaining"] = remaining - 1
        line, row, error = item
        since = state.get("since")
        if since is not None and row and row.get("kind") != "session_header":
            stamp = _parse_time(row.get("ts") or row.get("at") or row.get("timestamp"))
            try:
                too_old = stamp is None or stamp < since
            except TypeError:  # mixed naive/aware timestamps are not trusted
                too_old = True
            if too_old:
                state["since_skipped"] += 1
                continue
        yield line, row, error


def _header_and_bounded_stream(
        path: Path, state: dict[str, Any]) -> tuple[
            dict[str, Any], Iterator[tuple[int, dict[str, Any] | None, str | None]]]:
    """Extract leading headers from the same budgeted stream used for data."""
    rows = iter(_bounded_rows(path, state))
    header: dict[str, Any] = {}
    for item in rows:
        _, row, error = item
        if error or not row:
            return header, chain((item,), rows)
        if row.get("kind") == "session_header" or (
                "started_at" in row and "event" not in row and "frame" not in row):
            header.update(row)
            continue
        return header, chain((item,), rows)
    return header, iter(())


def build(root: str | Path, *, include_orphan_pngs: bool = False,
          max_jsonl: int | None = None, max_rows: int | None = None,
          since: str | datetime | None = None) -> dict[str, Any]:
    """Scan ``root`` read-only and return manifests in memory."""
    if max_jsonl is not None and max_jsonl <= 0:
        raise ValueError("max_jsonl_must_be_positive")
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows_must_be_positive")
    if isinstance(since, str):
        since_value = _parse_time(since)
        if since_value is None:
            raise ValueError("since_must_be_iso8601")
    elif isinstance(since, datetime) or since is None:
        since_value = since
    else:
        raise TypeError("since must be ISO-8601 text, datetime, or None")
    source_root = Path(root).resolve(strict=True)
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    discovered_jsonl = _discover_jsonl(source_root)
    jsonl_paths = (discovered_jsonl[:max_jsonl]
                   if max_jsonl is not None else discovered_jsonl)
    reviews, quarantine = _load_reviews(jsonl_paths)
    images = ImageIndex(source_root)
    referenced: set[str] = set()
    candidates: list[dict[str, Any]] = []
    scanned_rows = Counter()
    bounds = {"remaining": max_rows, "rows_truncated": False,
              "since": since_value, "since_skipped": 0}

    for path in jsonl_paths:
        if path.name == "recognition_reviews.jsonl":
            continue
        header, rows = _header_and_bounded_stream(path, bounds)
        if path.name == "recognition_audit.jsonl":
            for line, row, error in rows:
                if row is not None:
                    scanned_rows[path.name] += 1
                if error:
                    quarantine.append(_quarantine_row(path, line, error))
                elif row and row.get("kind") != "session_header":
                    candidates.append(_recognition_record(
                        source_root, path, line, row, header, reviews, images, referenced))
        elif path.name == "actions.jsonl":
            records, failures, count = _process_actions(
                source_root, path, header, rows, images, referenced)
            candidates.extend(records)
            quarantine.extend(failures)
            scanned_rows[path.name] += count
        elif path.name == "follow_session.jsonl":
            records, failures, count = _process_follow(
                source_root, path, header, rows, images, referenced)
            candidates.extend(records)
            quarantine.extend(failures)
            scanned_rows[path.name] += count
        elif path.name == "round_transition.jsonl":
            for line, row, error in rows:
                if row is not None:
                    scanned_rows[path.name] += 1
                if error:
                    quarantine.append(_quarantine_row(path, line, error))
                elif row and row.get("kind") != "session_header":
                    quarantine.append(_round_transition_record(source_root, path, line, row, header))

    evidence: list[dict[str, Any]] = []
    for record in candidates:
        if record.get("quarantine_reasons"):
            quarantine.append(_as_quarantine(record))
        elif any((record.get("label_usability") or {}).values()):
            evidence.append(record)
        else:
            record["quarantine_reasons"] = ["no_usable_label"]
            quarantine.append(_as_quarantine(record))
    evidence, duplicates = _deduplicate_evidence(evidence)
    quarantine.extend(duplicates)

    orphan_count = 0
    if include_orphan_pngs:
        seen_sha: dict[str, str] = {}
        for path in sorted(source_root.rglob("*.png")):
            if not _within(path, source_root):
                continue
            if str(path.resolve()) not in referenced:
                orphan_count += 1
                quarantine.append(_orphan_record(path, images, seen_sha))

    split_manifest = _assign_splits(evidence)
    reason_counts = Counter(reason for record in quarantine
                            for reason in record.get("quarantine_reasons") or [])
    report = {
        "root": str(source_root), "dry_run": True,
        "scan_mode": ("bounded_preflight" if any(
            value is not None for value in (max_jsonl, max_rows, since_value))
                      else "complete"),
        "complete_scan": all(value is None for value in
                             (max_jsonl, max_rows, since_value)),
        "limits": {"max_jsonl": max_jsonl, "max_rows": max_rows,
                   "since": since_value.isoformat() if since_value else None,
                   "rows_truncated": bool(bounds["rows_truncated"]),
                   "since_skipped": int(bounds["since_skipped"])},
        "jsonl_files_discovered": len(discovered_jsonl),
        "jsonl_files": len(jsonl_paths), "scanned_rows": dict(scanned_rows),
        "human_reviews": len(reviews), "referenced_frames": len(referenced),
        "png_files_inspected": images.open_count,
        "orphan_scan_enabled": include_orphan_pngs, "orphan_pngs": orphan_count,
        "evidence_records": len(evidence), "quarantine_records": len(quarantine),
        "evidence_types": dict(Counter(row["record_type"] for row in evidence)),
        "quarantine_reasons": dict(reason_counts.most_common()),
    }
    return {"evidence": evidence, "quarantine": quarantine,
            "split": split_manifest, "report": report}


def _write_file(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_bundle(directory: Path) -> None:
    names = {entry.name for entry in directory.iterdir()}
    if names != set(OUTPUT_NAMES):
        raise ValueError("manifest_bundle_incomplete")
    for name in OUTPUT_NAMES[:2]:
        for line in (directory / name).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            expected = SCHEMA if name == OUTPUT_NAMES[0] else QUARANTINE_SCHEMA
            if not isinstance(row, dict) or row.get("schema") != expected:
                raise ValueError(f"invalid_manifest_row:{name}")
    split = json.loads((directory / OUTPUT_NAMES[2]).read_text(encoding="utf-8"))
    if not isinstance(split, dict) or split.get("schema") != SPLIT_SCHEMA:
        raise ValueError("invalid_split_manifest")


def write_manifests(result: dict[str, Any], output_dir: str | Path, *,
                    force: bool = False) -> list[Path]:
    """Publish a validated three-file bundle without exposing a half-written set."""
    source_root = Path(result["report"]["root"]).resolve(strict=True)
    target = Path(output_dir).resolve(strict=False)
    if not _within(target, source_root):
        raise ValueError("output_path_outside_root")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        raise FileExistsError(target)
    if target.exists() and ({item.name for item in target.iterdir()} - set(OUTPUT_NAMES)):
        raise ValueError("refuse_to_replace_directory_with_unrelated_files")

    evidence_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                       default=_json_default) + "\n"
                            for row in result["evidence"])
    quarantine_text = "".join(json.dumps(_as_quarantine(row), ensure_ascii=False,
                                         sort_keys=True, default=_json_default) + "\n"
                              for row in result["quarantine"])
    split_text = json.dumps(result["split"], ensure_ascii=False, indent=2,
                            sort_keys=True, default=_json_default) + "\n"
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    backup: Path | None = None
    try:
        for name, content in zip(OUTPUT_NAMES, (evidence_text, quarantine_text, split_text)):
            _write_file(stage / name, content)
        _validate_bundle(stage)
        if target.exists():
            backup = target.with_name(f".{target.name}.backup-{os.getpid()}")
            if backup.exists():
                raise FileExistsError(backup)
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except BaseException:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return [target / name for name in OUTPUT_NAMES]


def main(argv: list[str] | None = None) -> int:
    bot_dir = Path(__file__).resolve().parents[1]
    default_root = bot_dir.parent / "采集"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root,
                        help="capture root to scan read-only")
    parser.add_argument("--out-dir", type=Path,
                        help="manifest directory, necessarily below ROOT")
    parser.add_argument("--write", action="store_true",
                        help="explicitly write the derived three-file bundle")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing manifest-only bundle")
    parser.add_argument("--include-orphan-pngs", action="store_true",
                        help="explicitly scan all PNGs for unreferenced evidence")
    parser.add_argument("--max-jsonl", type=int,
                        help="bounded dry-run preflight: inspect at most N JSONL files")
    parser.add_argument("--max-rows", type=int,
                        help="bounded dry-run preflight: stream at most N JSONL rows total")
    parser.add_argument("--since",
                        help="bounded dry-run preflight: only rows at/after ISO-8601 time")
    args = parser.parse_args(argv)
    if args.force and not args.write:
        parser.error("--force requires --write")
    if args.write and any(value is not None for value in
                          (args.max_jsonl, args.max_rows, args.since)):
        parser.error("bounded preflight options cannot be used with --write")
    result = build(args.root, include_orphan_pngs=args.include_orphan_pngs,
                   max_jsonl=args.max_jsonl, max_rows=args.max_rows,
                   since=args.since)
    report = dict(result["report"])
    if args.write:
        paths = write_manifests(result, args.out_dir or args.root / "manifests",
                                force=args.force)
        report["dry_run"] = False
        report["written"] = [str(path) for path in paths]
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
