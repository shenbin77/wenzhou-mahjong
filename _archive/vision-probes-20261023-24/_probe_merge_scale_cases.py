"""Check engineer-3's proposed case set under scale=1.0 and scale=1.5.

The cases he listed use absolute pixel widths. A predicate built on
48*scale / 6*scale / 125*scale does not give the same verdict once scale
moves, so every case has to be expressed in scaled units. This prints the
verdict per case per scale so the mismatch is visible before the tests land.
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


# engineer-3 列出的用例，原样（绝对像素）
CASES = [
    ("可并 47+5+47", [(0, 47), (52, 99)], "并"),
    ("不可并 两侧 50/50", [(0, 50), (55, 105)], "不并"),
    ("不可并 真实牌宽 82", [(0, 82), (87, 169)], "不并"),
    ("不可并 缝 13", [(0, 47), (60, 107)], "不并"),
    ("不可并 三段仅中间窄", [(0, 82), (90, 130), (140, 222)], "不并"),
    ("边界 宽恰 48", [(0, 48), (52, 100)], "不并"),
    ("边界 缝恰 6", [(0, 47), (53, 100)], "不并"),
    ("链 3 段全并", [(0, 20), (24, 44), (48, 68)], "并"),
    ("链 4 段必停", [(0, 20), (24, 44), (48, 68), (72, 92)], "半"),
    ("跨牌误并(已知边界)", [(0, 48), (52, 83), (87, 118), (122, 170)], "误并"),
]


def scaled(runs: Sequence[Run], scale: float) -> List[Run]:
    """Express the same geometry in scaled pixel units."""
    return [(int(round(a * scale)), int(round(b * scale))) for a, b in runs]


def main() -> int:
    for scale in (1.0, 1.5):
        print(f"\n===== scale={scale}  48s={48 * scale} 6s={6 * scale} "
              f"125s={125 * scale} =====")
        for name, runs, expect in CASES:
            raw = merge_split_runs(runs, scale)
            fix = merge_split_runs(scaled(runs, scale), scale)
            print(f"{name:<22} 原样像素 -> {raw}\n"
                  f"{'':<22} 按 scale 缩放 -> {fix}")
        print("  期望（原样）:", ", ".join(f"{n}={e}" for n, _, e in CASES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
