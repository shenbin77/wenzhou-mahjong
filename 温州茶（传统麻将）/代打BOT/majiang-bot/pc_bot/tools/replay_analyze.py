#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline replay-frame analysis: TileVision -> hand sequence -> stats.

Reads frames from replay_harvest (or dry-run fallbacks). Does not touch
emulators, does not click, does not change decision/vision algorithms.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

PC_BOT = Path(__file__).resolve().parents[1]
REPO = PC_BOT.parent
DATA = Path("/workspace/majiang-data")
sys.path.insert(0, str(PC_BOT))
sys.path.insert(0, str(REPO / "brain_v0"))

from tile_vision import TileVision, _read  # noqa: E402
from engine.advisor import Advisor  # noqa: E402
from engine.shanten import calc_shanten  # noqa: E402
from engine.tiles import CAISHEN_ID, TILE_NAMES  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
DEFAULT_HARVEST = DATA / "replay_harvest"
FALLBACK_LIVE = [
    DATA / "hana_runtime" / "frames_live",
    PC_BOT / "frames_live",
]
FALLBACK_RAW = [
    DATA / "hana_runtime" / "frames_raw",
    PC_BOT / "frames_raw",
]


def _now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def discover_frames(root: Path) -> list[Path]:
    """Collect image frames under root (recursive). Prefer mtime then name."""
    if not root or not root.is_dir():
        return []
    frames = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            frames.append(p)
    frames.sort(key=lambda p: (p.stat().st_mtime, str(p)))
    return frames


def load_meta(frame: Path) -> dict | None:
    for cand in (
        frame.with_suffix(".json"),
        frame.parent / (frame.stem + "_meta.json"),
        frame.parent / "meta.json",
    ):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                return {"_error": f"meta_parse_failed:{cand.name}"}
    return None


def resolve_input_dir(explicit: Path | None) -> tuple[Path, str, bool]:
    """Return (dir, note, is_dry_run). Prefer harvest; fall back for dry-run."""
    if explicit is not None:
        return explicit, f"explicit:{explicit}", False

    harvest = DEFAULT_HARVEST
    harvest_frames = discover_frames(harvest) if harvest.is_dir() else []
    # Treat as usable harvest only if at least one frame looks like a table
    # (caller still runs vision). Empty / missing -> dry-run fallback.
    if harvest_frames:
        return harvest, f"replay_harvest:{harvest} (n_files={len(harvest_frames)})", False

    for group, label in ((FALLBACK_LIVE, "frames_live"), (FALLBACK_RAW, "frames_raw")):
        for d in group:
            if d.is_dir() and discover_frames(d):
                return (
                    d,
                    f"dry-run fallback {label}:{d} — 真实回放待到 "
                    f"(replay_harvest 为空或尚无帧)",
                    True,
                )
    # Last resort: empty harvest path so report is still written
    return harvest, f"empty:{harvest} — 无可用帧，真实回放待到", True


def hand_ids(recognized) -> list[int]:
    return [int(t[0]) for t in recognized]


def analyze_frame(vision: TileVision, path: Path) -> dict:
    img = _read(path)
    row = {
        "path": str(path),
        "name": path.name,
        "rel": str(path),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S CST"
        ),
        "meta": load_meta(path),
    }
    if img is None:
        row.update(ok=False, reason="imread_failed")
        return row
    h, w = img.shape[:2]
    row["size"] = [w, h]
    try:
        recognized = vision.recognize_hand(img)
    except Exception as e:
        row.update(ok=False, reason=f"vision_error:{type(e).__name__}:{e}")
        return row

    concealed = hand_ids(recognized)
    melds = hand_ids(vision.last_melds)
    n_boxes = vision.detection_diagnostics.get("concealed_count", len(recognized))
    n_accepted = len(concealed)
    complete = bool(vision.last_complete)
    open_meld_count = len(melds) // 3 if melds else 0  # coarse: 3-tile groups
    if len(melds) == 4:
        open_meld_count = max(open_meld_count, 1)  # possible gang

    # Heuristic: gameplay if we see a plausible concealed hand size
    wait = 13 - 3 * open_meld_count
    is_hand14 = len(concealed) == wait + 1
    is_hand13 = len(concealed) == wait
    gameplay = complete and (is_hand13 or is_hand14) and n_accepted >= 10

    hongzhong = sum(1 for t in concealed + melds if t == CAISHEN_ID)
    shanten = None
    advise = None
    if gameplay and (is_hand13 or is_hand14):
        try:
            shanten = calc_shanten(concealed, open_meld_count=open_meld_count)
        except Exception:
            shanten = None
        if is_hand14:
            try:
                adv = Advisor().local_advise(
                    concealed, {}, turn_no=1, open_meld_count=open_meld_count
                )
                top = (adv.get("options") or [None])[0]
                advise = {
                    "shanten": adv.get("shanten"),
                    "suggested": None if not top else top.get("tile"),
                    "suggested_name": None if not top else top.get("name"),
                }
            except Exception as e:
                advise = {"error": str(e)}

    settle_score = None
    settle_note = None
    try:
        from settle_ocr import extract_settle_score

        settle_score = extract_settle_score(img)
    except Exception as e:
        settle_note = f"settle_ocr_unavailable:{type(e).__name__}"

    row.update(
        ok=True,
        concealed=concealed,
        concealed_names=[TILE_NAMES.get(t, str(t)) for t in concealed],
        melds=melds,
        n_boxes=n_boxes,
        n_accepted=n_accepted,
        complete=complete,
        gameplay=gameplay,
        is_hand13=is_hand13,
        is_hand14=is_hand14,
        open_meld_count=open_meld_count,
        hongzhong=hongzhong,
        shanten=shanten,
        advise=advise,
        settle_score=settle_score,
        settle_note=settle_note,
        rejected=sum(1 for d in vision.diagnostics if not d.get("accepted")),
    )
    return row


def segment_rounds(frames: list[dict]) -> list[dict]:
    """Group gameplay frames into rough 'rounds' by gaps / settle / hand reset."""
    rounds = []
    cur = None
    for fr in frames:
        if not fr.get("ok"):
            continue
        is_settle = fr.get("settle_score") is not None
        gp = fr.get("gameplay")
        if is_settle and cur is not None:
            cur["settle_score"] = fr["settle_score"]
            cur["settle_frame"] = fr["name"]
            cur["frames"].append(fr["name"])
            rounds.append(cur)
            cur = None
            continue
        if not gp:
            continue
        # Start new round on first gameplay or after long name/path jump
        if cur is None:
            cur = {
                "start_frame": fr["name"],
                "frames": [fr["name"]],
                "hands": [],
                "starting_hand": fr.get("concealed"),
                "starting_shanten": fr.get("shanten"),
                "starting_hongzhong": fr.get("hongzhong"),
                "settle_score": None,
                "settle_frame": None,
            }
            cur["hands"].append(
                {
                    "name": fr["name"],
                    "concealed": fr.get("concealed"),
                    "shanten": fr.get("shanten"),
                    "hongzhong": fr.get("hongzhong"),
                    "is_hand14": fr.get("is_hand14"),
                }
            )
            continue
        # Hand reset heuristic: hand14/13 that differs a lot from previous
        prev = cur["hands"][-1]["concealed"] or []
        now = fr.get("concealed") or []
        overlap = len(set(prev) & set(now))
        if prev and now and overlap <= 4 and len(now) >= 13:
            rounds.append(cur)
            cur = {
                "start_frame": fr["name"],
                "frames": [fr["name"]],
                "hands": [],
                "starting_hand": now,
                "starting_shanten": fr.get("shanten"),
                "starting_hongzhong": fr.get("hongzhong"),
                "settle_score": None,
                "settle_frame": None,
            }
        else:
            cur["frames"].append(fr["name"])
        cur["hands"].append(
            {
                "name": fr["name"],
                "concealed": fr.get("concealed"),
                "shanten": fr.get("shanten"),
                "hongzhong": fr.get("hongzhong"),
                "is_hand14": fr.get("is_hand14"),
            }
        )
    if cur is not None:
        rounds.append(cur)
    return rounds


def compute_stats(frames: list[dict], rounds: list[dict], *, dry_run: bool, note: str) -> dict:
    ok = [f for f in frames if f.get("ok")]
    complete = [f for f in ok if f.get("complete")]
    gameplay = [f for f in ok if f.get("gameplay")]
    with_hand = [f for f in ok if f.get("n_accepted", 0) > 0]

    # Recognition completeness: among frames with any boxes, fraction complete
    boxed = [f for f in ok if (f.get("n_boxes") or 0) > 0]
    complete_rate = (
        round(len(complete) / len(boxed), 4) if boxed else None
    )
    complete_rate_note = (
        None
        if boxed
        else "无检测到位的牌框帧，无法算完整率"
    )

    hz_dist = Counter(f.get("hongzhong", 0) for f in gameplay)
    start_hz = Counter(
        r.get("starting_hongzhong")
        for r in rounds
        if r.get("starting_hongzhong") is not None
    )
    start_shanten = [
        r.get("starting_shanten")
        for r in rounds
        if r.get("starting_shanten") is not None
    ]

    settled = [r for r in rounds if r.get("settle_score") is not None]
    wins = sum(1 for r in settled if (r.get("settle_score") or 0) > 0)
    win_rate = round(wins / len(settled), 4) if settled else None
    win_rate_note = None
    if not settled:
        win_rate_note = (
            "无法从结算帧/settle OCR 推出胜率：本批无可靠 settle_score"
        )

    hand14_n = sum(1 for f in gameplay if f.get("is_hand14"))
    hand13_n = sum(1 for f in gameplay if f.get("is_hand13"))

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S CST"),
        "dry_run": dry_run,
        "input_note": note,
        "n_frames": len(frames),
        "n_ok": len(ok),
        "n_with_tiles": len(with_hand),
        "n_complete": len(complete),
        "n_gameplay": len(gameplay),
        "n_hand14": hand14_n,
        "n_hand13": hand13_n,
        "n_rounds_estimated": len(rounds),
        "recognition_complete_rate": complete_rate,
        "recognition_complete_rate_note": complete_rate_note,
        "hongzhong_dist_gameplay": dict(sorted(hz_dist.items())),
        "starting_hongzhong_dist": {str(k): v for k, v in sorted(start_hz.items())},
        "starting_shanten_avg": (
            round(sum(start_shanten) / len(start_shanten), 3)
            if start_shanten
            else None
        ),
        "starting_shanten_note": (
            None if start_shanten else "无可用起手向听（缺完整 gameplay 起手）"
        ),
        "n_settled_rounds": len(settled),
        "n_wins_by_score_sign": wins,
        "win_rate": win_rate,
        "win_rate_note": win_rate_note,
        "hu_rate_note": win_rate_note,  # alias: 结算分>0 近似胡/胜
    }


def render_markdown(stats: dict, out_json: Path) -> str:
    lines = [
        f"# 回放分析报告",
        "",
        f"- 生成时间: {stats['generated_at']}",
        f"- 输入: {stats['input_note']}",
        f"- dry_run: {stats['dry_run']}",
        f"- JSON: `{out_json}`",
        "",
        "## 规模",
        f"- 帧数: **{stats['n_frames']}** (ok={stats['n_ok']}, "
        f"有牌={stats['n_with_tiles']}, 完整={stats['n_complete']}, "
        f"gameplay={stats['n_gameplay']})",
        f"- hand14/hand13: {stats['n_hand14']} / {stats['n_hand13']}",
        f"- 估计局数: **{stats['n_rounds_estimated']}**",
        "",
        "## 识牌完整率",
        f"- complete_rate: **{stats['recognition_complete_rate']}**",
    ]
    if stats.get("recognition_complete_rate_note"):
        lines.append(f"- 备注: {stats['recognition_complete_rate_note']}")
    lines += [
        "",
        "## 红中 / 起手",
        f"- gameplay 红中数分布: `{stats['hongzhong_dist_gameplay']}`",
        f"- 起手红中分布: `{stats['starting_hongzhong_dist']}`",
        f"- 起手向听均值: **{stats['starting_shanten_avg']}**",
    ]
    if stats.get("starting_shanten_note"):
        lines.append(f"- 起手备注: {stats['starting_shanten_note']}")
    lines += [
        "",
        "## 胜率 / 胡牌率",
        f"- 有结算分的局: {stats['n_settled_rounds']}",
        f"- score>0 局数: {stats['n_wins_by_score_sign']}",
        f"- win_rate: **{stats['win_rate']}**",
    ]
    if stats.get("win_rate_note"):
        lines.append(f"- 备注: {stats['win_rate_note']}")
    if stats.get("dry_run"):
        lines += [
            "",
            "> **真实回放待到**：当前为 dry-run / 回退数据，"
            "等 `/workspace/majiang-data/replay_harvest/` 采到对局帧后复跑。",
        ]
    lines += [
        "",
        "## 复跑命令",
        "```",
        "python -m tools.replay_analyze",
        "# 或指定目录:",
        "python pc_bot/tools/replay_analyze.py --input /workspace/majiang-data/replay_harvest",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay harvest → TileVision → stats")
    ap.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Frame root (default: replay_harvest or dry-run fallback)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=PC_BOT / "logs",
        help="Output directory for replay_*.json/md",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max frames (0=all)")
    ap.add_argument(
        "--template-dir",
        type=Path,
        default=PC_BOT / "templates_hongzhong_v2",
    )
    args = ap.parse_args(argv)

    root, note, dry_run = resolve_input_dir(args.input)
    frames_paths = discover_frames(root)
    if args.limit and args.limit > 0:
        frames_paths = frames_paths[: args.limit]

    vision = TileVision(template_dir=args.template_dir)
    vision.load_templates()

    t0 = time.time()
    rows = []
    for i, p in enumerate(frames_paths):
        row = analyze_frame(vision, p)
        row["rel"] = str(p.relative_to(root)) if root in p.parents or p.parent == root else p.name
        rows.append(row)
        if (i + 1) % 25 == 0:
            print(f"[replay_analyze] {i+1}/{len(frames_paths)} ...", flush=True)

    rounds = segment_rounds(rows)
    stats = compute_stats(rows, rounds, dry_run=dry_run, note=note)
    stats["elapsed_sec"] = round(time.time() - t0, 2)
    stats["template_dir"] = str(args.template_dir)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    out_json = args.out_dir / f"replay_{stamp}.json"
    out_md = args.out_dir / f"replay_{stamp}.md"
    latest_json = args.out_dir / "replay_latest.json"
    latest_md = args.out_dir / "replay_latest.md"

    payload = {
        "stats": stats,
        "rounds": rounds,
        "frames": rows,
    }
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = render_markdown(stats, out_json)
    out_md.write_text(md, encoding="utf-8")
    latest_json.write_text(out_json.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"[replay_analyze] wrote {out_json}")
    print(f"[replay_analyze] wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
