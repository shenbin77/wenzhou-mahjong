# Handover — 温州茶麻将 + MX Trader Bridge
Updated: 2026-10-16 ~18:05 Asia/Shanghai (box live check). For another AI continuing if CEO quota runs out.

## Priority now
- **Only 温州茶** (game name 温州麻将). **熟 / 红中放炮 on hold.**
- Harvest replays via 「我的比赛场」→ 详情 → 查看回放. Do **not** real-play until user OK with verification data.
- Emulator/login/harvest clicks: **Windows LDPlayer only**. Box keeps brain / templates / analysis code.
- Do **not** wake idle mahjong teammate bots (麻将代打 / 麻将识牌规则 / 麻将决策) — they burn quota. Run only the active working line.

---

## Red lines (must not violate)
1. **Never** enter 雷电游戏中心; only 浙江游戏大厅 (`com.xm.zjgamecenter`).
2. **Never** tap 充值 / buy room cards / spend in-game. If 可用房卡 = 0 → stop open/join table. Recharge only after user confirms; user prefers WeChat代充 (not game UI).
3. Promo popups (e.g. 全新BOSS/捕鱼侠): dismiss via **top-right X only** (`btn_close_x.png`). Never 立即前往 / CTA.
4. ANR 「浙江游戏大厅没有响应」: tap **等待** first; if stuck → 关闭应用 → relaunch package → resume.
5. Harvest: never 下一局 / 开始游戏. Prefer in-app buttons; max one BACK if stuck.
6. `bot.py` stays `dry_run=True` until explicit user approval for real taps / paid match.
7. Do not replace brain_v0 / tile_vision ~2s loop with LLM. Grok is ops layer only.
8. D:\抖音直播\温州麻将 (box `douyin_extra`) is **熟 footage** — do not train tea rules/vision on it.
9. MX: Chanlun lane must keep **Qwen**, never `chatgpt_plan`. Sole KPI: improve daily pick quality.
10. Do not post routine MX checks into WX ops group; anomalies + one daily summary only.

---

## 温州茶 — code

| Item | Value |
|------|--------|
| GitHub | `shenbin77/majiang-bot` (private) |
| `main` | `8f44474` — **do not push/alter main casually** |
| Active checkout | `/workspace/majiang-bot` @ `offline/tea-rules` `0cb633f` (温州茶 rules + 2p/4p settle hooks) |
| Venv | `/workspace/majiang-bot/.venv` |

### Worktrees / branches (offline)

| Branch | Path | HEAD (approx) | Role |
|--------|------|---------------|------|
| `offline/ceo-priority-5` | `/workspace/majiang-worktrees/ceo-priority-5` | `20ad370` | ROI / 退桌 / click checklist / INTEGRATION_READY |
| `offline/replay-and-hand14` | `/workspace/majiang-bot-wt-replay-hand14` | `588d670` | brain (七对阶段 / 放炮风险 / ukeire) |
| `offline/click-roi-tests` | `/workspace/majiang-worktrees/click-roi-tests` | `b2fbee3` | click ROI offline pytest |
| `offline/templates-and-sampler` | `/workspace/majiang-worktrees/templates-and-sampler` | `8231252` | tea ROI sampler |
| `offline/recog-gold-and-rules` | (was on main tree) | `c99ad0d` | side-seat gold (side line) |
| `offline/tea-rules` | `/workspace/majiang-bot` | `0cb633f` | tea rules package |

Integration notes (no ADB):  
`/workspace/majiang-worktrees/ceo-priority-5/pc_bot/docs/INTEGRATION_READY.md`  
`/workspace/majiang-worktrees/ceo-priority-5/pc_bot/docs/MERGE_INTEGRATION_CHECKLIST.md`  
Older blockers (pre-tea focus): `/workspace/majiang-bot-docs/mvp-blockers.md`

Recommended merge order for 代打联调: ceo-priority-5 base + cherry-pick/merge replay-and-hand14 engine range — see INTEGRATION_READY. Brain + ROI **not fully merged** yet.

Teammate bots (exist; **do not wake when idle**):  
- 麻将代打 `56dff22b-4c1b-45a9-ba63-7b9f5a570125`  
- 麻将识牌规则 `c7352d03-…`  
- 麻将决策 `598aaf27-…`  
User nickname in game: 晓红.

---

## 温州茶 — data & harvest

| Item | Path |
|------|------|
| Primary harvest (Windows) | `C:\Users\Administrator\agent-tools\tea_harvest\` (by date / room) |
| Box sync copy | `/workspace/majiang-data/replay_harvest/tea/` |
| Progress log | `/workspace/majiang-data/replay_harvest/tea/PROGRESS.md` |
| UI templates | `/workspace/majiang-data/replay_harvest/tea/templates/ui/` (esp. `btn_close_x.png`) |
| Continuous harvest script (Win) | `tea/_harvest_continuous_v3_wired.ps1` (ADB LDPlayer14 `emulator-5554` 1600×900) |
| Backups | Windows `D:\GrokBackup\`; box staging `/workspace/backup_staging/` |

### Correct UI path
Lobby → 比赛场 → **菁菁的比赛场 ID 745404** 进入 → 比赛战绩 (trophy) → date → 详情 → 回放 → frames → close via green X / template X.

### Harvest status (box, 2026-10-16)
| Date | On box | Notes |
|------|--------|-------|
| 2026-10-15 | 14 rooms COMPLETE | tar.gz present |
| 2026-10-16 | 1 room (740195) | done |
| 2026-10-12 | 3 rooms | done |
| 2026-10-13 | 3 rooms synced | |
| 2026-10-14 | partial on box (e.g. 400960,578740,614395); more may still be on Windows | continue harvest |

Overnight routine **麻将回放凌晨采集** enabled ~02:00 Asia/Shanghai (skip if疑似实打). Older twin routine paused.

### Open-table rules (decision; from user screenshots)
底分进阶 2468; 16局; 允许续桌; 平摊消耗; 杠算分; 杠上花双翻; 无中发; 半清; 清一色(无序). Common: 2人16局. 2p: no 三背/跟风; 4p: those settle flags on. Official: 有财神; soft/hard/double fan multipliers; 一炮一家胡.

---

## MX Trader Bridge

| Item | Value |
|------|--------|
| Repo | `shenbin77/mx-trader-bridge-private` |
| Box code | `/home/box/mx-trader-bridge` @ `fix/grok-hardening` `c27228d` |
| Config / DB / logs | `/home/box/.mx-trader-bridge/` (`config.json`, `db.sqlite`, `logs/`) |
| Process | supervisord + `deploy/start_all.sh` → `scheduler.py` (RUNNING as of this check) |
| MX API | `https://mkapi2.dfcfs.com/finskillshub` |
| Ownership | Fully on this box (Hana/Muse retired). Report to user. |

### LLM / lanes (non-secret)
- Contestant slot **pro-qwen-plus** → model **qwen3.8-max**, Beijing MaaS base `https://llm-5c7h83v7cw34l14t.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` (prefer over intl pricing).
- Also: pro-agnes, pro-ark-ds, pro-ds-official; Chanlun uses Qwen handoff `context_only`.
- `feishu_external_pro_webhook` intentionally blank (match Hana).
- `auto_trade_enabled=true`, `contest_mode=true`.
- Today’s close marker present: `report_done/2026-10-16.reported`.

Do not dump API keys / webhooks into chat. Keys live only in config.

---

## What to do next (suggested)
1. Finish syncing / harvesting **10-14** (and earlier tea dates) on Windows LDPlayer; keep box `PROGRESS.md` truthful.
2. Offline: merge tea-rules + ROI + brain per INTEGRATION docs; fill settle digit gaps; expand tea UI templates from harvest frames.
3. Real-play gate: user approval + 房卡余额 > 0 + verification metrics. Never self-recharge.
4. MX: keep supervisord healthy; focus pick quality; no spam to Feishu/WX.

## User prefs
- Drive mahjong end-to-end; only check in before real table / spend.
- Night harvest OK (user does not real-play 半夜).
- Cursor account: zhouxiaohon0707@gmail.com; GitHub: shenbin77.
