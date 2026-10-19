#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline TileVision + brain_v0 regression on historical frames (no emulator).

Reads existing templates and frames only; does not write template files.
Produces numeric metrics for recognition accuracy (when labels exist) and
Advisor.local_advise stability / optional historical-decision agreement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

PC_BOT = Path(__file__).resolve().parents[1]
REPO = PC_BOT.parent
DATA = Path("/workspace/majiang-data")
sys.path.insert(0, str(PC_BOT))
sys.path.insert(0, str(REPO / "brain_v0"))

from tile_vision import TARGET_IDS, TILE_IDS, TileVision, _read, face_region  # noqa: E402
from tools.rebuild_templates import CLUSTER_LABELS, pixel_hash  # noqa: E402
from engine.advisor import Advisor  # noqa: E402

# Hand-labelled full-scene fixtures (from pc_bot/tests/test_tile_vision.py).
LABELED_SCENES = {
    "live_070.png": {
        "concealed": [
            "w5", "bai", "w9", "w9", "s1", "s2", "s3", "s9",
            "t4", "t4", "t7", "t8", "t9", "xi", "hongzhong", "fa", "xi",
        ],
        "meld": [],
    },
    "live_090.png": {
        "concealed": [
            "w5", "bai", "w9", "w9", "s1", "s2", "s3", "s9",
            "t4", "t4", "t7", "t8", "t9", "fa",
        ],
        "meld": ["xi", "xi", "xi"],
    },
}


def template_stamp(template_dir: Path) -> dict:
    manifest = template_dir / "manifest.json"
    meta = {"path": str(manifest), "exists": manifest.exists()}
    if not manifest.exists():
        return meta
    st = manifest.stat()
    meta["mtime_local"] = datetime.fromtimestamp(st.st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S CST"
    )
    meta["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()[:16]
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    meta["version"] = raw.get("version")
    meta["created"] = raw.get("created")
    meta["template_count"] = raw.get("template_count") or len(raw.get("templates", []))
    meta["canonical_labels"] = raw.get("canonical_labels")
    return meta


def resolve_frame_dirs() -> dict:
    """Prefer repo-local (often symlinked) then majiang-data copies."""
    candidates = {
        "frames_live": [
            PC_BOT / "frames_live",
            DATA / "hana_runtime" / "frames_live",
        ],
        "frames_raw": [
            PC_BOT / "frames_raw",
            DATA / "hana_runtime" / "frames_raw",
        ],
        "harvest2_verify": [
            PC_BOT / "harvest2" / "verify",
            DATA / "hana_runtime" / "pc_bot" / "harvest2" / "verify",
        ],
    }
    out = {}
    for key, paths in candidates.items():
        for p in paths:
            if p.is_dir():
                out[key] = p.resolve()
                break
        else:
            out[key] = None
    return out


def eval_cluster_holdout(vision: TileVision, verify_dir: Path) -> dict:
    training = set()
    for r in json.loads(
        (vision.template_dir / "manifest.json").read_text(encoding="utf-8")
    )["templates"]:
        training.add(r["source_pixel_sha256"])

    total = checked = correct = wrong = rejected = 0
    failures = []
    for p in sorted(verify_dir.glob("*.png")):
        try:
            ci = int(p.name[1:3])
        except ValueError:
            continue
        expected = CLUSTER_LABELS.get(ci)
        if expected is None:
            continue
        im = _read(p)
        if im is None:
            continue
        total += 1
        if pixel_hash(im) in training:
            continue
        checked += 1
        d = vision.classify_crop(im)
        if not d["accepted"]:
            rejected += 1
            failures.append(
                {"source": p.name, "expected": expected, "reason": d["reason"],
                 "pred": d["label"], "score": round(d["score"], 4)}
            )
            continue
        if d["label"] == expected:
            correct += 1
        else:
            wrong += 1
            failures.append(
                {"source": p.name, "expected": expected, "pred": d["label"],
                 "score": round(d["score"], 4), "reason": "wrong_label"}
            )
    return {
        "N_crops_with_cluster_label": total,
        "N_nonduplicate_holdout": checked,
        "correct_accepted": correct,
        "wrong_accepted": wrong,
        "rejected": rejected,
        "accuracy_among_accepted": (
            round(correct / (correct + wrong), 4) if (correct + wrong) else None
        ),
        "accept_rate": round((correct + wrong) / checked, 4) if checked else None,
        "formula": (
            "holdout_acc = correct_accepted / (correct_accepted + wrong_accepted); "
            "exact training pixel duplicates excluded"
        ),
        "failures_sample": failures[:12],
    }


def eval_labeled_scenes(vision: TileVision, frames_live: Path) -> dict:
    results = []
    tile_correct = tile_total = scene_exact = 0
    for name, expected in LABELED_SCENES.items():
        path = frames_live / name
        if not path.exists():
            results.append({"source": name, "error": "missing"})
            continue
        im = _read(path)
        hand = vision.recognize_hand(im)
        pred_c = [t[0] for t in hand]
        exp_c = [TILE_IDS[x] for x in expected["concealed"]]
        pred_m = [t[0] for t in vision.last_melds]
        exp_m = [TILE_IDS[x] for x in expected["meld"]]
        n = max(len(pred_c), len(exp_c))
        # Position-wise: pad shorter with None
        match = 0
        for i in range(max(len(pred_c), len(exp_c))):
            tile_total += 1
            a = pred_c[i] if i < len(pred_c) else None
            b = exp_c[i] if i < len(exp_c) else None
            if a == b:
                match += 1
                tile_correct += 1
        exact = pred_c == exp_c and pred_m == exp_m and vision.last_complete
        if exact:
            scene_exact += 1
        results.append({
            "source": name,
            "detected_concealed": len(vision.diagnostics),
            "accepted_concealed": len(pred_c),
            "expected_concealed": len(exp_c),
            "position_matches": match,
            "position_total": max(len(pred_c), len(exp_c)),
            "meld_pred": pred_m,
            "meld_expected": exp_m,
            "complete": vision.last_complete,
            "exact_match": exact,
            "rejected_boxes": sum(1 for d in vision.diagnostics if not d["accepted"]),
        })
    n_scenes = len([r for r in results if "error" not in r])
    return {
        "N_labeled_scenes": n_scenes,
        "scene_exact_match": scene_exact,
        "scene_exact_rate": round(scene_exact / n_scenes, 4) if n_scenes else None,
        "tile_position_accuracy": (
            round(tile_correct / tile_total, 4) if tile_total else None
        ),
        "tile_correct": tile_correct,
        "tile_total": tile_total,
        "formula": (
            "scene_exact = full concealed+meld label match AND last_complete; "
            "tile_pos_acc = position-wise concealed id matches / max(len_pred,len_exp)"
        ),
        "scenes": results,
    }


def scan_unlabeled_frames(vision: TileVision, dirs: list[Path], limit: int | None) -> dict:
    files = []
    for d in dirs:
        if d is None:
            continue
        files.extend(sorted(d.glob("*.png")))
    # Deduplicate by resolved path
    seen = set()
    uniq = []
    for p in files:
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)
    if limit is not None:
        uniq = uniq[:limit]

    runnable = complete = partial = empty = 0
    reject_boxes = accept_boxes = detected_boxes = 0
    shapes = Counter()
    advise_ok = advise_fail = skip_nontarget = 0
    stability_ok = stability_n = 0
    advisor = Advisor()
    fail_samples = []

    for p in uniq:
        im = _read(p)
        if im is None:
            empty += 1
            continue
        runnable += 1
        shapes[f"{im.shape[0]}x{im.shape[1]}"] += 1
        hand = vision.recognize_hand(im)
        n_det = len(vision.diagnostics)
        n_acc = sum(1 for d in vision.diagnostics if d["accepted"])
        n_rej = n_det - n_acc
        detected_boxes += n_det
        accept_boxes += n_acc
        reject_boxes += n_rej
        if vision.last_complete and n_det > 0:
            complete += 1
        elif n_det > 0:
            partial += 1
            if len(fail_samples) < 8:
                fail_samples.append({
                    "source": str(p.name),
                    "detected": n_det,
                    "accepted": n_acc,
                    "complete": False,
                    "reasons": Counter(d["reason"] for d in vision.diagnostics if not d["accepted"]),
                })
        else:
            empty += 1

        # Brain: target vocabulary only (27 suits + 红中). Sample-mode honours
        # (东南西北白发) are rejected by engine.validate_tiles.
        tiles = [t[0] for t in hand]
        meld_n = len(vision.last_melds)
        open_meld_count = meld_n // 3 if meld_n >= 3 else 0
        wait = 13 - 3 * open_meld_count
        need = wait + 1
        if vision.last_complete and len(tiles) in (wait, need) and open_meld_count * 3 == meld_n:
            nontarget = sorted(set(tiles) - TARGET_IDS)
            if nontarget:
                skip_nontarget += 1
            else:
                try:
                    r1 = advisor.local_advise(
                        tiles, {}, turn_no=5, open_meld_count=open_meld_count
                    )
                    r2 = advisor.local_advise(
                        tiles, {}, turn_no=5, open_meld_count=open_meld_count
                    )
                    advise_ok += 1
                    if len(tiles) == need and r1.get("options"):
                        stability_n += 1
                        top1 = r1["options"][0]["tile"]
                        top2 = (
                            r2["options"][0]["tile"] if r2.get("options") else None
                        )
                        if top1 == top2 and r1["shanten"] == r2["shanten"]:
                            stability_ok += 1
                except Exception as e:  # noqa: BLE001
                    advise_fail += 1
                    if len(fail_samples) < 12:
                        fail_samples.append({
                            "source": str(p.name),
                            "brain_error": str(e),
                            "hand_len": len(tiles),
                            "open_meld_count": open_meld_count,
                        })

    return {
        "N_frame_files_scanned": len(uniq),
        "runnable_frames": runnable,
        "complete_hands": complete,
        "partial_hands": partial,
        "empty_or_undetected": empty,
        "complete_rate": round(complete / runnable, 4) if runnable else None,
        "box_accept_rate": (
            round(accept_boxes / detected_boxes, 4) if detected_boxes else None
        ),
        "box_reject_rate": (
            round(reject_boxes / detected_boxes, 4) if detected_boxes else None
        ),
        "detected_boxes": detected_boxes,
        "accepted_boxes": accept_boxes,
        "rejected_boxes": reject_boxes,
        "shapes": dict(shapes),
        "formula_unlabeled": (
            "complete_rate = frames with last_complete / runnable_frames; "
            "box_accept_rate = accepted_boxes / detected_boxes; "
            "no per-tile ground truth on unlabeled pool"
        ),
        "brain_advise_ok": advise_ok,
        "brain_advise_fail": advise_fail,
        "brain_skip_nontarget_vocab": skip_nontarget,
        "stability_N": stability_n,
        "stability_same_top": stability_ok,
        "stability_rate": (
            round(stability_ok / stability_n, 4) if stability_n else None
        ),
        "formula_stability": (
            "stability = same top discard tile + shanten on two local_advise "
            "calls with identical hand/played/turn (deterministic engine)"
        ),
        "fail_samples": fail_samples,
    }



def eval_brain_stability_suite() -> dict:
    """Deterministic local_advise stability on fixed target-vocab hands.

    Historical decision logs lack hand14, so agreement vs replay decisions is
    unavailable; this suite measures engine output stability (proxy metric).
    """
    advisor = Advisor()
    hands = [
        # From brain_v0/tests/test_advisor.py
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 47],
        # Near-ready / mixed shapes
        [1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 55, 55],
        [21, 22, 23, 24, 25, 26, 41, 42, 43, 44, 45, 46, 47, 55],
        [1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23],
        [2, 3, 4, 5, 6, 7, 8, 22, 23, 24, 42, 43, 44, 55],
        [1, 2, 3, 21, 22, 23, 41, 42, 43, 5, 5, 6, 7, 8],
        [9, 9, 9, 29, 29, 29, 49, 49, 1, 2, 3, 4, 5, 55],
    ]
    ok = same = 0
    failures = []
    for hand in hands:
        try:
            r1 = advisor.local_advise(hand, {}, turn_no=5)
            r2 = advisor.local_advise(hand, {}, turn_no=8)
            # Same hand, different turn_no may change danger; stability = identical
            # inputs must match. Re-run identical call.
            r3 = advisor.local_advise(hand, {}, turn_no=5)
            ok += 1
            t1 = r1["options"][0]["tile"] if r1.get("options") else None
            t3 = r3["options"][0]["tile"] if r3.get("options") else None
            if t1 == t3 and r1["shanten"] == r3["shanten"] and [
                o["tile"] for o in r1["options"]
            ] == [o["tile"] for o in r3["options"]]:
                same += 1
            else:
                failures.append({"hand": hand, "top1": t1, "top3": t3})
            # Turn sensitivity is informational only (danger rises with turn).
            _ = r2
        except Exception as e:  # noqa: BLE001
            failures.append({"hand": hand, "error": str(e)})
    return {
        "N_fixed_hands": len(hands),
        "advise_ok": ok,
        "identical_input_stable": same,
        "stability_rate": round(same / ok, 4) if ok else None,
        "formula": (
            "stability = identical hand/played/turn => identical option tile order "
            "+ shanten on two local_advise calls; no historical hand-labelled "
            "decisions available for agreement"
        ),
        "failures": failures[:8],
    }


def historical_decisions_note() -> dict:
    stats = DATA / "hana_runtime" / "logs" / "stats_20261014.jsonl"
    n = 0
    has_hand = 0
    if stats.exists():
        for line in stats.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "decision":
                n += 1
                if "hand" in row or "hand14" in row:
                    has_hand += 1
    return {
        "path": str(stats),
        "decision_rows": n,
        "rows_with_hand_state": has_hand,
        "note": (
            "stats_20261014.jsonl has discard+shanten but no hand14; "
            "cannot compute vision→advise vs historical decision agreement. "
            "Using engine output stability instead."
        ),
    }


def run_unit_tests() -> dict:
    import subprocess

    cmds = [
        [
            sys.executable, "-m", "unittest", "discover",
            "-s", str(PC_BOT / "tests"), "-p", "test_tile_vision.py", "-v",
        ],
        [
            sys.executable, "-m", "pytest",
            str(REPO / "brain_v0" / "tests" / "test_advisor.py"),
            "-q", "-p", "no:cacheprovider",
        ],
    ]
    results = []
    for cmd in cmds:
        t0 = time.time()
        proc = subprocess.run(
            cmd, cwd=str(REPO), capture_output=True, text=True, timeout=180
        )
        results.append({
            "cmd": " ".join(cmd),
            "returncode": proc.returncode,
            "elapsed_s": round(time.time() - t0, 2),
            "stdout_tail": proc.stdout[-1200:],
            "stderr_tail": proc.stderr[-800:],
        })
    return {"tests": results}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--limit-frames", type=int, default=None,
                    help="Cap unlabeled frame scan (default: all)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    template_dir = PC_BOT / "templates_hongzhong_v2"
    stamp = template_stamp(template_dir)
    dirs = resolve_frame_dirs()

    vision = TileVision(template_dir=template_dir)
    vision.load_templates()

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S CST"),
        "template": stamp,
        "data_roots": {k: str(v) if v else None for k, v in dirs.items()},
        "historical_decisions": historical_decisions_note(),
    }

    if dirs["harvest2_verify"]:
        report["cluster_holdout"] = eval_cluster_holdout(
            vision, dirs["harvest2_verify"]
        )
    else:
        report["cluster_holdout"] = {"error": "harvest2/verify missing"}

    if dirs["frames_live"]:
        report["labeled_scenes"] = eval_labeled_scenes(vision, dirs["frames_live"])
    else:
        report["labeled_scenes"] = {"error": "frames_live missing"}

    scan_dirs = [d for d in (dirs["frames_live"], dirs["frames_raw"]) if d]
    report["unlabeled_replay"] = scan_unlabeled_frames(
        vision, scan_dirs, args.limit_frames
    )
    report["brain_stability_suite"] = eval_brain_stability_suite()

    if not args.skip_tests:
        report["unit_tests"] = run_unit_tests()

    # Summary numbers for humans
    ch = report.get("cluster_holdout", {})
    ls = report.get("labeled_scenes", {})
    ur = report.get("unlabeled_replay", {})
    bs = report.get("brain_stability_suite", {})
    report["summary"] = {
        "holdout_N": ch.get("N_nonduplicate_holdout"),
        "holdout_accuracy_among_accepted": ch.get("accuracy_among_accepted"),
        "labeled_scene_N": ls.get("N_labeled_scenes"),
        "labeled_scene_exact_rate": ls.get("scene_exact_rate"),
        "labeled_tile_position_accuracy": ls.get("tile_position_accuracy"),
        "unlabeled_N": ur.get("runnable_frames"),
        "unlabeled_complete_rate": ur.get("complete_rate"),
        "unlabeled_box_accept_rate": ur.get("box_accept_rate"),
        "frame_brain_stability_N": ur.get("stability_N"),
        "frame_brain_stability_rate": ur.get("stability_rate"),
        "frame_brain_skip_nontarget": ur.get("brain_skip_nontarget_vocab"),
        "suite_brain_stability_N": bs.get("N_fixed_hands"),
        "suite_brain_stability_rate": bs.get("stability_rate"),
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    out = args.out or (PC_BOT / "logs" / "offline_regress_latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n# wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
