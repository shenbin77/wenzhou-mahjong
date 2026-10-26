"""Count exactly how many fixed-pixel cases flip verdict across scale.

The earlier headline said "6 of 10" while the printed table had 5 rows; this
recomputes the flip set from the predicate itself so the number is derived,
not counted by eye.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Run = Tuple[int, int]


def merge_split_runs(runs: Iterable[Sequence[int]], scale: float) -> List[Run]:
    """Pairwise greedy fold; identical to the inlined block in count_tiles."""
    merged: List[Run] = []
    for a, b in runs:
        if merged:
            pa, pb = merged[-1]
            if (pb - pa) < 48 * scale and (b - a) < 48 * scale \
                    and (a - pb) < 6 * scale and (b - pa) <= 125 * scale:
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    return merged


CASES = [
    ("可并 47+5+47", [(0, 47), (52, 99)]),
    ("不可并 两侧 50/50", [(0, 50), (55, 105)]),
    ("不可并 真实牌宽 82", [(0, 82), (87, 169)]),
    ("不可并 缝 13", [(0, 47), (60, 107)]),
    ("不可并 三段仅中间窄", [(0, 82), (90, 130), (140, 222)]),
    ("边界 宽恰 48", [(0, 48), (52, 100)]),
    ("边界 缝恰 6", [(0, 47), (53, 100)]),
    ("链 3 段全并", [(0, 20), (24, 44), (48, 68)]),
    ("链 4 段必停", [(0, 20), (24, 44), (48, 68), (72, 92)]),
    ("跨牌误并(已知边界)", [(0, 48), (52, 83), (87, 118), (122, 170)]),
]


def verdict(runs: Sequence[Run]) -> str:
    """Verdict is the run structure, i.e. how many boxes survive the fold."""
    out = merge_split_runs(runs, 1.0)
    return "|".join(f"{a}-{b}" for a, b in out)


def main() -> int:
    flips = []
    print(f"{'用例':<20}{'s=1.0':<34}{'s=1.5':<34}翻")
    for name, runs in CASES:
        base = merge_split_runs(runs, 1.0)
        alt = merge_split_runs(runs, 1.5)
        flipped = base != alt
        if flipped:
            flips.append(name)
        print(f"{name:<20}{str(base):<34}{str(alt):<34}{'✗' if flipped else ''}")
    print(f"\n翻转 {len(flips)}/{len(CASES)}：")
    for name in flips:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
