# MVP dry-run P0 blockers (box @8f44474)

Updated: 2026-10-15 18:24 Asia/Shanghai. Repo `/workspace/majiang-bot` (local dirty: ADB env fallback in `pc_bot/bot.py`, `.venv` gitignore). `dry_run=True` kept hardcoded in `main()` — never spends coins.

## Done on this box

- venv: `/workspace/majiang-bot/.venv` with `opencv-python-headless`, `numpy`, `pytest`
- ADB: prefers `ADB_PATH` / `ANDROID_SERIAL`; else `/workspace/android-sdk/platform-tools/adb` + `emulator-5554` (local only, do not push)
- `bot.py --once` exit 0 while ADB online: brain loads; 0 hand candidates (not in match). Log: `/workspace/majiang_once.log`
- MX supervisord left running (pid 131294)

---

## P0 — stable emulator

1. **Lifecycle flaky**: earlier session showed `emulator-5554 device` @1600x900 then dropped offline; emulator agent restarted with `-wipe-data` (qemu pid ~190905). Boot can take ~4+ min; `sys.boot_completed` often empty while ADB already says `device`.
2. **Black / ANR UI**: prior shots include System UI ANR (`majiang_box_shot.png`) and solid black screencap (`majiang_once_screen.png` during early boot). Focus often `null` until launcher settles.
3. **AVD wipe clears installs**: `-wipe-data` removes `com.xm.zjgamecenter`; APKs sit at `/workspace/majiang_apk/` but are **not installed** after wipe (`pm path` empty).
4. **Do not fight emulator agent**: start/stop/wipe owned by parallel track; bot track only consumes ADB when `get-state == device` and UI is non-black.

**Next:** wait for boot_completed=1 + non-black frame; reinstall channel APK without wipe; pin a durable AVD snapshot (no wipe on restart).

---

## P0 — auto-login

1. **Target package**: `com.xm.zjgamecenter` (浙江游戏大厅). Channel build: `channel_70900_zhejiangyouxi.apk`. Main activity hint: `com.bianfeng.addpermission.ui.AddPermissionActivity` (permission gate before lobby).
2. **Not launched / not installed** on current wiped emu → **no login screen observed this run**. Prior LDPlayer-class shot (`ld_emulator_shot.png`) shows home + interstitial ad overlay (“立即下载”), mahjong tile icon partly obscured — ad/close-X must be dismissed before launch.
3. **No login templates** under `templates_controls_v2` (no btn_login / agreement / captcha / QQ-WeChat). Tencent Open SDK assets present in APK → expect third-party login + permission dialogs on first run.
4. **Entry code does not auto-login**; CLI is observe-only (`dry_run=True`). Login automation is unbuilt.

**Next:** install APK → cold start → capture permission / agreement / account-login frames → add control templates + a dry-run navigation script that **never** taps paid match / 加倍.

---

## P0 — core match loop dry-run

1. **`templates_controls_v2/` incomplete** (dir now exists locally; see SOURCE.md):
   - present: `btn_guo.png`, `btn_xiayiju.png` (copied from `templates_new`)
   - **missing P0**: `btn_hu.png`, `btn_gang.png`, `btn_peng.png`
   - `bot.run()` warns on missing action templates; match loop cannot classify 胡/碰/杠.
2. **Settle OCR glyphs empty**: `templates_new/settle_digits/` has README only — need `0.png`–`9.png`, `plus.png`, `minus.png` from real settlement crops (`settle_ocr.py --train`). Blocks round accounting / 16-局 stop verification.
3. **Not in target match**: `--once` saw 0 tiles. Need logged-in lobby → 温州红中放炮 table (observe-only) before hand ROI / tile_vision validation on target skin.
4. **Tile templates OK for vision QA**: `templates_hongzhong_v2/` has 34 base tiles + variants + manifest (verified path). `templates_new/` is legacy/mislabel risk (missing `t7.png`; has stray btns) — do not treat as label truth.
5. **Coords assume 1600x900**: emu skin matches; still unproven on live target-app skin after install.

**Next (safe dry-run path):**

```bash
source /workspace/majiang-bot/.venv/bin/activate
export ADB_PATH=/workspace/android-sdk/platform-tools/adb ANDROID_SERIAL=emulator-5554
# after emu stable + app installed + logged in + at table (manual or separate nav):
python /workspace/majiang-bot/pc_bot/bot.py --once --serial emulator-5554
# longer observe (still dry_run=True, no taps):
python /workspace/majiang-bot/pc_bot/bot.py --observe-only --serial emulator-5554 --max-rounds 1 --duration 120
```

Capture action-btn + settlement screenshots from a **free/observe** session; fill `templates_controls_v2` and `settle_digits`; never enable real taps / paid match.

---

## Template inventory summary

| Asset | Status |
|-------|--------|
| `templates_hongzhong_v2/` (tile vision) | Present (34 + variants + manifest) |
| `templates_controls_v2/` | Partial: guo, xiayiju only; **missing hu/gang/peng** |
| `templates_new/settle_digits/` | **Empty** (README only) |
| `templates_new/btn_*.png` | guo + xiayiju only (legacy) |
| Login / lobby / ad-close templates | **Missing** |

## Entry points (reference)

- `pc_bot/bot.py` — CLI; `Bot(dry_run=True)` hardcoded
- `pc_bot/tile_vision.py` — detect-then-classify via `templates_hongzhong_v2`
- `pc_bot/adb_transport.py` — ADB screencap; `--adb` / env path
- `brain_v0/engine/` — advisor/shanten (loaded OK in --once)
- `brain_v0/src/config.py` — older region/template aliases; not the live bot path
