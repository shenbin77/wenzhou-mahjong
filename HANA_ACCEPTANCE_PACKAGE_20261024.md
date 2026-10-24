# Hana 验收包：全量接手第一批三项交付（2026-10-24）

交付方：WorkBuddy 团队（engineer / engineer-2 / engineer-3）。全部按"改动文件清单 + 本机全量测试数字 + 实机证据路径"三件套交卷，实机证据均为开发局 dev 数据、全程只读采集，未产生任何游戏输入，未开启任何自动动作。

## 交付 1：西杠几何分割修复（engineer-3）

**问题**：副露组中间牌抬高 26px，旧逐框基线判据把整组西杠算进暗手（16/0），整帧弃权。

- 改动：`bot/tile_vision.py`（删逐框基线判据，新增 `_split_exposed_prefix()` 组级判据：手牌基线=最大 bottom、抬升前缀、边界间隙阈值、组间间隙切组、组大小只进 `meld_groups` 审计字段）；新增 `bot/tests/test_tile_meld_seg.py`（13 条）；`bot/tests/test_tap_executor.py` FakeAdb 兼容环境 shim（无行为变更）。
- 备份：`温州茶（传统麻将）/_backups/20261024_tile_meld_seg/`（tile_vision.py.bak + sha256；改前 SHA `0a1d5aec…`，改后 `1b7778ea…`——已由主理人磁盘核对一致）。
- 核心对比（复现帧 `20261024_005700_684588_0ae39a.png`）：**before `concealed=16, meld_boxes=[]` → after `concealed=7, meld_boxes=9, groups=[3,3,3]`**，西杠三张读出"西"0.978–0.985；其余 6 正负例帧（真副露×2、完整暗手 17、暗杠动画 15/0、动作动画 12/0、回放多副露 10/5）数值逐字节不变。
- 暗杠结论：3 牌位/4 张，几何不可区分碰与杠（抬升中框是唯一信号），杠判定留决策层；`concealed + len(meld_boxes) == visible_count` 断言锁进测试。

## 交付 2：可信换局检测 v2（engineer-2）

**问题**：HUD 指纹区被红色头像遮挡 → 旧局手选财神跨局进入 advise（00:45 s1 → 00:47 t1 事故）；Codex 挡板实测触发率 99.7%，等于整局无建议。

- 改动：`bot/assist_window.py`（新增 `tea_round_caishen_evidence()` 读桌面财神小牌作局身份证据；重写 `_accept_continuous_result()` 为双证据 + 换局规则 + 挡板 + 遮挡放行；4 处状态重置点同步 `continuous_caishen`）；新增 `bot/tests/test_round_detect_v2.py`（8 条）；`test_spec005_live_v2.py` 补 `_FakeWindow._caishen_selection` 假接口；删除 `test_assist_tea.py` 尾部重复用例。
- 备份：`_backups/20261024_round_detect_v2/assist_window.py` SHA `0880ceb1…`（主理人已磁盘核对；现文件 `9855252c…`）。
- 行为验证（`采集/workbuddy_review_20261024/behavior_matrix.txt`，`repro_round_detect.py` 可重跑）：事故帧旧 s1 被隔离；模拟挡板漏报时 OLD 复现事故（旧 s1 进 advise）、NEW 仍挡下；同局遮挡帧放行（牌面证实同局），解决长期无建议。不变量：旧局手选财神绝不跨局进 advise，置信不足一律弃权。
- 勘察数据：`round_survey.csv`（1189 帧逐帧）。

## 交付 3：副露向听引擎 + 安全回归（engineer）

**问题**：`calc_shanten` 无 meld 参数，`TeaBot.see()` 遇明牌组抛异常，实战约四成帧无建议。

- 改动：`代打BOT/majiang-bot/brain_v0/engine/tea/shanten.py`（`calc_shanten/is_win/calc_ting_tiles/calc_ukeire/discard_analysis/best_discard_by_efficiency` 全加 `melds` 参数；`validate_tiles` 强制守恒「暗手+副露∈{16,17}」+ 跨副露同种≤4；向听向 (5−n) 面子收敛不塞虚拟牌；**杠余牌实体化**——暗杠第 4 张真实拥有，可与暗手凑面子；八对带副露一律不评估；财神下限按实际副露张数）；`bot/tea_bot.py`（张数按各组实际张数、validate 传 melds、副露胡牌结论显式标注"未验证"绝不静默）。
- 备份：`_backups/20261024_meld_engine/`（shanten.py.before/git_head、tea_bot.py.before/after、README；主理人已核对齐全）。
- 端到端实测（只读探针）：`20261023_220643` 80 帧：带副露 46 帧全部进入决策、0 帧再抛明牌组异常（改前可用仅 2/80）；`20261023_201640`：`205345` 帧 = 吃123万+吃123筒 → 向听 0、打 8 条，`205350` 听牌进张 4 张。暗杠守恒：复现帧 16 框 = 西暗杠 3 可见位 + 13 暗手（17−4=13）。
- 安全回归（新增 2 文件 6 条）：`test_tea_caishen_marked_tile.py`（高亮财神牌补全 + 财神未知不填牌 + 补偿只限角标那张）；`test_tea_dimmed_hand_abstains.py`（压暗弃权 + 已知财神只救角标一张 + 压暗缺牌无角标必须弃权）。

## 测试数字汇总（Python 3.11，独立 basetemp，跑前清残留）

| 范围 | 交付前 | 交付后 |
|---|---|---|
| bot/tests 全量 | 185 passed / 0 failed | **212 passed / 0 failed**（+27 全为新增） |
| 子仓库 brain_v0+pc_bot | 446 passed / 13 skipped | **480 passed / 13 skipped / 206 subtests**（+34 新增，零回归） |
| spec005 + round_detect（主理人独立复跑） | — | **44 passed / 0 failed** |

**Hana 复跑命令**：在 `D:\温州麻将\温州茶（传统麻将）` 下
`python -m pytest bot/tests -q -p no:cacheprovider --basetemp <临时目录>`
（须用 Python 3.11：`C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`；跑前清 basetemp 残留）
子仓库：`cd 代打BOT/majiang-bot && python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider`

> **复跑注意（2026-10-24 01:5x 更新）**：当前树最新一次全量为 **211 passed / 1 failed**，唯一失败 `test_recognition_audit.py::test_primary_window_preflight_shows_real_frame_without_adb[温州熟 · 4人]`，报 `TclError`（tkinter 初始化层）——已验证单独复跑 3/3 通过，属全量跑时 Tk 会话顺序依赖的**环境性偶发**，非代码回归。若复跑遇到该条，单独重跑该用例即可。

## 证据索引（均从 `D:\温州麻将\温州茶（传统麻将）` 相对）

- `采集/workbuddy_review_20261024/xigang_fix_evidence/`：before_after_xigang.png（双联对比图）、before_after.json、per_frame_table.md/.json、xigang_evidence.py、frame_table.py
- `采集/workbuddy_review_20261024/`：behavior_matrix.txt、repro_round_detect.py、round_survey.csv、ROUND_DETECT_SUMMARY.md
- 复现帧：`采集/assist_sessions/20261024_005400/live/tea_2p/20261024_005700_684588_0ae39a.png`；跨局对：`…/20261024_004545_426885_4a2921.png`（s1）与 `…/20261024_004748_088050_2169ad.png`（t1）

## 已声明边界（未验证项，不因本包宣称可实战托管）

1. 暗杠"3 连续同 label 框"信号识别层未产出，暗杠局面先保守弃权（不会算错，只是不算）。
2. 带副露胡牌/计分未验收：sh==1 弃权、非胡结论强制带"副露局面胡牌判定未验证"标注。
3. 真 4 人局副露布局零样本；杠按钮生产检测器缺样本未写（补采样需求见 `action_inventory.md` §6）。
4. 辅助器保持只读跟随；碰/杠/胡/过自动动作未开启；下一门禁 = 开发局半自动实局零错点验证。
5. 遗留：codex live 报表 SAME/MISS 序列疑似颠倒，不影响本批交付，留待下轮。
