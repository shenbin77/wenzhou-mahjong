"""Offline real-frame source verification; no emulator or application IO."""
from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import sys

import cv2
import numpy as np
import pytest

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT / "tests/fixtures/countdown_source"
PROVENANCE = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
CASES = PROVENANCE["fixtures"]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


new = load_module("stage_countdown_under_test", BOT / "countdown_vision.py")
old = load_module("baseline_countdown_under_test", ROOT / "baseline/countdown_vision.py")


def frame(case):
    path = ROOT / "fixtures" / case["file"]
    assert path.is_file(), path
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == case["sha256"], path
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape == (900, 1600, 3), path
    return image


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["file"])
def test_original_real_frame_result(case):
    reading = new.detect_countdown(frame(case))
    assert reading.seconds == case["expected_seconds"], reading.ledger()
    if case["expected_seconds"] is not None:
        assert reading.evidence["source_verified"] is True
        assert reading.evidence["digit_slot"]["complete_single"] is True
        assert reading.status == (new.STATUS_LOW if reading.seconds == 1 else new.STATUS_PRESENT)
    else:
        assert reading.status in (new.STATUS_UNREADABLE, new.STATUS_ABSENT)


@pytest.mark.parametrize("case", [c for c in CASES if c["file"].startswith("chi_false8")],
                         ids=lambda case: case["file"])
def test_baseline_animation_false_eight_is_reproduced_and_rejected(case):
    image = frame(case)
    assert old.detect_countdown(image).seconds == 8
    reading = new.detect_countdown(image)
    assert reading.seconds is None
    assert reading.evidence["source_verified"] is False


@pytest.mark.parametrize("case", [c for c in CASES if c["split"] == "calibration"],
                         ids=lambda case: case["file"])
def test_baseline_real_3_2_1_could_not_be_read(case):
    assert old.detect_countdown(frame(case)).seconds is None


def test_validation_images_never_supply_runtime_assets():
    manifest = json.loads((new.TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))
    calibrations = {c["sha256"] for c in CASES if c["split"] == "calibration"}
    validation = {c["sha256"] for c in CASES if c["split"] == "validation"}
    assert calibrations.isdisjoint(validation)
    records = [manifest["panel"], *manifest["digits"]]
    assert all(record["source_sha256"] in calibrations for record in records)
    assert all(record["source_sha256"] not in validation for record in records)
    assert new.REVIEWED_SECONDS == (1, 2, 3)


def test_source_failure_never_attempts_digit_read(monkeypatch):
    case = next(c for c in CASES if c["file"] == "chi_false8_d.png")
    def forbidden(*args, **kwargs):
        raise AssertionError("source rejection must happen before digit read")
    monkeypatch.setattr(new, "_read_complete_slot", forbidden)
    assert new.detect_countdown(frame(case)).seconds is None


@pytest.mark.parametrize("filename", ["unreviewed_10_17.png", "unreviewed_10_31.png"])
def test_two_digits_are_not_cropped_to_a_single_seconds_value(filename):
    case = next(c for c in CASES if c["file"] == filename)
    result = new.detect_countdown(frame(case))
    assert result.seconds is None
    assert result.evidence["source_verified"] is True
    assert result.evidence["digit_slot"]["complete_single"] is False


@pytest.mark.parametrize("fault", ["missing", "tampered", "manifest_nonobject", "manifest_malformed"])
def test_template_fault_fails_closed_after_previous_success(tmp_path, monkeypatch, fault):
    copy = tmp_path / "templates"
    shutil.copytree(new.TEMPLATE_DIR, copy)
    monkeypatch.setattr(new, "TEMPLATE_DIR", copy)
    case = next(c for c in CASES if c["file"] == "independent_3_me.png")
    image = frame(case)
    assert new.detect_countdown(image).seconds == 3
    asset = copy / "seconds_3.png"
    if fault == "missing":
        asset.unlink()
    elif fault == "tampered":
        asset.write_bytes(asset.read_bytes() + b"tamper")
    elif fault == "manifest_nonobject":
        (copy / "manifest.json").write_text("[]", encoding="utf-8")
    else:
        (copy / "manifest.json").write_text("{bad JSON", encoding="utf-8")
    result = new.detect_countdown(image)
    assert result.seconds is None
    assert result.evidence["reason"].startswith("countdown_template_error:")


def test_blank_and_invalid_frame_keep_unknown_seconds():
    assert new.detect_countdown(None).seconds is None
    assert new.detect_countdown(np.zeros((720, 1280, 3), np.uint8)).seconds is None
    blank = new.detect_countdown(np.zeros((900, 1600, 3), np.uint8))
    assert blank.seconds is None and blank.status == new.STATUS_ABSENT
    assert new.detect_countdown(np.zeros((900, 1600, 3), np.float32)).seconds is None


def test_runtime_assets_are_literal_crops_from_retained_calibration_frames():
    manifest = json.loads((new.TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))
    calibrations = {c["sha256"]: c for c in CASES if c["split"] == "calibration"}
    for record in [manifest["panel"], *manifest["digits"]]:
        original = frame(calibrations[record["source_sha256"]])
        x0, y0, x1, y1 = record["crop_xyxy"]
        asset_raw = (new.TEMPLATE_DIR / record["file"]).read_bytes()
        assert hashlib.sha256(asset_raw).hexdigest() == record["asset_sha256"]
        asset = cv2.imdecode(np.frombuffer(asset_raw, np.uint8), cv2.IMREAD_COLOR)
        assert np.array_equal(asset, original[y0:y1, x0:x1])
