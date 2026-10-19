"""Export real screenshot tile crops and import explicitly reviewed labels.

Run from any working directory. Source screenshots are never renamed or changed.
Every output directory must be new; no overwrite or automatic label predictions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from numbers import Integral
from pathlib import Path
import re
import shutil
import sys

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from pc_bot.tile_vision import TARGET_IDS, TILE_IDS, TileVision

VOCABULARIES = {
    "visual34": tuple(TILE_IDS),
    "target28": tuple(label for label, tile_id in TILE_IDS.items() if tile_id in TARGET_IDS),
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pixel_sha256(image: np.ndarray) -> str:
    # Match the existing template provenance convention, including array shape.
    return hashlib.sha256(str(image.shape).encode("ascii") + image.tobytes()).hexdigest()


def read_image(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if not data:
        raise ValueError(f"Cannot decode empty screenshot: {path}")
    try:
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ValueError(f"Cannot decode screenshot: {path}") from exc
    if image is None:
        raise ValueError(f"Cannot decode screenshot: {path}")
    return image


def write_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"Cannot encode PNG: {path}")
    with path.open("xb") as stream:
        stream.write(encoded.tobytes())


def checked_box(box, image: np.ndarray) -> tuple[int, int, int, int]:
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any(
        isinstance(value, bool) or not isinstance(value, Integral) for value in box
    ):
        raise ValueError(f"Box must contain four integer xyxy coordinates: {box!r}")
    x0, y0, x1, y1 = map(int, box)
    height, width = image.shape[:2]
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f"Box {box!r} outside image {width}x{height}")
    return x0, y0, x1, y1


def new_output(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Output must be a new directory; refusing overwrite: {path}")
    path.mkdir(parents=True, exist_ok=False)


def _write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _contact_sheets(output: Path, samples: list[dict], crops: list[np.ndarray]) -> list[str]:
    names = []
    # Bound each page rather than allocating one unbounded canvas for a directory.
    columns, per_page, cell_w, cell_h = 8, 64, 160, 210
    for page, start in enumerate(range(0, len(samples), per_page)):
        count = min(per_page, len(samples) - start)
        sheet = np.full((math.ceil(count / columns) * cell_h, columns * cell_w, 3), 240, np.uint8)
        for local in range(count):
            index = start + local
            crop = crops[index]
            height, width = crop.shape[:2]
            scale = min(140 / width, 170 / height)
            preview = cv2.resize(crop, (max(1, round(width * scale)), max(1, round(height * scale))),
                                 interpolation=cv2.INTER_NEAREST)
            top, left = (local // columns) * cell_h, (local % columns) * cell_w
            cv2.putText(sheet, samples[index]["sample_id"], (left + 8, top + 23),
                        cv2.FONT_HERSHEY_SIMPLEX, .55, (20, 20, 20), 1)
            sheet[top + 32:top + 32 + preview.shape[0], left + 8:left + 8 + preview.shape[1]] = preview
        name = "contact_sheet.png" if page == 0 else f"contact_sheet_{page:03d}.png"
        write_png(output / name, sheet)
        names.append(name)
    return names


def export_samples(source: Path | None, output: Path, *, vocabulary="visual34", roi=None,
                   serial: str | None = None, adb_path: str = "adb", vision=None) -> dict:
    """Export crops using only TileVision.count_tiles, never classification.

    A serial is opt-in and mutually exclusive with files; it captures one frame
    and sends no input. Live device capture is not required for offline exports.
    """
    if vocabulary not in VOCABULARIES:
        raise ValueError(f"Unknown vocabulary: {vocabulary}")
    if (source is None) == (serial is None):
        raise ValueError("Provide exactly one source screenshot/directory or explicit device serial")
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Output must be a new directory; refusing overwrite: {output}")
    captures = []
    if source is not None:
        source = Path(source).resolve()
        if source.is_file():
            paths = [source]
        elif source.is_dir():
            paths = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
        else:
            raise ValueError(f"Screenshot source does not exist: {source}")
        if not paths:
            raise ValueError(f"No screenshot images in source: {source}")
        # Decode each source during detection rather than holding a whole
        # screenshot directory (potentially gigabytes) in memory at once.
        captures = [(path, None, None) for path in paths]
    else:
        from pc_bot.adb_transport import AdbTransport
        with AdbTransport(serial, adb_path) as transport:
            image = transport.capture()
            png = transport.capture_png(image)
        captures = [(output / "captured_source.png", image, png)]
    vision = vision if vision is not None else TileVision()
    sources, samples, crops = [], [], []
    for source_index, (path, image, captured_png) in enumerate(captures):
        if image is None:
            image = read_image(path)
        requested_roi = checked_box(roi, image) if roi is not None else None
        count, boxes = vision.count_tiles(image, roi=requested_roi)
        if count != len(boxes):
            raise ValueError(f"Detector count/box mismatch for {path}: {count} vs {len(boxes)}")
        source_id = f"source_{source_index:03d}"
        sources.append({"source_id": source_id, "path": str(path), "shape": list(image.shape),
                        "file_sha256": hashlib.sha256(captured_png).hexdigest() if captured_png is not None else file_sha256(path),
                        "pixel_sha256": pixel_sha256(image), "roi_xyxy": list(requested_roi) if requested_roi else None,
                        "detected_count": count})
        seen_boxes = set()
        for box in boxes:
            x0, y0, x1, y1 = checked_box(box, image)
            if (x0, y0, x1, y1) in seen_boxes:
                raise ValueError(f"Detector returned duplicate box for {path}: {box}")
            seen_boxes.add((x0, y0, x1, y1))
            crop = image[y0:y1, x0:x1].copy()
            sample_id = f"tile_{len(samples):03d}"
            samples.append({"sample_id": sample_id, "file": sample_id + ".png", "source_id": source_id,
                            "bbox_xyxy": [x0, y0, x1, y1], "shape": list(crop.shape),
                            "pixel_sha256": pixel_sha256(crop), "label": "", "reviewed": False})
            crops.append(crop)
    if not samples:
        raise ValueError("No tiles detected; no dataset was written")
    new_output(output)
    for path, _, captured_png in captures:
        if captured_png is not None:
            with path.open("xb") as stream:
                stream.write(captured_png)
    for record, crop in zip(samples, crops):
        write_png(output / record["file"], crop)
        record["file_sha256"] = file_sha256(output / record["file"])
    sheets = _contact_sheets(output, samples, crops)
    _write_json(output / "labels.json", {record["sample_id"]: "" for record in samples})
    with (output / "labels.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample_id", "label"])
        writer.writerows((record["sample_id"], "") for record in samples)
    manifest = {"schema_version": 1, "kind": "unreviewed_visual_tile_samples",
                "not_gameplay_validation": True, "vocabulary": vocabulary,
                "allowed_labels": list(VOCABULARIES[vocabulary]),
                "pixel_hash_algorithm": "sha256(str(BGR_uint8_shape).encode('ascii') + BGR_uint8_bytes)",
                "sources": sources, "tiles": samples, "contact_sheets": sheets}
    _write_json(output / "manifest.json", manifest)
    return manifest


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key / multiple labels: {key}")
        result[key] = value
    return result


def read_reviewed_labels(path: Path) -> dict[str, str]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_json_pairs)
        if isinstance(data, dict):
            rows = [{"sample_id": key, "label": value} for key, value in data.items()]
        elif isinstance(data, list):
            rows = data
        else:
            raise ValueError("JSON labels must be a sample_id-to-label object or list of rows")
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or sorted(reader.fieldnames) != ["label", "sample_id"]:
                raise ValueError("CSV must have exactly the sample_id,label columns")
            rows = list(reader)
    else:
        raise ValueError("Reviewed labels must be JSON or CSV")
    result, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"sample_id", "label"}:
            raise ValueError(f"Malformed label row: {row!r}")
        sample_id, label = row["sample_id"], row["label"]
        if not isinstance(sample_id, str) or not re.fullmatch(r"tile_\d{3,}", sample_id):
            raise ValueError(f"Invalid sample_id: {sample_id!r}")
        if sample_id in seen:
            raise ValueError(f"Multiple labels / duplicate row for {sample_id}")
        seen.add(sample_id)
        if not isinstance(label, str):
            raise ValueError(f"Expected one text label for {sample_id}: {label!r}")
        if label == "":
            continue  # Blank rows are explicitly unreviewed, not inferred labels.
        result[sample_id] = label
    if not result:
        raise ValueError("No reviewed labels supplied; blank labels are not ground truth")
    return result


def _dataset_file(dataset: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError("Dataset file must be a relative path")
    path = (dataset / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(dataset):
        raise ValueError(f"Dataset file escapes source directory: {relative}")
    return path


def import_reviewed_labels(dataset: Path, labels_path: Path, output: Path, *, vocabulary=None) -> dict:
    dataset, labels_path, output = Path(dataset).resolve(), Path(labels_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Output must be a new directory; refusing overwrite: {output}")
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_pairs)
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "unreviewed_visual_tile_samples":
        raise ValueError("Unsupported sample manifest schema/kind")
    vocabulary = vocabulary or manifest.get("vocabulary")
    if vocabulary not in VOCABULARIES:
        raise ValueError(f"Unknown vocabulary: {vocabulary}")
    labels = read_reviewed_labels(labels_path)
    tiles, sources = {}, {}
    for row in manifest["tiles"]:
        if row["sample_id"] in tiles:
            raise ValueError(f"Duplicate sample_id in manifest: {row['sample_id']}")
        tiles[row["sample_id"]] = row
    for row in manifest["sources"]:
        if row["source_id"] in sources:
            raise ValueError(f"Duplicate source_id in manifest: {row['source_id']}")
        sources[row["source_id"]] = row
    verified_sources, planned, hashes = {}, [], {}
    for sample_id, label in labels.items():
        if sample_id not in tiles:
            raise ValueError(f"Unknown sample_id: {sample_id}")
        if label not in VOCABULARIES[vocabulary]:
            raise ValueError(f"Invalid or ambiguous label {label!r} for vocabulary {vocabulary}")
        row = tiles[sample_id]
        source_id = row["source_id"]
        if source_id not in sources:
            raise ValueError(f"Unknown source_id: {source_id}")
        source_record = sources[source_id]
        if source_id not in verified_sources:
            source_path = Path(source_record["path"])
            source_image = read_image(source_path)
            if file_sha256(source_path) != source_record["file_sha256"] or \
                    pixel_sha256(source_image) != source_record["pixel_sha256"]:
                raise ValueError(f"Source screenshot provenance mismatch: {source_path}")
            verified_sources[source_id] = source_image
        source_image = verified_sources[source_id]
        x0, y0, x1, y1 = checked_box(row["bbox_xyxy"], source_image)
        expected = source_image[y0:y1, x0:x1]
        source_crop = _dataset_file(dataset, row["file"])
        actual = read_image(source_crop)
        if file_sha256(source_crop) != row["file_sha256"] or pixel_sha256(actual) != row["pixel_sha256"] \
                or pixel_sha256(expected) != row["pixel_sha256"] or list(actual.shape) != row["shape"]:
            raise ValueError(f"Crop provenance mismatch: {sample_id}")
        digest = row["pixel_sha256"]
        if digest in hashes and hashes[digest] != label:
            raise ValueError(f"Identical crop pixels have conflicting labels: {sample_id}")
        hashes[digest] = label
        planned.append((sample_id, label, source_crop, row, source_record))
    # All labels and provenance are checked before any output is created.
    new_output(output)
    records = []
    for sample_id, label, source_crop, row, source_record in planned:
        relative = f"{label}/{sample_id}.png"
        destination = output / relative
        destination.parent.mkdir(exist_ok=True)
        with source_crop.open("rb") as source_stream, destination.open("xb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
        records.append({**row, "file": relative, "label": label, "tile_id": TILE_IDS[label], "reviewed": True,
                        "source_sample_file": str(source_crop), "source": source_record})
    result = {"schema_version": 1, "kind": "human_reviewed_visual_tile_samples",
              "not_gameplay_validation": True, "vocabulary": vocabulary,
              "derived_from_manifest": {"path": str(manifest_path), "file_sha256": file_sha256(manifest_path)},
              "reviewed_labels": {"path": str(labels_path), "file_sha256": file_sha256(labels_path)}, "tiles": records}
    _write_json(output / "manifest.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Crop screenshots without predicting labels")
    source = export.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="Screenshot or directory of screenshots")
    source.add_argument("--serial", help="Opt-in read-only capture from this exact ADB serial; sends no input")
    export.add_argument("--adb-path", default="adb", help="ADB executable used only with --serial")
    export.add_argument("--output", type=Path, required=True, help="New output directory; existing paths are rejected")
    export.add_argument("--vocabulary", choices=VOCABULARIES, default="visual34")
    export.add_argument("--roi", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    review = commands.add_parser("import-labels", help="Validate reviewed JSON/CSV labels and make labeled copies")
    review.add_argument("--dataset", type=Path, required=True)
    review.add_argument("--labels", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True, help="New directory; source data is never modified")
    review.add_argument("--vocabulary", choices=VOCABULARIES, default=None, help="Defaults to the exported dataset vocabulary")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            manifest = export_samples(args.source, args.output, vocabulary=args.vocabulary, roi=args.roi,
                                      serial=args.serial, adb_path=args.adb_path)
        else:
            manifest = import_reviewed_labels(args.dataset, args.labels, args.output, vocabulary=args.vocabulary)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, cv2.error) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(args.output.resolve()), "tiles": len(manifest["tiles"]),
                      "vocabulary": manifest["vocabulary"], "source_changed": False,
                      "not_gameplay_validation": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
