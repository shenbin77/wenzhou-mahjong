"""Metadata and tampering coverage for read-only caishen manifest audit."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from caishen_asset_audit import audit_manifest


def _make(tmp_path):
    raw = b"template bytes"
    (tmp_path / "a.png").write_bytes(raw)
    entry = {"file": "a.png", "label": "w1",
             "asset_sha256": hashlib.sha256(raw).hexdigest(), "source": "live"}
    (tmp_path / "manifest.json").write_text(
        json.dumps({"roi": [1, 2, 4, 6], "templates": [entry]}), encoding="utf-8")
    return entry


def test_clean_asset_is_not_automatically_approved(tmp_path):
    _make(tmp_path)
    result = audit_manifest(tmp_path)
    assert result["passed_integrity"] and not result["approved_for_production"]
    assert result["labels"] == 1 and result["assets"] == 1


def test_asset_tamper_is_detected(tmp_path):
    _make(tmp_path)
    (tmp_path / "a.png").write_bytes(b"unsafe alteration")
    result = audit_manifest(tmp_path)
    assert not result["passed_integrity"]
    assert any(x["code"] == "sha_mismatch" for x in result["issues"])


def test_path_escape_is_rejected(tmp_path):
    entry = _make(tmp_path)
    entry["file"] = "../unsafe.png"
    (tmp_path / "manifest.json").write_text(
        json.dumps({"roi": [1, 2, 4, 6], "templates": [entry]}), encoding="utf-8")
    result = audit_manifest(tmp_path)
    assert not result["passed_integrity"]
    assert result["issues"][0]["code"] == "path_escape"


def test_actual_manifest_has_no_integrity_problems():
    result = audit_manifest()
    assert result["labels"] >= 30
    assert result["passed_integrity"], result["issues"]
