# templates/ui wiring (tea_harvest) — 2026-10-16

All templates live in `templates/ui/`. Matcher: `match_template.py` (cv2 `TM_CCOEFF_NORMED`, default thresh **0.82**) via `Find-Template` / `Find-TemplateTap` in `harvest_continuous_v3.ps1`.

## Template → function map

| File | Role | Used by |
|------|------|---------|
| `btn_close_x.png` | **ONLY** safe promo/GameCenter dismiss X; also preferred in-replay close when visible | `DismissPromoCloseX`, `Close-ReplayBar` |
| `ui_replay_controls.png` | Replay pause/ff/speed bar (+ close X on right). Detect+save; close taps right edge of match | `Detect-ActionButtons`, `Close-ReplayBar` (never blind `$X_CLOSE`) |
| `btn_bisai.png` | Lobby 比赛 entry | `Try-LobbyTemplateNav` |
| `btn_zhanji.png` | 比赛战绩 | `Try-LobbyTemplateNav` |
| `btn_huifang.png` | 回放 tab/button | `Try-LobbyTemplateNav`, `EnsureReplayTab` (fallback coords + `TEMPLATE_MISS`) |
| `btn_enter.png` | 进入 (room enter) | `Try-LobbyTemplateNav`, `Tap-XiangqingOrEnter` |
| `btn_xiangqing.png` | 详情 | `Tap-XiangqingOrEnter` (preferred before enter / `$X_DETAIL`) |
| `ui_date_field.png` | Date field chrome | `ForceDate` (open picker) |
| `btn_date_dropdown.png` | Date dropdown control | `ForceDate` (preferred open) |
| `icon_zjgamecenter.png` | 浙江游戏大厅 icon (detect-only) | `Try-LobbyTemplateNav` → `Detect-SaveTemplates` |
| `btn_chi.png` | 吃 — detect+save only | `Detect-ActionButtons` |
| `btn_peng.png` | 碰 — detect+save only | `Detect-ActionButtons` |
| `btn_gang.png` | 杠 — detect+save only | `Detect-ActionButtons` |
| `btn_hu.png` | 胡 — detect+save only | `Detect-ActionButtons` |
| `btn_hu_glow.png` | 胡 (glow) — detect+save only | `Detect-ActionButtons` |
| `btn_guo.png` | 过 — detect+save only | `Detect-ActionButtons` |
| `mark_zhuang.png` | 庄 mark — detect+save only | `Detect-ActionButtons` |
| `avatar_dealer_mark.png` | Dealer avatar mark — detect+save only | `Detect-ActionButtons` |
| `mark_caishen.png` | 财神 mark — detect+save only | `Detect-ActionButtons` |
| `overlays/*.png` | Dry-run / reference shots (not tapped) | dry-run only |

## Functions

- **`Find-Template`** — cv2 match; returns JSON `ok,score,x,y,tl_*,w,h`.
- **`Find-TemplateTap`** — screencap → match → **one** tap at center. Miss = no tap.
- **`Save-MatchCrop` / `Detect-SaveTemplates` / `Detect-ActionButtons`** — observe-only pause crops under `lists/action_detect/` and per-round `action_detect/`. **Never taps** 吃/碰/杠/胡/过.
- **`DismissPromoCloseX`** — up to 3 tries on `btn_close_x`; miss → `NEED_USER_CLOSE` (never 前往游戏/启动 / never blind X).
- **`Close-ReplayBar`** — in-replay close via `btn_close_x` or right edge of `ui_replay_controls`; miss → `TEMPLATE_MISS` (**no** `$X_CLOSE` blind tap).
- **`Try-LobbyTemplateNav`** — sequential `btn_bisai` → `btn_zhanji` → `btn_huifang` → `btn_enter`.
- **`Tap-XiangqingOrEnter`** — `btn_xiangqing` / `btn_enter`; fallback `$X_DETAIL` + `TEMPLATE_MISS`.
- **`EnsureReplayTab`** — `btn_huifang` then legacy coords + `TEMPLATE_MISS`.
- **`ForceDate`** — `btn_date_dropdown` / `ui_date_field` then legacy `$X_DATE` + `TEMPLATE_MISS`.
- **`EnsurePkg`** — on LOST focus: `DismissPromoCloseX` then optional `Try-LobbyTemplateNav`.

## Hard rules

- Package: **浙江游戏大厅** `com.xm.zjgamecenter` only.
- Never tap 开始游戏 / 充值 / 前往游戏 / 启动 / 雷电游戏中心 CTA.
- Action buttons are **detect+save only** during harvest (observe-only).
- `$X_CLOSE/$Y_CLOSE` are legacy reference only — `Close-ReplayBar` must not blind-tap them.
- Nav/date/detail: template first; coords only after `TEMPLATE_MISS` log.

## Dry-run proof (2026-10-16 Asia/Shanghai)

Thresh **0.82**. Full matrix: `lists/_dryrun_alltmpl.txt` on Windows (323 pairs).

### Notable HITs
| Shot | Template | Score | Center |
|------|----------|------:|--------|
| overlays/gc_popup_*.png / lists/_gc_now.png | btn_close_x.png | 1.0000 | (1534,118) |
| overlays/lobby_main.png | btn_bisai.png | 0.9715 | (374,581) |
| overlays/bisai_mine.png | btn_enter.png | 1.0000 | (358,688) |
| _after_close.png | btn_date_dropdown.png | 1.0000 | (400,140) |
| _after_close.png | ui_date_field.png | 1.0000 | (236,142) |
| _after_close.png | btn_xiangqing.png | 1.0000 | (1456,262) |
| frame_06.png (r01) | btn_guo.png | 0.9160 | (1433,604) |

### Best-below-thresh (MISS, expected until matching screen state)
btn_huifang 0.64, ui_replay_controls 0.64, mark_caishen 0.70, btn_chi 0.62, mark_zhuang 0.58, avatar_dealer_mark 0.59, btn_zhanji 0.49, btn_hu/peng/gang/hu_glow <0.52, icon_zjgamecenter 0.53.

Harvest left **STOPPED** (not resumed).
