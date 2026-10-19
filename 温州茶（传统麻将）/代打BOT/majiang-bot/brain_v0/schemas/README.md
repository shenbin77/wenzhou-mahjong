# HandRecord v0 — 离线回测牌谱 schema

权威文件：[`hand_record.v0.json`](hand_record.v0.json)  
版本标记：`"schema": "hand-record/v0"`

回测报告固定格式：[`backtest_report.v0.json`](backtest_report.v0.json)（`"schema": "backtest-report/v0"`）。

本 v0 由离线回测任务起草，并按「麻将识牌规则」2026-10-15 答复 + 同日识牌对齐修订定稿。  
**无单独更权威文档**；金标对齐以本字段表 + 识牌侧确认为准。

## tile_id 目标词表（TARGET_IDS）

与 `brain_v0/engine/tiles.py` 一致，落盘优先 **int**：

| 范围 | 含义 |
|------|------|
| 1–9 | 1–9 万 |
| 21–29 | 1–9 筒 |
| 41–49 | 1–9 条 |
| 55 | 红中（固定财神 / joker） |

输入别名（适配层可接受，**不进权威 JSON 字段**）：

| 别名 | 含义 | 权威性 |
|------|------|--------|
| `w1`…`w9` | 万 | 兼容 |
| `t1`…`t9` | 筒 | 兼容 |
| `s1`…`s9` | 条 | **权威别名** |
| `b1`…`b9` | 条（历史） | **仅兼容、非权威** |
| `hongzhong` / `zhong` | 红中 | 兼容 |

## 三联映射

| 来源 | 角色 | 映射到 HandRecord |
|------|------|-------------------|
| harvest `meta.json` | 局元数据（无事件） | `round.*`：见下表 |
| `pc_bot/logs/decisions.jsonl` | 决策面（建议，非实际） | `decisions[]`：`hand`←`hand14`，`suggested_discard`，…；**无 actual** |
| `replay_analyze` 帧 / `rounds[].hands[]` | 帧面 | `frames[]` + 可由 frame_diff 填 actual |

### harvest meta → round

| meta | hand_record.round |
|------|-------------------|
| `round_uid` | `round_uid` |
| `date, room_id, session_start` | 同名 |
| `players[]` | **UI 列表序，非座位序**；多数局可 `null` |
| `seats: {bottom,right,top,left}` | `seats`（座位→昵称） |
| `user_seat` / 推断 | `user_seat`（通常 `bottom`） |
| `replay_code` | `replay_code` |
| `dealer`（昵称） | `dealer_name`；再由 `seats` 反查 → `dealer_seat`（失败则 null） |
| `frames[], capture_timestamps[], status, user_account` | 同名 |

### decisions.jsonl → decision_point

| decisions.jsonl | hand_record decision |
|-----------------|----------------------|
| `hand14` | `hand` + `is_hand14=true` |
| `suggested_discard` / `suggested_name` | `suggested_discard`（与 actual 分离） |
| `played_counts` | `played_counts` |
| `turn_no` | `turn_no` |
| `open_meld_count` | **权威**副露计数 |
| `shanten` | `shanten_before` |
| （无字段） | `actual_discard=null`，`actual_discard_source="none"`，`actual_missing=true` |

### 副露

- **权威**：`open_meld_count`
- **过渡**：`open_melds` 仅为 `[]` 或 `[{kind:"unknown", tiles:[...]}]`
- **禁止**臆造 `peng` / `chi` / `gang_*` kind（识牌确认前）

### 非 bottom 三家

自动导出默认只填 `bottom`；其余座位省略，或仅给 `unknown_count`。

### frame_diff → actual_discard（稳定规则）

同 `round_uid` 内，时间相邻两帧同时满足：

1. 两帧均为 `complete` 且 `gameplay`
2. `open_meld_count` 不变
3. `concealed` 多重集合恰好少 1 张（`Counter(prev) - Counter(next)` 总和为 1）
4. `len(prev) == len(next) + 1`

则该少掉的一张为 `actual_discard`，`actual_discard_source="frame_diff"`。  
否则 `actual_discard=null`，`source="none"`。

- **禁止**用河牌推 actual  
- `manual_gold` 可覆盖 frame_diff

### replay_analyze frame → frame_snapshot / decision

| frame / hand 字段 | hand_record |
|-------------------|-------------|
| `concealed` | `frames[].concealed`；`is_hand14` 时可生成 decision.hand |
| `shanten` | `frames[].shanten` |
| `hongzhong` | `frames[].hongzhong` / decision.`hongzhong_count` |
| `is_hand14` | 同名 |
| 四家 | 默认仅 `bottom`；见上 |

## 实际出牌（v0 必须预留 + 已接 frame_diff）

每个决策点：

- `actual_discard`: `int | null`
- `actual_discard_source`: `"manual_gold" | "frame_diff" | "none"`
- `actual_action`: `{type, tile} | null`
- `suggested_discard` **不得**当作 actual

回测在 `actual_missing=true` 时：仍跑 bot 建议，一致率记为 **N/A**。

## 事件流（新建约定）

`events[]`：`event_type ∈ {discard,peng,gang,hu,pass,ting,draw}` + `seat` + `tile` + `turn_no` + `source`。  
v0 可空；真值到位后再填。

## 已对齐点（识牌答复 + 2026-10-15 修订）

1. 无统一旧 schema 文档 → 本 v0 为三联起草权威。
2. tile_id 统一 brain_v0 TARGET_IDS；落盘 int；条别名权威为 `s1..s9`。
3. actual 与 suggested 分离；`actual_discard_source` 三态；frame_diff 规则已定。
4. 帧面座位枚举 `bottom|right|top|left`；`seats` 映射 + `user_seat`。
5. harvest meta：`replay_code`、`dealer_name`←`dealer`、`dealer_seat` 反查。
6. 副露权威 `open_meld_count`；`open_melds` 仅 unknown 过渡。
7. 非 bottom 默认不自动导出。

## 待确认项（转识牌金标）

1. `players[]` UI 序与座位的稳定关系（多数局可继续 null）。
2. 副露结构化 kind 何时从 `unknown` 升级为 peng/chi/gang。
3. 非 bottom `tiles[]` 何时开始落盘。

## BacktestReport v0 必填段（顺序）

`schema, generated_at, data_sources, n_rounds, n_decision_points, n_actual_missing, agree_rate, agree_rate_display, shanten_summary, ukeire_summary, points, blockers`  
详见 [`backtest_report.v0.json`](backtest_report.v0.json)。产出目录：`brain_v0/data/backtest/`（`latest` + 时间戳各一份，md/json 双输出）。

## 示例（无 actual）

```json
{
  "schema": "hand-record/v0",
  "round": {
    "round_uid": "346299-2026-10-15-r01",
    "user_account": "晓红",
    "user_seat": "bottom",
    "seats": {"bottom": "晓红", "left": "李哥", "top": "dkl", "right": "啊泽哥"},
    "players": null,
    "dealer_name": "晓红",
    "dealer_seat": "bottom",
    "replay_code": null,
    "status": "captured"
  },
  "decisions": [
    {
      "turn_no": 1,
      "hand": [1, 2, 4, 5, 6, 8, 21, 22, 24, 26, 41, 43, 45, 55],
      "open_melds": [],
      "open_meld_count": 0,
      "played_counts": {},
      "is_hand14": true,
      "joker": 55,
      "suggested_discard": 8,
      "actual_discard": null,
      "actual_discard_source": "none",
      "actual_action": null,
      "actual_missing": true,
      "source": "decisions.jsonl"
    }
  ],
  "frames": [],
  "events": [],
  "notes": "adapted; actual_missing"
}
```
