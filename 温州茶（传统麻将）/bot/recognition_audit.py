"""Append-only evidence for every frame analyzed by the read-only assistant.

Flags are reasons to inspect a frame, never independent ground-truth labels.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "recognition_audit/v1"
# Review hints only. The recognizer's acceptance thresholds remain unchanged.
REVIEW_SCORE = 0.84
REVIEW_MARGIN = 0.07


def build_frame_event(raw: bytes, frame_path: str | Path, result: dict[str, Any],
                      *, mode: str, source: str = "live") -> dict[str, Any]:
    diagnostics = []
    for item in result.get("vision_diagnostics") or []:
        diagnostics.append({
            "box": list(item.get("box") or []),
            "region": item.get("region", "concealed"),
            "label": item.get("label"),
            "tile_id": item.get("tile_id"),
            "score": item.get("score"),
            "margin": item.get("margin"),
            "accepted": bool(item.get("accepted")),
            "reason": item.get("reason"),
        })
    flags = []
    if result.get("status") == "error":
        flags.append("analysis_error")
    if result.get("status") == "abstain":
        flags.append("abstain")
    if not diagnostics:
        flags.append("no_tile_evidence")
    if any(not item["accepted"] for item in diagnostics):
        flags.append("unrecognized_tile")
    if any(item["accepted"] and (
        item["score"] is not None and item["score"] < REVIEW_SCORE or
        item["margin"] is not None and item["margin"] < REVIEW_MARGIN
    ) for item in diagnostics):
        flags.append("near_acceptance_boundary")
    if diagnostics:
        count = sum(item["region"] == "concealed" and item["reason"] != "no_box"
                    for item in diagnostics)
        if mode.startswith("tea_"):
            valid = {16, 17}
        else:
            meld_count = int(result.get("meld_count", 0) or 0)
            base = 13 - 3 * meld_count
            valid = {base, base + 1}
        if count not in valid:
            flags.append("unexpected_hand_count")
        ids = Counter(item["tile_id"] for item in diagnostics
                      if item["accepted"] and item["tile_id"] is not None)
        if any(value > 4 for value in ids.values()):
            flags.append("more_than_four")
    return {
        "schema": SCHEMA,
        "at": datetime.now().astimezone().isoformat(),
        "frame": str(Path(frame_path).resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": source,
        "mode": mode,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "caishen": result.get("caishen"),
        "caishen_source": result.get("caishen_source"),
        "caishen_id": result.get("caishen_id"),
        "suggestion": result.get("suggestion"),
        "suggestion_id": result.get("suggestion_id"),
        "suggested_boxes": list(result.get("suggested_boxes") or []),
        "shanten": result.get("shanten"),
        "candidates": list(result.get("candidates") or []),
        "hand": list(result.get("hand") or []),
        "hand_ids": list(result.get("hand_ids") or []),
        "diagnostics": diagnostics,
        "flags": flags,
        "verification": "unreviewed_prediction",
    }


def append_frame_event(session_dir: str | Path, event: dict[str, Any]) -> Path:
    target = Path(session_dir) / "recognition_audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


def append_review(session_dir: str | Path, event: dict[str, Any], verdict: str,
                  corrected_hand: str = "") -> Path:
    if verdict not in {"correct", "wrong"}:
        raise ValueError("invalid review verdict")
    target = Path(session_dir) / "recognition_reviews.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": "recognition_review/v1",
        "at": datetime.now().astimezone().isoformat(),
        "frame": event["frame"],
        "sha256": event["sha256"],
        "verdict": verdict,
        "corrected_hand": corrected_hand.strip() if verdict == "wrong" else "",
    }
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target


def read_events(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    rows = []
    with target.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break  # A concurrently appended last line is not ready yet.
            if line.strip():
                rows.append(json.loads(line))
    return rows


def read_appended_events(path: str | Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read only complete newly appended JSONL rows; retain a partial tail."""
    target = Path(path)
    if not target.is_file():
        return [], 0
    rows = []
    with target.open("rb") as stream:
        stream.seek(offset)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                return rows, start
            if line.strip():
                rows.append(json.loads(line.decode("utf-8")))
