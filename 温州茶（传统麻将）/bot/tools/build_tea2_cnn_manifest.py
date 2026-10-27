#!/usr/bin/env python3
r"""Build a read-only tea2 weak-label index; never train or copy source images.

Example:
  python build_tea2_cnn_manifest.py --project-root D:\project \
    --capture-root D:\project\captures --inventory-json inventory.json \
    --policy tea2_cnn_v1_policy.json --output-dir dataset-v001 --dry-run

The output directory must not already exist. "dry-run" still writes reviewable
indexes there, but neither mode writes source data, crops, models or game files.
All recognition_audit predictions remain weak, including algorithmic verification.
Pillow is used only to decode original PNGs and hash exact RGB crops.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time

from PIL import Image, __version__ as PILLOW_VERSION

BUILDER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA = "tea2_cnn_manifest/v1"
EXPECTED_LABELS = {
    **{f"w{i}": i for i in range(1, 10)},
    **{f"t{i}": 20 + i for i in range(1, 10)},
    **{f"s{i}": 40 + i for i in range(1, 10)},
    "dong": 61, "nan": 62, "xi": 63, "bei": 64, "bai": 67,
}
HEX256 = re.compile(r"^[0-9a-fA-F]{64}$")
PROVENANCE_PATH_KEYS = frozenset({
    "source_frame", "origin_frame", "original_frame", "copied_from",
    "source_session", "origin_session", "parent_session", "source_audit",
    "original_audit", "origin_path", "source_path",
})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def finite(value):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value))


def valid_sha(value):
    return isinstance(value, str) and HEX256.fullmatch(value) is not None


def load_policy(path):
    policy = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (policy.get("schema") != "tea2_cnn_dataset_policy/v1"
            or policy.get("labels") != EXPECTED_LABELS
            or policy.get("native_size") != [1600, 900]):
        raise ValueError("unsupported label space or native frame contract")
    weak = policy["weak"]
    if (not finite(weak.get("score_min")) or weak["score_min"] < 0.86
            or not finite(weak.get("margin_min")) or weak["margin_min"] < 0.10
            or weak.get("require_screen_mode_evidence") is not True
            or weak.get("tier") != "weak_unreviewed_prediction"
            or weak.get("region") != "concealed"
            or not set(weak.get("allowed_reasons") or ()).issubset(
                {"accepted", "accepted_narrow_bai_retry"})):
        raise ValueError("policy must preserve strict weak/mode gates")
    if (policy["source"]["allowed_family"] != "assist_sessions"
            or policy["source"]["forbidden_family"] != "dev_auto_play"
            or policy["source"]["required_source"] != "live"
            or policy["source"]["required_mode"] != "tea_2p"
            or policy["source"]["frame_subdir"] != ["live", "tea_2p"]
            or set(policy["source"].get("audit_schemas") or ()) != {
                "recognition_audit/v1", "recognition_audit/v2"}):
        raise ValueError("unsupported source policy")
    if (policy["gold"].get("audit_predictions_are_never_gold") is not True
            or policy["gold"].get("gold_import_enabled") is not False):
        raise ValueError("this builder has no gold importer")
    ratios = policy["split"]["ratios"]
    if (set(ratios) != {"train", "validation", "test"}
            or not all(finite(v) and 0 <= v <= 1 for v in ratios.values())
            or abs(sum(ratios.values()) - 1.0) > 1e-9
            or type(policy["split"]["seed"]) is not int):
        raise ValueError("invalid split policy")
    return policy


def _path(value):
    return Path(str(value).replace("\\", os.sep)).expanduser()


def _relative_real(path, root):
    """Reject aliases/junctions and paths escaping the caller's canonical root."""
    absolute = path.absolute()
    real = absolute.resolve()
    if real != absolute:
        raise ValueError("aliased_or_symlink_path")
    try:
        return real.relative_to(root)
    except ValueError as exc:
        raise ValueError("outside_canonical_capture_tree") from exc


def classify_audit(path, capture_root, policy):
    try:
        rel = _relative_real(path, capture_root)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    parts = rel.parts
    if (len(parts) != 3 or parts[0] not in ("assist_sessions", "dev_auto_play")
            or parts[-1] != policy["source"]["audit_name"]
            or not re.fullmatch(policy["source"]["session_pattern"], parts[1])):
        return None, "not_canonical_session_audit"
    return (parts[0], "/".join(parts[:2])), None


def exclusion_reasons(obj, policy, capture_root, depth=0):
    """Explicit exclusion propagates from session headers and nested provenance."""
    if not isinstance(obj, Mapping):
        return []
    if depth > 8:
        return ["provenance_nesting_unbounded"]
    found = []
    for key in ("dev", "dev_only", "is_dev"):
        if key in obj and obj[key] is not False:
            found.append("explicit_dev" if obj[key] is True else "ambiguous_dev_marker")
    for key, allowed in (("training_allowed", True), ("exclude_from_training", False)):
        if key in obj and obj[key] is not allowed:
            found.append("explicit_training_exclusion")
    note = str(obj.get("note") or "").lower()
    if any(s.lower() in note for s in policy["source"]["forbidden_note_fragments"]):
        found.append("training_exclusion_note")
    for key, value in obj.items():
        if key in PROVENANCE_PATH_KEYS and value:
            if not isinstance(value, str):
                found.append("invalid_origin_reference")
                continue
            parts = value.replace("\\", "/").split("/")
            if any(p in policy["source"]["forbidden_path_parts"] for p in parts):
                found.append("forbidden_origin")
            p = _path(value)
            if p.is_absolute():
                try:
                    rel = _relative_real(p, capture_root)
                    if not rel.parts or rel.parts[0] != "assist_sessions":
                        found.append("non_assist_origin")
                except (OSError, ValueError):
                    found.append("noncanonical_origin")
        if isinstance(value, Mapping):
            found.extend(exclusion_reasons(value, policy, capture_root, depth + 1))
        elif key in ("provenance", "origins", "lineage") and isinstance(value, list):
            for item in value:
                found.extend(exclusion_reasons(item, policy, capture_root, depth + 1))
    return sorted(set(found))


def stable_bytes(path):
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
            (after.st_size, after.st_mtime_ns, after.st_ino)
            or len(data) != after.st_size):
        raise ValueError("source_changed_during_read")
    return data, {"size_bytes": after.st_size, "mtime_ns": after.st_mtime_ns,
                  "sha256": digest(data)}


def inherited_exclusions(session_dir, policy, capture_root):
    reasons, evidence = [], []
    for name in policy["source"]["header_files"]:
        path = session_dir / name
        if not path.exists():
            continue
        try:
            _relative_real(path, capture_root)
            # Only the bounded session header; action bodies are not a data source.
            with path.open("rb") as stream:
                first = (stream.read(1024 * 1024 + 1) if path.suffix == ".json"
                         else stream.readline(1024 * 1024 + 1))
            if len(first) > 1024 * 1024:
                raise ValueError("header_too_large")
            row = json.loads(first.decode("utf-8-sig"))
            if not isinstance(row, Mapping):
                raise ValueError("session_header_not_object")
            why = exclusion_reasons(row, policy, capture_root)
            reasons.extend(why)
            evidence.append({"path": str(path), "header_sha256": digest(first),
                             "exclusions": why})
        except (OSError, ValueError, UnicodeError) as exc:
            reasons.append("unreadable_session_header")
            evidence.append({"path": str(path), "error": type(exc).__name__})
    return sorted(set(reasons)), evidence


def frame_path(row, session_dir, capture_root, policy):
    value = row.get("frame")
    if not isinstance(value, str) or not value:
        raise ValueError("frame_path_missing")
    path = _path(value)
    if not path.is_absolute():
        path = session_dir / path
    _relative_real(path, capture_root)
    try:
        rel = path.absolute().relative_to(session_dir)
    except ValueError as exc:
        raise ValueError("frame_outside_own_session") from exc
    prefix = tuple(policy["source"]["frame_subdir"])
    if (rel.parts[:2] != prefix or len(rel.parts) < 3
            or path.suffix.lower() != ".png"):
        raise ValueError("frame_not_live_tea2_png")
    return path.absolute()


def exclusion_fingerprint_path(row, session_dir, capture_root):
    """Broader read-only ban index; this function never grants training eligibility."""
    value = row.get("frame")
    if not isinstance(value, str) or not value:
        raise ValueError("excluded_frame_path_missing")
    path = _path(value)
    if not path.is_absolute():
        path = session_dir / path
    _relative_real(path, capture_root)
    if path.suffix.lower() != ".png":
        raise ValueError("excluded_frame_not_png")
    return path.absolute()


def screen_mode_ok(row, policy):
    ev = row.get("mode_evidence")
    return (isinstance(ev, Mapping) and type(ev.get("player_count")) is int
            and ev["player_count"] == 2
            and ev.get("source") == policy["weak"]["mode_evidence_source"]
            and ev.get("verified") is True
            and finite(ev.get("score")) and 0 <= ev["score"] <= 1.0001
            and finite(ev.get("margin")) and ev["margin"] >= 0)


def checked_box(value, size):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) is not int for v in value)):
        raise ValueError("box_not_four_integers")
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]):
        raise ValueError("box_outside_native_frame")
    return tuple(value)


def weak_candidate(diag, policy):
    if not isinstance(diag, Mapping):
        return None, "diagnostic_not_object"
    weak = policy["weak"]
    if diag.get("accepted") is not True:
        return None, "not_accepted"
    if (not finite(diag.get("score")) or not finite(diag.get("margin"))
            or diag["score"] < weak["score_min"]
            or diag["margin"] < weak["margin_min"]):
        return None, "outside_strong_weak_band"
    if diag.get("region") != weak["region"]:
        return None, "not_concealed"
    if diag.get("reason") not in weak["allowed_reasons"]:
        return None, "reason_not_admitted"
    label = diag.get("label")
    if label not in policy["labels"]:
        return None, "outside_32_class_space"
    if type(diag.get("tile_id")) is not int or diag["tile_id"] != policy["labels"][label]:
        return None, "label_tile_id_mismatch"
    try:
        box = checked_box(diag.get("box"), policy["native_size"])
    except ValueError as exc:
        return None, str(exc)
    return {"box": list(box), "label": label, "tile_id": diag["tile_id"],
            "score": float(diag["score"]), "margin": float(diag["margin"]),
            "reason": diag.get("reason")}, None


def pixel_hash(image):
    rgb = image.convert("RGB")
    prefix = b"tea2-rgb-v1\0" + struct.pack(">II", *rgb.size)
    return digest(prefix + rgb.tobytes())


def decode_png(path, native_size):
    data, metadata = stable_bytes(path)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not_png")
    # Verify chunk integrity, then load pixels from the exact same bytes.
    with Image.open(io.BytesIO(data)) as probe:
        if probe.format != "PNG" or probe.size != tuple(native_size):
            raise ValueError("not_native_1600x900")
        if getattr(probe, "n_frames", 1) != 1:
            raise ValueError("animated_png_not_a_single_frame")
        probe.verify()
    with Image.open(io.BytesIO(data)) as decoded:
        decoded.load()
        image = decoded.convert("RGB")
    metadata["pixel_sha256"] = pixel_hash(image)
    metadata["width"], metadata["height"] = image.size
    return image, metadata


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, key):
        self.parent.setdefault(key, key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != key:
            previous, self.parent[key] = self.parent[key], root
            key = previous
        return root

    def union(self, first, second):
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def assign_groups(sessions, shared_keys, seed, ratios):
    uf = UnionFind()
    for session in sessions:
        uf.find(session)
    for key in sorted(shared_keys):
        members = sorted(shared_keys[key])
        for member in members[1:]:
            uf.union(members[0], member)
    groups = defaultdict(list)
    for session in sorted(sessions):
        groups[uf.find(session)].append(session)
    by_session, rows = {}, []
    for members in sorted(groups.values()):
        group_id = digest("\n".join(members).encode("utf-8"))
        value = int(digest(f"{seed}:{group_id}".encode("ascii")), 16) / (1 << 256)
        split = ("train" if value < ratios["train"] else
                 "validation" if value < ratios["train"] + ratios["validation"] else "test")
        row = {"group_id": group_id, "sessions": members, "split": split}
        rows.append(row)
        for session in members:
            by_session[session] = row
    return by_session, sorted(rows, key=lambda row: row["group_id"])


def validate_no_leakage(frames, crops):
    """Fatal invariant: exported exact frames/crops/session components never cross splits."""
    seen = defaultdict(dict)
    def add(kind, key, split):
        if key in seen[kind] and seen[kind][key] != split:
            raise ValueError(f"cross_split_{kind}:{key}")
        seen[kind][key] = split
    for row in frames:
        for kind in ("frame_sha256", "frame_pixel_sha256", "group_id"):
            add(kind, row[kind], row["split"])
        for session in row["sessions"]:
            add("session", session, row["split"])
    unique_crops = set()
    for row in crops:
        add("crop_sha256", row["crop_sha256"], row["split"])
        if row["crop_sha256"] in unique_crops:
            raise ValueError("crop_not_deduplicated")
        unique_crops.add(row["crop_sha256"])
        add("group_id", row["group_id"], row["split"])
        for origin in row["origins"]:
            add("session", origin["session_id"], row["split"])
            add("frame_sha256", origin["frame_sha256"], row["split"])
        if row["label_tier"] != "weak_unreviewed_prediction":
            raise ValueError("unexpected_gold_promotion")


def build_manifest(audit_paths, *, capture_root, output_dir, policy,
                   inventory_metadata=None, policy_source=None, dry_run=True, progress=None):
    capture_root = Path(capture_root).absolute().resolve()
    output_dir = Path(output_dir).absolute()
    if output_dir.exists():
        raise FileExistsError("output directory must be a new explicit version directory")
    # Output must never be written into original collection data.
    if output_dir.resolve().is_relative_to(capture_root):
        raise ValueError("output_inside_source_tree")
    if digest(Path(__file__).read_bytes()) != BUILDER_SOURCE_SHA256:
        raise ValueError("builder_source_changed_since_import")
    policy = json.loads(canonical_json(policy))
    policy_sha = digest(canonical_json(policy).encode("utf-8"))
    output_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    counts, source_counts, diagnostic_rejections = Counter(), defaultdict(Counter), Counter()
    quarantines, sources = [], []
    candidates_by_path = defaultdict(list)
    forbidden_paths, forbidden_sha, forbidden_pixels = {}, set(), set()
    shared, sessions = defaultdict(set), set()
    frames, crop_instances = {}, defaultdict(list)
    declared_observations, pixel_observations = defaultdict(dict), defaultdict(dict)
    schema_counts = Counter()

    def quarantine(reason, **fields):
        counts["quarantine_records"] += 1
        quarantines.append({"reason": reason, **fields})

    try:
        for audit_path in sorted(set(Path(p).absolute() for p in audit_paths), key=str):
            identity, rejection = classify_audit(audit_path, capture_root, policy)
            if rejection:
                counts["excluded_noncanonical_audit_files"] += 1
                sources.append({"audit": str(audit_path), "admitted": False, "reason": rejection})
                continue
            family, session = identity
            counts["canonical_audit_files"] += 1
            source_counts[family]["files"] += 1
            session_dir = audit_path.parent
            inherited, header_evidence = inherited_exclusions(session_dir, policy, capture_root)
            if family == "dev_auto_play":
                inherited = sorted(set(inherited + ["dev_source_tree"]))
            try:
                content, audit_meta = stable_bytes(audit_path)
                lines = content.decode("utf-8-sig").splitlines()
            except (OSError, ValueError, UnicodeError) as exc:
                quarantine("audit_unreadable", audit=str(audit_path), error=str(exc))
                sources.append({"audit": str(audit_path), "admitted": False,
                                "reason": "audit_unreadable"})
                continue
            sources.append({"audit": str(audit_path), "family": family, "session_id": session,
                            "admitted": not inherited, "exclusions": inherited,
                            "headers": header_evidence, **audit_meta})
            if family == "assist_sessions" and not inherited:
                sessions.add(session)
            for line_no, text in enumerate(lines, 1):
                if not text.strip():
                    continue
                counts["audit_nonempty_lines"] += 1
                try:
                    row = json.loads(text)
                    if not isinstance(row, dict):
                        raise ValueError("row_not_object")
                except (ValueError, UnicodeError) as exc:
                    quarantine("invalid_audit_json", audit=str(audit_path), line=line_no,
                               error=str(exc))
                    continue
                counts["audit_entries"] += 1
                schema_counts[str(row.get("schema"))] += 1
                stats = source_counts[family]
                stats["entries"] += 1
                diags = row.get("diagnostics") or []
                if not isinstance(diags, list):
                    quarantine("diagnostics_not_list", audit=str(audit_path), line=line_no)
                    continue
                stats["diagnostics"] += len(diags)
                stats["accepted"] += sum(isinstance(d, dict) and d.get("accepted") is True for d in diags)
                stats["strong_weak_unfiltered"] += sum(
                    isinstance(d, dict) and d.get("accepted") is True
                    and finite(d.get("score")) and finite(d.get("margin"))
                    and d["score"] >= policy["weak"]["score_min"]
                    and d["margin"] >= policy["weak"]["margin_min"] for d in diags)
                ref = {"audit": str(audit_path), "line": line_no, "session_id": session}
                exclusions = sorted(set(inherited + exclusion_reasons(row, policy, capture_root)))
                if row.get("source") != "live":
                    exclusions.append("source_not_live")
                if row.get("mode") != "tea_2p":
                    exclusions.append("mode_not_tea2")
                if row.get("schema") not in policy["source"]["audit_schemas"]:
                    exclusions.append("unsupported_audit_schema")
                expected_sha = row.get("sha256")
                if valid_sha(expected_sha):
                    expected_sha = expected_sha.lower()
                else:
                    expected_sha = None
                try:
                    path = frame_path(row, session_dir, capture_root, policy)
                except (OSError, ValueError) as exc:
                    path = None
                    exclusions.append(str(exc))
                if exclusions:
                    stats["excluded_entries"] += 1
                    # Training exclusions follow copies, even if a new audit says live/assist.
                    if expected_sha:
                        forbidden_sha.add(expected_sha)
                    try:
                        ban_path = exclusion_fingerprint_path(
                            row, session_dir, capture_root)
                        forbidden_paths[str(ban_path)] = ban_path
                    except (OSError, ValueError):
                        counts["excluded_frames_without_canonical_png_reference"] += 1
                    quarantine("source_excluded", exclusions=sorted(set(exclusions)),
                               declared_sha256=expected_sha, **ref)
                    continue
                if expected_sha is None:
                    quarantine("frame_sha_missing_or_invalid", **ref)
                    continue
                # Conservative over-grouping from a declared hash is safe; it never
                # makes any image eligible. Every exported frame is verified below.
                shared["declared:" + expected_sha].add(session)
                selected = []
                for index, diag in enumerate(diags):
                    candidate, why = weak_candidate(diag, policy)
                    if why:
                        diagnostic_rejections[why] += 1
                    else:
                        selected.append((index, candidate))
                counts["weak_candidate_crops_before_mode_gate"] += len(selected)
                if not selected:
                    continue
                annotations = sorted(
                    ({"box": c["box"], "label": c["label"], "tile_id": c["tile_id"]}
                     for _, c in selected),
                    key=lambda c: (c["box"], c["label"], c["tile_id"]))
                observation_id = str(audit_path) + ":" + str(line_no)
                declared_observations[expected_sha][observation_id] = {
                    "observation_id": observation_id, **ref,
                    "audit_schema": row["schema"], "annotations": annotations,
                    "signature": canonical_json(annotations),
                }
                if not screen_mode_ok(row, policy):
                    counts["candidate_crops_missing_screen_mode"] += len(selected)
                    quarantine("screen_mode_evidence_missing_or_untrusted",
                               candidate_crops=len(selected), declared_sha256=expected_sha, **ref)
                    continue
                counts["weak_candidate_crops_after_mode_gate"] += len(selected)
                for index, candidate in selected:
                    candidates_by_path[path].append({
                        **candidate, **ref, "diagnostic_index": index,
                        "declared_sha256": expected_sha, "frame_path": str(path),
                        "verification": row.get("verification"), "audit_schema": row["schema"],
                        "status": row.get("status"), "mode_evidence": row["mode_evidence"],
                        "device_serial": row.get("device_serial"),
                        "room_id": row.get("room_id"), "match_id": row.get("match_id"),
                    })
        if progress:
            progress({"phase": "audit_indexed", "canonical_files": counts["canonical_audit_files"],
                      "entries": counts["audit_entries"], "candidate_paths": len(candidates_by_path),
                      "excluded_frame_paths_to_hash": len(forbidden_paths)})
        # Excluded frames are read only to propagate the ban through byte and
        # lossless PNG re-encoding copies. No labels/crops from them are admitted.
        for index, path in enumerate(sorted(forbidden_paths.values(), key=str), 1):
            try:
                image, meta = decode_png(path, policy["native_size"])
                forbidden_sha.add(meta["sha256"])
                forbidden_pixels.add(meta["pixel_sha256"])
                image.close()
                counts["excluded_frames_content_verified"] += 1
            except (OSError, ValueError, SyntaxError) as exc:
                counts["excluded_frames_unreadable"] += 1
                quarantine("excluded_frame_unreadable", frame_path=str(path), error=str(exc))
            if progress and index % 500 == 0:
                progress({"phase": "exclusion_hashes", "done": index, "total": len(forbidden_paths)})
        for index, (path, records) in enumerate(sorted(candidates_by_path.items(), key=lambda p: str(p[0])), 1):
            try:
                image, meta = decode_png(path, policy["native_size"])
            except (OSError, ValueError, SyntaxError) as exc:
                quarantine("candidate_frame_unreadable", frame_path=str(path),
                           candidate_crops=len(records), error=str(exc))
                continue
            sha, pix = meta["sha256"], meta["pixel_sha256"]
            counts["candidate_frame_paths_verified"] += 1
            if sha in forbidden_sha or pix in forbidden_pixels:
                counts["candidate_crops_inherited_exclusion"] += len(records)
                quarantine("excluded_source_content_copy", frame_path=str(path),
                           frame_sha256=sha, frame_pixel_sha256=pix, candidate_crops=len(records))
                image.close()
                continue
            valid = []
            for record in records:
                if record["declared_sha256"] != sha:
                    quarantine("frame_sha_mismatch", frame_path=str(path),
                               actual_sha256=sha, declared_sha256=record["declared_sha256"],
                               audit=record["audit"], line=record["line"],
                               diagnostic_index=record["diagnostic_index"])
                else:
                    valid.append(record)
            if not valid:
                image.close()
                continue
            members = {row["session_id"] for row in valid}
            pixel_observations[pix].update(declared_observations[sha])
            shared["verified:" + sha].update(members)
            shared["pixels:" + pix].update(members)
            frame = frames.setdefault(sha, {
                "frame_sha256": sha, "frame_pixel_sha256": pix,
                "width": meta["width"], "height": meta["height"],
                "paths": set(), "sessions": set(), "verified_paths": [],
            })
            frame["paths"].add(str(path))
            frame["sessions"].update(members)
            frame["verified_paths"].append({"path": str(path), **meta})
            crop_cache = {}
            for record in valid:
                box = checked_box(record["box"], image.size)
                if box not in crop_cache:
                    crop = image.crop(box)
                    crop_cache[box] = pixel_hash(crop)
                    crop.close()
                record["frame_sha256"] = sha
                record["frame_pixel_sha256"] = pix
                crop_instances[crop_cache[box]].append(record)
                counts["content_verified_candidate_crops"] += 1
            image.close()
            if progress and index % 500 == 0:
                progress({"phase": "candidate_hashes", "done": index, "total": len(candidates_by_path)})
        # Compare complete admitted annotation sets per original observation.
        # Crop hashes alone miss a shifted rectangle carrying a different label.
        # One disagreement quarantines the whole verified pixel frame, including
        # every session/PNG encoding; no score or majority is used to pick a winner.
        conflict_pixels = set()
        for pix, observations in sorted(pixel_observations.items()):
            if len({o["signature"] for o in observations.values()}) > 1:
                conflict_pixels.add(pix)
                counts["frame_annotation_conflict_pixel_frames"] += 1
                quarantine("frame_annotation_conflict", frame_pixel_sha256=pix,
                           observations=[{k: v for k, v in o.items() if k != "signature"}
                                         for _, o in sorted(observations.items())])
        if conflict_pixels:
            frames = {sha: row for sha, row in frames.items()
                      if row["frame_pixel_sha256"] not in conflict_pixels}
            for crop_sha, instances in list(crop_instances.items()):
                kept = [r for r in instances if r["frame_pixel_sha256"] not in conflict_pixels]
                counts["frame_annotation_conflict_crop_records"] += len(instances) - len(kept)
                if kept:
                    crop_instances[crop_sha] = kept
                else:
                    del crop_instances[crop_sha]
        by_session, groups = assign_groups(
            sessions, shared, policy["split"]["seed"], policy["split"]["ratios"])
        frame_rows = []
        for frame in sorted(frames.values(), key=lambda row: row["frame_sha256"]):
            frame["paths"] = sorted(frame["paths"])
            frame["sessions"] = sorted(frame["sessions"])
            assignments = {by_session[s]["group_id"] for s in frame["sessions"]}
            if len(assignments) != 1:
                raise ValueError("same_frame_not_union_grouped")
            assignment = by_session[frame["sessions"][0]]
            frame.update(group_id=assignment["group_id"], split=assignment["split"])
            frame_rows.append(frame)
        crop_rows = []
        for crop_sha, instances in sorted(crop_instances.items()):
            labels = sorted({row["label"] for row in instances})
            group_ids = {by_session[row["session_id"]]["group_id"] for row in instances}
            if len(labels) != 1:
                counts["conflicting_crop_hashes"] += 1
                counts["conflicting_crop_records"] += len(instances)
                quarantine("conflicting_labels", crop_sha256=crop_sha, labels=labels,
                           origins=[{"audit": r["audit"], "line": r["line"],
                                     "diagnostic_index": r["diagnostic_index"],
                                     "label": r["label"]} for r in instances])
                continue
            if len(group_ids) != 1:
                counts["cross_group_identical_crop_hashes"] += 1
                counts["cross_group_identical_crop_records"] += len(instances)
                quarantine("identical_crop_across_independent_session_groups",
                           crop_sha256=crop_sha, group_ids=sorted(group_ids),
                           occurrences=len(instances))
                continue
            instances.sort(key=lambda r: (r["frame_path"], r["box"], r["audit"],
                                          r["line"], r["diagnostic_index"]))
            representative = instances[0]
            assignment = by_session[representative["session_id"]]
            origins = [{key: row.get(key) for key in (
                "audit", "line", "diagnostic_index", "session_id", "frame_path",
                "frame_sha256", "frame_pixel_sha256", "box", "score", "margin",
                "reason", "verification", "audit_schema", "status", "mode_evidence", "device_serial",
                "room_id", "match_id")} for row in instances]
            crop_rows.append({
                "crop_sha256": crop_sha, "label": labels[0],
                "tile_id": policy["labels"][labels[0]], "label_tier": "weak_unreviewed_prediction",
                "frame_path": representative["frame_path"], "frame_sha256": representative["frame_sha256"],
                "box": representative["box"], "group_id": assignment["group_id"], "split": assignment["split"],
                "occurrences": len(instances), "origins": origins,
                "room_match_provenance_complete": all(r.get("room_id") and r.get("match_id") for r in instances),
                "independent_human_gold": False,
            })
        validate_no_leakage(frame_rows, crop_rows)
        counts["unique_verified_png_frames"] = len(frame_rows)
        counts["unique_verified_frame_pixels"] = len({r["frame_pixel_sha256"] for r in frame_rows})
        counts["unique_weak_crops"] = len(crop_rows)
        counts["gold_crops"] = 0
        counts["session_components"] = len(groups)
        counts["multi_session_components"] = sum(len(g["sessions"]) > 1 for g in groups)
        counts["weak_crops_without_complete_room_match"] = sum(not c["room_match_provenance_complete"] for c in crop_rows)
        coverage = {split: {label: 0 for label in policy["labels"]}
                    for split in ("train", "validation", "test")}
        group_sizes = Counter()
        for crop in crop_rows:
            coverage[crop["split"]][crop["label"]] += 1
            group_sizes[crop["group_id"]] += 1
        for group in groups:
            group["unique_weak_crops"] = group_sizes[group["group_id"]]
        if digest(Path(__file__).read_bytes()) != BUILDER_SOURCE_SHA256:
            raise ValueError("builder_source_changed_during_run")
        report = {
            "builder_source_sha256": BUILDER_SOURCE_SHA256,
            "runtime": {"python": sys.version, "pillow": PILLOW_VERSION},
            "policy_source": policy_source,
            "audit_schema_counts": dict(sorted(schema_counts.items())),
            "schema": SCHEMA, "run_mode": "dry_run_index_only" if dry_run else "index_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "policy_sha256": policy_sha, "capture_root": str(capture_root),
            "inventory": inventory_metadata or {}, "counts": dict(sorted(counts.items())),
            "source_counts": {k: dict(sorted(v.items())) for k, v in sorted(source_counts.items())},
            "diagnostic_rejections": dict(sorted(diagnostic_rejections.items())),
            "split_class_coverage": coverage,
            "split_missing_classes": {s: [k for k, v in c.items() if v == 0] for s, c in coverage.items()},
            "exact_cross_split_leakage_check": "passed",
            "independent_match_split": False, "independent_human_evaluation_ready": False,
            "training_performed": False, "source_images_copied": False,
            "limits": [
                "Every exported label is an unreviewed weak prediction, never human gold.",
                "Recorded screen-rule evidence is required; missing mode provenance is quarantined.",
                "Components are sessions joined by declared/verified frame equality, not proven independent games.",
                "Exact PNG/pixel/crop equality is checked; near-duplicate gameplay leakage remains unmeasured.",
                "Unreadable excluded frames remain banned by declared hash; unknown rewritten provenance cannot be reconstructed.",
                "No human box-level gold importer, training, GPU setup or classifier change is included.",
            ],
        }
        def write_jsonl(name, rows):
            with (output_dir / name).open("x", encoding="utf-8", newline="\n") as stream:
                for row in rows:
                    stream.write(canonical_json(row) + "\n")
        write_jsonl("frames.jsonl", frame_rows)
        write_jsonl("weak_crops.jsonl", crop_rows)
        write_jsonl("quarantine.jsonl", quarantines)
        write_jsonl("sources.jsonl", sources)
        (output_dir / "split_manifest.json").write_text(
            json.dumps({"schema": SCHEMA, "policy_sha256": policy_sha, "groups": groups},
                       ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (output_dir / "policy_snapshot.json").write_text(
            json.dumps(policy, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Complete marker is last; exceptions never leave an apparently usable dataset.
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return report
    except Exception as exc:
        (output_dir / "FAILED.json").write_text(
            json.dumps({"schema": SCHEMA, "error_type": type(exc).__name__, "error": str(exc)},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inventory-json", type=Path,
                        help="cnn_inv_summary.json containing audit.per_file; no recursive source walk")
    source.add_argument("--audit-list", type=Path,
                        help="JSON list of explicit recognition_audit.jsonl paths")
    parser.add_argument("--project-root", type=Path, help="base of inventory relative paths")
    parser.add_argument("--capture-root", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="write only reviewable indexes (never images/models)")
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    policy_bytes, policy_source = stable_bytes(args.policy)
    if canonical_json(json.loads(policy_bytes.decode("utf-8-sig"))) != canonical_json(policy):
        raise ValueError("policy_source_changed_during_load")
    policy_source["path"] = str(args.policy.absolute())
    input_path = args.inventory_json or args.audit_list
    data, metadata = stable_bytes(input_path)
    payload = json.loads(data.decode("utf-8-sig"))
    if args.inventory_json:
        if args.project_root is None:
            parser.error("--project-root is required with --inventory-json")
        paths = [args.project_root / _path(key) / policy["source"]["audit_name"]
                 for key in payload["audit"]["per_file"]]
    else:
        if not isinstance(payload, list) or not all(isinstance(p, str) for p in payload):
            parser.error("--audit-list must contain a JSON array of paths")
        paths = [_path(p) for p in payload]
    metadata.update(path=str(input_path.absolute()), listed_audit_paths=len(paths))
    report = build_manifest(paths, capture_root=args.capture_root, output_dir=args.output_dir,
                            policy=policy, inventory_metadata=metadata, policy_source=policy_source,
                            dry_run=args.dry_run,
                            progress=lambda event: print(canonical_json(event), flush=True))
    print(canonical_json({"phase": "complete", "output_dir": str(args.output_dir),
                          "counts": report["counts"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
