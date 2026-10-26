"""Append-only evidence for every frame analyzed by the read-only assistant.

Flags are reasons to inspect a frame, never independent ground-truth labels.
"""

from __future__ import annotations

import hashlib
import json
import getpass
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from tile_vision import TARGET_IDS

SCHEMA = "recognition_audit/v2"
# Review hints only. The recognizer's acceptance thresholds remain unchanged.
REVIEW_SCORE = 0.84
REVIEW_MARGIN = 0.07

_TARGET_TILE_IDS = frozenset(TARGET_IDS)
_CHINESE_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9}
_HONOUR_ALIASES = {
    "东": 61, "東": 61, "dong": 61,
    "南": 62, "nan": 62,
    "西": 63, "xi": 63,
    "北": 64, "bei": 64,
    "白": 67, "白板": 67, "bai": 67,
}


def _copy_evidence_fields(event: dict[str, Any], result: dict[str, Any]) -> None:
    """Copy only explicit evidence fields; never infer labels from predictions."""
    for key in ("device_serial", "room_id", "match_id", "round_seq",
                "mode_evidence", "meld_count", "melds", "dev"):
        value = result.get(key)
        if value not in (None, ""):
            event[key] = value


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
    event = {
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
    _copy_evidence_fields(event, result)
    return event


def append_frame_event(session_dir: str | Path, event: dict[str, Any]) -> Path:
    target = Path(session_dir) / "recognition_audit.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


def parse_corrected_hand(value: Any) -> list[int]:
    """Parse a human correction into production vision tile ids.

    Accepted examples include a JSON integer list, ``w1 t2 dong``, and
    ``一万,2筒,三条,东``.  Unknown words and non-production tiles fail closed.
    """
    if isinstance(value, list):
        tiles = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("纠错手牌 JSON 格式无效") from exc
            if not isinstance(parsed, list):
                raise ValueError("纠错手牌必须是牌列表")
            tiles = parsed
        else:
            tokens = [item for item in re.split(r"[\s,，、;/；|]+", text)
                      if item]
            tiles = []
            for token in tokens:
                normalized = token.strip().lower()
                tile_id: int | None = None
                if normalized.isdigit():
                    tile_id = int(normalized)
                elif normalized in _HONOUR_ALIASES:
                    tile_id = _HONOUR_ALIASES[normalized]
                else:
                    match = re.fullmatch(r"([wts])([1-9])", normalized)
                    if match:
                        base = {"w": 0, "t": 20, "s": 40}[match.group(1)]
                        tile_id = base + int(match.group(2))
                    else:
                        match = re.fullmatch(r"([1-9一二三四五六七八九])"
                                             r"(万|萬|筒|饼|餅|条|條|索)", token)
                        if match:
                            number = (int(match.group(1)) if match.group(1).isdigit()
                                      else _CHINESE_NUMBERS[match.group(1)])
                            base = 0 if match.group(2) in {"万", "萬"} \
                                else 20 if match.group(2) in {"筒", "饼", "餅"} \
                                else 40
                            tile_id = base + number
                if tile_id not in _TARGET_TILE_IDS:
                    raise ValueError(f"无法识别或不属于温州茶牌集：{token}")
                tiles.append(tile_id)
    else:
        raise ValueError("纠错手牌必须是文本或整数列表")
    if any(type(tile) is not int or tile not in _TARGET_TILE_IDS for tile in tiles):
        raise ValueError("纠错手牌包含非法牌 ID")
    if any(count > 4 for count in Counter(tiles).values()):
        raise ValueError("纠错手牌中同一种牌不能超过 4 张")
    return list(tiles)


def append_review(session_dir: str | Path, event: dict[str, Any], verdict: str,
                  corrected_hand: Any = "", *, reviewer: str | None = None,
                  source: str = "review_ui") -> Path:
    if verdict not in {"correct", "wrong", "corrected", "rejected"}:
        raise ValueError("invalid review verdict")
    correction: list[int] = []
    original_correction = corrected_hand.strip() if isinstance(corrected_hand, str) else ""
    if verdict in {"wrong", "corrected"}:
        correction = parse_corrected_hand(corrected_hand)
        verdict = "corrected" if correction else "rejected"
    reviewer_id = (reviewer if reviewer is not None else getpass.getuser()).strip()
    if not reviewer_id:
        raise ValueError("reviewer is required")
    if source != "review_ui":
        raise ValueError("unsupported review source")
    target = Path(session_dir) / "recognition_reviews.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": "recognition_review/v2",
        "at": datetime.now().astimezone().isoformat(),
        "frame": event["frame"],
        "sha256": event["sha256"],
        "verdict": verdict,
        "reviewer": reviewer_id,
        "source": source,
    }
    if verdict == "corrected":
        row["corrected_hand"] = correction
        if original_correction:
            row["corrected_hand_text"] = original_correction
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
