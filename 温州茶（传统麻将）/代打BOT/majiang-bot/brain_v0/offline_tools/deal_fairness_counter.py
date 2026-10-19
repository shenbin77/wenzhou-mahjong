#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发牌公平性空壳计数接口（数据不足时只返回 insufficient_data）。

不触碰实盘。完整检验见 docs/deal_fairness_plan.md。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_v0"))

from engine.tiles import CAISHEN_ID  # noqa: E402

DEFAULT_MIN_ROUNDS = 100


def count_hongzhong(hand) -> int:
    return sum(1 for t in hand if int(t) == CAISHEN_ID)


def load_deal_samples(path: Path) -> list[dict]:
    """支持 JSON 数组、JSONL、或 {samples:[...]}。样本需 starting_hand13。"""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return list(data)
    if text.startswith("{"):
        obj = json.loads(text)
        if "samples" in obj:
            return list(obj["samples"])
        if "starting_hand13" in obj:
            return [obj]
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def analyze(samples: list[dict], min_rounds: int = DEFAULT_MIN_ROUNDS) -> dict:
    hist: Counter = Counter()
    usable = []
    for s in samples:
        hand = s.get("starting_hand13") or s.get("hand13") or s.get("hand")
        if not hand or len(hand) not in (13, 14):
            continue
        # 14 张若含庄首摸，仍记红中数但标记
        hz = count_hongzhong(hand[:13] if len(hand) >= 13 else hand)
        hist[hz] += 1
        usable.append({"round_uid": s.get("round_uid"), "hongzhong": hz, "n": len(hand)})

    n = len(usable)
    report = {
        "verdict": "insufficient_data" if n < min_rounds else "counted_only",
        "rounds": n,
        "min_rounds": min_rounds,
        "hongzhong_hist": {str(k): hist[k] for k in sorted(hist)},
        "note": (
            "样本不足，不下发牌公平性结论；见 brain_v0/docs/deal_fairness_plan.md"
            if n < min_rounds
            else "仅计数，完整 χ²/蒙特卡洛尚未启用"
        ),
        "samples_preview": usable[:5],
    }
    return report


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description="Deal fairness counter stub")
    p.add_argument("--input", type=Path, help="deal samples JSON/JSONL")
    p.add_argument("--min-rounds", type=int, default=DEFAULT_MIN_ROUNDS)
    args = p.parse_args(argv)
    samples = load_deal_samples(args.input) if args.input else []
    rep = analyze(samples, min_rounds=args.min_rounds)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return rep


if __name__ == "__main__":
    main()
