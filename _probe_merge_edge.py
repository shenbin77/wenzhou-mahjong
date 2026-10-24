"""Verify _merge_split_runs edge cases before handing the contract to tests.

Mirrors the extracted function byte-for-byte in behaviour, then probes the
boundaries engineer-3 wants to encode as exhaustive cases: exact-equal widths,
the 125*scale cap, chain merges, and scale != 1.0.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Run = Tuple[int, int]


def merge_split_runs(runs: Iterable[Sequence[int]], scale: float) -> List[Run]:
    """Merge glyph-split fragments of one tile face back into a single run."""
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


def show(name: str, runs: Sequence[Run], scale: float = 1.0) -> List[Run]:
    """Print one case and return the merged runs."""
    got = merge_split_runs(runs, scale)
    print(f"{name:<52} scale={scale:<4} {list(runs)} -> {got}")
    return got


def main() -> int:
    print("== 正常形态：两张真牌不得被粘 ==")
    show("两个完整牌(83px)+缝4", [(0, 83), (87, 170)])
    show("恰 48px 的牌 + 39px 碎片", [(0, 48), (52, 91)])

    print("\n== 一张牌被竖纹切成两片 ==")
    show("48 门槛下的两片 42+39(缝2)", [(452, 494), (496, 535)])

    print("\n== 边界：严格/非严格比较 ==")
    show("碎片恰 = 48*scale（应不合并）", [(0, 48), (52, 100)])
    show("碎片 47 + 缝 5 + 47", [(0, 47), (52, 99)])
    show("碎片 47 + 缝 6（应不合并，6<6 假）", [(0, 47), (53, 100)])
    show("碎片 47 + 缝 5.5 视作 5", [(0, 47), (52, 99)])

    print("\n== 125*scale 上限是否可达 ==")
    widest = merge_split_runs([(0, 47), (52, 99)], 1.0)[0]
    print(f"单次合并理论最大宽 = 47 + 5 + 47 = {widest[1] - widest[0]}"
          f"  (125*scale = 125)")
    chain = show("链式：20/20/20 三段各缝4", [(0, 20), (24, 44), (48, 68)])
    print(f"链式合并后宽度 = {chain[0][1] - chain[0][0]}（仍 < 102）")

    print("\n== 链式合并何时停 ==")
    show("40 + 40 合并后 84 即停", [(0, 40), (44, 84), (88, 128)])
    show("45+45 合并 → 94，第三段进不来", [(0, 45), (49, 94), (98, 143)])

    print("\n== 跨牌边界误并的风险面 ==")
    show("A 完整(83)+B 两片", [(0, 83), (87, 127), (131, 170)])
    show("A/B 各自两片(缝4)，正确配对", [(10, 50), (54, 83), (87, 126), (130, 170)])
    show("A 首片恰 48(不参与合并)→ 误并风险", [(0, 48), (52, 83), (87, 118), (122, 170)])

    print("\n== scale != 1.0 ==")
    show("scale=1.2：两片 40+40", [(0, 40), (44, 84)], 1.2)
    show("scale=1.2：57px 片（<57.6 算碎片）", [(0, 57), (61, 118)], 1.2)
    show("scale=1.2：58px 片（>=57.6 不算碎片）", [(0, 58), (62, 120)], 1.2)
    show("scale=0.8：38px 片（<38.4 算碎片）", [(0, 38), (41, 79)], 0.8)

    print("\n== 恒等输入 ==")
    show("空", [])
    show("单 run 95px", [(0, 95)])

    print("\n== 不变量扫描：合并结果宽度上界 ==")
    worst = 0
    for first in range(1, 48):
        for second in range(1, 48):
            for gap in range(0, 6):
                out = merge_split_runs([(0, first), (first + gap, first + gap + second)], 1.0)
                if len(out) == 1:
                    worst = max(worst, out[0][1] - out[0][0])
    print(f"穷举 first/second<48、gap<6：合并后最大宽度 = {worst}"
          f"（上界 48+6+48-3 = 99）")
    print(f"125 上限是否曾被触发 = {'是' if worst > 125 else '否（死条款）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
