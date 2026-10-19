#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch settle-score OCR over harvest / frames_raw (or an explicit list).

Writes JSON + Markdown under pc_bot/logs/settle_ocr_batch/.
Does not invent accuracy when no human gold scores exist — None / incomplete /
suspicious seats go into an error checklist for manual review.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]  # pc_bot/
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

from settle_ocr import (  # noqa: E402
    SEAT_SCORE_ROIS,
    _binarize,
    _classify_glyph,
    _load_templates,
    _segment_glyphs,
    extract_score_in_roi,
    extract_settle_score,
)

DEFAULT_SCAN_ROOTS = [
    Path("/workspace/majiang-data/replay_harvest"),
    Path("/workspace/majiang-data/hana_runtime/frames_raw"),
]
KNOWN_SETTLE = [
    Path("/workspace/majiang-data/hana_runtime/frames_raw/f41.png"),
    Path("/workspace/majiang-data/hana_runtime/frames_raw/f42.png"),
    Path("/workspace/majiang-data/hana_runtime/frames_raw/f43.png"),
    Path("/workspace/majiang-data/hana_runtime/frames_raw/f69.png"),
]
SEAT_ORDER = ("bottom", "left", "right", "top")
# Optional human gold (seat -> score). Only known samples; do not invent accuracy.
GOLD_SCORES: dict[str, dict[str, int | None]] = {
    # f41系：目视 +3587 / −2153 / −1435 / +0（right 读不出时常为 None 待核）
    "f41.png": {"bottom": 3587, "left": -2153, "right": -1435, "top": 0},
    "f42.png": {"bottom": 3587, "left": -2153, "right": -1435, "top": 0},
    "f43.png": {"bottom": 3587, "left": -2153, "right": -1435, "top": 0},
    # f69：目视不完全，仅记录机器可读项；缺金标座位不记准确率
}


def _roi_scaled(roi_ref, w: int, h: int):
    x1, y1, x2, y2 = roi_ref
    sx, sy = w / 1600.0, h / 900.0
    return int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)


def seat_roi_diagnostics(img) -> dict[str, Any]:
    """Per-seat: score / fail_reason / glyph_count / empty_roi."""
    h, w = img.shape[:2]
    templates = _load_templates()
    missing_tpl = [n for n in ["minus", "plus"] + [str(d) for d in range(10)] if n not in templates]
    out: dict[str, Any] = {}
    for seat in SEAT_ORDER:
        roi_ref = SEAT_SCORE_ROIS[seat]
        x1, y1, x2, y2 = _roi_scaled(roi_ref, w, h)
        crop = img[y1:y2, x1:x2]
        info: dict[str, Any] = {
            "roi": [x1, y1, x2, y2],
            "score": None,
            "fail_reason": None,
            "glyph_count": 0,
        }
        if crop.size == 0:
            info["fail_reason"] = "roi_empty"
            out[seat] = info
            continue
        bw = _binarize(crop)
        boxes = _segment_glyphs(bw)
        info["glyph_count"] = len(boxes)
        if not boxes:
            info["fail_reason"] = "no_glyphs"
            out[seat] = info
            continue
        if len(templates) < 5:
            info["fail_reason"] = "missing_templates"
            out[seat] = info
            continue
        score = extract_score_in_roi(img, roi_ref)
        info["score"] = score
        if score is None:
            info["fail_reason"] = "ocr_none"
        out[seat] = info
    return out


def _seat_has_sign_glyph(crop) -> bool:
    """True if ROI binarization yields a plus/minus-like leading glyph."""
    if crop is None or crop.size == 0:
        return False
    templates = _load_templates()
    if "minus" not in templates and "plus" not in templates:
        return False
    bw = _binarize(crop)
    boxes = _segment_glyphs(bw)
    if not boxes:
        return False
    # check first 1-2 glyphs for sign
    for box in boxes[:2]:
        x, y, ww, hh = box
        pad = 3
        g = bw[max(0, y - pad):y + hh + pad, max(0, x - pad):x + ww + pad]
        name, score = _classify_glyph(g, templates)
        if name in ("minus", "plus") and score >= 0.45:
            return True
        # geometric dash: short wide bar
        if hh >= bw.shape[0] * 0.06 and ww >= hh * 2.0 and ww >= 12 and hh <= bw.shape[0] * 0.35:
            return True
    return False


def looks_like_settle(img) -> tuple[bool, str]:
    """Strict settle heuristic.

    Accept when:
      - ≥2 seats OCR to a score, or
      - ≥1 OCR hit AND ≥1 seat shows ± glyph AND ≥2 chroma-strong score ROIs.

    Pure chroma (≥3 seats) without ± / OCR is rejected (blocks f172-class
    false positives from decorative gold/cyan UI).
    """
    h, w = img.shape[:2]
    chroma_hits = 0
    ocr_hits = 0
    sign_hits = 0
    for seat, roi_ref in SEAT_SCORE_ROIS.items():
        x1, y1, x2, y2 = _roi_scaled(roi_ref, w, h)
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        bright = float((gray > 170).mean())
        boxes = _segment_glyphs(_binarize(crop))
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv, (18, 120, 160), (36, 255, 255))
        cyan = cv2.inRange(hsv, (88, 60, 140), (102, 255, 255))
        chroma = float(((gold > 0) | (cyan > 0)).mean())
        if len(boxes) >= 2 and chroma > 0.05 and bright > 0.025:
            chroma_hits += 1
        if _seat_has_sign_glyph(crop):
            sign_hits += 1
        if extract_score_in_roi(img, roi_ref) is not None:
            ocr_hits += 1
    if ocr_hits >= 2:
        return True, f"ocr_hit={ocr_hits}"
    if ocr_hits >= 1 and sign_hits >= 1 and chroma_hits >= 2:
        return True, f"ocr_hit={ocr_hits},sign={sign_hits},chroma={chroma_hits}"
    if ocr_hits >= 1 and sign_hits >= 2:
        return True, f"ocr_hit={ocr_hits},sign={sign_hits}"
    return False, f"not_settle_like(ocr={ocr_hits},sign={sign_hits},chroma={chroma_hits})"


def discover_frames(
    roots: list[Path],
    explicit: list[Path] | None,
    heuristic_only: bool,
) -> list[Path]:
    found: list[Path] = []
    if explicit:
        for p in explicit:
            if p.is_file():
                found.append(p.resolve())
            elif p.is_dir():
                found.extend(sorted(p.rglob("*.png")))
        return sorted(set(found))

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            found.append(root.resolve())
            continue
        found.extend(sorted(root.rglob("*.png")))

    # always keep known settle samples even if heuristic would drop them
    known = {p.resolve() for p in KNOWN_SETTLE if p.is_file()}
    if not heuristic_only:
        return sorted(set(found) | known)

    kept: list[Path] = []
    for p in found:
        img = cv2.imread(str(p))
        if img is None:
            continue
        ok, _ = looks_like_settle(img)
        if ok or p.resolve() in known:
            kept.append(p.resolve())
    return sorted(set(kept) | known)


def suspicious_score(v: int | None) -> bool:
    if v is None:
        return False
    # settle scores in this skin are typically |v| < 100000 and multiples of small units
    if abs(v) > 200000:
        return True
    return False


def classify_frame(path: Path, seats: dict[str, Any], settle_like: bool, reason: str) -> dict[str, Any]:
    scores = {s: seats[s]["score"] for s in SEAT_ORDER}
    n_ok = sum(1 for v in scores.values() if v is not None)
    n_none = 4 - n_ok
    gold = GOLD_SCORES.get(path.name)
    issues: list[str] = []
    if not settle_like:
        issues.append(f"skip_or_weak:{reason}")
    if n_ok == 0:
        issues.append("all_seats_none")
    elif n_ok < 4:
        issues.append(f"incomplete_seats:{n_ok}/4")
    for s, v in scores.items():
        if v is None and seats[s]["fail_reason"]:
            issues.append(f"{s}:{seats[s]['fail_reason']}")
        if suspicious_score(v):
            issues.append(f"{s}:suspicious={v}")
    gold_mismatch: dict[str, Any] = {}
    pending_review: list[str] = []
    if gold:
        for s, g in gold.items():
            pred = scores.get(s)
            if pred is None:
                pending_review.append(f"{s}:pred=None gold={g}")
            elif pred != g:
                gold_mismatch[s] = {"pred": pred, "gold": g}
                pending_review.append(f"{s}:pred={pred} gold={g}")
    return {
        "path": str(path),
        "settle_like": settle_like,
        "settle_reason": reason,
        "scores": scores,
        "seats": seats,
        "n_ok": n_ok,
        "n_none": n_none,
        "issues": issues,
        "gold": gold,
        "gold_mismatch": gold_mismatch,
        "pending_review": pending_review,
        "bottom_extract_settle_score": None,  # filled by caller
    }


def run_batch(frames: list[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in frames:
        img = cv2.imread(str(path))
        if img is None:
            rows.append(
                {
                    "path": str(path),
                    "settle_like": False,
                    "settle_reason": "unreadable",
                    "scores": {s: None for s in SEAT_ORDER},
                    "seats": {},
                    "n_ok": 0,
                    "n_none": 4,
                    "issues": ["unreadable"],
                    "gold": None,
                    "gold_mismatch": {},
                    "pending_review": [],
                    "bottom_extract_settle_score": None,
                }
            )
            continue
        settle_like, reason = looks_like_settle(img)
        seats = seat_roi_diagnostics(img)
        row = classify_frame(path, seats, settle_like, reason)
        row["bottom_extract_settle_score"] = extract_settle_score(img)
        rows.append(row)

    settle_rows = [
        r
        for r in rows
        if r["settle_like"] or r.get("gold") or Path(r["path"]).name in GOLD_SCORES
        or r["n_ok"] > 0
    ]
    # Error checklist only for frames we treat as settle candidates (not weak rejects).
    error_list = [
        r
        for r in settle_rows
        if r["n_none"] > 0 or r["gold_mismatch"] or r["pending_review"] or any(
            "suspicious" in i for i in r["issues"]
        )
    ]
    # also list weak heuristic misses that OCR somehow hit
    ocr_any = [r for r in rows if r["n_ok"] > 0]

    templates = sorted(_load_templates().keys())
    missing_tpl = [n for n in ["minus", "plus"] + [str(d) for d in range(10)] if n not in _load_templates()]

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "n_scanned": len(rows),
        "n_settle_like": sum(1 for r in rows if r["settle_like"]),
        "n_any_ocr_hit": len(ocr_any),
        "n_error_checklist": len(error_list),
        "n_all_seats_ok": sum(1 for r in settle_rows if r["n_ok"] == 4),
        "n_incomplete": sum(1 for r in settle_rows if 0 < r["n_ok"] < 4),
        "n_all_none_among_settle_like": sum(1 for r in settle_rows if r["n_ok"] == 0),
        "templates_present": templates,
        "templates_missing": missing_tpl,
        "note": "无全量人工金标时不报准确率；仅 GOLD_SCORES 有对照的帧记 pending_review/mismatch",
    }
    return {"summary": summary, "frames": rows, "error_checklist": error_list}


def render_md(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# settle OCR 批处理报告（{s['generated_at']}）",
        "",
        "## 摘要",
        "",
        f"- 扫描帧数：**{s['n_scanned']}**",
        f"- 启发式像结算：**{s['n_settle_like']}**",
        f"- 任一座位 OCR 命中：**{s['n_any_ocr_hit']}**",
        f"- 错误清单条数：**{s['n_error_checklist']}**（None/座位不全/可疑/与金标不符）",
        f"- 四座全读出（在 settle_like∪金标样例中）：**{s['n_all_seats_ok']}**",
        f"- 座位不全：**{s['n_incomplete']}**；settle_like 但全 None：**{s['n_all_none_among_settle_like']}**",
        f"- 模板已有：`{', '.join(s['templates_present'])}`",
        f"- 模板仍缺：`{', '.join(s['templates_missing']) or '（无）'}`",
        "",
        f"> {s['note']}",
        "",
        "## 错误清单（待核）",
        "",
        "| 帧 | n_ok | scores | issues / pending |",
        "|----|------|--------|------------------|",
    ]
    for r in report["error_checklist"]:
        name = Path(r["path"]).name
        scores = " ".join(f"{k}={v}" for k, v in r["scores"].items())
        pend = "; ".join(r["pending_review"] + r["issues"][:6])
        lines.append(f"| `{name}` | {r['n_ok']}/4 | `{scores}` | {pend} |")
    if not report["error_checklist"]:
        lines.append("| （空） | | | |")

    lines += [
        "",
        "## 已知结算样例明细",
        "",
        "| 帧 | bottom | left | right | top | extract_settle_score |",
        "|----|--------|------|-------|-----|----------------------|",
    ]
    known_names = {p.name for p in KNOWN_SETTLE}
    for r in report["frames"]:
        if Path(r["path"]).name not in known_names:
            continue
        sc = r["scores"]
        lines.append(
            f"| `{Path(r['path']).name}` | {sc['bottom']} | {sc['left']} | {sc['right']} | {sc['top']} | {r['bottom_extract_settle_score']} |"
        )

    lines += [
        "",
        "## 启发式命中且有 OCR 的帧（节选，最多 40）",
        "",
        "| 帧 | n_ok | scores |",
        "|----|------|--------|",
    ]
    shown = 0
    for r in report["frames"]:
        if not r["settle_like"] or r["n_ok"] == 0:
            continue
        sc = r["scores"]
        scores = " ".join(f"{k}={v}" for k, v in sc.items())
        lines.append(f"| `{Path(r['path']).name}` | {r['n_ok']}/4 | `{scores}` |")
        shown += 1
        if shown >= 40:
            break
    lines.append("")
    lines.append("脚本：`pc_bot/tools/batch_settle_ocr.py`")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="optional PNG files or dirs; default = harvest + frames_raw",
    )
    ap.add_argument(
        "--roots",
        nargs="*",
        type=Path,
        default=DEFAULT_SCAN_ROOTS,
        help="scan roots when no inputs given",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="process every PNG under roots (no settle heuristic filter)",
    )
    ap.add_argument(
        "--heuristic-only",
        action="store_true",
        help="only keep settle-like + known samples (default: scan all PNGs under roots)",
    )
    ap.add_argument("--no-heuristic-filter", action="store_true", help="alias of --all (default behavior)")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "logs" / "settle_ocr_batch",
    )
    ap.add_argument("--tag", default=None, help="filename tag YYYYMMDD (default today local)")
    args = ap.parse_args(argv)

    # Default: scan all PNGs; --heuristic-only filters to settle-like + known.
    heuristic_only = bool(args.heuristic_only) and not (args.all or args.no_heuristic_filter)
    frames = discover_frames(
        list(args.roots),
        list(args.inputs) if args.inputs else None,
        heuristic_only=heuristic_only,
    )
    report = run_batch(frames)
    tag = args.tag or datetime.now().strftime("%Y%m%d")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"settle_batch_{tag}.json"
    md_path = args.out_dir / f"settle_batch_{tag}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    s = report["summary"]
    print(f"scanned={s['n_scanned']} settle_like={s['n_settle_like']} "
          f"ocr_hit={s['n_any_ocr_hit']} errors={s['n_error_checklist']}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
