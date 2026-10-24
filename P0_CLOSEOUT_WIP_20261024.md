# P0 收口 WIP（2026-10-24 晚，Asia/Shanghai）

执行人：Grok Bot（用户「那你就开始收口」）
CLAIM_TAP_CHANNEL_READY：**仍为 False**（未开真机点击 / 未开实战）

## 测试基线（今晚重跑）

| 套件 | 结果 |
|---|---|
| 主辅助器 ot/tests | **656 passed**（约 64s） |
| 子仓库 rain_v0/tests + pc_bot/tests | **487 passed, 13 skipped, 206 subtests**（约 33s） |

原先主库 653 pass / 2 fail 已清零。

## 已完成

### 原 2 个失败用例对齐正确茶规
1. 	est_four_tile_kong_group_counts_as_one_meld：闭手改为 13 张（13+逻辑3=16）；新增回归 	est_twelve_plus_one_kong_is_rejected_as_illegal_spare 禁止旧「12+1杠」。
2. 	est_tea_rule_gate_accepts_visible_no_zhong_fa_and_abstains_when_obscured：重置进程级 RULE_GATES；未确认遮挡仍 fail-closed；确认后遮挡走 TeaDeckLatch（deck_source=latched）。

### 主库跟进 P0-1 逻辑位（此前只在子仓 e82bf00）
- 	ea_bot.see / 	hink 改用 expected_concealed_count（每组副露含杠只占 3 逻辑位），不再按实物 4 扣暗手。

### P0-2 部分：来牌归属未知一律弃权
- ction_policy.evaluate_claim_candidates：incoming_source is None → 吃/碰/杠/胡全部 L2 弃权。
- 夹具补 incoming_source="opponent"；旧「None 仍可碰」测试改为弃权断言。

## 能用但未验收

| 项 | 状态 |
|---|---|
| P0-1 杠第4张不回灌 | 子仓 DONE @ offline/tea-rules@e82bf00；主库今晚接上逻辑位闸 |
| P0-2 未知归属弃权 | 代码+单测已通；**未**做真机按钮帧验收 |
| 玩法/牌组门禁粘滞 | 单测覆盖；与旧「遮挡即未确认无中发」冲突已按 latch 契约改测 |

## 仍不能用 / 未做完（P0-2～5 余量）

1. **P0-2 余**：自动模式强制真实按钮模板（
equire_template_match 仍默认 False）；明杠/暗杠/加杠/杠后出牌状态机拆分；超时/新鲜时间戳闸；2 人局吃 vs 补2 实战按钮证据冲突未裁定。
2. **P0-3**：弃牌两段点击（先抬起再确认）、倒计时证据、禁止合成 	ap_probe 冒充实机 — 未改。
3. **P0-4**：财神 provenance（source_kind/review_method/heldout）、leave-one-source-out、禁止 auto top1 当金标 — 未改。
4. **P0-5**：每帧刷新玩法/HUD/来牌后再决策；abstain 清空按钮；统一 TableState；16 张停止 / 未知财神停手 — 未改。
5. **红线**：未设 CLAIM_TAP_CHANNEL_READY=True；未 harvest/ADB；未删 采集/；未 force-push。

## 文件 diff（今晚）

- 温州茶（传统麻将）/bot/tea_bot.py
- 温州茶（传统麻将）/bot/action_policy.py
- 温州茶（传统麻将）/bot/tests/test_tea_bot.py
- 温州茶（传统麻将）/bot/tests/test_tea_meld_geometry_groups.py
- 温州茶（传统麻将）/bot/tests/test_action_policy.py
- 本 WIP 笔记
