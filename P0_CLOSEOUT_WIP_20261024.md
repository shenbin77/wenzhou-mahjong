# P0 收口 WIP（2026-10-24 续，Asia/Shanghai）

执行人：Grok Bot（用户：「继续收口」）。
CLAIM_TAP_CHANNEL_READY：**仍为 False**（未开通道 / 未做实战点）。
更新时间：2026-10-24 20:38（Asia/Shanghai）

## 测试基线（本机全量）

| 套件 | 结果 |
|---|---|
| 主仓库 `bot/tests` | 上一轮 **662 passed**；本 chunk 财神专项 **58 passed**（validation+coverage+collector+vision） |
| 子仓库 brain_v0 / pc_bot | 本轮未重跑（上一轮 487 passed / 13 skipped） |

原基线 656 → 本轮 +6（新增 default/age/kong/claim-ui 等回归）。

## 已完成（代码绿；实战未验收）

### P0-1（此前）
- 杠逻辑位 / `expected_concealed_count` / 未知来源弃权 @ `8de3d7b` + tea-rules。

### P0-2 余（本轮）
- `ActionPolicyConfig.require_template_match` **默认 True**；`auto_click_enabled` 时强制 True。
- `max_claim_age_s` + `ClaimState.captured_at`：超龄 L1 弃权。
- `turn_gate.logical_meld_slots`：杠按 3 逻辑位算 `expected_concealed`（物理 `meld_total` 仍记 4）。
- 明/暗/加杠引擎路径本轮未改（既有 L2 文案已覆盖）；2 人吃 vs 非 2 实战按钮冲突仍未拍板。

### P0-3（本轮部分）
- `tap_executor` 两下 + 抬起确认：**沿用既有绿测**（未改行为）。
- `turn_gate`：行动圆盘可见 → `gate_claim_ui` 等待，不出牌。
- `countdown_status=unknown` 证据桩已落；**倒计时视觉检测仍未接线**。

### P0-4（本轮部分）
- `collect_auto_candidate`：high/low/unknown **一律无标签提名**（`suggested_label`+topk，不许 top1 当金标）。
- `review_caishen_pending --confirm` 写入 `source_kind/review_method/reviewer/source_frame_hash/heldout_passed`；缺见证帧时 **fail-closed 保持 False**。
- 新增 `caishen_validation.py`：`check_witness` / `source_replay_check`（同源自匹配永不为 heldout）/ `evaluate_promotion_heldout` / `leave_one_source_out_or_fail_closed`。
- `review --witness` + `--require-heldout`：无独立见证则拒绝入库；默认仍可写 provisional（`heldout_passed=False`）。
- 单测：`tests/test_caishen_validation.py`（合成帧测闸门，**不发明第二套生产金标**）。
- **第二套真实实战金标帧 / 跨房间 heldout 实跑仍未做 → 仍不能宣称财神泛化通过**。

### P0-5（本轮部分）
- `auto_play_loop`：行动行路径**本帧 analyze** 后再 `decide_action`，不再用上一帧 `_last_result`。
- `assist_window` abstain → `_invalidate_live_advice`：**此前已有**，本轮未再改。
- 统一 `TableState` / 16 张停止 / 未知财神停机：**未做**。

## 能用但未验收

| 项 | 状态 |
|---|---|
| require_template_match 默认 + auto 强制 | 单测绿；缺真实按钮模板帧实战 |
| 声称行时间戳闸门 | 单测绿；需实战 `max_claim_age_s` 标定 |
| 逻辑位 expected（含杠） | 单测绿；需实战一杠帧 |
| 行动圆盘挡出牌 | 单测绿；倒计时未识别 |
| 财神自动提名无标签 + provenance + heldout 闸门 | 单测绿（合成帧）；真金标 heldout 未跑 |
| 行动行本帧刷新再决策 | 单测绿；未开 CLAIM 通道实战 |

## 仍不能用 / 未做

1. **倒计时视觉检测**（P0-3 余）——无模板/无 ROI，只有 `unknown` 桩。
2. **第二套真实实战金标帧上的 heldout / leave-one-source-out 实跑**（P0-4 余；闸门与 fail-closed 桩已落，缺真金标）。
3. **统一 TableState + 16 张停止 / 未知财神停机台账**（P0-5 余）。
4. **CLAIM_TAP_CHANNEL_READY=True** —— 明确不做。
5. harvest / ADB 实点 / 删 `采集/` / force-push —— 明确不做。
6. 明杠/暗杠/加杠/补杠**分类型状态机与实战按钮证据对齐** —— 代码有 L2 分支，缺实战矩阵。

## 红线（仍守）

- 未开 `CLAIM_TAP_CHANNEL_READY`
- 未 harvest / 未 ADB 实点
- 未删 `采集/`
- 未 force-push

## 本轮触及文件（P0-4 chunk B 拟提交）

- `温州茶（传统麻将）/bot/caishen_validation.py`（heldout / LOSO fail-closed 闸门）
- `温州茶（传统麻将）/bot/caishen_vision.py`（校验依赖，此前未入库）
- `温州茶（传统麻将）/bot/review_caishen_pending.py`（`--witness` / `--require-heldout`）
- `温州茶（传统麻将）/bot/tests/test_caishen_validation.py`（新）
- `温州茶（传统麻将）/bot/tests/test_caishen_coverage.py`（provenance 断言加固）
- `P0_CLOSEOUT_WIP_20261024.md`
