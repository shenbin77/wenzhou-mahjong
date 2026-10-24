# SPEC-002 v4: Phase 1 辅助器 + 录像（数据契约定稿·终版）

> 本版只冻结契约。四审只核对 v3 复审的 4 个阻塞点是否闭合；实现细节问题请记入实现备注，不要再扩展契约范围。

## 1. mode 枚举（与代码 assist_tea.py 对齐）
`tea_2p` | `tea_4p` | `shu_4p`，禁止别名。

## 2. 决策状态机与去重（冻结）
- 状态：`detected → suggested → resolved | abstained | timeout`。
- 稳定帧：连续 2 帧手牌多重集相同，才允许从 detected 进入 suggested。
- **活动决策锁**：同一会话同时最多 1 个未关闭决策；未关闭期间的新稳定帧一律不生成新决策。
- **去重键（语义键，非图片哈希）**：`sha256(mode + 排序后手牌多重集 + round + caishen_id)`。
- **decision_id** = `{mode}_{语义键前16位}`，不含 room_id、不含 PNG 哈希。
- **重新布防**：决策关闭后，仅当新语义键与已关闭决策的语义键不同，才允许生成新决策（语义键含 caishen_id，财神变化即新键）。
- 写入时机：关闭时一次性追加完整一行；suggested 阶段不落盘。30 秒无稳定结果帧 → timeout 关闭，agreed=null。

## 3. 元数据来源契约（冻结）
| 字段 | 来源 | 缺失时 |
|---|---|---|
| account | 会话配置：开窗时必选（默认上次值） | 无会话不启动记录 |
| room_id | 窗口人工输入（可选） | null，允许落盘 |
| round | 记录器会话计数：从 1 起，每按一次"下一局"按钮 +1 | 必有值 |
| screen_rule_text | 规则文字区模板/OCR 读取 | 读不到 → gate 判定失败 |
| caishen_id | 熟4 固定红中；茶系手动选 | 未选 → 不给建议（abstained/unrecognized 不适用，记 abstain_reason=gate_mismatch 不准确——用 status=abstained + abstain_reason=null + diff_note="caishen_unset"） |

## 4. JSONL 字段表（assist_record/v4，每行一决策点）
| 字段 | 类型 | 说明 |
|---|---|---|
| schema | str | "assist_record/v4" |
| decision_id | str | 见 §2 |
| timestamp | str | ISO8601 带时区，关闭时刻 |
| account | str | 必填，来自会话配置 |
| room_id | str\|null | 人工输入，可空 |
| mode | enum | tea_2p/tea_4p/shu_4p |
| screen_rule_text | str\|null | 读不到为 null |
| gate_ok | bool | 规则文字与 mode 一致且可读 |
| round | int | 会话计数 |
| tiles | array | 每张 {id:int\|null, score:float, margin:float, reason:enum}；reason ∈ ok/low_score/ambiguous/no_box |
| caishen_id | int\|null | |
| melds | array | Phase 1 可为空 |
| shanten | int\|null | 有未识别牌时 null |
| bot_suggest | int\|null | = bot_candidates[0].tile；弃权时 null |
| bot_candidates | array | **统一对象 Top-5**，按 final_score 降序：{tile:int, final_score:float, shanten_after:int, ukeire_count:int}。茶系现有 (score,tile) 元组由实现层用 shanten/advisor 补齐其余字段；熟系现有对象多余字段（name）丢弃 |
| human_discard | int\|null | 差分成功才非空 |
| agreed | bool\|null | 无法判定一律 null，禁止猜测 |
| status | enum | resolved/abstained/timeout |
| abstain_reason | enum\|null | gate_mismatch/gate_unreadable/wrong_count/unrecognized；**仅 status=abstained 时非空** |
| diff_note | str\|null | **唯一确定**：status=resolved 且差分失败时 ="diff_failed"（此时 abstain_reason=null、human_discard=null、agreed=null）；其余情况 null。例外：§3 财神未选的弃权用 diff_note="caishen_unset" 且 status=abstained、abstain_reason=null |
| decision_frame | obj | {path, sha256} |
| result_frame | obj\|null | {path, sha256}；abstained/timeout 可为 null |

四类误差口径：漏分=no_box/low_score；分错=与金标不一致；错误高置信=有建议但与金标建议不符；正确弃权=金标不可判时 abstained。

## 5. human_discard 差分（冻结）
- 结果帧须稳定帧；多重集净消失恰 1 张且该张已识别 → human_discard，agreed 与 bot_suggest 比较。
- 其余情况 → diff_note="diff_failed"，见 §4。

## 6. 金标准清单（冻结要求）
- tea_2p：3 张沿用（DEV_RECORD 路径），r01 用修正 16 张标签（万 3/6/8；筒 9/2/2/3/3/7；条 2/2/4/6/6/7/5），r03 17 张、r12 16 张；实现前生成 manifest（文件名+sha256+逐牌标签）冻结。
- tea_4p / shu_4p：实现前各选 3 局亮手帧建同格式 manifest 冻结；未冻结的模式不进入验收。选片标准：底部手牌完整可见、规则文字可确认模式。
- 熟4 前置核对：`pc_bot/templates_hongzhong_v2` 已存在（2026-10-20 核到 35 个文件）；仍须冻结熟4金标并做实机验收，不能仅凭模板目录存在宣布通过。

## 7. 验收（每模式各做，分别达标）
1. 识别：金标逐牌分错=0。
2. 门禁：选错模式或规则不可读 → abstained 且原因正确，不给建议。
3. 记录：10 个决策点 decision_id 无重复（语义去重生效）；resolved 行 agreed 与人工复核一致（含 diff_failed 的 null）。
4. 稳定性：单模式连续 10 局不崩；每行可 json.loads。
5. 弃权覆盖：低分/歧义/无框各造一例，reason 正确。

## 8. 并行小任务
财神模板 t2 缺失：补模板+回归样本，独立于本 spec。

## 不做（Phase 2）
自动点击（G3/G4）、结算 OCR（Phase 2 再加 settle 字段）。
