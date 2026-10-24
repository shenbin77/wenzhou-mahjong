"""Read-only: recompute the platform-to-hand gap ratio engineer-3 proposed.

Mirrors tea_bot's own arithmetic (hand_left - group_right) so the numbers are
directly comparable with the abstain message text. Writes nothing to disk.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(r"D:/温州麻将/温州茶（传统麻将）")
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

from tile_vision import TileVision  # noqa: E402

FRAMES = [
    ("015126", "采集/assist_sessions/20261024_015005/live/tea_2p/"
               "20261024_015126_255781_543a04.png"),
    ("西杠005700", "采集/assist_sessions/20261024_005400/live/tea_2p/"
                  "20261024_005700_684588_0ae39a.png"),
    ("真碰205534", "采集/assist_sessions/20261023_201640/live/tea_2p/"
                  "20261023_205534_475580_f4a34e.png"),
    ("动画224750", "采集/assist_sessions/20261023_223759/live/tea_2p/"
                  "20261023_224750_469278_eec744.png"),
]


def load(path: Path) -> np.ndarray:
    """imdecode keeps non-ASCII Windows paths working."""
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                        cv2.IMREAD_COLOR)


def probe(name: str, rel: str, vision: TileVision) -> tuple:
    """Return (count, melds, median_width, gap, ratio) for one frame."""
    image = load(ROOT / rel)
    count, hand = vision.count_tiles(image)
    meld_boxes = [tuple(int(v) for v in box) for box in vision.last_meld_boxes]
    widths = [box[2] - box[0] for box in meld_boxes]
    hand_left = min(int(row[0]) for row in hand) if hand else None
    right = max(box[2] for box in meld_boxes) if meld_boxes else None
    gap = (hand_left - right) if (hand_left is not None
                                  and right is not None) else None
    median_width = statistics.median(widths) if widths else None
    ratio = (gap / median_width) if (gap is not None and median_width) else None
    groups = (vision.detection_diagnostics or {}).get("meld_groups")
    print(f"{name}\tcount={count}\tmelds={len(meld_boxes)}\t"
          f"groups={groups}\twidths={min(widths) if widths else '-'}"
          f"..{max(widths) if widths else '-'}\tmedian={median_width}\t"
          f"gap={gap}\tratio={ratio if ratio is None else round(ratio, 3)}")
    return count, len(meld_boxes), median_width, gap, ratio


def main() -> int:
    vision = TileVision(BOT / "templates")
    ratios = []
    for name, rel in FRAMES:
        try:
            got = probe(name, rel, vision)
        except Exception as exc:  # noqa: BLE001 - report, keep going
            print(f"{name}\tERROR {type(exc).__name__}: {exc}")
            continue
        if got[4] is not None:
            ratios.append((name, got[4]))
    print("\n--- 只统计『暗手紧跟平台』的帧（排除动画帧）---")
    tight = [(n, r) for n, r in ratios if r < 1.0]
    for name, ratio in tight:
        print(f"{name}\t{ratio:.3f}")
    if tight:
        print(f"min={min(r for _, r in tight):.3f} "
              f"max={max(r for _, r in tight):.3f}")
        print(f"建议带宽 = [{min(r for _, r in tight) - 0.01:.2f}, "
              f"{max(r for _, r in tight) + 0.03:.2f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
