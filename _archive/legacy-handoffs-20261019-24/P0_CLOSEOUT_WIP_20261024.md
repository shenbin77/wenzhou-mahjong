# P0 收口 WIP（2026-10-24 续，Asia/Shanghai）

执行人：Grok Bot（用户：「Continue remaining P0… Focus chunk A」）。
CLAIM_TAP_CHANNEL_READY：**仍为 False**（未开通道 / 未做实战点）。
更新时间：2026-10-24 20:50（Asia/Shanghai）

## 测试基线（本机全量）

| 套件 | 结果 |
|---|---|
| 主仓库 bot/tests | **686 passed**（chunk A + follow_session.jsonl；含 countdown/table_state + logger） |
| 子仓库 brain_v0 / pc_bot | 本轮未重跑（上一轮 487 passed / 13 skipped） |

原基线 662 → 本轮 +18（countdown/table_state + wall≤16 / unknown-caishen / turn_gate countdown 接线回归）。

## 已完成（代码绿；实战未验收）

### P0-1（此前）
- 杠逻辑位 / expected_concealed_count / 未知来源弃权 @ 8de3d7b + tea-rules。

### P0-2 余（此前 chunk）
- ActionPolicyConfig.require_template_match **默认 True**；uto_click_enabled 时强制 True。
- max_claim_age_s + ClaimState.captured_at：超龄 L1 弃权。
- 	urn_gate.logical_meld_slots：杠按 3 逻辑位算 expected_concealed。
- 明/暗/加杠引擎路径既有 L2 文案已覆盖；2 人吃 vs 茶 2 实战按钮冲突仍未实盘。

### P0-3 余（chunk A）
- 新增 countdown_vision.py：detect_countdown → statuses bsent / present / low / expired / unreadable（失败封闭；无永久 unknown stub）。
- 	urn_gate.action_ui_evidence 接线真实 detector；low/expired/unreadable 写入紧急闸门说明（仍 wait，从不 tap）。
- 实战表盘数字模板仍缺 → 多数真帧落 bsent/unreadable，属「能用未验收」。

### P0-4 余（此前 chunk B）
- 财神自动提名无标签 + provenance + heldout 闸门 fail-closed；第二来源实战帧 / 跨房 heldout 实跑未做。

### P0-5 余（chunk A）
- 统一 	able_state.py：TableState / uild_table_state / WALL_CARDS_STOP_AT=16 / should_stop_for_wall / should_stop_for_unknown_caishen。
- uto_play_loop：决策前用本帧刷新 TableState（resolved caishen = key_caishen or result）；牌墙/房卡 ≤16 → card_exhausted 停止开桌；未知财神 → gate_unknown_caishen 停止。
- 手牌 16 仍为 wait-only（与牌墙≤16 停止语义分离，符合审计「16张停止」= 牌墙/房卡）。

## 能用但未验收

| 项 | 状态 |
|---|---|
| require_template_match 默认 + auto 强制 | 代码绿；缺真实按钮模板帧实战 |
| 吃碰杠超龄闸门 | 代码绿；待实战 max_claim_age_s 标定 |
| 逻辑位 expected（含杠） | 代码绿；待实战一致帧 |
| 判定圆盘可见闸门 | 代码绿；倒计时视觉已接线，真帧数字识别未验收 |
| 倒计时视觉检测 | 代码绿；ROI+合成数字 TM 能用；缺专用表盘模板 / 真帧标定 |
| 统一 TableState + 牌墙≤16 / 未知财神停台 | 代码绿；单元测覆盖；待实战房卡读数 |
| 财神自动提名无标签 + provenance + heldout 闸门 | 代码绿；合成帧过闸；跨源 heldout 未跑 |
| 判定中本帧刷新再决策 | 代码绿；未开 CLAIM 通道实战 |

## 仍不能用 / 未做

1. **第二来源真实实战标注帧上的 heldout / leave-one-source-out 实跑**（P0-4 余；闸门与 fail-closed 桩已绿，缺素材）。
2. **CLAIM_TAP_CHANNEL_READY=True** 开通道 → 明确禁止。
3. harvest / ADB 实机 / 删 采集/ / force-push → 明确禁止。
4. 明杠/暗杠/加杠/补杠**完整四态矩阵实战按钮证据对齐** → 引擎有 L2 文案，缺实战矩阵。

## 红线（仍守）

- 未开 CLAIM_TAP_CHANNEL_READY
- 未 harvest / 未 ADB 实机
- 未删 采集/
- 未 force-push

## 本轮触达文件（chunk A，待提交）

- 温州茶（传统麻将）/bot/countdown_vision.py（新）
- 温州茶（传统麻将）/bot/table_state.py（新）
- 温州茶（传统麻将）/bot/tests/test_countdown_table_state.py（新）
- 温州茶（传统麻将）/bot/turn_gate.py（countdown 接线 + 紧急说明）
- 温州茶（传统麻将）/bot/auto_play_loop.py（TableState / wall≤16 / unknown-caishen）
- 温州茶（传统麻将）/bot/tests/test_turn_gate.py
- 温州茶（传统麻将）/bot/tests/test_auto_play_loop.py
- P0_CLOSEOUT_WIP_20261024.md

## Follow-session 日志（2026-10-24 20:50 追加）

- 新增 session_logger.py → <session>/follow_session.jsonl（vision/hand、gates/abstain、caishen、decision）
- uto_play_loop._write_event 双写；**CLAIM_TAP_CHANNEL_READY 仍 False**
- 测试：	ests/test_session_logger.py + 	est_follow_session_jsonl_dual_writes_without_claim_channel
- 全量：**686 passed**
- Codex 审计包：CODEX_P0_AUDIT_PACKET_20261024.md
