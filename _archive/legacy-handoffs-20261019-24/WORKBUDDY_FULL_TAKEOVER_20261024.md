# WorkBuddy 全量接手：温州茶 2 人辅助与代打（2026-10-24 01:00）

本文件替代 `WORKBUDDY_PARALLEL_HANDOFF_20261024.md` 的分工限制。用户已明确要求把后续开发全部交给 WorkBuddy；Codex 从本文件交付后停止修改核心代码和操作雷电。WorkBuddy 可接手原先保留给 Codex 的 `assist_window.py`、`assist_tea.py`、`tea_bot.py`、`tile_vision.py`、`live_capture.py`、`recorder.py`、`tap_executor.py`、财神 manifest，以及测试；先核对工作树与备份，不覆盖双方已有改动。根目录 `AGENTS.md`、`HANDOVER.md`、`SPECS/QUEUE.md` 与原并行交接仍需阅读。用户的目标是雷电上的茶 2 人自动代打，另一账号由本人用手机控制；请按安全门禁逐步验收，不把离线测试当作已可托管实战。

## 立即接手时的运行状态

- 雷电 ADB 序列号 `emulator-5556`，游戏前台包应为 `com.xm.zjgamecenter`。只用原生 1600×900 ADB 截图和 ADB 输入；不要用桌面鼠标坐标点击游戏。
- 旧辅助器 PID 21932 已停止，因为它在换局后沿用上局手选财神并给出错误建议。新窗口 PID **28344** 已启动，采集目录 `温州茶（传统麻将）/采集/assist_sessions/20261024_005400/`；01:00:44 最近审计为 `wait`、财神 `t3`（画面识别）。PID、画面、玩法、房号与轮次都可能变化，执行前重新核对。新窗口加载了下述 HUD 遮挡补丁，头像遮住房号/局数时清除建议并继续采集。
- 用户刚提供的截图里，左下是**西杠**：三张可见“西”，中间抬高。原生同刻帧：`温州茶（传统麻将）/采集/assist_sessions/20261024_005400/live/tea_2p/20261024_005700_684588_0ae39a.png`；原审计 `20261024_005400/recognition_audit.jsonl` 记录 `识牌不完整，停止决策`。先以这张为正例，不把三张可见牌直接当三张暗手。
- WorkBuddy A/B/C 产物在 `温州茶（传统麻将）/采集/workbuddy_review_20261024/`：`SUMMARY.md`、`frame_review.csv`、`action_inventory.md`、证据图与 probe。C 只修改 `bot/tests/test_spec005_live_v2.py` 的替身，运行时未改。A/B 的候选和按钮位置是证据，不是可直接驱动点击的生产检测器。

## 已定位的两个实战阻塞

1. **西杠被算进暗手。** 对 00:57 原生帧直接运行 `TileVision.count_tiles()` 得到 `visible_count=16`、`concealed_count=16`、`last_meld_boxes=[]`。前三框为 `(6,761,89,875)`、`(93,735,175,849)`、`(179,761,263,875)`；中框明显抬高 26px。`tile_vision.py::count_tiles` 当前要求左侧每个候选框底部都高于基线，第一框不满足，因此 `cut=0`，整组西杠漏分。后续识牌又有两个 `low_score`，最终 `last_complete=False`。应基于副露组整体形状、组间间隙与牌面证据重做分割，并用真实碰牌帧作反例；**三同牌不等于可安全判杠**。还要单独验证暗杠只显示三牌位时 `meld_count`/闭手数的算术，不虚构第 4 个牌框。
2. **换局时手选财神串局。** 00:45 的原生帧 `20261024_004545_426885_4a2921.png` 是 `s1`（一条）；到 00:47 的 `20261024_004748_088050_2169ad.png`，财神画面已为 `t1`（一筒），旧窗口审计却仍记录 `caishen=s1, source=manual, status=advise`。原因是 `assist_window.py::tea_round_fingerprint` 用 `(15,55,225,140)` 的像素值锁房号/局数，动态“买底请神顶”头像覆盖该区域，造成误判/无法可靠换局。Codex 已加保守补丁：红色头像遮挡时返回 `None`，不展示建议，也不据此清空或续用手选财神；测试 `test_live_avatar_covering_room_hud_does_not_fake_a_new_round` 通过。**这是安全挡板，不是完整的房号/局数识别方案**，会使头像持续遮挡时长期无建议。WorkBuddy 需做可信的换局检测，确保旧局手选财神不会跨局进入可执行建议；无法确认时继续弃权。

## 财神模板与识牌状态

- Codex 已按用户手选/原图目视核对并入库实战 `s2`、`s1`，又据 WorkBuddy A 的候选帧目视核对入库 `w3`、`w6`。生产文件在 `bot/templates/caishen/manifest.json` 与同目录对应 PNG；改动前备份分别在 `温州茶（传统麻将）/_backups/20261024_{s2_live_caishen,s1_live_caishen,w3_w6_live_caishen}/`。
- 用 00:32 同局四帧回归 `s2=1.0`；00:45 三帧 `s1=1.0`；A 的 64 张低分候选补库后为 `w3=60`、`w6=3`、`none=1`（最后一张仍需查角标遮挡），没有调整 `_MIN_SCORE`/margin。旧 `t2`、`t8` 来源帧各自仍认回原标签。模板缓存会按 manifest 变化自动失效，无需为补模板重启。
- 仍有手牌漏识和空白牌面；A 报告中 `w3@000406` 与 `t4@000756` 是低 margin 候选，`w4` 被错读为六万的样本应保持弃权。**不要降低 0.82/0.055 阈值来换表面通过率**；优先按原图和正负样本修模板/裁框。压暗整手必须弃权，高亮财神只有在本局财神可信时才可按角标计入。

## WorkBuddy 后续顺序与验收

1. **先修安全与副露。** 保住错局挡板；用 00:45→00:47 跨局帧复现，确保旧手选财神不能产生 `advise`。随后修西杠几何，分别拿真实吃/碰/明杠/暗杠正负帧验证分组、`meld_count`、闭手张数、建议与记录；未知牌/组继续弃权。不要只改 `calc_shanten` 或只看框数。
2. **补动作证据。** `action_inventory.md` 扫了 3172 帧，但杠只有 1 个独立正样本，胡按钮与胡牌面板易混，当前没有可靠生产检测器。先按该文 §6 采集跨局杠、胡边界、碰/吃/过及选择弹窗；使用原生坐标证据、点前重截图与点后验证。未确认动作时停手，绝不点充值/开始游戏/游戏中心。
3. **跑完半自动实局。** `tap_executor.py` 目前只对可信出牌做单次 ADB tap，并在点前重新识牌定位、点后核对闭手少一张；不支持自动碰/杠/胡/过。需在同一真实茶 2 人对局里逐次记录点前/点后截图、决策与结果，直到完整一局零错点、零串局、零误触。用户手动乱打的历史帧只当视觉样本，不当正确策略标签。
4. **再启全自动循环。** 建议、动作、换局、前台包和房卡状态均有明确门禁后才开启；连续轮询遇任何不确定立即暂停并显示原因，不重试失败的 tap。熟 4 人与茶 4 人另行验收，不把茶 2 的结果外推。

## 测试、工作树与回退

- 2026-10-24 约 01:00，本机 Python 3.11 全量测试：在 `D:\温州麻将\温州茶（传统麻将）` 运行 `python -m pytest bot/tests -q -p no:cacheprovider --basetemp C:/Users/Administrator/AppData/Local/Temp/tea_after_workbuddy_20261024_0058`，**185 passed / 0 failed**。`bot/tests/test_assist_tea.py` 单文件 **21 passed**。这仅证明本次代码回归，尚无全局实机验收。子仓库 `brain_v0/tests pc_bot/tests` 本次未跑。
- 当前根仓库有大量原有改动，`AGENTS.md`、`assist_window.py`、`tea_bot.py`、`tile_vision.py` 等为修改状态；`bot/templates/caishen/manifest.json` 和 `bot/tests/test_spec005_live_v2.py` 在根仓库显示为未跟踪。不要 `reset --hard`、清空目录、批量提交或覆盖截图/审计。修改前备份并核对文件哈希。
- HUD 补丁备份在 `温州茶（传统麻将）/_backups/20261024_round_hud_guard/`，本次全交接前的根规则备份在 `_backups/20261024_full_workbuddy_handoff/`。Codex 的旧窗口 PID 21932 已停止；新窗口 PID 28344 只读采集，重启后先确认玩法与账户标记再跟随。若发现可执行建议跨局或财神不符，立即停止跟随与代点，并保存原生帧和审计再修。

WorkBuddy 接手后请把上述四步作为一个主任务持续推进，及时告知用户：实战已验证了什么、还卡在哪个门禁、何时能从半自动转连续代打。不要把截图里的“西杠未识别”归咎于用户乱打；该帧有明确的几何分割缺陷。
