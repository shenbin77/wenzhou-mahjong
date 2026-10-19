"""Retention, provenance and abstention checks without a connected device."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observation import ObservationRecorder


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recorder = ObservationRecorder(self.root, 'explicit-device', interval=2)
        self.frame = np.zeros((90, 160, 3), np.uint8)
        self.boxes = [(5, 60, 15, 85), (16, 60, 26, 85), (27, 60, 37, 85)]

    def record(self, **kwargs):
        options = dict(boxes=self.boxes, predictions=[], recognized=[1, 2, 3], complete=True,
                       rounds_started=1, rounds_done=0, now=1)
        options.update(kwargs)
        return self.recorder.record(self.frame, **options)

    def test_waiting_screen_retains_no_pixels_or_log(self):
        self.assertIsNone(self.record(boxes=[], recognized=[], complete=False))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lossless_frame_hashes_and_unreviewed_status(self):
        before = self.frame.copy()
        row = self.record()
        path = self.root / row['source_frame']
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['source_sha256'])
        self.assertEqual(hashlib.sha256(self.frame.tobytes()).hexdigest(), row['frame_pixel_sha256'])
        decoded = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(decoded, before)
        np.testing.assert_array_equal(self.frame, before)
        self.assertIsNone(row['gold_labels'])
        self.assertIsNone(row['outcome'])
        self.assertEqual(row['review_status'], 'unreviewed')
        self.assertEqual(row['gameplay_variant'], 'unverified')
        self.assertEqual(row['detected_boxes'], [list(box) for box in self.boxes])
        self.assertEqual(json.loads(self.recorder.log_path.read_text('utf-8')), row)

    def test_identical_pixels_deduplicate_but_new_frames_survive_interval(self):
        self.record()
        self.assertIsNone(self.record(now=5))
        self.frame[0, 0] = 255
        self.assertIsNone(self.record(now=2))
        self.assertIsNotNone(self.record(now=5))
        self.assertEqual(self.recorder.saved_frames, 2)

    def test_settlement_can_be_retained_without_visible_tiles(self):
        row = self.record(boxes=[], recognized=[], complete=False, settlement=True)
        self.assertEqual(row['retention_reason'], 'settlement_template')
        self.assertFalse(row['all_candidates_classified'])

    def test_bad_box_or_unserializable_prediction_does_not_write_evidence(self):
        with self.assertRaises(ValueError):
            self.record(boxes=[(-1, 0, 20, 40)] * 3)
        with self.assertRaises(ValueError):
            self.record(predictions=[{'score': float('nan')}])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reused_output_folder_keeps_sessions_separate(self):
        first = self.record()
        second = ObservationRecorder(self.root, 'second-device', interval=0)
        row = second.record(self.frame, boxes=self.boxes, predictions=[], recognized=[],
                            complete=False, rounds_started=0, rounds_done=0)
        self.assertNotEqual(first['session_id'], row['session_id'])
        self.assertNotEqual(first['source_frame'], row['source_frame'])
        self.assertEqual(len(list(self.root.glob('observations_*.jsonl'))), 2)


if __name__ == '__main__':
    unittest.main()
