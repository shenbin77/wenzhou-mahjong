# Grok 替换评估：majiang-bot 栈是否应由「Grok bot」取代

> 评估对象：`/workspace/majiang-bot` @ `main` `8f44474`  
> 依据：交接文档 + 仓库源码（`pc_bot/bot.py`、`tile_vision.py`、`brain_v0/engine/*`、`observation.py`、`AGENTS.md`、`pc_bot/README.md`）  
> 日期：2026-10-15（Asia/Shanghai）

---

## 0. 名词澄清（本产品语境）

| 说法 | 本评估中的含义 | 是否适合进 2s 出牌环 |
|------|----------------|----------------------|
| **Grok Bot（队友/编排）** | Cursor/Grok Bot teammate agents：写文档、跑测试、改模板、巡检日志、开 PR、远程运维编排；**不是**打牌决策引擎 | 否（也不该进） |
| **LLM API 决策/视觉** | 调用远程大模型（含 xAI Grok chat、多模态 vision）每回合读图或输出「打哪张 / tap 哪」 | 技术上可试，**产品上不推荐** |
| **现有本地栈** | LDPlayer ADB → OpenCV `tile_vision`（34 模板）→ `brain_v0`（shanten/advisor/danger/scoring）→ ADB tap；外层 winagent/pc-server 仅远程运维 | 已是目标架构 |

**结论先说：** 「用 Grok 替换 majiang-bot」若指用 LLM 替代识牌+向听+出牌，应 **不替换**；若指用 Grok Bot 队友做项目运维/联调编排，应 **部分采用（仅 ops）**，与热路径解耦。

---

## 1. 现有分层对照

```
雷电 1600×900
  → ADB screencap          [execution / I/O]
  → tile_vision 34 模板    [vision]
  → Brain.decide → Advisor [decision]
  → ADB tap                [execution]
  → Bot.tick 主循环        [orchestration 热路径]
  → winagent / pc-server   [orchestration 冷路径：远程运维]
```

### 1.1 Vision（识牌）

| 证据 | 路径 / 事实 |
|------|-------------|
| 生效模板 | `pc_bot/templates_hongzhong_v2/`：34 个 canonical PNG + `variants/`（manifest 写 `canonical_labels: 34`, `template_count: 63`） |
| 实现 | `pc_bot/tile_vision.py`（294 行），颜色感知 detect-then-classify |
| 入口接入 | `bot.py`：`from tile_vision import TileVision`；`TileRecognizer(TileVision)` 默认 `CONFIG['tile_template_dir']` = `templates_hongzhong_v2` |
| 门禁 | 完整识牌才允许点击；`valid_target_hand`；歧义/未知拒识（`README.md`） |
| 实机 QA | 交接：17/17 识别，置信度 0.86–0.99，margin > 0.14（2026-10-14） |
| 延迟探针 | `pc_bot/timing_probe.py`：测 `screencap` + `recognize_hand`；`README`：本地决策 26–184ms；截图实例约 0.4–4s |

**LLM 视觉替代代价：** 每 tick 上传 1600×900 图 → 远程推理 → 结构化 13/14 张 tile_id。延迟通常数百 ms～数秒且抖动大；红中财神组合、副露后张数变化、同色近邻（s8/s9 等）需要稳定离散标签，当前模板已有像素级 provenance（manifest 含 `source_pixel_sha256`）。用 LLM 替换等于放弃已验证的确定性分类器。

### 1.2 Decision（决策）

| 证据 | 路径 / 事实 |
|------|-------------|
| 引擎 | `brain_v0/engine/`：`shanten.py`、`advisor.py`、`danger.py`、`scoring.py`、`simulator.py`、`opponent_model.py`… |
| Bot 调用 | `Brain.decide` → `Advisor.local_advise` → 取 `options[0]['tile']`（禁止打财神除非全财神） |
| 攻守 | `should_fold` / `should_fold_uncle`：向听 + 进张 + 回合 → fold 信号 |
| 测试 | `brain_v0/tests/`：约 158 个 `test_*` 用例函数；交接称 `pytest brain_v0/tests` **131 passed, 13 skipped**（2026-10-14）；`pc_bot/tests` 另有 ~85 |
| 规则域 | 112 张、红中=财神、点数制 8 月规则；**离散、可证明、可回归** |

**LLM 决策替代代价：** shanten 是组合搜索/DP（财神枚举已优化）；LLM 无法保证「打出后向听不升」「合法副露张数」「不打唯一红中」。`Brain.decide` 已对非法手牌 / 已胡 / 无候选抛错——这是安全门，不是 prompt 能稳定复现的。

### 1.3 Execution（执行）

| 证据 | 路径 / 事实 |
|------|-------------|
| 传输 | `pc_bot/adb_transport.py`（165 行）：截图 / tap / 连接检查 |
| 点击 | `Bot.do_tap` → `adb.tap`；出牌用牌框中心；动作按钮模板匹配（阈值 0.96） |
| 确认环 | `PendingDiscard` / `PendingPeng`：点击后必须观察到手牌变化，超时停机防连点 |
| 缺模板 | `btn_peng/gang/hu` 等仍可能缺失；`templates_controls_v2` 在本 clone 中未随库出现（gitignore 未排除，可能未提交） |

**LLM 输出 tap 坐标：** UI 为固定 1600×900 ROI，本地已有几何；远程模型输出坐标不稳定，且无「点击后确认」语义除非再写一套状态机——状态机已在 `runtime_guard.py` + `bot.py`。

### 1.4 Orchestration（编排）

| 层级 | 现状 | Grok Bot 适配 |
|------|------|----------------|
| **热路径** `Bot.run` / `tick` | `tick_interval=0.5s`，`capture_interval=2.0s`，16 局停 | **不要**让 LLM/队友进环 |
| **冷路径** winagent / pc-server / MUSE | 远程读盘、截图、键鼠、隧道 | Grok Bot 可编排「重启模拟器、拉日志、跑 pytest、做 shadow 观察」 |
| **项目 ops** | `AGENTS.md` 已定义 agent 协作规则 | **正是** Grok Bot teammate 的定位 |

---

## 2. 延迟 / 成本 / 稳定性

### 2.1 本地环（现状）

| 阶段 | 量级（仓库证据） |
|------|------------------|
| 主循环间隔 | `CONFIG['tick_interval']=0.5`（`bot.py:77`） |
| 观察落盘间隔 | `capture_interval=2.0`（`bot.py:118`） |
| 决策 | `decision_ms` 日志；README：首次本地决策约 **26–184ms** |
| 识牌 | `timing_probe`；README：目标实例首次 screencap 可到 ~4s，稳态目标亚秒～2s 级 |
| 失败模式 | 本地、可复现；测试套件门禁 |

整局节奏按「约 2s 感知循环」设计（交接文档）；决策本身远小于 2s。

### 2.2 远程 LLM 每回合

| 维度 | 评估 |
|------|------|
| 延迟 | 典型 API round-trip 0.5–5s+，超时/限流常见；浙江游戏出牌窗口紧，叠加 ADB 抖动易超时或重复点 |
| 成本 | 多模态按图计费：16 局 × 每局数十次决策 × 全屏图 → 单会话美元级～更高；本地引擎边际成本 ≈ 0 |
| 稳定性 | JSON schema 漂移、幻觉牌、漏财神、坐标漂移；无法用现有 131 项离散测试做同样强度的回归 |
| 结构化命令 | 「稳定发出 `{"action":"discard","tile_id":25}`」在 demo 可，在**连续实机**不可作为唯一真相源；仍需本地校验器——校验器即现有 `brain_v0` + `runtime_guard` |

### 2.3 「LLM 能否稳定给浙江游戏真机发结构化命令？」

**短答：不能作为热路径唯一决策器。**

理由（代码）：

1. `play_turn` 要求 `last_complete` 且 `valid_target_hand`，否则抛错停机——LLM 视觉误差会直接触发停机或误点。  
2. 出牌后有 4s 确认窗（`PendingDiscard`）；延迟抖动会导致「二次点击」或 `RuntimeError`。  
3. 规则引擎已覆盖 shanten/危险/弃胡；LLM 重复实现无测试优势。  
4. 仓库内 **无** 现成 LLM 客户端接入打牌环（全库检索无 openai/anthropic/grok API 打牌路径）。

可接受的窄用途（非替换）：离线审帧、结算 OCR 辅助标注、日志摘要——均在环外。

---

## 3. 方案对比

| 方案 | Vision | Decision | Execution | Orchestration | 推荐度 |
|------|--------|----------|-----------|---------------|--------|
| **A. 不替换** | 保持 `tile_vision` | 保持 `brain_v0` | 保持 ADB | winagent 照旧；Grok Bot 可选做文档/测试 | ★★★★★ |
| **B. 部分替换** | 本地 | 本地 | 本地 | **Grok Bot = 项目 ops/联调编排** | ★★★★★（与先验一致） |
| **C. 全替换** | LLM vision | LLM 决策 | LLM→tap 或自然语言驱 winagent | 全交给 agent | ★☆☆☆☆ |
| **D. 混合热路径** | 本地 | LLM 每手决策 | 本地 tap | — | ★★☆☆☆（延迟+不可回归） |

---

## 4. 对先验判断的核验

> 先验：keep `brain_v0`+`tile_vision` local；Grok Bot teammate only as project ops；Do NOT put LLM in the 2s tap loop for shanten.

| 断言 | 核验 | 证据 |
|------|------|------|
| 保持本地 brain_v0 | **成立** | 158 测例级覆盖；`Brain.decide` 已接 `local_advise`；决策 26–184ms |
| 保持本地 tile_vision | **成立** | 34/63 模板 + manifest 哈希；实机 17/17；旧灰度方案已废弃 |
| Grok Bot 仅 ops | **成立** | 热路径是 Python 进程内循环；agent 适合 pytest、shadow、模板制作、日志复盘 |
| LLM 不进 2s/向听环 | **成立** | 向听是确定性 DP；LLM 无稳定性/成本优势；会破坏 Pending* 确认时序 |

**无需反驳先验；代码证据强化该结论。**

---

## 5. 明确推荐

### 推荐：**部分替换（仅编排/运维层）≡ 热路径「不替换」**

更精确的产品语言：

- **打牌热路径：不替换**（vision + decision + ADB 执行保持本地）。  
- **项目协作冷路径：引入 Grok Bot teammate**（按 `AGENTS.md`：跑测试、只读观察编排、交接文档、审计模板、查 `logs/bot_*.log`）。  
- **禁止：全替换；禁止：LLM 进入出牌/向听热循环。**

### 理由摘要

1. 向听/计分/危险度已是可测离散引擎，不是开放域聊天问题。  
2. 识牌已有像素级模板与门禁，远程多模态无回归优势。  
3. 主循环时序（0.5s tick、点击确认、16 局停）与远程 LLM 延迟模型冲突。  
4. Grok Bot 的比较优势在 **仓库与运维编排**，与 winagent 互补，不替代 `bot.py`。

### 若未来再评估 LLM 的条件（门槛，非现在做）

- 结算 OCR / 牌河跟踪完成且本地胜率分析跑通；  
- 有 shadow 模式录制的金标决策集；  
- LLM 仅作 **shadow 对比**（记录建议但不 tap），且本地引擎仍执行。

---

## 6. 一句话给用户

**不替换打牌栈；Grok Bot 只做项目运维队友，绝不要把 LLM 塞进 2 秒出牌/向听环。**
