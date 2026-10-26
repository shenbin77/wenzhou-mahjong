# WorkBuddy 接手交卷（2026-10-24 05:45）

对应交接：`WORKBUDDY_LIVE_TAKEOVER_HANDOFF_20261024_0424.md`

## TL;DR

交接文档列出的 P0/P1 代码项**全部落地并通过独立复核**，但**尚未取得"连点两次真打出"的实机证据**，因此**仍不能宣称可实战代打**。原因不在代码，在于房间已结算：当前实机是「牌局结束」+「多数玩家同意解散比赛」弹窗，没有活局可验。

## 一、当前能做什么 / 不能做什么

| 能力 | 状态 | 依据 |
|---|---|---|
| 只读跟随（截图→识牌→建议→台账） | 可用 | 实机 dry_run 跑通 |
| 结算/弹窗/牌局结束画面自动停手 | 可用 | 实机 100% 停机、零点击 |
| 非我方回合（16 张/压暗/动画）不出手 | 可用 | 183~1463 帧真实帧验证 |
| 真实点击出牌（dev 局） | **未验证** | 需活局；双击"成功路径"从未在实机走通一次 |
| 吃/碰/杠/胡 自动动作 | **禁止开启** | 无实战验证，且杠的副露几何刚确认判据 |
| 4 人局 / 熟 | 不支持 | 沿用既有边界 |

## 二、改动清单

备份：`温州茶（传统麻将）/_backups/20261024_wb_turn_gate/`、`_backups/20261024_wb_pip_discriminator/`

### 新增
| 文件 | 作用 |
|---|---|
| `bot/turn_gate.py` | 回合所有权门禁（纯函数，无 ADB/无写盘）：结算画面判定 + 6 条放行判据 |
| `bot/pip_discriminator.py` | 筒牌圆点独立判据（与模板签名两条独立证据链） |
| `bot/tests/test_turn_gate.py` | 49 例 |
| `bot/tests/test_pip_discriminator.py` | 39 例 |

### 修改
| 文件 | 改动 | 定性 |
|---|---|---|
| `bot/auto_play_loop.py` | 接入门禁：`wait`→idle 不停机不计异常；`blocked`→`_stop("gate_turn")`，`turn_gate.code` 区分 `gate_settled` | 纯增量，既有门禁一条未删未放宽 |
| `bot/tile_vision.py` | +`_resolve_pip_ambiguity`，仅在 top1/top2 都是筒且 `reason=='ambiguous'` 时接管 | 全局阈值 `.82/.055` 逐字未改 |
| `bot/assist_window.py` | +13 行：手选财神值解析不出时，换局照样停机+清空 | **收紧**（旧行为是静默降级成自动财神并跨局沿用） |
| `bot/tests/test_assist_tea.py` | 唯一失败用例按真实 UI 契约修正 | 测试替身过期，非源码 bug |
| `bot/tests/test_auto_play_loop.py` | +11 例，fixture 对齐真实返回体 | 断言只增不减 |

## 三、门禁判据（放行 `discard_ready` 需全部满足）

```
0. 结算/牌局结束/模态弹窗？ 中央亮面板≥4.5% 且 手牌带 y≥85% 平均V<45，两条同时命中 → blocked 停机
   单项命中 → wait（不轮询放行，也不硬停机）
1. mode == tea_2p
2. status == advise 且 suggested_boxes 非空
3. 本局财神可解析
4. 手牌面亮度 p70(V) 均值 ≥ 150     ← 压暗帧必然伴随识别不全，故排在张数之前
5. 物理牌框数 == 17 - 副露实际张数（吃/碰 3、杠 4，叠杠 +1）
6. accepted < physical → wait「有牌未读出」；accepted > physical → wait；|physical-expected|≥2 → blocked
7. 连续 ≥2 帧手牌多重集一致（抗摸牌/发牌动画）
```

阈值实测：亮度 150.0 取自 1074 帧双峰（压暗簇 65.0–135.6 / 正常簇 168.4–230.0，中间空带）；结算 4.5% 与 45 取自 1138 帧（正常帧最大 2.79% vs 弹窗帧最小 6.28%；正常帧最低 60.00 vs 命中帧最高 36.14）。留出集 1463 帧复核：**blocked = 0**。

## 四、验证数字

### 单元/回归（Python 3.11，非沙箱，`--basetemp` 指向项目外）
```
cd "D:/温州麻将/温州茶（传统麻将）/bot"
PYTHONUTF8=1 <py311> -m pytest tests -q -p no:cacheprovider --basetemp="$TEMP/pytest_leave"
→ 373 passed （连续三次一致：50.88s / 53.06s / 43s 级别）
```
> 沙箱下会假报 130 个 setup error（safe-delete 守卫拦住 pytest tmp 工厂）；`--basetemp` 必须在项目外，否则撞离线 subprocess 夹具。

### 冻结指纹（任何一项再变，本结论作废）
```
turn_gate.py        25154 B  sha256 02a7ddb1d5bc15b2…  md5 7632183230ea470979f3db2fb8fb316e
auto_play_loop.py   26701 B  sha256 546ef0dec9eaecca…
tile_vision.py      27772 B  md5 fa27bdcfd7bf9fca15282449ba58a6fa
pip_discriminator.py        md5 dc15ad1edab14085e117453f9e4b39cf
```

### 真实帧端到端
| 帧 | 期望 | 实测 |
|---|---|---|
| `takeover_live/handoff_latest.png`（16 张等待态） | wait | wait（physical 16 / accepted 16 / 亮度 214.0） |
| `2026-10-16/740195/r08/frame_02.png`（17 张） | ready | discard_ready（暗手17+副露0=17，亮度 214.00，连续 2 帧一致） |
| `takeover_live/wb_probe_043707.png`（解散弹窗） | blocked 零点击 | stop / gate_turn / code=gate_settled，execute=0，再 tick=skip |
| 192 帧真实扫描（10-15 四个房间） | 无 blocked | wait 183 / blocked 0 |
| 1483 帧真实链路（QA 口径 A，冻结版） | 无 blocked | 有效 1463 / 分析异常 0 / blocked 0 / advise 10 帧预热后 10/10 `discard_ready` |

**`physical` 分布（两份独立样本，回答"会不会被 18 框误停机"）**
- 真实链路 1463 帧：`{0:1425, 10:1, 16:23, 17:14}`，**最大 17，≥18 为 0 帧**
- 1468 帧直接调 `count_tiles`（跳过 status 关卡）：`{0:742, 13:33, 14:35, 15:14, 16:529, 17:92, 其余个位数}`，**最大 17，≥18 为 0 帧**
- 交叉验证：`physical != expected` 有 1449/1463 帧，其中 `status == "advise"` 为 **0 帧**——99% 的"张数不符"其实是 `physical=0`（识牌前就 abstain，根本没给框），全部在 status 关卡被 `wait` 掉。因此"张数矛盾 → blocked"的可达交集在真实数据里是空集。

> 结论：`physical != expected → blocked` 当前是**防御性分支（0/1463 触发）**，不是现网行为。建议后续改成"连续 3 帧同值才停机"（≈3.6s，跨过摸牌/发牌动画），但**必须先有动态帧证据**再改。


### 实机证据（冻结版，只读，零 input）
```
gates: {'enabled': False, 'dry_run': True, 'foreground': 'com.xm.zjgamecenter'}
tick = stop | gate = gate_turn | code = gate_settled
reason = 牌局已结束/结算或模态弹窗，停止代打（central_panel+bottom_blackout；
         中央浅色面板 14.3%、手牌带亮度 23.29）：不点确定/返回大厅/分享战绩/开始游戏
stopped = True | 2nd tick = skip
```
证据帧：`采集/dev_auto_play/takeover_live/wb_probe_043707.png`

## 五、已知限制与风险（都要记住）

1. **双击「成功路径」未实证**：`TapExecutor` 只验证过"第一击没抬起就安全中止"，从未在实机走通一次"抬起→再点→手牌 −1"。
2. **筒牌判据收益被高估**：188 帧"修复"实为**同一个牌面签名**（score 0.8921 / margin 0.0123）在不同回放里重复，独立样本个位数。它没解决通用筒牌歧义，更没解决万/条/字。
3. **"五筒被认成四筒 = 0" 证不了**：留出集没有「真五筒 + 字形歧义」样本。能说的是：触发 35 次全判四筒，且这 35 张经独立红点占比（四筒 0.000 / 五筒 0.556）证实确为四筒。
4. **`physical != expected` 会硬停机**：若真实动画让 `count_tiles` 短暂多检一个框（18 框），该帧即停机。静态帧集未观察到（0/183、0/1463），但动态动画未验。建议后续改成"连续 N 帧不改才停机"。
5. **结算判据只有一帧正样本**：`wb_probe_043707.png`。其他结算/遮罩样式未覆盖。
6. **`count_tiles` 检测缺陷**：复现目录有 1 帧 13 框 vs 屏幕 14 张，属既有 `unresolved_regions` 口径，本次未处理。
7. 仓储：`bot/` 下 40+ 文件（含本次全部改动）在 git 中 **untracked**；`_accept_pip_eval.json` 2.25MB 不宜入库。
8. 死代码（改动前既有）：`assist_window.py:862` `TILE_CHOICES.get(self.caishen.get(), "")` 永远取不到 key，财字小牌候选采集实际不工作。
9. **真实瓶颈已从门禁转移到财神可读性**：1463 帧真实链路里 1450 帧在识牌前就 `abstain`，归因是——看不清本局财字小牌 **762 帧（52%）**、茶杯 4 人局 483 帧、未确认 2 人局 180 帧、识牌不完整仅 25 帧。门禁已稳，要提升代打可用率下一个该啃的是**财神角标可读率**，不是继续调门禁阈值。

## 六、下一步（按顺序）

1. 由**用户手动**重建房间开局（禁止脚本点「确定/开始游戏/充值/福利」）。
2. 开局后先跑 `enabled=False` 只读跟随，确认同一局内 16→17 张切换时门禁能正确 wait→ready。
3. 取得用户当次明确授权后，放**一次** `enabled=True`、`max_actions=1`，验收三件事：牌真的抬起、第二击后消失、手牌恰好 −1。
4. 上述通过后再谈连续代打；吃/碰/杠/胡 的自动动作仍需单独实战验证（杠的几何判据见 `AGENTS.md` 2026-10-24 条）。

## 七、验收边界声明

- 本次交付**未经过 Hana 验收**。
- 本次结论只对上述冻结指纹有效。
- `IS_PASS: YES` 仅代表：门禁与筒牌判据在离线真实帧上 fail-closed 且零点击、全量 373 例绿。
- **不代表**"可实机代打"，也不代表"识别准确率达标"。
