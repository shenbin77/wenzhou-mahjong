# 录制与数据体系设计：majiang-bot

> 仓库：`/workspace/majiang-bot` @ `main` `8f44474`  
> 目标：可回放、可查询、可算胜率/攻守，且 16 局会话长期不撑爆磁盘  
> 日期：2026-10-15（Asia/Shanghai）

---

## 1. 现有截图 / 日志 / 产物盘点

### 1.1 `.gitignore` 已排除（交接称约 800MB 未进库）

```
pc_bot/artifacts/
pc_bot/frames_raw/
pc_bot/frames_live/
pc_bot/logs/
pc_bot/strips/
pc_bot/harvest/
pc_bot/harvest2/
pc_bot/_old_templates/
.codex_backups/
```

这些目录在本 clone **不存在或为空**（大文件未随 Git 拉取），但代码与工具仍引用它们作为运行时/采集路径。

### 1.2 路径与职责对照

| 路径 | 谁写 | 内容 | Git | 耐久性 |
|------|------|------|-----|--------|
| `pc_bot/logs/bot_YYYYMMDD_HHMMSS.log` | `setup_logger()` in `bot.py` | 文本运行日志（`[TICK]`、识牌、决策、点击） | 忽略 | 中：文本小，易堆积 |
| `pc_bot/logs/stats_YYYYMMDD.jsonl` | `_slog()` in `bot.py` | 统计 JSONL（钩子存在） | 忽略 | 低：调用点稀疏 |
| `pc_bot/logs/rounds_YYYYMMDD.jsonl` | `GameLogger` 构造于 `Bot.__init__` | **设计**为每局一条 `new_round_record` | 忽略 | **缺口：未见 `.append` 调用** |
| `pc_bot/logs/*_err_*.png` / `runtime_guard_*.png` | `save_error_shot` | 异常帧 | 忽略 | 中 |
| `pc_bot/logs/once_*.png` | `--once` | 单次截图 | 忽略 | 低 |
| `brain_v0/data/mahjong.db` | `engine/storage.py` `save_round` / `save_play` | SQLite：`rounds` + `plays` | **未**在根 `.gitignore`（16KB 样例库在库内） | 高意图，字段未满 |
| `ObservationRecorder` 输出目录（`--capture-dir`） | `observation.py` | `frames/<session>/frame_######.png` + `observations_<session>.jsonl` + `status.json` | 视目录位置；README 示例 `pc_bot/artifacts/observations` → 被 ignore | 高（无损 PNG，体积大） |
| `pc_bot/frames_raw/`、`frames_live/` | 采集脚本 / 历史 | 模板溯源截图 | 忽略 | 归档 |
| `pc_bot/harvest/`、`harvest2/` | `harvest*.py` | 聚类裁片 | 忽略 | 归档 |
| `pc_bot/artifacts/` | `tools/rebuild_templates.py` 等 | 视觉验收、旧模板备份 | 忽略 | 归档 |
| `pc_bot/settle_ocr.py` + `templates_new/settle_digits/` | 独立模块 | 结算分 OCR（**未完成**，需正样本字形） | 模板在库；产物无 | 阻塞「真实 deltas」 |
| `pc_bot/strips/` | `strip.py` 等 | 条带裁剪 | 忽略 | 低 |

### 1.3 `bot.py` 已接存储钩子（证据）

```text
round_start  → 清空 _cur_round_plays，记录 _cur_round_start
出牌确认     → _cur_round_plays.append({tile_id, tile_name, player:'bottom', turn_no, timestamp})
round_end    → save_round(record, _cur_round_plays)  # storage.py → SQLite
GameLogger   → 仅构造 LOG_DIR/rounds_*.jsonl，**当前未 append 结局记录**
ObservationRecorder → 仅当 CONFIG['capture_dir'] 非空（观察模式常用）
```

`storage.save_round` 支持 `outcome`（`is_zimo` / `win_type` / seats）与 `deltas` JSON，但 `bot.py` round_end 传入的是：

```python
self_score=None, is_win=None, result='unknown', note='confirmed_bot_actions_only'
```

→ **长期库目前主要是「已确认的自家弃牌流水」**，不是完整胜负番型（与 `storage.py` 模块注释一致：缺实时比分 OCR）。

样例库现状（本 clone）：`rounds` 5 行，`plays` 13 行。

### 1.4 `ObservationRecorder` schema（已有可复用骨架）

`pc_bot/observation.py` `observation/v1`：

- `session_id`, `timestamp`, `source_frame`, `source_sha256`, `frame_pixel_sha256`
- `detected_boxes`, `predictions`, `recognized_tiles`, `all_candidates_classified`
- `candidate_round_event`, `candidate_rounds_started/done`
- `retention_reason`: `candidate_tile_row` | `settlement_template`
- `gold_labels=None`, `review_status=unreviewed`, `outcome=None`
- 去重：像素 hash + `interval`（默认 2.0s）+ 要求 `len(boxes)>=3` 或结算

这已是 **keyframe 序列 + 旁路 JSONL**，应升级为正式录制后端，而不是另起炉灶。

---

## 2. 可持续落盘方案

### 2.1 推荐目录布局（运行时，全部 gitignore）

```text
pc_bot/recordings/                      # 新增根（加入 .gitignore）
  sessions/
    20261015T090000Z_<shortid>/         # = ObservationRecorder.session_id 风格
      meta.json                         # serial, git_sha, max_rounds, observe_only
      status.json                       # 沿用现有原子写
      observations.jsonl                # 关键帧索引（可从 observations_*.jsonl 改名）
      decisions.jsonl                   # 每手决策（新增，热路径写）
      rounds.jsonl                      # 每局结局（接通 GameLogger.append）
      frames/                           # keyframe PNG 或抽帧
        frame_000000.png
      video/                            # 可选：整局或整会话 mp4
        round_01.mp4
      settle/                           # 结算原图 + OCR 原始结果
        round_01.png
  sqlite/
    mahjong.db                          # 或继续 brain_v0/data/mahjong.db，二选一写清
  archive/                              # 冷归档：tar.zst / 外置盘
```

`.gitignore` 追加一行：`pc_bot/recordings/`（保留 `artifacts/` 等旧排除）。

### 2.2 视频 vs Keyframe（选择）

| 模式 | 优点 | 缺点 | 建议 |
|------|------|------|------|
| **每局 H.264 视频**（2–5 fps，1600×900） | 体积可控；人类回放友好 | 需 ffmpeg；单帧精确 seek 稍烦 | **主存档**（可选开启） |
| **Keyframe PNG 序列**（现有 Recorder） | 与识别框/预测一一对应；无损可审计 | compression=1 的 PNG 极大 | **决策相关帧必留**；其余降质 |
| 全量每 tick PNG | 证据最全 | 16 局易到 GB 级 | **禁止**作为默认 |

**推荐默认策略（混合）：**

1. **决策关键帧**：出牌前、点击后确认、动作按钮、结算 —— 无损或 PNG compression≥6 / JPEG q85。  
2. **背景表态帧**：维持 2s interval + hash 去重，但改为 **JPEG q80**（或缩放 0.5× 长存）。  
3. **可选整局视频**：会话结束用 ffmpeg 从 keyframe 或并行 `adb` 录屏生成 `round_XX.mp4`；成功后可删中间「背景 JPEG」。

### 2.3 决策日志 schema（`decisions.jsonl`，每行一手）

```json
{
  "schema": "decision/v1",
  "session_id": "...",
  "round_no": 3,
  "turn_no": 7,
  "ts": "2026-10-15T17:00:00+08:00",
  "hand": [1,2,3,5,5,21,22,41,42,43,55,55,8,9],
  "open_meld_count": 0,
  "played_counts": {"21": 2},
  "shanten": 2,
  "candidates": [
    {"tile": 9, "shanten_after": 2, "ukeire_count": 12, "danger": 0.15, "eff_score": 0.4, "score": 0.31}
  ],
  "choice": 9,
  "fold": false,
  "fold_reason": null,
  "decision_ms": 42.5,
  "frame_ref": "frames/frame_000128.png",
  "tap": {"x": 1200, "y": 820, "confirmed": true}
}
```

**实现要点：** 在 `Brain.decide` 保留完整 `local_advise` 结果再取 top-1（今日 `decide` 丢掉了 candidates/fold——需小改，不改算法）。`advisor.local_advise` 已返回 `shanten`、`options`（含 danger）；`advise` 路径含 `fold`。

### 2.4 结局日志（接通现有 `game_log.new_round_record`）

```json
{
  "ts": 1729000000.0,
  "room": "302805",
  "round_no": 3,
  "dealer": "bottom",
  "winner": 0,
  "fangpao_by": 2,
  "win_type": "point",
  "fans": ["对对胡"],
  "deltas": {"0": 2, "1": 0, "2": -2, "3": 0},
  "hands": {"bottom": [...]},
  "rivers": {"bottom": [...], "top": [...]},
  "engine": "brain_v0",
  "notes": "settle_ocr=pending"
}
```

同时调用 `storage.save_round(..., outcome=..., deltas=...)`，结束「result=unknown」常态。

**依赖：** `settle_ocr.extract_settle_score` 完成 + 胡/点炮座位启发式（结算画面模板）；未完成前允许 `win_type=unknown` 但必须存结算 PNG 到 `settle/`。

### 2.5 结果字段（胜率分析最小集）

| 字段 | 来源 | 状态 |
|------|------|------|
| hu / pao / zimo / draw | 结算 UI + 规则 | 待 OCR/模板 |
| fan 列表 | 结算或本地 `scoring` 重算 | 引擎有；需结局牌面 |
| self_score / deltas | `settle_ocr` + `outcome_to_deltas` | OCR 未完成 |
| opponents 座位弃牌画像 | `session_opponent_profiles(plays)` | **已有纯函数** |
| 自家弃牌习惯 | `get_longterm_stats` | **已有** |

---

## 3. Replay：从录制重建手牌 / 按局查询

### 3.1 重建手牌（优先级）

1. **主路径：** `decisions.jsonl` 中每手的 `hand` + `choice` + `open_meld_count` → 逐步重放暗手。  
2. **校验路径：** 对应 `frame_ref` → `TileVision.recognize_hand` 离线重跑，对比 `recognized_tiles`。  
3. **弱路径：** 仅有 `plays`（storage）时只能重建「已确认弃牌序列」，不能可靠还原中间手牌（缺摸牌观测）——故 **必须落 decisions**。

### 3.2 按局查询

```text
输入: session_id + round_no
  → rounds.jsonl 过滤 round_no
  → decisions.jsonl 过滤 round_no 排序 turn_no
  → frames 按 frame_ref
  → sqlite: SELECT * FROM rounds/plays WHERE id=? / round_id=?
```

CLI 草图（实现阶段，非本次交付）：

```powershell
python pc_bot/tools/replay_round.py --session 20261015T... --round 3
python pc_bot/tools/query_logs.py --day 20261015 --win-type zimo
```

---

## 4. 胜率 / 攻守分析字段

在 `decisions.jsonl` + `rounds.jsonl` 上可算：

| 分析项 | 字段 / 算法 |
|--------|-------------|
| **向听路径** | 每手 `shanten` 时间序列；`shanten_after`（打出后）；听牌时刻 turn_no |
| **危险分** | `candidates[].danger`；实际 `choice` 的 danger 分位（是否总打最危） |
| **攻守切换点** | `fold` False→True 的 turn；`fold_reason` ∈ {`uncle_early_fold`, late fold…}（见 `advisor.should_fold_uncle`） |
| **效率** | `ukeire_count` / `eff_score`；是否选了 options 中最优 eff |
| **局结果** | `win_type`, `focus_hu_rate`, `focus_fangpao_rate`, `avg_net_per_round`（`game_log.stats_from_records` **已实现**） |
| **净 EV** | `storage.get_net_ev`（**已实现**；依赖 deltas 质量） |

**看板：** 可复用 `test_dashboard_api.py` 所测 API 思路；先 JSONL/SQLite 离线报表即可。

---

## 5. 存储预算（16 局会话，长期）

### 5.1 粗算假设

- 分辨率 1600×900；约 16 局/会话；局均有效 ~60–90s；决策 ~10–20 手/局。  
- 决策关键帧：16×15 ≈ 240 帧。  
- 背景 keyframe：2s × 20min ≈ 600 候选，hash 去重后假设 ~300 帧。

| 策略 | 单会话体积 | 100 会话 | 评价 |
|------|------------|----------|------|
| 现状 Recorder 全 PNG compression=1 | 500 帧 × ~2–4MB ≈ **1–2GB** | 100–200GB | 不可长期默认 |
| 关键 PNG + 背景 JPEG q80 | 240×1.5MB + 300×0.3MB ≈ **0.45GB** | ~45GB | 可接受若有月度归档 |
| 关键 JPEG q90 + 背景 JPEG q70 + 每局 mp4 | ~80–200MB | 8–20GB | **推荐默认** |
| 仅 decisions+rounds JSONL + 结算图 | **<20MB** | <2GB | 最小；无视觉复盘 |

### 5.2 配额策略（写进 meta，开机执行）

1. **单会话软上限** 300MB；超限停止背景帧，只留决策帧+结算。  
2. **磁盘水位：** `recordings/` 总用量 > 20GB → 自动把 7 天前 session 打成 `archive/*.tar.zst` 并删原始 frames。  
3. **SQLite** 只存结构化；BLOB 不进库。  
4. **日志：** `bot_*.log` 按天 rotate，保留 14 天。  
5. **禁止**把 `recordings/` 推进 Git；与现有 800MB 排除同一原则。

### 5.3 与现有 800MB 排除的关系

历史 `frames_raw` / `harvest2` / `artifacts` 属于 **模板生产语料**，应：

- 保留在外置归档（如 `D:\archive_温州麻将202608` 同类）；  
- 新实战数据走 `recordings/sessions/`；  
- 不要混回 `frames_raw` 以免工具误删（`collect_v2.py` 有破坏性删除风险，README 已警告）。

---

## 6. 落地优先级（不启动 live bot，仅设计）

| P | 项 | 说明 |
|---|----|------|
| **P0** | 接通 `GameLogger.append` + `decisions.jsonl` | 小改 `Brain.decide`/`play_turn`/`round_end`；零成本、立刻可查询 |
| **P0** | `.gitignore` 增加 `pc_bot/recordings/` | 防止再涨 800MB 进库 |
| **P1** | Recorder：JPEG 档位 + 决策帧强制保留 | 控盘 |
| **P1** | 结算帧必存 + `settle_ocr` 正样本 | 解锁真实 deltas/胜率 |
| **P2** | 每局 mp4 + 归档脚本 | 长期预算 |
| **P2** | `replay_round.py` / 报表 | 胜率与攻守切换分析 |

---

## 7. 与 Grok 替换评估的接口

- **录制/回放/报表**：适合 Grok Bot teammate **离线**跑工具、写分析 MD。  
- **写 decisions / tap 循环**：保持本地进程；agent 不进环。

---

## 8. 一句话

**在现有 `ObservationRecorder` + `storage`/`game_log` 上补全 decisions/结局落盘，默认「决策帧+JPEG+配额归档」，不要再堆无损全量 PNG。**
