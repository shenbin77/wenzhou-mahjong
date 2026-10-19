#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quantify side-seat probe metrics when tile-level gold is unavailable.

Definitions (no fake tile accuracy):
  detection_rate = sum(detected_boxes) / sum(expected_count)
      expected_count = gold unknown_count (or len(tiles) if labeled) else 13
  classify_accept_rate = accepted_crops / classified_crops
      each detected box crop is passed to TileVision.classify_crop;
      "unable to gold-label" is separate — we never claim label accuracy.

Covers existing side_probe sample frames + gold_hands side seats.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import importlib.util as _ilu  # noqa: E402

def _load_probe():
    spec = _ilu.spec_from_file_location(
        "side_seat_probe",
        Path(__file__).resolve().parent / "side_seat_probe.py",
    )
    mod = _ilu.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

_probe = _load_probe()
SIDE_ROIS = _probe.SIDE_ROIS
count_boxes_heuristic = _probe.count_boxes_heuristic
scale_roi = _probe.scale_roi

from tile_vision import TileVision  # noqa: E402

DATA_HARVEST = Path("/workspace/majiang-data/replay_harvest")
GOLD_LOG = ROOT / "logs" / "hand_recog_gold_20261015.json"
DEFAULT_SIDE_PROBE_FRAMES = [
    DATA_HARVEST / "2026-10-14" / "r01" / "frame_02.png",
    DATA_HARVEST / "2026-10-15" / "r01" / "frame_03.png",
]
SIDE_SEATS = ("right", "top", "left")


def load_gold_expectations() -> dict[str, dict[str, int]]:
    """Map relative frame path -> {seat: expected_count} from gold_hands / gold log."""
    exp: dict[str, dict[str, int]] = {}

    def ingest_frame(rel: str, seats: list[dict]):
        m: dict[str, int] = {}
        for s in seats:
            seat = s.get("seat")
            if seat not in SIDE_SEATS:
                continue
            tiles = s.get("tiles") or []
            unk = s.get("unknown_count")
            if tiles:
                m[seat] = len(tiles)
            elif unk is not None:
                m[seat] = int(unk)
            else:
                m[seat] = 13
        if m:
            exp[rel.replace("\\", "/")] = m

    if GOLD_LOG.is_file():
        g = json.loads(GOLD_LOG.read_text(encoding="utf-8"))
        for fr in g.get("frames") or []:
            ingest_frame(fr["frame"], fr.get("gold_seats") or [])

    for p in DATA_HARVEST.rglob("gold_hands.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        for fr in d.get("frames") or []:
            name = fr.get("name") or fr.get("frame")
            if not name:
                continue
            ingest_frame(name, fr.get("seats") or [])

    return exp


def resolve_frame(rel_or_path: str | Path) -> Path | None:
    p = Path(rel_or_path)
    if p.is_file():
        return p.resolve()
    cand = DATA_HARVEST / rel_or_path
    if cand.is_file():
        return cand.resolve()
    # try under each date folder
    for hit in DATA_HARVEST.rglob(p.name):
        if hit.is_file():
            return hit.resolve()
    return None


def relative_key(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(DATA_HARVEST)).replace("\\", "/")
    except ValueError:
        return path.name


def eval_frame(
    path: Path,
    expected: dict[str, int] | None,
    vision: TileVision,
    default_expected: int = 13,
) -> dict[str, Any]:
    img = cv2.imread(str(path))
    if img is None:
        return {"frame": str(path), "error": "unreadable"}
    h, w = img.shape[:2]
    exp = expected or {s: default_expected for s in SIDE_SEATS}
    seats_out: dict[str, Any] = {}
    for seat in SIDE_SEATS:
        x0, y0, x1, y1 = scale_roi(SIDE_ROIS[seat], w, h)
        crop = img[y0:y1, x0:x1]
        n, boxes = count_boxes_heuristic(crop, seat)
        expect_n = int(exp.get(seat, default_expected))
        classified = 0
        accepted = 0
        reject_reasons: dict[str, int] = {}
        crop_details = []
        for bx0, by0, bx1, by1 in boxes:
            tile_crop = crop[by0:by1, bx0:bx1]
            if tile_crop.size == 0:
                continue
            classified += 1
            d = vision.classify_crop(tile_crop)
            if d.get("accepted"):
                accepted += 1
            reason = d.get("reason") or ("accepted" if d.get("accepted") else "rejected")
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            crop_details.append(
                {
                    "box": [bx0, by0, bx1, by1],
                    "accepted": bool(d.get("accepted")),
                    "label": d.get("label"),
                    "score": round(float(d.get("score") or 0), 4),
                    "reason": reason,
                }
            )
        seats_out[seat] = {
            "expected": expect_n,
            "detected": n,
            "detection_hit": min(n, expect_n),  # capped for rate numerator option
            "has_tile_gold": False,  # side gold tiles absent in current dataset
            "classified_crops": classified,
            "accepted_crops": accepted,
            "reject_reasons": reject_reasons,
            "roi": [x0, y0, x1, y1],
            "crops_sample": crop_details[:5],
        }
    return {
        "frame": str(path),
        "rel": relative_key(path),
        "size": [w, h],
        "seats": seats_out,
        "note": "侧家无 tiles 金标；detection 用 unknown_count/13；classify 只计 accept，不计标签正确率",
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    det_num = det_den = 0
    cls_acc_num = cls_acc_den = 0
    by_seat = {s: {"det_num": 0, "det_den": 0, "cls_num": 0, "cls_den": 0} for s in SIDE_SEATS}
    for r in rows:
        if "seats" not in r:
            continue
        for seat, info in r["seats"].items():
            det_num += info["detected"]
            det_den += info["expected"]
            cls_acc_num += info["accepted_crops"]
            cls_acc_den += info["classified_crops"]
            by_seat[seat]["det_num"] += info["detected"]
            by_seat[seat]["det_den"] += info["expected"]
            by_seat[seat]["cls_num"] += info["accepted_crops"]
            by_seat[seat]["cls_den"] += info["classified_crops"]

    def rate(n, d):
        return None if d == 0 else round(n / d, 4)

    seat_rates = {}
    for s, v in by_seat.items():
        seat_rates[s] = {
            "detection_rate": rate(v["det_num"], v["det_den"]),
            "detection_fraction": f"{v['det_num']}/{v['det_den']}",
            "classify_accept_rate": rate(v["cls_num"], v["cls_den"]),
            "classify_accept_fraction": f"{v['cls_num']}/{v['cls_den']}",
        }
    return {
        "detection_rate": rate(det_num, det_den),
        "detection_fraction": f"{det_num}/{det_den}",
        "detection_definition": "sum(detected_boxes) / sum(expected); expected=unknown_count or 13; 无侧家 tiles 金标",
        "classify_accept_rate": rate(cls_acc_num, cls_acc_den),
        "classify_accept_fraction": f"{cls_acc_num}/{cls_acc_den}",
        "classify_definition": "TileVision.classify_crop accepted / crops from detected boxes; 非标签准确率",
        "by_seat": seat_rates,
        "n_frames": len([r for r in rows if "seats" in r]),
    }


def render_md(summary: dict[str, Any], rows: list[dict[str, Any]], out_path: Path) -> str:
    lines = [
        f"# 侧家探针可量化指标（{datetime.now().astimezone().isoformat(timespec='seconds')}）",
        "",
        "## 定义（无侧家 tiles 金标）",
        "",
        f"- **detection_rate** = `{summary['detection_definition']}`",
        f"- **classify_accept_rate** = `{summary['classify_definition']}`",
        "- 不做「识牌正确率」——侧家 `tiles: []`，无法金标时禁止假报。",
        "",
        "## 总览",
        "",
        f"- 帧数：**{summary['n_frames']}**",
        f"- detection_rate：**{summary['detection_rate']}**（{summary['detection_fraction']}）",
        f"- classify_accept_rate：**{summary['classify_accept_rate']}**（{summary['classify_accept_fraction']}）",
        "",
        "### 分座",
        "",
        "| seat | detection | classify_accept |",
        "|------|-----------|-----------------|",
    ]
    for s in SIDE_SEATS:
        b = summary["by_seat"][s]
        lines.append(
            f"| {s} | {b['detection_rate']} ({b['detection_fraction']}) | "
            f"{b['classify_accept_rate']} ({b['classify_accept_fraction']}) |"
        )
    lines += ["", "## 逐帧", ""]
    for r in rows:
        if "error" in r:
            lines.append(f"- `{r['frame']}` ERROR {r['error']}")
            continue
        lines.append(f"### `{r.get('rel') or r['frame']}`")
        lines.append("")
        lines.append("| seat | expected | detected | classified | accepted |")
        lines.append("|------|----------|----------|------------|----------|")
        for s in SIDE_SEATS:
            info = r["seats"][s]
            lines.append(
                f"| {s} | {info['expected']} | {info['detected']} | "
                f"{info['classified_crops']} | {info['accepted_crops']} |"
            )
        lines.append("")
    lines += [
        f"报告路径：`{out_path}`",
        "",
        "脚本：`pc_bot/tools/side_seat_acc.py`（复用 `side_seat_probe.SIDE_ROIS` / `count_boxes_heuristic`）",
        "",
    ]
    return "\n".join(lines)


def collect_frames(extra: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for p in DEFAULT_SIDE_PROBE_FRAMES:
        if p.is_file():
            paths.append(p.resolve())
    exp = load_gold_expectations()
    for rel in exp:
        rp = resolve_frame(rel)
        if rp:
            paths.append(rp)
    for p in extra:
        if p.is_file():
            paths.append(p.resolve())
        elif p.is_dir():
            paths.extend(sorted(p.glob("*.png")))
    # unique preserve order
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out



# --- Gold-label classify path (right/left/top, offline) ---
# Wider face ROI toward table center; SIDE_ROIS remains for detection probe.
FACE_ROIS = {
    "right": (1280, 250, 1380, 695),  # 1600x900; shifted toward table vs SIDE_ROIS
    "left": (220, 250, 320, 695),  # mirror of right face ROI
    "top": (460, 48, 1140, 128),  # horizontal strip; upper face, less orange base
}
# Per-seat preprocess (rollback: mode=none). Top is already face-ish → scale only.
SEAT_PREPROCESS = {
    "right": "rot90_scale",
    "left": "rot270_scale",  # mirror of right (faces opposite table edge)
    "top": "scale",
}
# index -> alias; only tiles a human could read (TARGET_IDS aliases). Unclear → omit.
SIDE_GOLD_TILES: dict[str, dict[str, dict[int, str]]] = {
    "right": {
        "2026-10-14/r01/frame_02.png": {4: "s5", 8: "s2", 9: "s1", 10: "w6"},
        "2026-10-15/r01/frame_03.png": {4: "s6"},
        "2026-10-14/r01/frame_03.png": {4: "s5"},
    },
    "left": {
        # equal-pitch 13 on left face ROI; only clear glyphs (Read + color/classify cross-check)
        "2026-10-14/r01/frame_02.png": {4: "s8", 5: "s9"},
        "2026-10-14/r01/frame_03.png": {4: "s8", 5: "s9"},
        "2026-10-15/r01/frame_02.png": {2: "s2", 4: "t1"},
        "2026-10-14/r02/frame_02.png": {2: "s4"},
    },
    "top": {
        # horizontal equal-pitch; skip fa/non-TARGET and partial edge tiles
        "2026-10-14/r01/frame_02.png": {
            0: "t8",
            1: "t4",
            2: "t4",
            3: "t2",
            4: "t1",
            6: "w7",
            7: "w8",
            8: "w9",
        },
        "2026-10-15/r01/frame_02.png": {
            0: "t3",
            2: "t1",
            3: "t1",
            4: "s6",
            5: "s2",
            6: "w8",
        },
    },
}

# backward-compat aliases used by older call sites / docs
RIGHT_FACE_ROI = FACE_ROIS["right"]
RIGHT_GOLD_TILES = SIDE_GOLD_TILES["right"]


def side_preprocess(crop, mode: str = "rot90_scale"):
    """Side-seat preprocess. mode='none' is the rollback (identity)."""
    if mode == "none" or crop is None or getattr(crop, "size", 0) == 0:
        return crop
    img = crop.copy()
    if "rot180" in mode:
        img = cv2.rotate(img, cv2.ROTATE_180)
    elif "rot270" in mode:
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    elif "rot90" in mode:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if "contrast" in mode:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(2.0, (4, 4)).apply(l)
        img = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    if "scale" in mode:
        img = cv2.resize(
            img, (img.shape[1] * 2, img.shape[0] * 2), interpolation=cv2.INTER_CUBIC
        )
    return img


def slice_equal_pitch(img, face_roi, n: int = 13, axis: str = "vertical"):
    """Slice a face strip into n equal-pitch tile crops.

    axis='vertical' for left/right (pitch along y); 'horizontal' for top (pitch along x).
    """
    x0, y0, x1, y1 = face_roi
    strip = img[y0:y1, x0:x1]
    h, w = strip.shape[:2]
    tiles = []
    if axis == "horizontal":
        pitch = w / float(n)
        face_h = max(8, int(h * 0.75))  # drop orange base
        for i in range(n):
            a, b = int(i * pitch), int((i + 1) * pitch)
            pad = max(1, int(pitch * 0.05))
            tile = strip[1:face_h, a + pad : b - pad]
            if tile.size and min(tile.shape[:2]) >= 8:
                tiles.append(
                    (
                        i,
                        tile,
                        [x0 + a + pad, y0 + 1, x0 + b - pad, y0 + face_h],
                    )
                )
    else:
        pitch = h / float(n)
        for i in range(n):
            a, b = int(i * pitch), int((i + 1) * pitch)
            pad = max(1, int(pitch * 0.06))
            tile = strip[a + pad : b - pad, 2 : w - 2]
            if tile.size and min(tile.shape[:2]) >= 10:
                tiles.append(
                    (
                        i,
                        tile,
                        [x0 + 2, y0 + a + pad, x0 + w - 2, y0 + b - pad],
                    )
                )
    return tiles


def _eval_seat_gold(
    vision: TileVision,
    seat: str,
    gold_map: dict[str, dict[int, str]],
    mode: str,
) -> dict[str, Any]:
    face_roi = FACE_ROIS[seat]
    axis = "horizontal" if seat == "top" else "vertical"
    frames_out = []
    lab_c = lab_n = 0
    acc_c = acc_n = acc_any = 0
    for rel, gmap in gold_map.items():
        path = DATA_HARVEST / rel
        if not path.is_file():
            continue
        img = cv2.imread(str(path))
        if img is None:
            continue
        by_i = {i: (tile, box) for i, tile, box in slice_equal_pitch(img, face_roi, axis=axis)}
        details = []
        for i, gold in sorted(gmap.items()):
            if i not in by_i:
                continue
            tile, box = by_i[i]
            pp = side_preprocess(tile, mode)
            d = vision.classify_crop(pp)
            pred = d.get("label")
            match = pred == gold
            lab_n += 1
            if match:
                lab_c += 1
            acc_n += 1
            if d.get("accepted"):
                acc_any += 1
                if match:
                    acc_c += 1
            details.append(
                {
                    "index": i,
                    "gold": gold,
                    "pred": pred,
                    "match": match,
                    "accepted": bool(d.get("accepted")),
                    "score": round(float(d.get("score") or 0), 3),
                    "reason": d.get("reason"),
                    "box_abs": box,
                }
            )
        frames_out.append(
            {
                "frame": rel,
                "seat": seat,
                "n_gold": len(details),
                "label_correct": sum(1 for x in details if x["match"]),
                "accept_correct": sum(1 for x in details if x["match"] and x["accepted"]),
                "details": details,
            }
        )

    def rate(n, d):
        return None if d == 0 else round(n / d, 4)

    return {
        "seat": seat,
        "face_roi_1600x900": list(face_roi),
        "preprocess": mode,
        "preprocess_rollback": "mode=none",
        "n_frames": len(frames_out),
        "n_gold_tiles": lab_n,
        "label_accuracy": {"correct": lab_c, "total": lab_n, "rate": rate(lab_c, lab_n)},
        "classify_accept_rate_on_gold": {
            "accepted": acc_any,
            "total": acc_n,
            "rate": rate(acc_any, acc_n),
        },
        "classify_accept_and_correct": {
            "correct": acc_c,
            "total": acc_n,
            "rate": rate(acc_c, acc_n),
        },
        "frames": frames_out,
    }


def run_gold_eval(
    vision: TileVision,
    mode: str | None = None,
    seats: tuple[str, ...] = ("right", "left", "top"),
) -> dict[str, Any]:
    """Evaluate manual side-seat gold tiles. mode overrides all seats if set."""
    by_seat = {}
    for seat in seats:
        gmap = SIDE_GOLD_TILES.get(seat) or {}
        if not gmap:
            continue
        m = mode or SEAT_PREPROCESS.get(seat, "rot90_scale")
        by_seat[seat] = _eval_seat_gold(vision, seat, gmap, m)

    def rate(n, d):
        return None if d == 0 else round(n / d, 4)

    tot_c = sum(s["label_accuracy"]["correct"] for s in by_seat.values())
    tot_n = sum(s["label_accuracy"]["total"] for s in by_seat.values())
    acc_c = sum(s["classify_accept_and_correct"]["correct"] for s in by_seat.values())
    acc_n = sum(s["classify_accept_and_correct"]["total"] for s in by_seat.values())
    acc_any = sum(s["classify_accept_rate_on_gold"]["accepted"] for s in by_seat.values())

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seats_evaluated": list(by_seat.keys()),
        "gold_note": "仅目视可确认的牌（TARGET_IDS 别名）；看不清保持 unknown，不硬标 13 张",
        "by_seat": by_seat,
        "overall": {
            "n_gold_tiles": tot_n,
            "label_accuracy": {"correct": tot_c, "total": tot_n, "rate": rate(tot_c, tot_n)},
            "classify_accept_rate_on_gold": {
                "accepted": acc_any,
                "total": acc_n,
                "rate": rate(acc_any, acc_n),
            },
            "classify_accept_and_correct": {
                "correct": acc_c,
                "total": acc_n,
                "rate": rate(acc_c, acc_n),
            },
        },
        # legacy single-seat fields (right) for older readers
        "seat": "right",
        "face_roi_1600x900": list(FACE_ROIS["right"]),
        "preprocess": SEAT_PREPROCESS["right"],
        "preprocess_rollback": "mode=none",
        "n_frames": by_seat.get("right", {}).get("n_frames", 0),
        "n_gold_tiles": by_seat.get("right", {}).get("n_gold_tiles", 0),
        "label_accuracy": by_seat.get("right", {}).get(
            "label_accuracy", {"correct": 0, "total": 0, "rate": None}
        ),
        "classify_accept_rate_on_gold": by_seat.get("right", {}).get(
            "classify_accept_rate_on_gold",
            {"accepted": 0, "total": 0, "rate": None},
        ),
        "classify_accept_and_correct": by_seat.get("right", {}).get(
            "classify_accept_and_correct",
            {"correct": 0, "total": 0, "rate": None},
        ),
        "frames": by_seat.get("right", {}).get("frames", []),
    }


def render_gold_md(report: dict[str, Any]) -> str:
    lines = [
        f"# 侧家 classify 金标准确率（{report['generated_at']}）",
        "",
        "## 设定",
        "",
        f"- **座位**：{', '.join(f'`{s}`' for s in report.get('seats_evaluated') or ['right'])}",
        f"- **金标原则**：{report['gold_note']}",
        "- **回退**：任意座位 `mode=none`",
        "",
        "### 分座 ROI / 预处理",
        "",
        "| seat | face ROI (1600×900) | preprocess | 金标张数 |",
        "|------|---------------------|------------|----------|",
    ]
    by_seat = report.get("by_seat") or {}
    for seat in ("right", "left", "top"):
        if seat not in by_seat:
            continue
        s = by_seat[seat]
        lines.append(
            f"| {seat} | `{tuple(s['face_roi_1600x900'])}` | `{s['preprocess']}` | {s['n_gold_tiles']} |"
        )

    lines += ["", "## 准确率（分座位）", ""]
    lines += [
        "| seat | label accuracy | classify_accept（金标子集） | accept ∧ 正确 |",
        "|------|----------------|------------------------------|---------------|",
    ]
    for seat in ("right", "left", "top"):
        if seat not in by_seat:
            continue
        s = by_seat[seat]
        la, ar, aa = s["label_accuracy"], s["classify_accept_rate_on_gold"], s["classify_accept_and_correct"]
        lines.append(
            f"| **{seat}** | **{la['correct']}/{la['total']} = {la['rate']}** | "
            f"{ar['accepted']}/{ar['total']} = {ar['rate']} | "
            f"{aa['correct']}/{aa['total']} = {aa['rate']} |"
        )
    ov = report.get("overall") or {}
    if ov:
        la = ov["label_accuracy"]
        lines += [
            "",
            f"- **overall label accuracy**：**{la['correct']}/{la['total']} = {la['rate']}**",
            "",
        ]

    for seat in ("right", "left", "top"):
        if seat not in by_seat:
            continue
        s = by_seat[seat]
        lines += [f"## 分帧（{seat}）", ""]
        lines += [
            "| 帧 | 金标数 | label 正确 | accept∧正确 |",
            "|----|--------|------------|-------------|",
        ]
        for fr in s["frames"]:
            lines.append(
                f"| `{fr['frame']}` | {fr['n_gold']} | {fr['label_correct']}/{fr['n_gold']} | "
                f"{fr['accept_correct']}/{fr['n_gold']} |"
            )
        lines += ["", "### 明细", ""]
        for fr in s["frames"]:
            lines.append(f"#### `{fr['frame']}` ({seat})")
            lines.append("")
            lines.append("| idx | gold | pred | match | accepted | score |")
            lines.append("|-----|------|------|-------|----------|-------|")
            for d in fr["details"]:
                lines.append(
                    f"| {d['index']} | {d['gold']} | {d['pred']} | {d['match']} | "
                    f"{d['accepted']} | {d['score']} |"
                )
            lines.append("")

    lines += [
        "报告：`pc_bot/logs/side_seat_acc_YYYYMMDD.md`",
        "",
        "金标：`pc_bot/logs/gold/side_seat_gold_YYYYMMDD.json`",
        "",
        "脚本：`pc_bot/tools/side_seat_acc.py --gold-eval`",
        "",
    ]
    return "\n".join(lines)


def write_side_gold_bundle(out_path: Path) -> dict[str, Any]:
    """hand-record/v0-compatible side gold (partial tiles + unknown_count)."""
    from tile_vision import TILE_IDS  # local import; pc_bot on path

    def alias_to_id(alias: str) -> int:
        if alias not in TILE_IDS:
            raise KeyError(alias)
        tid = int(TILE_IDS[alias])
        if tid not in TARGET_IDS_SET:
            raise ValueError(f"{alias} tid={tid} not in TARGET_IDS")
        return tid

    # TARGET_IDS from tile_vision
    from tile_vision import TARGET_IDS as TARGET_IDS_SET  # noqa: N814

    # group by frame
    frames: dict[str, dict[str, Any]] = {}
    for seat, fmap in SIDE_GOLD_TILES.items():
        for rel, gmap in fmap.items():
            fr = frames.setdefault(
                rel,
                {
                    "name": rel,
                    "seats": {
                        "bottom": {"seat": "bottom", "tiles": [], "unknown_count": None, "note": "see round gold_hands"},
                        "right": {"seat": "right", "tiles": [], "unknown_count": 13, "gold_by_index": {}},
                        "top": {"seat": "top", "tiles": [], "unknown_count": 13, "gold_by_index": {}},
                        "left": {"seat": "left", "tiles": [], "unknown_count": 13, "gold_by_index": {}},
                    },
                    "notes": "",
                },
            )
            seat_obj = fr["seats"][seat]
            ordered = []
            for idx, alias in sorted(gmap.items()):
                tid = alias_to_id(alias)
                seat_obj["gold_by_index"][str(idx)] = {
                    "alias": alias,
                    "tile_id": tid,
                }
                ordered.append(tid)
            seat_obj["tiles"] = ordered  # partial known tiles (order = gold index ascending)
            seat_obj["unknown_count"] = max(0, 13 - len(ordered))
            seat_obj["note"] = (
                f"partial side gold: {len(ordered)} readable TARGET_IDS tiles; "
                f"indices {sorted(gmap)}; unclear slots not forced"
            )

    bundle = {
        "schema": "hand-record/v0-side-seat-gold",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "compatible_with": "hand-record/v0 seats (tile_id int TARGET_IDS; aliases w/t/s/hongzhong)",
        "face_rois_1600x900": {k: list(v) for k, v in FACE_ROIS.items()},
        "preprocess": dict(SEAT_PREPROCESS),
        "gold_note": "仅目视可确认；tiles=已知牌 tile_id 列表（按 index 升序），unknown_count=13-已知数；gold_by_index 保留切片下标",
        "n_frames": len(frames),
        "counts_by_seat": {
            s: sum(len(g) for g in SIDE_GOLD_TILES[s].values()) for s in SIDE_GOLD_TILES
        },
        "frames": [
            {
                "name": fr["name"],
                "seats": [fr["seats"][s] for s in ("bottom", "right", "top", "left")],
                "notes": fr["notes"],
            }
            for fr in sorted(frames.values(), key=lambda x: x["name"])
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("frames", nargs="*", type=Path, help="extra PNG frames")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="markdown report path (default logs/side_seat_acc_YYYYMMDD.md)",
    )
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument(
        "--gold-eval",
        action="store_true",
        help="evaluate side-seat manual gold tiles with side_preprocess (label accuracy)",
    )
    ap.add_argument(
        "--preprocess",
        default=None,
        help="override preprocess for all seats in --gold-eval "
        "(none|rot90|rot90_scale|rot270_scale|scale|rot180_scale|...); default per-seat",
    )
    ap.add_argument(
        "--seats",
        default="right,left,top",
        help="comma seats for --gold-eval (default right,left,top)",
    )
    ap.add_argument(
        "--write-side-gold",
        type=Path,
        default=None,
        help="write hand-record-compatible side gold JSON (default logs/gold/side_seat_gold_YYYYMMDD.json)",
    )
    args = ap.parse_args(argv)

    tag = datetime.now().strftime("%Y%m%d")
    out_md = args.out or (ROOT / "logs" / f"side_seat_acc_{tag}.md")
    out_json = args.json_out or (ROOT / "logs" / f"side_seat_acc_{tag}.json")

    if args.gold_eval:
        vision = TileVision()
        seats = tuple(s.strip() for s in args.seats.split(",") if s.strip())
        report = run_gold_eval(vision, mode=args.preprocess, seats=seats)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_gold_md(report), encoding="utf-8")
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        gold_path = args.write_side_gold or (ROOT / "logs" / "gold" / f"side_seat_gold_{tag}.json")
        write_side_gold_bundle(gold_path)
        for seat, s in (report.get("by_seat") or {}).items():
            la = s["label_accuracy"]
            print(
                f"gold_eval seat={seat} tiles={la['total']} "
                f"label_acc={la['correct']}/{la['total']} ({la['rate']}) "
                f"preprocess={s['preprocess']}"
            )
        print(f"wrote {out_md}")
        print(f"wrote {out_json}")
        print(f"wrote {gold_path}")
        return 0

    expectations = load_gold_expectations()
    frames = collect_frames(list(args.frames))
    vision = TileVision()
    rows = []
    for fr in frames:
        rel = relative_key(fr)
        exp = expectations.get(rel) or expectations.get(fr.name)
        # also try date/rXX/name form from DEFAULT
        if exp is None:
            for k, v in expectations.items():
                if k.endswith(fr.name) or fr.name in k:
                    exp = v
                    break
        rows.append(eval_frame(fr, exp, vision))

    summary = aggregate(rows)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "frames": rows,
    }
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_md(summary, rows, out_md), encoding="utf-8")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"frames={summary['n_frames']} "
        f"detection={summary['detection_fraction']} ({summary['detection_rate']}) "
        f"classify_accept={summary['classify_accept_fraction']} ({summary['classify_accept_rate']})"
    )
    print(f"wrote {out_md}")
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
