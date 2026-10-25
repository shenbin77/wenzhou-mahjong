"""Read-only provenance audit for the live caishen template manifest.

Reports manifest SHA, missing/corrupt assets and live-variant coverage.
Does not approve or modify any template: approval requires independent gold.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from caishen_vision import _DIR


def audit_manifest(directory: str | Path = _DIR) -> dict:
    base = Path(directory)
    path = base / "manifest.json"
    raw = path.read_bytes()
    obj = json.loads(raw.decode("utf-8-sig"))
    issues: list[dict] = []
    sources: dict[str, set[str]] = defaultdict(set)
    by_label: dict[str, int] = defaultdict(int)
    files_seen: dict[str, str] = {}
    roi = obj.get("roi")
    if not (isinstance(roi, list) and len(roi) == 4
            and roi[2] > roi[0] and roi[3] > roi[1]):
        issues.append({"code": "invalid_roi"})
    for idx, item in enumerate(obj.get("templates", [])):
        label = str(item.get("label") or "")
        rel = str(item.get("file") or "")
        source = str(item.get("source") or item.get("kind") or "unknown")
        if not label or not rel:
            issues.append({"code": "missing_metadata", "index": idx})
            continue
        asset = (base / rel).resolve()
        if not asset.is_relative_to(base.resolve()):
            issues.append({"code": "path_escape", "file": rel})
            continue
        try:
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        except OSError:
            issues.append({"code": "missing_file", "file": rel})
            continue
        if digest != item.get("asset_sha256"):
            issues.append({"code": "sha_mismatch", "file": rel})
        if rel in files_seen and files_seen[rel] != label:
            issues.append({"code": "duplicate_file_conflict", "file": rel})
        files_seen[rel] = label
        sources[label].add(source)
        by_label[label] += 1
    return {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "labels": len(by_label),
        "assets": sum(by_label.values()),
        "by_label": dict(sorted(by_label.items())),
        "sources_by_label": {k: sorted(v) for k, v in sorted(sources.items())},
        "issues": issues,
        "passed_integrity": not issues,
        "approved_for_production": False,
        "approval_note": "完整性检查不代替独立房间/日期的人工金标与误火率验证",
    }


if __name__ == "__main__":
    print(json.dumps(audit_manifest(), indent=2, ensure_ascii=False))
