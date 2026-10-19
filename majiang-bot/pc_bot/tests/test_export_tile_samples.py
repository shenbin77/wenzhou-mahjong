"""Offline sample-export QA; no emulator connection, prediction or input."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from pc_bot.tools.export_tile_samples import (
    VOCABULARIES, export_samples, file_sha256, import_reviewed_labels,
    pixel_sha256, read_image, read_reviewed_labels, write_png,
)


class ExportSamplesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "原截图.png"
        self.image = np.arange(20 * 30 * 3, dtype=np.uint16).reshape(20, 30, 3).astype(np.uint8)
        write_png(self.source, self.image)
        self.original_sha = file_sha256(self.source)
        self.dataset = self.base / "samples"
        self.vision = Mock()
        self.vision.count_tiles.return_value = (2, [(1, 2, 10, 15), (15, 3, 25, 16)])

    def exported(self, **options):
        return export_samples(self.source, self.dataset, vision=self.vision, **options)

    def labels(self, content, suffix=".json"):
        path = self.base / ("reviewed" + suffix)
        path.write_text(json.dumps(content) if not isinstance(content, str) else content, encoding="utf-8")
        return path

    def test_lossless_crops_blank_labels_and_coordinate_provenance(self):
        result = self.exported()
        self.vision.count_tiles.assert_called_once()
        self.vision.classify_crop.assert_not_called()
        self.vision.recognize_hand.assert_not_called()
        self.assertEqual(file_sha256(self.source), self.original_sha)
        self.assertEqual(len(result["tiles"]), 2)
        self.assertEqual(result["sources"][0]["file_sha256"], self.original_sha)
        self.assertTrue(result["not_gameplay_validation"])
        self.assertEqual(json.loads((self.dataset / "labels.json").read_text()), {"tile_000": "", "tile_001": ""})
        for row in result["tiles"]:
            x0, y0, x1, y1 = row["bbox_xyxy"]
            crop = read_image(self.dataset / row["file"])
            np.testing.assert_array_equal(crop, self.image[y0:y1, x0:x1])
            self.assertEqual(row["pixel_sha256"], pixel_sha256(crop))
            self.assertEqual(row["label"], "")
            self.assertFalse(row["reviewed"])
        self.assertGreater(read_image(self.dataset / "contact_sheet.png").size, 0)

    def test_directory_export_orders_sources_without_name_collisions(self):
        folder = self.base / "inputs"
        folder.mkdir()
        write_png(folder / "b.png", self.image)
        write_png(folder / "a.png", self.image)
        (folder / "notes.txt").write_text("not an image")
        result = export_samples(folder, self.dataset, vision=self.vision)
        self.assertEqual([Path(row["path"]).name for row in result["sources"]], ["a.png", "b.png"])
        self.assertEqual([row["file"] for row in result["tiles"]],
                         ["tile_000.png", "tile_001.png", "tile_002.png", "tile_003.png"])

    def test_existing_output_is_rejected_without_changing_any_file(self):
        self.dataset.mkdir()
        marker = self.dataset / "existing.txt"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.exported()
        self.assertEqual(marker.read_text(), "keep")
        self.vision.count_tiles.assert_not_called()
        self.assertEqual(file_sha256(self.source), self.original_sha)

    def test_out_of_bounds_roi_and_detector_boxes_fail_before_writing(self):
        for roi in ((-1, 0, 10, 10), (0, 0, 31, 20), (5, 5, 5, 8)):
            with self.subTest(roi=roi), self.assertRaises(ValueError):
                self.exported(roi=roi)
            self.assertFalse(self.dataset.exists())
        for boxes in ([(-1, 0, 10, 10)], [(0, 0, 31, 10)], [(0, 0, 10, 10)] * 2,
                      [(0, 0, 10.5, 10)]):
            self.vision.count_tiles.return_value = (len(boxes), boxes)
            with self.subTest(boxes=boxes), self.assertRaises(ValueError):
                self.exported()
            self.assertFalse(self.dataset.exists())

    def test_zero_tiles_bad_images_and_count_mismatch_are_explicit(self):
        self.vision.count_tiles.return_value = (0, [])
        with self.assertRaisesRegex(ValueError, "No tiles detected"):
            self.exported()
        self.assertFalse(self.dataset.exists())
        self.vision.count_tiles.return_value = (2, [(0, 0, 10, 10)])
        with self.assertRaisesRegex(ValueError, "count/box mismatch"):
            self.exported()
        bad = self.base / "bad.png"
        bad.write_bytes(b"not PNG")
        with self.assertRaisesRegex(ValueError, "Cannot decode"):
            export_samples(bad, self.dataset)
        self.assertFalse(self.dataset.exists())
        empty = self.base / "empty.png"
        empty.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty screenshot"):
            export_samples(empty, self.dataset)

    def test_vocabularies_are_target28_and_visual34_with_one_red_zhong(self):
        self.assertEqual(len(VOCABULARIES["target28"]), 28)
        self.assertEqual(len(VOCABULARIES["visual34"]), 34)
        self.assertIn("hongzhong", VOCABULARIES["target28"])
        self.assertNotIn("dong", VOCABULARIES["target28"])
        self.assertNotIn("zhong", VOCABULARIES["visual34"])

    def test_json_import_makes_labeled_copies_and_preserves_sources(self):
        self.exported()
        crop_hashes = {path.name: file_sha256(path) for path in self.dataset.iterdir() if path.is_file()}
        labels = self.labels({"tile_000": "w1", "tile_001": "hongzhong"})
        out = self.base / "reviewed"
        result = import_reviewed_labels(self.dataset, labels, out, vocabulary="target28")
        self.assertEqual(result["vocabulary"], "target28")
        self.assertEqual(result["reviewed_labels"]["file_sha256"], file_sha256(labels))
        self.assertEqual(result["derived_from_manifest"]["file_sha256"], file_sha256(self.dataset / "manifest.json"))
        for row in result["tiles"]:
            self.assertTrue(row["reviewed"])
            self.assertEqual(file_sha256(out / row["file"]), crop_hashes[row["sample_id"] + ".png"])
            self.assertEqual(row["source"]["path"], str(self.source))
        self.assertEqual(crop_hashes, {path.name: file_sha256(path) for path in self.dataset.iterdir() if path.is_file()})
        self.assertEqual(file_sha256(self.source), self.original_sha)

    def test_csv_import_supports_partial_review_without_filling_blanks(self):
        self.exported()
        labels = self.labels("sample_id,label\ntile_000,w5\ntile_001,\n", ".csv")
        result = import_reviewed_labels(self.dataset, labels, self.base / "reviewed")
        self.assertEqual([row["sample_id"] for row in result["tiles"]], ["tile_000"])

    def test_rejects_ambiguous_invalid_unknown_or_out_of_vocabulary_labels(self):
        self.exported()
        for labels, vocabulary in (({"tile_000": "w1/w2"}, "visual34"),
                                   ({"tile_000": ["w1", "w2"]}, "visual34"),
                                   ({"tile_000": "w10"}, "visual34"),
                                   ({"tile_000": "dong"}, "target28"),
                                   ({"tile_999": "w1"}, "visual34"),
                                   ({"tile_000": ""}, "visual34")):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                import_reviewed_labels(self.dataset, self.labels(labels), self.base / "bad_output", vocabulary=vocabulary)
            self.assertFalse((self.base / "bad_output").exists())

    def test_duplicate_json_keys_csv_rows_and_malformed_rows_are_rejected(self):
        for text, suffix in (('{"tile_000":"w1","tile_000":"w2"}', ".json"),
                             ('[{"sample_id":"tile_000","label":"w1"},{"sample_id":"tile_000","label":"w1"}]', ".json"),
                             ('sample_id,label\ntile_000,w1\ntile_000,w2\n', ".csv"),
                             ('sample_id,label,label\ntile_000,w1,w2\n', ".csv"),
                             ('sample_id,label\ntile_000,w1,extra\n', ".csv"),
                             ('[{"sample_id":"tile_000"}]', ".json")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_reviewed_labels(self.labels(text, suffix))

    def test_tampered_crop_or_original_screenshot_provenance_is_rejected(self):
        self.exported()
        labels = self.labels({"tile_000": "w1"})
        crop_path = self.dataset / "tile_000.png"
        original_crop = crop_path.read_bytes()
        write_target = read_image(crop_path)
        write_target[0, 0] = (0, 0, 0)
        crop_path.unlink()
        write_png(crop_path, write_target)
        with self.assertRaisesRegex(ValueError, "Crop provenance mismatch"):
            import_reviewed_labels(self.dataset, labels, self.base / "bad_output")
        crop_path.write_bytes(original_crop)
        original = self.source.read_bytes()
        self.source.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "Source screenshot provenance mismatch"):
            import_reviewed_labels(self.dataset, labels, self.base / "bad_output")
        self.assertFalse((self.base / "bad_output").exists())

    def test_manifest_out_of_bounds_and_path_escape_are_rejected(self):
        self.exported()
        manifest_path = self.dataset / "manifest.json"
        original = json.loads(manifest_path.read_text())
        labels = self.labels({"tile_000": "w1"})
        for field, value in (("bbox_xyxy", [0, 0, 99, 99]), ("file", "../原截图.png")):
            changed = json.loads(json.dumps(original))
            changed["tiles"][0][field] = value
            manifest_path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaises(ValueError):
                import_reviewed_labels(self.dataset, labels, self.base / "bad_output")
            self.assertFalse((self.base / "bad_output").exists())

    def test_identical_pixels_cannot_be_imported_under_conflicting_labels(self):
        self.image[:, 15:25] = self.image[:, 1:11]
        self.source.unlink()
        write_png(self.source, self.image)
        self.vision.count_tiles.return_value = (2, [(1, 2, 10, 15), (15, 2, 24, 15)])
        self.exported()
        with self.assertRaisesRegex(ValueError, "conflicting labels"):
            import_reviewed_labels(self.dataset, self.labels({"tile_000": "w1", "tile_001": "w2"}), self.base / "bad_output")

    def test_import_refuses_output_collision_before_any_source_change(self):
        self.exported()
        out = self.base / "reviewed"
        out.mkdir()
        (out / "keep.txt").write_text("keep")
        with self.assertRaises(FileExistsError):
            import_reviewed_labels(self.dataset, self.labels({"tile_000": "w1"}), out)
        self.assertEqual((out / "keep.txt").read_text(), "keep")
        self.assertEqual(file_sha256(self.source), self.original_sha)

    def test_help_runs_outside_project_directory(self):
        script = Path(__file__).resolve().parents[1] / "tools/export_tile_samples.py"
        result = subprocess.run([sys.executable, str(script), "--help"], cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("import-labels", result.stdout)
        self.assertIn("export", result.stdout)

    def test_opt_in_device_capture_only_reads_and_keeps_source_provenance(self):
        with patch("pc_bot.adb_transport.AdbTransport") as transport:
            connection = transport.return_value.__enter__.return_value
            connection.capture.return_value = self.image
            connection.capture_png.return_value = self.source.read_bytes()
            result = export_samples(None, self.dataset, serial="offline-unit-test", adb_path="fake-adb",
                                    vision=self.vision)
            transport.assert_called_once_with("offline-unit-test", "fake-adb")
            connection.tap.assert_not_called()
        self.assertEqual(result["sources"][0]["path"], str(self.dataset / "captured_source.png"))
        self.assertEqual(result["sources"][0]["file_sha256"], file_sha256(self.dataset / "captured_source.png"))
        self.assertEqual(result["sources"][0]["pixel_sha256"], pixel_sha256(self.image))

    def test_source_and_device_are_explicitly_exclusive(self):
        for source, serial in ((None, None), (self.source, "offline-unit-test")):
            with self.subTest(source=source, serial=serial), self.assertRaisesRegex(ValueError, "exactly one"):
                export_samples(source, self.dataset, serial=serial)
        self.assertFalse(self.dataset.exists())


if __name__ == "__main__":
    unittest.main()
