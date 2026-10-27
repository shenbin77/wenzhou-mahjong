"""Contract tests for source exclusions and leakage-safe CNN indexes, not model tests."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from PIL import Image, ImageDraw
import pytest

HERE = Path(__file__).resolve().parent
BUILDER_PATH = HERE.parent / "tools/build_tea2_cnn_manifest.py"
POLICY_PATH = HERE.parent / "datasets/tea2_cnn_v1_policy.json"
SPEC = importlib.util.spec_from_file_location("tea2_cnn_manifest_builder", BUILDER_PATH)
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


@pytest.fixture
def policy():
    return builder.load_policy(POLICY_PATH)


@pytest.fixture
def capture(tmp_path):
    root = tmp_path / "original_capture"
    root.mkdir()
    return root


def create_frame(capture, name, *, family="assist_sessions", seed=1, size=(1600, 900)):
    directory = capture / family / name
    path = directory / "live" / "tea_2p" / "frame.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (seed % 200, 20, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((7, 770, 92, 890), fill=((seed * 13) % 255, 100, 140))
    draw.line((10, 780, 80, 880), fill=(200, seed % 255, 1), width=7)
    image.save(path, format="PNG")
    image.close()
    return path


def event(path, *, label="w1", box=None, mode_evidence=True):
    row = {
        "schema": "recognition_audit/v2", "source": "live", "mode": "tea_2p",
        "frame": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": "wait", "reason": "向听2，等待摸牌",
        "verification": "unreviewed_prediction",
        "diagnostics": [{
            "box": box or [6, 768, 95, 892], "label": label,
            "tile_id": builder.EXPECTED_LABELS.get(label, 55), "accepted": True,
            "region": "concealed", "score": 0.96, "margin": 0.20, "reason": "accepted",
        }],
    }
    if mode_evidence:
        row["mode_evidence"] = {
            "player_count": 2, "source": "screen_rule_text", "verified": True,
            "score": 0.99, "margin": 0.20,
        }
    return row


def audit_for(path, rows):
    audit = path.parents[2] / "recognition_audit.jsonl"
    audit.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return audit


def run_index(tmp_path, capture, policy, paths, version="v1"):
    output = tmp_path / version
    report = builder.build_manifest(paths, capture_root=capture, output_dir=output,
                                    policy=policy, dry_run=True)
    def rows(name):
        return [json.loads(s) for s in (output / name).read_text(encoding="utf-8").splitlines()]
    return report, rows("frames.jsonl"), rows("weak_crops.jsonl"), rows("quarantine.jsonl"), output


def test_valid_wait_prediction_remains_weak_and_sources_untouched(tmp_path, capture, policy):
    frame = create_frame(capture, "20261023_120000", seed=11)
    row = event(frame)
    row["verification"] = "independently_verified_by_rule_engine"
    audit = audit_for(frame, [row, row])
    before = {p: p.read_bytes() for p in (frame, audit)}
    report, frames, crops, rejected, output = run_index(tmp_path, capture, policy, [audit])
    assert report["counts"]["gold_crops"] == 0
    assert report["counts"]["unique_weak_crops"] == 1
    assert crops[0]["occurrences"] == 2
    assert crops[0]["label_tier"] == "weak_unreviewed_prediction"
    assert crops[0]["independent_human_gold"] is False
    assert crops[0]["room_match_provenance_complete"] is False
    assert crops[0]["origins"][0]["status"] == "wait"
    assert report["independent_match_split"] is False
    assert report["independent_human_evaluation_ready"] is False
    assert len(frames) == 1 and not rejected
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not list(output.rglob("*.png"))


@pytest.mark.parametrize("dev_kind", ["tree", "header", "ambiguous_header", "nested", "copied_bytes", "reencoded_pixels"])
def test_dev_exclusion_is_inherited_by_copies(tmp_path, capture, policy, dev_kind):
    paths = []
    source = create_frame(capture, "ui_20261023_120000_device", family="dev_auto_play")
    source_row = event(source)
    source_audit = audit_for(source, [source_row])
    (source_audit.parent / "actions.jsonl").write_text(
        json.dumps({"dev": True, "note": "开发局数据，不进训练语料"}) + "\n", encoding="utf-8")
    target = create_frame(capture, "20261023_120001", seed=3)
    row = event(target)
    if dev_kind == "tree":
        paths = [source_audit]
    elif dev_kind == "header":
        (target.parents[2] / "actions.jsonl").write_text(
            '{"dev": true, "note":"开发局数据，不进训练语料"}\n', encoding="utf-8")
    elif dev_kind == "ambiguous_header":
        (target.parents[2] / "actions.jsonl").write_text(
            '{"dev": "true"}\n', encoding="utf-8")
    elif dev_kind == "nested":
        row["provenance"] = {"source_frame": str(source), "dev": False}
    elif dev_kind == "copied_bytes":
        shutil.copyfile(source, target)
        row = event(target)
        paths.append(source_audit)
    else:
        with Image.open(source) as image:
            image.save(target, format="PNG", compress_level=0)
        assert source.read_bytes() != target.read_bytes()
        row = event(target)
        paths.append(source_audit)
    if dev_kind != "tree":
        paths.append(audit_for(target, [row]))
    report, frames, crops, rejected, _ = run_index(tmp_path, capture, policy, paths)
    assert not frames and not crops
    assert report["counts"]["gold_crops"] == 0
    assert rejected
    if dev_kind in ("copied_bytes", "reencoded_pixels"):
        assert any(q["reason"] == "excluded_source_content_copy" for q in rejected)


def test_mirror_audit_is_never_opened_as_a_source(tmp_path, capture, policy):
    frame = create_frame(capture, "20261023_120000")
    original = audit_for(frame, [event(frame)])
    mirror = tmp_path / "mirror" / "assist_sessions" / "20261023_120000" / "recognition_audit.jsonl"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("invalid copied JSON which must not be parsed", encoding="utf-8")
    report, _, crops, rejected, _ = run_index(tmp_path, capture, policy, [mirror, original])
    assert len(crops) == 1
    assert report["counts"]["excluded_noncanonical_audit_files"] == 1
    assert not any(q["reason"] == "invalid_audit_json" for q in rejected)


@pytest.mark.parametrize("fault", ["sha", "size", "missing_mode", "four_players", "selected_mode_only",
                                   "bbox_outside", "bbox_float", "bbox_bool", "tile_id", "caishen", "red",
                                   "preflight", "missing_frame", "not_png"])
def test_bad_evidence_never_enters_dataset(tmp_path, capture, policy, fault):
    frame = create_frame(capture, "20261023_120000", size=(800, 450) if fault == "size" else (1600, 900))
    row = event(frame)
    if fault == "sha":
        row["sha256"] = "0" * 64
    elif fault == "missing_mode":
        row.pop("mode_evidence")
    elif fault == "four_players":
        row["mode_evidence"]["player_count"] = 4
    elif fault == "selected_mode_only":
        row["mode_evidence"]["source"] = "ui_selection"
    elif fault == "bbox_outside":
        row["diagnostics"][0]["box"] = [-1, 768, 95, 892]
    elif fault == "bbox_float":
        row["diagnostics"][0]["box"][0] = 6.0
    elif fault == "bbox_bool":
        row["diagnostics"][0]["box"][0] = True
    elif fault == "tile_id":
        row["diagnostics"][0]["tile_id"] = 2
    elif fault == "caishen":
        row["diagnostics"][0]["reason"] = "caishen_marked"
    elif fault == "red":
        row["diagnostics"][0].update(label="hongzhong", tile_id=55)
    elif fault == "preflight":
        row["source"] = "offline_preflight"
    elif fault == "missing_frame":
        frame.unlink()
    elif fault == "not_png":
        frame.write_bytes(b"not a PNG image")
        row["sha256"] = hashlib.sha256(frame.read_bytes()).hexdigest()
    audit = audit_for(frame, [row])
    report, _, crops, _, _ = run_index(tmp_path, capture, policy, [audit])
    assert crops == []
    assert report["counts"]["unique_weak_crops"] == 0


def test_same_frame_in_different_sessions_is_one_component_and_deduplicated(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000", seed=7)
    b = create_frame(capture, "20261023_120001", seed=9)
    shutil.copyfile(a, b)
    paths = [audit_for(a, [event(a)]), audit_for(b, [event(b)])]
    report, frames, crops, _, output = run_index(tmp_path, capture, policy, paths)
    groups = json.loads((output / "split_manifest.json").read_text(encoding="utf-8"))["groups"]
    assert len(frames) == len(crops) == len(groups) == 1
    assert len(groups[0]["sessions"]) == 2
    assert len(crops[0]["origins"]) == 2
    assert report["counts"]["multi_session_components"] == 1


def test_reencoded_same_frame_is_grouped_by_verified_pixels(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000", seed=7)
    b = create_frame(capture, "20261023_120001", seed=9)
    with Image.open(a) as image:
        image.save(b, format="PNG", compress_level=0)
    assert a.read_bytes() != b.read_bytes()
    paths = [audit_for(a, [event(a)]), audit_for(b, [event(b)])]
    _, frames, crops, _, _ = run_index(tmp_path, capture, policy, paths)
    assert len(frames) == 2 and len(crops) == 1
    assert len({f["group_id"] for f in frames}) == 1
    assert len({f["frame_pixel_sha256"] for f in frames}) == 1


def test_label_conflicts_are_quarantined_not_resolved_by_score(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000")
    first, second = event(a, label="w1"), event(a, label="w2")
    second["diagnostics"][0]["score"] = 0.999
    audit = audit_for(a, [first, second])
    report, _, crops, rejected, _ = run_index(tmp_path, capture, policy, [audit])
    assert not crops
    assert report["counts"]["frame_annotation_conflict_pixel_frames"] == 1
    assert any(q["reason"] == "frame_annotation_conflict" for q in rejected)


def test_identical_crop_in_different_frame_groups_is_quarantined(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000", seed=7)
    b = create_frame(capture, "20261023_120001", seed=7)
    with Image.open(b) as image:
        copy_b = image.copy()
    ImageDraw.Draw(copy_b).rectangle((500, 100, 550, 150), fill=(244, 123, 11))
    copy_b.save(b)
    copy_b.close()
    paths = [audit_for(a, [event(a)]), audit_for(b, [event(b)])]
    report, frames, crops, rejected, _ = run_index(tmp_path, capture, policy, paths)
    assert len(frames) == 2 and not crops
    assert report["counts"]["cross_group_identical_crop_hashes"] == 1
    assert any(q["reason"] == "identical_crop_across_independent_session_groups" for q in rejected)


def test_fixed_seed_components_are_order_independent(tmp_path, capture, policy):
    paths = []
    for i in range(10):
        frame = create_frame(capture, f"20261023_1200{i:02d}", seed=i + 15)
        paths.append(audit_for(frame, [event(frame)]))
    _, frames_a, crops_a, _, out_a = run_index(tmp_path, capture, policy, paths, "v1")
    _, frames_b, crops_b, _, out_b = run_index(tmp_path, capture, policy, list(reversed(paths)), "v2")
    assert frames_a == frames_b and crops_a == crops_b
    assert (out_a / "split_manifest.json").read_bytes() == (out_b / "split_manifest.json").read_bytes()


@pytest.mark.parametrize("key", ["frame_sha256", "frame_pixel_sha256", "session", "crop_sha256"])
def test_final_export_check_rejects_cross_split_duplicates(key):
    frames = [
        {"frame_sha256": "a", "frame_pixel_sha256": "pa", "group_id": "ga", "sessions": ["s1"], "split": "train"},
        {"frame_sha256": "b", "frame_pixel_sha256": "pb", "group_id": "gb", "sessions": ["s2"], "split": "test"},
    ]
    crops = []
    if key == "session":
        frames[1]["sessions"] = ["s1"]
    elif key == "crop_sha256":
        crops = [{"crop_sha256": "same", "group_id": g, "split": s,
                  "origins": [], "label_tier": "weak_unreviewed_prediction"}
                 for g, s in (("ga", "train"), ("gb", "test"))]
    else:
        frames[1][key] = frames[0][key]
    with pytest.raises(ValueError, match="cross_split"):
        builder.validate_no_leakage(frames, crops)


def test_union_is_transitive_and_seed_assignment_is_whole_group():
    shared = {"frame1": {"A", "B"}, "frame2": {"B", "C"}}
    mapping, groups = builder.assign_groups({"A", "B", "C", "D"}, shared, 123,
                                            {"train": .8, "validation": .1, "test": .1})
    assert mapping["A"]["group_id"] == mapping["B"]["group_id"] == mapping["C"]["group_id"]
    assert len(groups) == 2


def test_output_never_overwrites_or_writes_into_capture_tree(tmp_path, capture, policy):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        builder.build_manifest([], capture_root=capture, output_dir=existing, policy=policy)
    with pytest.raises(ValueError, match="output_inside_source"):
        builder.build_manifest([], capture_root=capture, output_dir=capture / "dataset", policy=policy)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_symlink_audit_is_rejected(tmp_path, capture, policy):
    frame = create_frame(capture, "20261023_120000")
    audit = audit_for(frame, [event(frame)])
    alias_dir = capture / "assist_sessions" / "20261023_120001"
    try:
        alias_dir.symlink_to(audit.parent, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit unprivileged symlinks")
    identity, reason = builder.classify_audit(alias_dir / "recognition_audit.jsonl", capture.resolve(), policy)
    assert identity is None and reason == "aliased_or_symlink_path"


def test_policy_cannot_relax_mode_or_promote_gold(tmp_path, policy):
    for section, key, value in [
        ("weak", "score_min", 0.7), ("weak", "require_screen_mode_evidence", False),
        ("gold", "gold_import_enabled", True),
    ]:
        modified = copy.deepcopy(policy)
        modified[section][key] = value
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(modified), encoding="utf-8")
        with pytest.raises(ValueError):
            builder.load_policy(path)


def test_audit_referencing_other_session_frame_is_rejected(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000")
    b = create_frame(capture, "20261023_120001", seed=2)
    wrong_audit = audit_for(a, [event(b)])
    _, _, crops, rejected, _ = run_index(tmp_path, capture, policy, [wrong_audit])
    assert not crops
    assert any("frame_outside_own_session" in q.get("exclusions", []) for q in rejected)


@pytest.mark.parametrize("variant", ["shift_same_label", "shift_other_label",
                                      "cross_session", "cross_encoding"])
def test_original_observation_box_conflict_quarantines_entire_frame(tmp_path, capture, policy, variant):
    a = create_frame(capture, "20261023_120000", seed=7)
    first = event(a)
    second = event(a, label="w1" if variant == "shift_same_label" else "w2",
                   box=[7, 768, 95, 892])
    if variant in ("cross_session", "cross_encoding"):
        b = create_frame(capture, "20261023_120001", seed=9)
        if variant == "cross_encoding":
            with Image.open(a) as image:
                image.save(b, format="PNG", compress_level=0)
        else:
            shutil.copyfile(a, b)
        second["frame"] = str(b)
        second["sha256"] = hashlib.sha256(b.read_bytes()).hexdigest()
        paths = [audit_for(a, [first]), audit_for(b, [second])]
    else:
        paths = [audit_for(a, [first, second])]
    report, frames, crops, rejected, _ = run_index(tmp_path, capture, policy, paths)
    assert not frames and not crops
    assert report["counts"]["frame_annotation_conflict_pixel_frames"] == 1
    conflict = next(q for q in rejected if q["reason"] == "frame_annotation_conflict")
    assert len(conflict["observations"]) == 2
    assert {tuple(o["annotations"][0]["box"]) for o in conflict["observations"]} == {
        (6, 768, 95, 892), (7, 768, 95, 892)}


def test_excluded_preflight_reencoded_into_live_remains_excluded(tmp_path, capture, policy):
    generated = create_frame(capture, "20261023_120000", seed=7)
    source = generated.parents[2] / "preflight" / "tea_2p" / "frame.png"
    source.parent.mkdir(parents=True)
    shutil.copyfile(generated, source)
    source_row = event(source)
    source_row["source"] = "offline_preflight"
    source_audit = audit_for(source, [source_row])
    destination = create_frame(capture, "20261023_120001", seed=9)
    with Image.open(source) as image:
        image.save(destination, format="PNG", compress_level=0)
    assert source.read_bytes() != destination.read_bytes()
    destination_audit = audit_for(destination, [event(destination)])
    report, frames, crops, rejected, _ = run_index(
        tmp_path, capture, policy, [source_audit, destination_audit])
    assert report["counts"]["excluded_frames_content_verified"] == 1
    assert not frames and not crops
    assert any(q["reason"] == "excluded_source_content_copy" for q in rejected)


def test_crop_label_conflict_across_distinct_frames_remains_quarantined(tmp_path, capture, policy):
    a = create_frame(capture, "20261023_120000", seed=7)
    b = create_frame(capture, "20261023_120001", seed=7)
    with Image.open(b) as image:
        changed = image.copy()
    ImageDraw.Draw(changed).rectangle((500, 100, 550, 150), fill=(244, 123, 11))
    changed.save(b)
    changed.close()
    report, _, crops, rejected, _ = run_index(
        tmp_path, capture, policy, [audit_for(a, [event(a, label="w1")]),
                                   audit_for(b, [event(b, label="w2")])])
    assert not crops
    assert report["counts"]["conflicting_crop_hashes"] == 1
    assert any(q["reason"] == "conflicting_labels" for q in rejected)


@pytest.mark.parametrize("schema", ["recognition_audit/v1", "recognition_audit/v2"])
def test_supported_producer_schema_keeps_exact_frame_provenance(tmp_path, capture, policy, schema):
    frame = create_frame(capture, "20261023_120000", seed=11)
    row = event(frame)
    row["schema"] = schema
    report, _, crops, _, _ = run_index(tmp_path, capture, policy, [audit_for(frame, [row])])
    assert len(crops) == 1
    assert crops[0]["origins"][0]["audit_schema"] == schema
    assert report["audit_schema_counts"] == {schema: 1}
    assert report["builder_source_sha256"] == hashlib.sha256(
        (BUILDER_PATH).read_bytes()).hexdigest()
