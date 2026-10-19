# Tea harvest progress (2026-10-16 CST)

## Correct path
Lobby → **比赛场**(373,581) → **菁菁的比赛场 745404** 进入(378,668) → **比赛战绩** trophy(1306,62) → date → 详情 → 回放 → frames → green X **(1050,404)**

NEVER 游戏中心. NEVER 下一局 / 开始游戏. Prefer in-app buttons; max one BACK if stuck.

## Done (synced under tea/)
| Date | Rooms | Frames/room | Notes |
|------|-------|-------------|-------|
| 2026-10-12 | 756609,010478,964826 | 48+ | done earlier |
| 2026-10-16 | 740195 | 48 | done |
| 2026-10-15 | 14 rooms | 48 each | COMPLETE 2026-10-16 15:32 CST |

### 2026-10-15 rooms (14 = 今日场数)
790928, 291755, 855053, room_p2a(21:34), 543271, 026997, 424821, 059418, 569283, 426128, 125000, 215121, 905891, 286734

## In progress
- 2026-10-14: 8 rooms harvesting (400960,614395,425556,578740,684875,034929,710418,+room4)
- Then 10-13 and earlier with 温州麻将

## Coords 1600x900
- Date arrow (392,145); from 10-16: 10-15 y≈364, 10-14 y≈311
- Close X (1050,404); 回放 tab (1540,248); rounds X=1495 Y=320,403,487,570,653,734
- 详情 (1458, 265/443/629); 返回列表 (1440,133)
- Large swipe 800,700→250 ≈ page; ForceDate always before each room

## Quarantine
_quarantine_mislabeled deleted after disk cleanup — do not reimport without date fix

## Template / ROI sampling (2026-10-16 ~16:35 CST)

- Synced to box: full 10-13; 10-14 rooms 400960 + 614395 (Win still has 034929,425556,578740,684875,710418,room4).
- Dense ROI pass: `majiang-worktrees/templates-and-sampler/pc_bot/tools/sample_tea_rois.py` → `pc_bot/captures/tea_roi_samples/` + `TEA_ROI_GAP.md`.
- No ADB clicks during this work.
