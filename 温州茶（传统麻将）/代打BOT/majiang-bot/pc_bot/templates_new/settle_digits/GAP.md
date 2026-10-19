# settle_digits 缺口与进度（2026-10-15 盒子补齐）

`settle_ocr.py` 期望路径：`pc_bot/templates_new/settle_digits/{minus,plus,0..9}.png`（白字黑底二值）。

## 本批已补（来源结算帧，**待目标场验收**）

来源全屏：`/workspace/majiang-data/hana_runtime/frames_raw/f41.png`（同内容近帧 f42/f43；我方分约 **+3587**，另有 −2153 / −1435 / +0）。

| 文件 | 裁切来源区 | 备注 |
|------|------------|------|
| plus.png | 底部金色 +3587 的 `+` | 金黄 3D 字 |
| minus.png | 左侧青色 −2153 的 `−` | 青白 3D 字；与 plus 色不同，匹配时靠二值形状 |
| 0.png | 顶部 +0 | 金色 |
| 1.png | 右侧 −1435 | 青色 |
| 2.png | 左侧 −2153 | 青色 |
| 3.png | 底部 +3587 | 金色 |
| 4.png | 右侧 −1435 | 青色 |
| 5.png | 底部 +3587 | 金色 |
| 7.png | 底部 +3587 | 金色 |
| 8.png | 底部 +3587 | 金色 |

## 仍缺

| 文件 | 卡点 |
|------|------|
| 6.png | 现有唯一带分数的 1600×900 结算帧（f41 系）四家分数为 +3587/−2153/−1435/+0，**未出现 6** |
| 9.png | 同上，**未出现 9** |

## 已搜且不可用 / 不足

- `templates_new/settle_digits/` 补齐前仅有 README
- `pc_bot/numbers/`：牌面聚类切片，非结算分数字形
- `archive_lab/tools/settle_templates/`：仅 dianpao/zimo_region
- `artifacts/.../settlement_candidate_*.png`：录像误检（大厅/闪屏），非目标结算
- 大量 `frames_raw` 的 xiayiju 帧：有「下一局」，默认分数 ROI 落在手牌区，无 ± 分
- gold_evidence 中多数「自摸」尾帧为黑屏或非分数结算 UI

## 模拟器稳后补 6/9 与验收

1. 人工进免费局至出现带分数结算页（勿花币/勿真金），1600×900 全屏
2. 采样：`cd /workspace/majiang-bot && .venv/bin/python pc_bot/tools/sample_action_settle_rois.py --mode settle`
3. 训练切片：`.venv/bin/python pc_bot/settle_ocr.py --train pc_bot/captures/samples/settle/settle_full_*.png`
4. 将 `/tmp/glyphs/g*.png` 中缺的 6/9（及更清晰 ±）标入本目录；标注「目标场皮肤待验收」

注意：默认分数 ROI `(640,630)-(980,790)` 在 f41 上只盖住 +3587 的前几位；模板齐后若仍识不出，需另调 ROI（不在本轨道改引擎逻辑时可先人工扩 ROI 复检）。

## 2026-10-15 晚：0–8 跑通验收（盒子）

- 生产 `settle_ocr.py` 已对齐修正 ROI `(800,600)-(1080,710)` + minus 短横保留 + `SEAT_SCORE_ROIS` / `extract_score_in_roi`。
- 复现日志：`pc_bot/logs/settle_ocr_run/settle_ocr_20261015.{md,json}`
  - f41 bottom **+3587**；left **-2153**；top **0**
  - f69 left **-3376**（命中 digit **6**）
- **仍缺 9.png**：现有结算帧未出现字形 9，**禁止伪造**。下一批有含 9 的结算全屏后再补。


## 2026-10-16 17:59
- 用户裁图补 **9.png**（来源点炮 −9 青色，房间 614395 第2局）；已从 −9 整图右裁数字 9。
- 仍建议二值化对齐 settle_ocr 白字黑底后再验收。
