# 发牌规律验证方案（温州红中放炮）

状态：**仅方案 + 可选空壳计数接口**。  
当前 harvest meta **32 局**，其中 **captured=3 / pending=29**，帧极少，**不足以做统计结论**。本阶段不下「公平/不公平」判定。

账号焦点：`晓红`。玩法：红中为财神（tile_id=55），牌山 112 张（27 种×4 + 红中×4）。

## 1. 需要采集的字段

每局至少一条「起始手牌」记录（建议写入 HandRecord v0 `events` 或独立 `deal_samples.jsonl`）：

| 字段 | 说明 | 优先级 |
|------|------|--------|
| `round_uid` | 与 harvest meta 一致 | 必采 |
| `ts` / `capture_timestamps` | 开局或首帧时间戳 | 必采 |
| `user_account` | 如 晓红 | 必采 |
| `seat` | `bottom\|right\|top\|left`（关注座） | 必采 |
| `dealer_seat` | 庄 | 必采 |
| `starting_hand13` | 起手 13 张 tile_id 列表 | 必采 |
| `starting_hongzhong` | 起手红中张数（0–4，可由 hand 推导） | 必采 |
| `room_id` / `date` / `round_index` | 局元数据 | 必采 |
| `wall_remaining_visible` | 牌山剩余是否对 UI 可见；可见则记张数 | 尽量 |
| `wall_tiles_if_visible` | 若回放露牌山则记录（少见） | 可选 |
| `players[]` | 四家昵称 | 必采 |
| `seat_hands` | 回放若露四家起手：各 seat 的 13 张 | 高价值 |
| `recognition_confidence` | 识牌置信/完整度，过滤脏样本 | 建议 |

数据源对齐：

- harvest `meta.json`：局级字段已有，**无起手牌**。
- `replay_analyze` 帧：`concealed` 多为中盘 13 张；需用**最早 gameplay 帧**近似起手，并标记 `starting_approx=true`。
- 真起手：回放开局暂停帧或 `frame_diff` 金标。

## 2. 样本量建议（粗检验力）

理论：每人起手 13 张，自 112 张牌山无放回抽取；红中共 4 张。

关注一维：起手红中数 \(X \sim \mathrm{Hypergeometric}(N=112, K=4, n=13)\)。

| \(k\) | \(P(X=k)\)（约） |
|-------|------------------|
| 0 | 0.607 |
| 1 | 0.325 |
| 2 | 0.062 |
| 3 | 0.005 |
| 4 | ≈0.0001 |

粗算（χ² 拟合优度，df=3 合并 k≥3，α=0.05）：

| 目标 | 建议局数（关注一座） | 说明 |
|------|----------------------|------|
| 仅发现极端偏（如红中均值偏离 >0.15） | **≥80–100** | 与 deprecated `min_rounds=100` 同量级 |
| 稳定估 P(X=0), P(X=1) ±0.05 | **≥200** | |
| 花色/序数均匀 + 座位偏差 + 多重校正后仍有功率 | **≥400–500** | 四家都有起手时可按 4n 计「座位-局」样本 |
| 当前 | **3 captured** | 只可做管道冒烟，**禁止下结论** |

四家起手均可见时，同一局提供 4 条相关样本（非独立）；推断时用 **按局 cluster** 或只分析 focus seat。

## 3. 检验方法

### 3.1 起手红中分布

- 零假设：\(X \sim Hypergeometric(112,4,13)\)（若确认庄多摸一张等规则，则庄用 n=14 或「13+首摸」分开建模）。
- 统计量：χ² 拟合优度（k=0,1,2,≥3）；或精确多项/蒙特卡洛（因期望次数小）。
- 报告：观测频数、期望频数、p 值、效应量（TV 距离 / 均值差）。

### 3.2 花色 / 序数均匀性

- 起手中万/筒/条张数 ~ 近似 \(\mathrm{Multinomial}\) 或对每种 tile_id 的边际 Hypergeometric(112,4,13)。
- 序数 1–9：在门内是否近似均匀（条件于花色张数）。
- KS 或 χ²；低计数用蒙特卡洛。

### 3.3 座位偏差

- 比较四家起手红中均值、向听分布（需识牌四家）。
- 庄 vs 闲：若庄起手多 1 张，模型分开。
- 关注账号座位是否系统更好（需控制庄闲）。

### 3.4 与随机洗牌 null

- 原假设：整副 112 张均匀随机洗牌后按规则发牌。
- 蒙特卡洛：模拟 M=10000 次发牌，得到检验统计量的 null 分布（红中频数向量、最大座位均值差、自相关等）。
- 自相关 / 连胜：参考 deprecated `dealing_structure_check`（输后必赢等），但那是**胜负**不是发牌；发牌侧可用「连续局起手红中」的 lag-1 相关。

### 3.5 多重检验校正

- 预先登记主检验：**focus seat 起手红中 χ²**（或蒙特卡洛 p）。
- 探索性：花色、序数、座位、庄闲、时间段 → Benjamini–Hochberg FDR 或 Bonferroni。
- 文档中区分 **confirmatory vs exploratory**。

### 3.6 数据质量门禁

- 仅用 `status=captured` 且起手识别完整（13 张、无非法 id、同牌≤4）。
- `starting_approx` 样本与金标分开分层，不混报。

## 4. 当前数据结论边界

```
meta 局数     = 32
captured      = 3
pending       = 29
decisions.jsonl 决策行 = 3（同一手重复；且无 actual）
replay n_hand14 = 0（样例 replay_latest）
```

→ **本阶段只交付本方案 + 空壳计数脚本**；输出 `verdict=insufficient_data`，与 deprecated fairness 冒烟一致。

## 5. 空壳脚本接口

`brain_v0/offline_tools/deal_fairness_counter.py`：

- 输入：目录或 JSONL（含 `starting_hand13` / 或 HandRecord）。
- 输出：`{verdict, n_rounds, hongzhong_hist, note}`；n < `min_rounds`（默认 100）→ `insufficient_data`。
- **不**对发牌机制下结论；不读 ADB/实盘。

## 6. 落地后验收

1. 采集满 ≥100 局 focus 起手金标（或四家可见等价独立信息）。
2. 主检验 p 与效应量写入 `brain_v0/data/fairness_report_*.json`。
3. 与 HandRecord v0 / 识牌 schema 字段名一致后再合并进回测管道。
