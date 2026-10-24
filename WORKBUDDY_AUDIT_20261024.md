# WorkBuddy 交付复核与 Codex 续开发（2026-10-24）

本记录复核 WorkBuddy 的识牌候选、行动按钮盘点、财神与换局证据、副露修复、自动循环及相应测试。附件和聊天中的数字是待核报告；以下“复验”均来自当前磁盘代码和保存的原生帧。全程未向模拟器发送 ADB 输入。

## 已独立复验

| 范围 | 结果 |
|---|---|
| `bot/tests` 原始基线 | 253 passed；与 WorkBuddy 冻结口径一致。 |
| 副露 43 帧 | 独立调用当前 `analyze_snapshot` 重放：19 帧建议、19 帧等待、5 帧弃权；5 帧均为非法 `[2,2,4]` 牌组。即 38 帧恢复可分析，与报告一致。 |
| 西碰与西杠 | 会话 `20261024_005400` 共 580 帧；184 帧首组为三张已接受的西。83 帧中牌不抬高（平放碰），101 帧中牌完整抬高 26px（叠牌杠）。新逻辑对 184 帧分别返回逻辑 3/4 张，零错分；第四张只进入规则计数，不虚构屏幕牌框。 |
| 财神模板 | `bot/templates/caishen/manifest.json` 54 个条目、32 个目标 label；54 个模板文件和 54 张来源帧均存在，所存 SHA-256 全部匹配。`t1` 实战财神小牌已目视核对。20 个 label 有实战来源；12 个仅有旧回放来源。 |
| 手牌模板 | `采集/templates/tea_tiles/manifest.json` 39 个条目，文件和所存哈希均匹配。WorkBuddy 筛选的 s6/t6/t9 三张候选已目视核对为六条、六筒、九筒；被判错标的七张仍隔离在 `hand_pending_rejected_misread/`。 |
| 子仓库 | Windows 默认 GBK 下 1 个旧测试读 JSON 时 `UnicodeDecodeError`；设置 `PYTHONUTF8=1` 后为 480 passed、13 skipped、206 subtests passed。此项是测试编码环境差异，未改子仓库代码。 |

## 本次续开发

1. `bot/auto_play_loop.py`：换局帧跳过点击；手选财神遇换局即停；HUD 与画面财神均不可读时拒绝真实点击；删除可能绕过头像遮挡判断的裸像素 HUD 兜底；点击异常或点后验证失败立即停手。对应测试补入 `bot/tests/test_auto_play_loop.py`。
2. `bot/tea_bot.py`：只有同牌三张且中牌整张叠高、两侧齐平、牌宽与间距一致时，规则层把明牌计作四张杠。平放三张仍按碰，非法组仍弃权。对应实战帧测试在 `bot/tests/test_tea_meld_geometry_groups.py`。
3. `bot/assist_tea.py` 与 `bot/assist_window.py`：其他牌识别失败时仍可只读展示已证实的叠牌杠，例如“西杠（4张）”；不因此放出出牌建议。新增 `bot/tests/test_stacked_kong_evidence.py`。

最终离线回归：`python -m pytest tests -q -p no:cacheprovider` 为 **263 passed / 0 failed**（Python 3.11，独立临时目录）。西碰/西杠 184 帧全量复核为 `(平放,3张)=83`、`(中牌抬高26px,4张)=101`，零错分。未运行模拟器 `--check-only`、实时 dry_run 或真实点击。

修改前备份：`温州茶（传统麻将）/_backups/20261024_codex_autoplay_audit/`、`20261024_codex_xigang_logic/`、`20261024_codex_xigang_evidence/`。恢复时按文件名从对应目录复制回 `bot/`；这些备份包含原始源码与测试，未覆盖采集帧。

## 未验收与边界

- WorkBuddy 的 `action_inventory.md` 是按钮画面盘点，不是可点击的动作检测器。杠按钮只有 2 帧、仅 1 帧独立于模板；吃/碰/杠/胡/过尚未接入自动操作。
- `glyph_confusion_numbers.txt` 所列 340 个“危险接受”是低 margin 候选统计，并非 340 个已人工确认的错误；3996 帧也没有逐张独立真值，因此不能据此宣称识牌准确率。
- 当前西杠帧仍因别的牌面不完整而弃权；“西杠（4张）”是可视证据，不代表该帧允许出牌。自动循环和按钮动作均没有实机连续验收，不可称为可托管代打。
- 财神清单虽覆盖 32 个目标 label，仍有 12 个没有当前实战来源；新牌种首次出现在实时桌面时需再核对。茶四人、熟四人的规则与识牌尚须各自验收。

## 下次实战验收顺序

先确认目标茶 2 人房间和当局财神；运行 `run_auto_play_dev.py --check-only`；再以默认 dry_run 记录台账并逐帧核对目标牌框、换局和弃权；只有台账无误才小步验证真实单次出牌。任何异常、身份不明或点后不符立即停手；不点充值、开局或未实测的吃碰杠胡过按钮。
