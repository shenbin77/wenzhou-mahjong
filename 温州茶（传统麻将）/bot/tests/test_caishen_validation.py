"""P0-4: heldout / leave-one-source-out fail-closed gates (no invented gold).

Synthetic frames only prove the *gate* behaviour. They are not production gold
labels and must not be treated as a second real-play caishen corpus.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import caishen_validation as cv  # noqa: E402
import caishen_vision  # noqa: E402

ROI = (622, 348, 677, 423)
H, W = 75, 55  # y1-y0, x1-x0


def _badge_crop(pattern: int = 1) -> np.ndarray:
    """55x75 BGR crop with a red 财 badge and a distinctive body pattern."""
    crop = np.full((H, W, 3), 40, dtype=np.uint8)
    # Red badge region (B<110, G<160, R>175), >=70 pixels.
    crop[:22, :30] = (40, 40, 220)
    # Body pattern — unique per pattern id so templates don't cross-match.
    body = crop[22:, :]
    body[:] = (30 + pattern * 20, 60, 90)
    cv2.rectangle(body, (5, 5), (20, 40), (10, 10, 10 + pattern * 40), -1)
    cv2.circle(body, (35, 25), 8 + pattern, (200, 200 - pattern * 30, 50), -1)
    return crop


def _frame_with_crop(crop_bgr: np.ndarray, *, noise: int = 0) -> np.ndarray:
    frame = np.full((900, 1600, 3), 18, dtype=np.uint8)
    x0, y0, x1, y1 = ROI
    placed = crop_bgr.copy()
    if noise:
        rng = np.random.default_rng(noise)
        placed = np.clip(
            placed.astype(np.int16) + rng.integers(-noise, noise + 1, placed.shape),
            0, 255).astype(np.uint8)
        # Keep badge red enough after noise.
        placed[:22, :30] = (40, 40, 220)
    frame[y0:y1, x0:x1] = placed
    return frame


def _write_png(path: Path, image: np.ndarray) -> Path:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buf.tobytes())
    return path


def _tile_gray(crop_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)


def test_thresholds_untouched():
    assert caishen_vision._MIN_SCORE == 0.98
    assert caishen_vision._MIN_MARGIN == 0.02


def test_evaluate_promotion_heldout_fail_closed_without_witness():
    tile = _tile_gray(_badge_crop(1))
    out = cv.evaluate_promotion_heldout(tile, "abc" * 10, witness_path=None)
    assert out["passed"] is False
    assert out["witness_frame"] is None
    assert "fail-closed" in out["reason"]


def test_check_witness_rejects_same_hash_as_source_replay_only(tmp_path):
    crop = _badge_crop(2)
    frame = _frame_with_crop(crop)
    path = _write_png(tmp_path / "src.png", frame)
    fhash = hashlib.sha256(path.read_bytes()).hexdigest()
    out = cv.check_witness(_tile_gray(crop), path, fhash)
    assert out["passed"] is False
    assert "同源" in out["reason"] or "自匹配" in out["reason"]


def test_source_replay_never_sets_heldout_passed(tmp_path):
    crop = _badge_crop(3)
    path = _write_png(tmp_path / "src.png", _frame_with_crop(crop))
    out = cv.source_replay_check(_tile_gray(crop), path)
    assert out["source_replay_passed"] is True
    assert out["heldout_passed"] is False
    assert out["score"] >= caishen_vision._MIN_SCORE
    assert "同源自匹配" in out["note"]


def test_check_witness_passes_on_independent_synthetic_frame(tmp_path):
    """Synthetic independent frame proves the *API*, not production gold."""
    crop = _badge_crop(4)
    src = _write_png(tmp_path / "src.png", _frame_with_crop(crop))
    # Distinct bytes (extra pixel outside ROI) → different hash, same ROI.
    wit_img = _frame_with_crop(crop)
    wit_img[10, 10] = (1, 2, 3)
    wit = _write_png(tmp_path / "wit.png", wit_img)
    assert hashlib.sha256(src.read_bytes()).hexdigest() != hashlib.sha256(
        wit.read_bytes()).hexdigest()
    out = cv.check_witness(
        _tile_gray(crop), wit, hashlib.sha256(src.read_bytes()).hexdigest())
    assert out["passed"] is True
    assert out["score"] >= caishen_vision._MIN_SCORE


def test_leave_one_source_out_fail_closed_on_empty_pairs():
    out = cv.leave_one_source_out_or_fail_closed(None)
    assert out["passed"] is False
    assert out["n_pairs"] == 0
    assert "不得发明" in out["reason"] or "fail-closed" in out["reason"]
    out2 = cv.leave_one_source_out_or_fail_closed([])
    assert out2["passed"] is False


def test_leave_one_source_out_single_source_is_unconfirmed(tmp_path, monkeypatch):
    """Only one source for a label → hold-out leaves no remaining template."""
    crop = _badge_crop(5)
    frame_path = _write_png(tmp_path / "only.png", _frame_with_crop(crop))
    fhash = hashlib.sha256(frame_path.read_bytes()).hexdigest()
    tdir = tmp_path / "tmpl"
    tdir.mkdir()
    tile_path = tdir / "syn_a.png"
    ok, buf = cv2.imencode(".png", _tile_gray(crop))
    assert ok
    tile_path.write_bytes(buf.tobytes())
    manifest = {
        "roi": list(ROI),
        "templates": [{
            "label": "w1",
            "file": "syn_a.png",
            "source_frame_hash": fhash,
            "asset_sha256": hashlib.sha256(tile_path.read_bytes()).hexdigest(),
        }],
    }
    (tdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # true_label is a *test fixture* label for the synthetic pair, not a
    # claimed second production gold corpus.
    out = cv.leave_one_source_out_or_fail_closed(
        [(frame_path, "w1")], template_dir=tdir)
    assert out["passed"] is False
    assert out["n_passed"] == 0
    assert out["results"][0]["note"] == "no_remaining_template"


def test_leave_one_source_out_cross_source_can_pass(tmp_path):
    """Two independent synthetic sources for the same label → LOSO can pass."""
    crop = _badge_crop(6)
    src_a = _write_png(tmp_path / "a.png", _frame_with_crop(crop))
    img_b = _frame_with_crop(crop)
    img_b[20, 20] = (9, 8, 7)  # hash differs; ROI identical
    src_b = _write_png(tmp_path / "b.png", img_b)
    ha = hashlib.sha256(src_a.read_bytes()).hexdigest()
    hb = hashlib.sha256(src_b.read_bytes()).hexdigest()
    assert ha != hb

    tdir = tmp_path / "tmpl"
    tdir.mkdir()
    gray = _tile_gray(crop)
    for name in ("syn_a.png", "syn_b.png"):
        ok, buf = cv2.imencode(".png", gray)
        assert ok
        (tdir / name).write_bytes(buf.tobytes())
    manifest = {
        "roi": list(ROI),
        "templates": [
            {"label": "w2", "file": "syn_a.png", "source_frame_hash": ha,
             "asset_sha256": hashlib.sha256((tdir / "syn_a.png").read_bytes()).hexdigest()},
            {"label": "w2", "file": "syn_b.png", "source_frame_hash": hb,
             "asset_sha256": hashlib.sha256((tdir / "syn_b.png").read_bytes()).hexdigest()},
        ],
    }
    (tdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out = cv.leave_one_source_out_or_fail_closed(
        [(src_a, "w2"), (src_b, "w2")], template_dir=tdir)
    assert out["passed"] is True
    assert out["n_passed"] == 2
    for r in out["results"]:
        assert r["cross_source_score"] >= caishen_vision._MIN_SCORE
        assert r["held_out"]  # each frame held out its own source template


def test_review_require_heldout_refuses_without_witness(tmp_path, monkeypatch):
    import review_caishen_pending as review
    import caishen_collector

    templates = tmp_path / "caishen"
    pending = templates / "pending"
    pending.mkdir(parents=True)
    manifest = templates / "manifest.json"
    manifest.write_text(
        json.dumps({"roi": list(ROI), "templates": [], "note": ""},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(review, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(review, "PENDING_DIR", pending)
    monkeypatch.setattr(review, "MANIFEST", manifest)
    monkeypatch.setattr(caishen_vision, "_DIR", templates)
    monkeypatch.setattr(caishen_vision, "_TEMPLATE_CACHE", {})
    # Seed one dummy template so rank_labels/detect have something — not needed
    # for confirm path once a pending sidecar exists.
    crop = _badge_crop(7)
    ok, buf = cv2.imencode(".png", crop)
    assert ok
    name = "unknown_unit_deadbeef"
    (pending / f"{name}.png").write_bytes(buf.tobytes())
    record = {
        "label": None,
        "suggested_label": "w3",
        "file": f"{name}.png",
        "source": "unrecorded",
        "source_sha256": "a" * 64,
        "asset_sha256": hashlib.sha256(buf.tobytes()).hexdigest(),
        "session_id": "unit",
        "at": "2026-10-24",
        "visual_check": "pending: auto_high",
        "source_kind": "auto_nomination",
        "pending": True,
    }
    (pending / f"{name}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rc = review.confirm(f"{name}.json", "w3", require_heldout=True)
    assert rc == 2
    assert (pending / f"{name}.json").exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["templates"] == []


def test_review_confirm_writes_heldout_false_without_witness(tmp_path, monkeypatch):
    import review_caishen_pending as review

    templates = tmp_path / "caishen"
    pending = templates / "pending"
    pending.mkdir(parents=True)
    manifest = templates / "manifest.json"
    manifest.write_text(
        json.dumps({"roi": list(ROI), "templates": [], "note": ""},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(review, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(review, "PENDING_DIR", pending)
    monkeypatch.setattr(review, "MANIFEST", manifest)

    crop = _badge_crop(8)
    ok, buf = cv2.imencode(".png", crop)
    assert ok
    name = "unknown_unit_cafe"
    (pending / f"{name}.png").write_bytes(buf.tobytes())
    record = {
        "label": None,
        "suggested_label": "w4",
        "file": f"{name}.png",
        "source": "unit-src",
        "source_sha256": "b" * 64,
        "asset_sha256": hashlib.sha256(buf.tobytes()).hexdigest(),
        "session_id": "unit",
        "at": "2026-10-24",
        "visual_check": "pending: auto_high",
        "source_kind": "auto_nomination",
        "pending": True,
    }
    (pending / f"{name}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert review.confirm(f"{name}.json", "w4") == 0
    entry = json.loads(manifest.read_text(encoding="utf-8"))["templates"][0]
    assert entry["source_kind"] == "auto_nomination"
    assert entry["review_method"] == "human_eye"
    assert entry["reviewer"] == "operator"
    assert entry["source_frame_hash"] == "b" * 64
    assert entry["heldout_passed"] is False
    assert "fail-closed" in entry["heldout_reason"]
