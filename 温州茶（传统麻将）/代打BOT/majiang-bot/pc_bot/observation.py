"""Frame-backed observation evidence; predictions are never promoted to labels."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

import cv2
import numpy as np


class ObservationRecorder:
    """Keep candidate table frames, with provenance and explicit review status.

    A detected row is only a retention filter, not proof of the game variant.
    Frames without a candidate tile row/verified settlement are not retained.
    Callers still update status.json on every tick, including waiting screens.
    """

    def __init__(self, output, serial, interval=2.0):
        if interval < 0:
            raise ValueError('Observation interval must be nonnegative')
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.serial = str(serial)
        self.interval = float(interval)
        self.session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:12]
        self.log_path = self.output / f'observations_{self.session_id}.jsonl'
        self.frames_dir = self.output / 'frames' / self.session_id
        self._last_saved_at = None
        self._last_pixels_hash = None
        self.saved_frames = 0

    def record(self, frame, *, boxes, predictions, recognized, complete,
               rounds_started, rounds_done, event=None, settlement=False, now=None):
        # Three candidates retain reduced hands after exposed melds. The detector
        # can abstain without forcing a 13/14-card interpretation on unknown UI.
        if len(boxes) < 3 and not settlement:
            return None
        current = time.monotonic() if now is None else float(now)
        if self._last_saved_at is not None and current - self._last_saved_at < self.interval:
            return None
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError('Observation frame must be BGR uint8')
        height, width = frame.shape[:2]
        for box in boxes:
            if len(box) != 4:
                raise ValueError('Each observation box must be xyxy')
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ValueError('Observation box outside frame')
        pixels_hash = hashlib.sha256(np.ascontiguousarray(frame).tobytes()).hexdigest()
        if pixels_hash == self._last_pixels_hash:
            return None
        ok, encoded = cv2.imencode('.png', frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError('Failed to encode lossless observation PNG')
        png = encoded.tobytes()
        relative = Path('frames') / self.session_id / f'frame_{self.saved_frames:06d}.png'
        record = {
            'schema': 'observation/v1',
            'session_id': self.session_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'serial': self.serial,
            'source_frame': relative.as_posix(),
            'source_sha256': hashlib.sha256(png).hexdigest(),
            'frame_pixel_sha256': pixels_hash,
            'shape': [height, width, 3],
            'detected_boxes': [[int(v) for v in box] for box in boxes],
            'predictions': predictions,
            'recognized_tiles': [int(tile) for tile in recognized],
            'all_candidates_classified': bool(complete),
            'candidate_round_event': event,
            'candidate_rounds_started': int(rounds_started),
            'candidate_rounds_done': int(rounds_done),
            'retention_reason': 'settlement_template' if settlement else 'candidate_tile_row',
            'gameplay_variant': 'unverified',
            'gold_labels': None,
            'review_status': 'unreviewed',
            'outcome': None,
        }
        # Validate serialization before writing either artifact. Never hide a
        # filesystem error or claim a saved frame when evidence was not written.
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        with (self.output / relative).open('xb') as stream:
            stream.write(png)
        with self.log_path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(line)
        self._last_saved_at = current
        self._last_pixels_hash = pixels_hash
        self.saved_frames += 1
        return record
