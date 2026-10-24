# 财神 fail-frame：bei vs s7 冲突纪要（2026-10-24）

**红线**：未改 `templates/caishen` 生产模板；`_MIN_SCORE=0.98` / `_MIN_MARGIN=0.02` 保持；`CLAIM_TAP_CHANNEL_READY=False`。

## 同一帧路径

`温州茶（传统麻将）/采集/assist_sessions/20261024_035412/live/tea_2p/20261024_104839_549465_396b27.png`

## 冲突来源

| 来源 | 结论 | 证据 |
|---|---|---|
| `WORKBUDDY_NEXT_TASK_20261024.md` | 用户手选 + Codex 目视曾记为 **bei（北）**；当时 `detect_caishen` 弃权（best≈0.91 &lt; 0.98） | 任务书叙述 |
| `WORKBUDDY_UI_AUDIT_20261024.md` §5 | 同帧「财字小牌实为 **7 条 s7**，补模板后 1.0000」 | 审计表 |
| `tests/test_caishen_vision.py` | 回归断言该帧 **必须是 s7，且不得为 bei**（注释：曾被误标为 bei，pending 复核已驳回） | `test_20261024_fail_frame_is_not_bei` + 参数表 `("…396b27.png", "s7")` |
| 本机 `detect_caishen`（2026-10-24 21:03） | **label=s7, score≈0.99998**, runner_up≈0.836 | 运行时探测（unicode-safe imdecode） |

## 裁定（文档层，非改模板）

- **生产回归口径以测试与当前 detector 为准：该帧 = s7，不是 bei。**
- 「bei」属于历史误标 / 手选兜底记录，不得再写入生产 manifest。
- 未做新的目视裁框复核前，**禁止**为该帧新增/替换模板。

## Heldout / 第二来源

- `tests/test_caishen_validation.py` 覆盖 **fail-closed 闸门**（无 witness → heldout_passed=False；leave-one-source-out）。
- 合成帧只证明 API，**不是**第二实战 gold 语料（测试文件头注释写明）。
- 真实跨源 heldout 仍缺独立实战标注集；本轮只跑现有测试，不降阈值、不编造 gold。
