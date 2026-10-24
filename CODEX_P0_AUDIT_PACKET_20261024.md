# Codex P0 Audit Packet — 温州茶辅助器（2026-10-24）

时区：Asia/Shanghai（UTC+8）  
包生成：2026-10-24 20:50（Asia/Shanghai）  
执行人：Grok Bot（用户确认「需要」：① follow-session 结构化日志；② 本审计包）  
红线（本包期间仍守）：**CLAIM_TAP_CHANNEL_READY=False**；无 harvest；无 ADB 实机；无删 `采集/`；无 force-push。

---

## 1. 审计范围（给 Codex）

请只审下列 **P0 收口代码路径**（茶传统麻将 bot），不要扩到 harvest / ADB / 删除采集 / 开 CLAIM 通道。

根目录：`D:\温州麻将\`  
Bot：`D:\温州麻将\温州茶（传统麻将）\bot\`  
WIP：`D:\温州麻将\P0_CLOSEOUT_WIP_20261024.md`

本包文件：`D:\温州麻将\CODEX_P0_AUDIT_PACKET_20261024.md`

---

## 2. Commits（P0 closeout 链，`8de3d7b` → `33dce6b`）

| SHA | 时间 (Asia/Shanghai) | 说明 |
|---|---|---|
| `8de3d7b` | 2026-10-24 20:21 | fix(tea): P0 closeout — logical meld counts, latch gate tests, unknown-source abstain |
| `e1b90a3` | 2026-10-24 20:33 | fix(bot): P0-2/3 template default, claim age, logical kong slots, claim-UI gate |
| `bcaab9b` | 2026-10-24 20:33 | fix(bot): P0-4/5 unlabeled caishen nominations, provenance fields, fresh-frame claim path |
| `c71cc50` | 2026-10-24 20:39 | fix(bot): P0-4 caishen heldout/LOSO fail-closed gates + tests |
| `33dce6b` | 2026-10-24 20:40 | P0 chunk A: countdown vision + TableState + wall≤16 / unknown-caishen stop |

分支状态（生成时）：`main...origin/main [ahead 15]`（含上述 5 个 + 更早自动点/牌河等）。

### 未提交（本包之后追加，待 Codex 一并看）

| 路径 | 状态 | 说明 |
|---|---|---|
| `温州茶（传统麻将）/bot/session_logger.py` | 新 | follow-session JSONL（vision/hand、gates/abstain、caishen、decision） |
| `温州茶（传统麻将）/bot/tests/test_session_logger.py` | 新 | logger 单元测 |
| `温州茶（传统麻将）/bot/auto_play_loop.py` | 改 | `_write_event` 双写 `follow_session.jsonl`；header 记 `claim_tap_channel_ready` |
| `温州茶（传统麻将）/bot/tests/test_auto_play_loop.py` | 改 | 双写集成测；断言 CLAIM 仍 False、零 tap |

---

## 3. 测试计数

| 套件 | 结果 | 备注 |
|---|---|---|
| `bot/tests` 全量（chunk A 后，WIP） | **680 passed** | countdown/table_state + wall≤16 / unknown-caishen / turn_gate 接线 |
| 本轮 logger 相关回归 | **71 passed** | `test_session_logger` + `test_auto_play_loop` + `test_countdown_table_state` + CLAIM 断言 |
| 子仓库 `brain_v0` / `pc_bot` | 上一轮 **487 passed / 13 skipped** | 本包未重跑 |

原基线约 662 → chunk A 后 +18 → 680 → follow-session 后 **686 passed**（本机 55.25s）。

硬断言：`tests/test_action_policy.py::test_claim_tap_channel_not_wired_yet` → `CLAIM_TAP_CHANNEL_READY is False`。

---

## 4. 已完成 / 未验收 / 仍不能用

### 已完成（代码绿；实战未验收）

| ID | 内容 | 关键 commit / 文件 |
|---|---|---|
| P0-1 | 杠逻辑位 / expected_concealed / 未知来源弃权 | `8de3d7b` + tea-rules |
| P0-2 | `require_template_match` 默认 True；auto 强制；`max_claim_age_s`；杠按 3 逻辑位 | `e1b90a3` `action_policy` / `turn_gate` / `claim_vision` |
| P0-3 | `countdown_vision.detect_countdown`；`turn_gate.action_ui_evidence` 接线；low/expired/unreadable 只 wait 不 tap | `33dce6b` |
| P0-4 | 财神无标签提名 + provenance；heldout / LOSO fail-closed 闸门 | `bcaab9b` `c71cc50` |
| P0-5 | 统一 `TableState`；牌墙/房卡 ≤16 停台；未知财神停台；判定前本帧刷新 | `33dce6b` `bcaab9b` |
| Follow-log | `follow_session.jsonl` 结构化会话日志（调试用；不开 CLAIM） | 未提交 `session_logger.py` |

### 能用但未验收（缺实战帧 / 模板 / 标定）

- `require_template_match` 默认 + auto 强制（缺真实按钮模板帧）
- 吃碰杠超龄闸门（待 `max_claim_age_s` 实战标定）
- 逻辑位 expected（含杠）与实战一致帧
- 倒计时视觉：ROI+合成数字 TM 能用；缺专用表盘模板 / 真帧标定 → 真帧多落 absent/unreadable
- 统一 TableState + 牌墙≤16 / 未知财神停台（单元绿；待实战房卡读数）
- 财神 heldout 闸门（合成帧过闸；**跨源 heldout 第二套未跑**）
- 判定中本帧刷新再决策（未开 CLAIM 通道实战）
- follow_session 日志落盘（单元/循环测绿；待真会话跟局）

### 仍不能用 / 明确未做

1. **第二来源真实实战标注帧上的 heldout / leave-one-source-out 实跑**（缺素材）。
2. **`CLAIM_TAP_CHANNEL_READY=True`** → **禁止**（本包保持 False）。
3. harvest / ADB 实机 / 删 `采集/` / force-push → **禁止**。
4. 明杠/暗杠/加杠/补杠完整四态矩阵 **实战按钮证据对齐**（引擎 L2 文案有，缺实战矩阵）。
5. 2 人吃 vs 茶 2 实战按钮冲突仍未实盘。

---

## 5. Codex 优先审阅文件清单

### 核心（必读）

| 文件 | 为何 |
|---|---|
| `温州茶（传统麻将）/bot/action_policy.py` | P0-2 模板默认 / 超龄 / CLAIM_TAP_CHANNEL_READY=False |
| `温州茶（传统麻将）/bot/turn_gate.py` | 逻辑位 / claim-UI / countdown 紧急说明 |
| `温州茶（传统麻将）/bot/claim_vision.py` | 行动行视觉 |
| `温州茶（传统麻将）/bot/countdown_vision.py` | P0-3 倒计时（失败封闭） |
| `温州茶（传统麻将）/bot/table_state.py` | P0-5 统一台面态 / wall≤16 / unknown caishen |
| `温州茶（传统麻将）/bot/auto_play_loop.py` | 本帧刷新、停台、claim 只落账、follow_session 双写 |
| `温州茶（传统麻将）/bot/caishen_vision.py` | 财神视觉 + provenance |
| `温州茶（传统麻将）/bot/caishen_validation.py` | heldout / LOSO fail-closed |
| `温州茶（传统麻将）/bot/caishen_collector.py` | 无标签提名 |
| `温州茶（传统麻将）/bot/session_logger.py` | follow_session JSONL schema（**未提交**） |
| `温州茶（传统麻将）/bot/tea_bot.py` | 逻辑副露 / 未知来源弃权 |

### 测试（必读）

| 文件 |
|---|
| `.../tests/test_action_policy.py` |
| `.../tests/test_turn_gate.py` |
| `.../tests/test_countdown_table_state.py` |
| `.../tests/test_auto_play_loop.py` |
| `.../tests/test_caishen_validation.py` |
| `.../tests/test_caishen_coverage.py` |
| `.../tests/test_caishen_vision.py` |
| `.../tests/test_tea_bot.py` |
| `.../tests/test_tea_meld_geometry_groups.py` |
| `.../tests/test_session_logger.py`（未提交） |

### 上下文（可读）

| 文件 |
|---|
| `P0_CLOSEOUT_WIP_20261024.md` |
| `HANDOVER.md`（若需历史约束） |

### 明确排除（不要审成「去实现」）

- `harvest_tea.py` / harvest 会话目录 / ADB 实机流程
- 打开 `CLAIM_TAP_CHANNEL_READY`
- 删除或整理 `采集/`
- force-push / 改远程历史

---

## 6. follow_session 日志契约（给审计）

路径：`<session_dir>/follow_session.jsonl`（与 `actions.jsonl` 并列）  
Schema：`follow_session/v1`

每行（header 除外）含：

- `vision`：status / reason / hand_ids / suggestion*
- `caishen`：label / id / known / source
- `gates`：turn_gate、action_guard、`abstain_reasons[]`、stop_kind
- `table`：wall_cards / countdown_* / incoming_* / notes…
- `decision`：discard|claim|none；claim 决策摘要；**`claim_tap_channel_ready: false`**
- `safety`：`claim_channel_closed`（若常量被误开会标 `UNEXPECTED_CLAIM_CHANNEL_READY`）

**永不**因 logger 发 ADB / 点行动圆盘。

---

## 7. 请 Codex 重点回答

1. P0-2/3/4/5 的 **fail-closed** 是否有缺口（尤其：未知财神、超龄 claim、countdown unreadable、跨局 discard）？
2. `TableState.should_stop_for_wall`（≤16）与手牌 16 wait-only 语义是否被某处混用？
3. heldout/LOSO 闸门在缺第二来源素材时，是否仍保证「过不了就停 / 弃权」而不是静默放行？
4. `CLAIM_TAP_CHANNEL_READY` 是否存在任何旁路（auto_click、tap_executor 白名单、测试 monkeypatch 泄漏）？
5. `follow_session.jsonl` 字段是否足够复盘一局「为何 idle/blocked/stop」？缺什么字段？
6. 未验收项里，哪 1–2 个最值得下一次真机跟局优先取证（仍不开 CLAIM）？

---

## 8. 本包未做事项（诚实清单）

- 未开 CLAIM 通道、未实战点碰/吃/杠/胡
- 未跑第二来源 heldout 真帧
- 未 harvest / 未 ADB 跟局采集新样本
- 未提交 `session_logger` 相关改动（工作区仍脏；由主理人决定何时 commit）
- 全量 `bot/tests` 在 logger 落地后的最终数字：见包末「附录」或稍后补一行

---

*End of packet.*

---

## 附录：本机全量重跑

`
686 passed in 55.25s
`

证据文件：D:\温州麻将\_pytest_followlog.txt
