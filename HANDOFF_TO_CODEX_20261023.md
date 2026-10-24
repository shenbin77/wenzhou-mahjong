# 交接给 Codex（2026-10-23 深夜，由 WorkBuddy 团队产出）

接手前先读根目录 `AGENTS.md`、`OPENCODE_HANDOFF_20261023.md`、`SPECS/QUEUE.md`、`SPECS/DIAG-20261023-live-all-abstain.md`。本文件是今晚（10-23 19:00–23:15）实战联调之后的最新交接，旧文件里的过时结论以本文件为准。

## 1. 用户当前目标（今晚升级）

1. **茶 2 人局辅助器要能"帮我打"**：用户两个号都是自己的，自对打。已走完"只读建议"→"半自动代点"，**全自动代打是明确授权的下一步**。用户已明确授权消耗房卡（原话："如果你不用担忧房卡…房卡都没关系"）。
2. 红线（写死在 tap_executor 黑名单里，不可商量）：充值按钮、开始游戏按钮、雷电游戏中心**绝不点击**；房卡为 0 立即停手；不留任何绕过入口。

## 2. 关键认知（今晚实测出来的，接手者必读）

- **财神每局翻定且牌种随机**，小牌渲染与回放帧存在系统性灰度偏差。旧回放模板在实战上全部 ~0.90 分，过不了 0.98。**唯一解法是实战来源模板**，不能用回放录像补（有损压缩是根因）。
- **手牌里的财神牌被游戏高亮成灰底 + 红"财"角标**，普通模板匹配只有 ~0.27 分。已由 `caishen_badge()` 判定角标后按已知财神 label 计入（`reason='caishen_marked'`）。财神 label 未知时只上报、绝不填牌。
- **整手压暗（对手回合）必须弃权**：亮度补偿只允许作用于命中角标的单张牌。这是安全契约，`test_dimmed_two_player_replay_hand_abstains` 等 2 条测试锁着，不许改判。
- **茶局有暗杠**：4 张同牌叠放，屏幕只显示 3 个牌位 → 识牌少 1 张、闭手张数变成 15/12 等。副露引擎必须把"手牌区 3 个连续同 label 框"当暗杠信号（meld_count+1），**不要**把第 4 张塞回手牌。
- **副露（吃/碰/杠）是当前最大缺口**：`calc_shanten` 没有 meld 参数，`TeaBot.see()` 遇到明牌组直接抛"检测到明牌组"。用户实战约四成帧因此无建议。
- **半自动点牌必须有建议才可点**：用户按空格"没反应"的原因就是财神或识牌弃权 → 无 `suggested_boxes` → `plan()` 校验直接拒绝。这是设计行为，不是 bug。

## 3. 今晚已完成（全部有实测数据）

| 项 | 内容 | 验证 |
|---|---|---|
| 财神实战模板 ×15 | s4/s5/t3/dong/nan/w1/w9/w8/s3/t2(×2)/t4/w7/w2，manifest 45 条，逐张 8x 目视核对 | 4 局 861 帧：708 OK，125 非牌桌帧，仅 28 帧失败 |
| 财神高亮牌计入 | `tile_vision.py` `caishen_badge()` + 亮度归一化，opt-in（传 label 才生效） | 205345 帧闭手 11/11；金标 3 帧 wrong=0 |
| 财神不可用时仍显示识牌证据 | `assist_tea.py` `_abstain_with_vision_evidence()`，只 `see()` 不 `think()`，`advice_blocked_by="caishen_unconfirmed"` | 新增回归通过 |
| 手选财神放行 | 第 184 行门禁改为 `and not caishen_label`；手选=人工标注真值 | 新用例 `test_manual_caishen_substitutes_only_when_marker_is_unreadable` |
| 换局清空手选 | `assist_window.py` `_forget_manual_caishen()`（554-566 行），防跨局串财神 | 新用例锁定 |
| **半自动点牌** | `bot/tap_executor.py` + 窗口"允许代点"勾选框 + 空格确认按钮 | 5 条离线测试全过；全量 4F/164P 基线不变 |

**tap_executor 四道安全闸**（改任何一条先想清楚）：① `enabled` 只认显式 bool，缺省 dry_run 零 adb；② 几何白名单（y≥720 且在手牌 box 内，离边 ≥2px）——充值/开始游戏/游戏中心无实测像素，先用几何兜底，**日后补显式像素黑名单**；③ 点击前重新截屏并按牌面 label 重新定位，找不到零点击；④ 点击后必须手牌数 −1，否则 `TapUnverified` 报错**不重试**。

## 4. 进行中（三位工程师的原任务书在本团队记录里，要点如下）

1. **副露引擎**（最重要）：`brain_v0/engine/tea/shanten.py::calc_shanten` 加 `meld_count`（向 4−n 面子+1 将收敛，不要塞虚拟牌）；`validate_tiles` 按 16/17−3n 放宽但拒非法形态；`pa_analyze` 可暂不支持副露但胡牌结论必须显式标注；暗杠算术见第 2 节。验收用真实帧：`采集/assist_sessions/{20261023_201640,20261023_220643,20261023_223759,20261023_225317}`。
2. **财神自动收集**：用户手选财神=人工标注 → `bot/caishen_collector.py` 捕获 ROI 到 `pending/`，复核命令确认后才入 manifest；同 label 一次会话只收一次；触发条件=识别失败+角标存在+手选合法。
3. **模板缓存自动失效**：`caishen_vision.py::_templates` 的 `lru_cache` 改为按 manifest `(mtime_ns, st_size)` 失效——否则每次补模板用户都要重启。
4. **桌面牌况记录**（可后置）：双方副露 + 牌河，append-only JSONL，纯旁路不接决策。

## 5. 测试基线与环境坑

- `cd 温州茶（传统麻将）` 后 `python -m pytest bot/tests -q -p no:cacheprovider`：**基线 4 failed / 165 passed**。4 个失败全是 `test_spec005_live_v2.py` 的 `_FakeWindow` 缺 `_result_source`（既有），别修也别让它变多。
- 跑前必须清理 `.pytest_tmp/basetemp`（残留 → setup 阶段 `FileExistsError` → 级联几十个假 error）。建议把 `rm -rf .pytest_tmp/basetemp` 写进跑测流程。
- **多人并行改文件时全量数字不可信**（测试文件会在运行中被替换）。以"失败集合"为准，最终验收等所有人停手。
- `cv2.imread` 读不了中文路径，必须 `cv2.imdecode(np.fromfile(p, np.uint8), ...)`。
- 测试期间 `harvest` 用例按磁盘产物自增参数化（总数 165↔175 漂移），比数字无意义，比对失败集合。
- 子仓库测试：`cd 代打BOT/majiang-bot && python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider`。

## 6. 数据资产（都是实战 ADB 原生 1600×900 PNG，未覆盖未删除）

- `采集/assist_sessions/20261023_201640`：324 帧（含 36 帧带副露、退大厅段）
- `20261023_220643`：80 帧（吃×2 局面）
- `20261023_223759`：339 帧（暗杠局面、财神 w2）
- `20261023_225317`：44+ 帧（财神 w2、亮牌 16 张无副露，适合做"能出建议"的正例）
- 窗口进程每秒左右抓一帧，`recognition_audit.jsonl` 逐帧记录弃权原因与识牌诊断，**排查先读它**。

## 7. 半自动使用说明（可原样给用户）

1. 桌面快捷方式启动辅助器（`pythonw`，改代码后必须重启）。
2. 玩法选"温州茶·2人"，填账号标记，勾"已核对本局玩法"，点"开始跟随"。
3. 财神自动识别（识别不出就下拉手选——手选有效且会自动成为模板采集素材）。
4. 勾「允许代点（半自动）」→ 出现绿色"打X" → 按空格 → 它重截屏、重新定位、点击、验证手牌少一张。
5. 一吃碰/杠就无建议（副露引擎未完），那几手自己打。

## 8. 建议的接手顺序

1. 副露引擎（含暗杠）——做完用户"四成时间没建议"的问题消失，这是全自动的前置。
2. collector + 缓存失效——财神模板自动收敛，用户不再重启。
3. 全自动循环：在 tap_executor 上加"wait/advise 状态机 + 弃权即停 + 房卡监测"，**必须先让用户用半自动跑满一局且零错点**才允许上全自动。
4. 桌面牌况、牌河识别，最后。

## 9. 备份与回退

- 所有改动都有 `.before` 备份在 `_backups/20261023_*/`：`caishen_live_tpl/`、`caishen_marked_tile/`、`caishen_fields/`、`tap_executor/`、`meld_fix/`（后者经查非缺陷，文件未改）。
- 根仓库有大量既有未提交改动，**不要**清理、重置或批量提交。
