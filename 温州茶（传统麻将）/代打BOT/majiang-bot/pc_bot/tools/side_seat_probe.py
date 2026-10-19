#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侧家手牌 ROI 探针：裁 right/top/left 候选区并启发式数盒。

不过线、不分类。详见 pc_bot/docs/side_seat_roi.md。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

REF_W, REF_H = 1600, 900
# (x0, y0, x1, y1) in 1600x900 — tiles inward of seat avatars
SIDE_ROIS = {
    "right": (1340, 240, 1440, 710),
    "top": (450, 45, 1150, 140),
    "left": (170, 240, 280, 710),
}
OUT_DIR = Path(__file__).resolve().parents[1] / "logs" / "side_probe"


def scale_roi(roi, w, h):
    x0, y0, x1, y1 = roi
    sx, sy = w / REF_W, h / REF_H
    return (
        max(0, int(x0 * sx)),
        max(0, int(y0 * sy)),
        min(w, int(x1 * sx)),
        min(h, int(y1 * sy)),
    )


def _split_by_troughs(energy: np.ndarray, expected_pitch: float, min_gap: int = 6):
    """Split a 1-D activity profile into peaks separated by local minima."""
    e = energy.astype(np.float32)
    if e.size < 8:
        return []
    # smooth
    k = max(3, int(expected_pitch // 4) | 1)
    ker = np.ones(k, np.float32) / k
    sm = np.convolve(e, ker, mode="same")
    thr = max(float(np.percentile(sm, 40)), float(sm.max()) * 0.15)
    active = sm > thr
    if not active.any():
        return []
    # find contiguous active regions, then subdivide by troughs
    changes = np.diff(np.r_[False, active, False].astype(np.int8))
    regions = list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))
    boxes_1d = []
    for a, b in regions:
        seg = sm[a:b]
        length = b - a
        if length < min_gap:
            continue
        n_est = max(1, int(round(length / max(expected_pitch, 1))))
        if n_est == 1 or length < expected_pitch * 1.4:
            boxes_1d.append((int(a), int(b)))
            continue
        # place troughs near expected boundaries
        troughs = [a]
        for i in range(1, n_est):
            center = a + int(i * length / n_est)
            lo = max(a + min_gap, center - int(expected_pitch // 3))
            hi = min(b - min_gap, center + int(expected_pitch // 3))
            if hi <= lo:
                continue
            local = sm[lo:hi]
            troughs.append(lo + int(np.argmin(local)))
        troughs.append(b)
        troughs = sorted(set(troughs))
        for u, v in zip(troughs, troughs[1:]):
            if v - u >= min_gap:
                boxes_1d.append((int(u), int(v)))
    return boxes_1d


def count_boxes_heuristic(crop_bgr, seat: str) -> tuple[int, list]:
    """Rough box count. Returns (n, boxes_xyxy_in_crop). Not production."""
    if crop_bgr is None or crop_bgr.size == 0:
        return 0, []
    h, w = crop_bgr.shape[:2]
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    boxes = []

    if seat in ("left", "right"):
        # prefer table-center half where faces sit
        if seat == "left":
            x0b, x1b = w // 4, w
        else:
            x0b, x1b = 0, (3 * w) // 4
        band = gray[:, x0b:x1b]
        # energy = deviation from dark green (tiles are brighter / more textured)
        row_energy = band.std(axis=1) + 0.25 * band.mean(axis=1)
        pitch = h / 13.0  # ~13 concealed tiles
        spans = _split_by_troughs(row_energy, expected_pitch=pitch, min_gap=max(8, int(pitch * 0.35)))
        for a, b in spans:
            strip = band[a:b]
            col_e = strip.std(axis=0) + 0.2 * strip.mean(axis=0)
            cols = np.flatnonzero(col_e > max(3.0, float(np.percentile(col_e, 20))))
            if not len(cols):
                continue
            boxes.append((x0b + int(cols[0]), a, x0b + int(cols[-1]) + 1, b))
    else:
        band = gray
        col_energy = band.std(axis=0) + 0.25 * band.mean(axis=0)
        pitch = w / 13.0
        spans = _split_by_troughs(col_energy, expected_pitch=pitch, min_gap=max(10, int(pitch * 0.35)))
        for a, b in spans:
            strip = band[:, a:b]
            row_e = strip.std(axis=1) + 0.2 * strip.mean(axis=1)
            rows = np.flatnonzero(row_e > max(3.0, float(np.percentile(row_e, 20))))
            if not len(rows):
                continue
            boxes.append((a, int(rows[0]), b, int(rows[-1]) + 1))
    return len(boxes), boxes


def probe_frame(path: Path, out_dir: Path = OUT_DIR) -> dict:
    img = cv2.imread(str(path))
    if img is None:
        raise FileNotFoundError(path)
    h, w = img.shape[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    parent_tag = path.parent.name
    grand = path.parent.parent.name if path.parent.parent else ""
    tag = f"{grand}_{parent_tag}_{path.stem}" if grand else f"{parent_tag}_{path.stem}"
    result = {"frame": str(path), "size": [w, h], "seats": {}}
    for seat, ref in SIDE_ROIS.items():
        x0, y0, x1, y1 = scale_roi(ref, w, h)
        crop = img[y0:y1, x0:x1].copy()
        n, boxes = count_boxes_heuristic(crop, seat)
        for bx0, by0, bx1, by1 in boxes:
            cv2.rectangle(crop, (bx0, by0), (bx1, by1), (0, 255, 0), 1)
        out_path = out_dir / f"{tag}_{seat}.png"
        cv2.imwrite(str(out_path), crop)
        result["seats"][seat] = {
            "roi": [x0, y0, x1, y1],
            "box_count": n,
            "crop": str(out_path),
        }
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("frames", nargs="+", type=Path, help="gameplay PNG paths")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    for fr in args.frames:
        r = probe_frame(fr, args.out)
        print(f"== {r['frame']} ({r['size'][0]}x{r['size'][1]}) ==")
        for seat, info in r["seats"].items():
            print(
                f"  {seat}: boxes={info['box_count']} roi={info['roi']} -> {info['crop']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
