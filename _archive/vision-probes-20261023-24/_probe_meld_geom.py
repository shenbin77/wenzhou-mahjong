"""Measure raw hand/meld box geometry on real frames (read-only probe)."""
import sys
from pathlib import Path

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')

import cv2
import numpy as np

from tile_vision import _read, face_region


def raw_boxes(img, roi=None):
    """Identical to TileVision.count_tiles lines 165..223, returns sorted boxes."""
    h, w = img.shape[:2]
    if roi is None:
        roi = (0, round(h * .8), w, h)
    x0, y0, x1, y1 = map(int, roi)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    band = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 85, 255]))
    scale = w / 1600
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    skin_rows = gray[gray.shape[0] // 3:]
    column_skin = np.percentile(skin_rows, 90, axis=0).astype(np.float32)
    edge_window = max(5, round(17 * scale)) | 1
    local_skin = cv2.dilate(column_skin[None, :], np.ones((1, edge_window), np.uint8))[0]
    low_skin = column_skin < local_skin * .82
    visible_border = np.zeros_like(low_skin)
    ld = np.diff(np.r_[False, low_skin, False].astype(np.int8))
    for a, b in zip(np.flatnonzero(ld == 1), np.flatnonzero(ld == -1)):
        if b - a <= max(5, round(11 * scale)):
            visible_border[a:b] = True
    on = (np.count_nonzero(mask, axis=0) >= max(12 * scale, band.shape[0] * .07))
    on &= ~visible_border
    changes = np.diff(np.r_[False, on, False].astype(np.int8))
    runs = zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1))
    boxes, unresolved = [], []
    for a, b in runs:
        if b - a < 48 * scale:
            continue
        bx, ex = int(a), int(b)
        bw = ex - bx
        rows = np.flatnonzero(np.count_nonzero(mask[:, bx:ex], axis=1) >= bw * .4)
        if not len(rows):
            continue
        by, ey = int(rows[0]), int(rows[-1]) + 1
        bh = ey - by
        candidate = band[by:ey, bx:ex]
        face = face_region(candidate)
        offset = (face.ctypes.data - candidate.ctypes.data) // candidate.strides[0]
        by += int(offset)
        ey = by + face.shape[0]
        bh = ey - by
        if bw > 125 * scale:
            unresolved.append([x0 + bx, y0 + by, x0 + ex, y0 + ey])
            continue
        if not (48 * scale <= bw <= 125 * scale and 80 * scale <= bh <= 180 * scale):
            continue
        if not (.42 <= bw / bh <= 1.05):
            continue
        boxes.append((x0 + bx, y0 + by, x0 + ex, y0 + ey))
    boxes.sort(key=lambda b: b[0])
    return boxes, unresolved, scale


def report(tag, path):
    img = _read(path)
    if img is None:
        print(f'{tag}: CANNOT READ {path}')
        return
    boxes, unresolved, scale = raw_boxes(img)
    print(f'=== {tag}  {Path(path).name}  size={img.shape[1]}x{img.shape[0]} scale={scale:.4f}')
    print(f'    visible={len(boxes)} unresolved={len(unresolved)}')
    base = float(np.median([b[3] for b in boxes])) if boxes else 0.
    tw = float(np.median([b[2] - b[0] for b in boxes])) if boxes else 0.
    th = float(np.median([b[3] - b[1] for b in boxes])) if boxes else 0.
    print(f'    median bottom={base:.1f} median w={tw:.1f} h={th:.1f}')
    prev = None
    for i, b in enumerate(boxes):
        x0, y0, x1, y1 = b
        gap = '-' if prev is None else f'{x0 - prev:+.1f}'
        print(f'    [{i:02d}] x={x0:4d}..{x1:4d} (w={x1-x0:3d}) y={y0:4d}..{y1:4d} '
              f'(h={y1-y0:3d}) bottom={y1:4d} d_bottom={y1-base:+.1f} '
              f'w_ratio={(x1-x0)/max(1.,tw):.3f} gap={gap}')
        prev = x1


FRAMES = [
    ('RECON  复现帧', r'D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\20261023_201640\live\tea_2p\20261023_205345_749900_23750c.png'),
    ('GOLD   旧金标', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r08\frame_02.png'),
    ('MELD   r10副露', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-16\740195\r10\frame_01.png'),
]

for tag, p in FRAMES:
    report(tag, p)
    print()
