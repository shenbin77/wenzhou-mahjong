# 模板缺口总表（TEMPLATES_GAP）

> 更新日期：2026-10-15（盒子离线）  
> 范围：`pc_bot/templates_new/` 结算数字 + 动作按钮；与 `settle_digits/GAP.md`、`BUTTONS_GAP.md` 对齐。

## 总览

| 缺项 / 模板 | 状态 | 能补的帧 / 路径 | 卡点 |
|-------------|------|-----------------|------|
| `settle_digits/plus.png` | **已有** | `hana_runtime/frames_raw/f41.png`（底家 +3587） | 待目标场皮肤验收 |
| `settle_digits/minus.png` | **已有** | f41 左家 −2153 | 青白 3D，靠二值形状匹配 |
| `settle_digits/0.png` | **已有** | f41 顶家 +0 | |
| `settle_digits/1.png` | **已有** | f41 右家 −1435 | |
| `settle_digits/2.png` | **已有** | f41 左家 −2153 | |
| `settle_digits/3.png` | **已有** | f41 底家 +3587 | |
| `settle_digits/4.png` | **已有** | f41 右家 −1435 | |
| `settle_digits/5.png` | **已有** | f41 底家 +3587 | |
| `settle_digits/6.png` | **已有** | `frames_raw/f69.png` 左家 −3376 | 2026-10-15 晚补入；待验收 |
| `settle_digits/7.png` | **已有** | f41 底家 +3587 | |
| `settle_digits/8.png` | **已有** | f41 底家 +3587 | |
| `settle_digits/9.png` | **缺** | 尚无；需新结算全屏含字形 9 | 现有 f41 系 / f69 分数未出现 9；**禁止伪造** |
| `btn_guo.png` | **已有** | 动作条青色圆形「过」 | 见 `BUTTONS_GAP.md` |
| `btn_xiayiju.png` | **已有** | 结算「下一局」 | |
| `btn_hu.png` | **缺** | 无实战「胡」钮帧 | 搜遍 frames_raw / live / replay_harvest / captures / stash；需真打出现动作条 1600×900 再裁 |
| `btn_gang.png` | **缺** | 无实战「杠」钮帧 | 同上 |

## 仍缺清单（短）

1. **settle 9** — `templates_new/settle_digits/9.png`
2. **btn_hu** — `templates_new/btn_hu.png`（或 bot 配置实际路径）
3. **btn_gang** — `templates_new/btn_gang.png`

## 相关路径

| 用途 | 路径 |
|------|------|
| 结算 OCR | `pc_bot/settle_ocr.py`（`SEAT_SCORE_ROIS` / `extract_score_in_roi`） |
| 数字模板目录 | `pc_bot/templates_new/settle_digits/` |
| 数字缺口详表 | `pc_bot/templates_new/settle_digits/GAP.md` |
| 按钮缺口 | `pc_bot/templates_new/BUTTONS_GAP.md` |
| 已知结算样例 | `…/hana_runtime/frames_raw/f41.png`（及 f42/f43）、`f69.png` |
| 批处理报告 | `pc_bot/logs/settle_ocr_batch/settle_batch_YYYYMMDD.{json,md}` |

## 补齐建议（模拟器/真机稳后）

1. 免费局打到带 **9** 的结算分（或含 9 的其它座），1600×900 全屏 → `settle_ocr.py --train` → 标入 `9.png`。
2. 对局出现「胡 / 杠」动作条时截全屏，裁圆形按钮入 `btn_hu.png` / `btn_gang.png`。
3. 补齐后跑 `pc_bot/tools/batch_settle_ocr.py` 与点击/ROI 离线 pytest 做回归。
