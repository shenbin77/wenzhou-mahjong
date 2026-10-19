# 温州麻将项目 总交接（2026-10-17，由 Grok Bot 从 box 迁移）

> 给接手 AI（OpenCode 等）：本文件自包含，无需任何历史上下文。

## 1. 目录地图（Windows）
```
D:\温州麻将\
├─ HANDOVER.md                      ← 本文件（总览）
├─ 温州茶（传统麻将）\              ← 当前主线（茶馆传统麻将）
│  ├─ HANDOVER.md
│  ├─ 代打BOT\majiang-bot\          ← git clone（私有 GitHub shenbin77/majiang-bot）
│  └─ 采集\                          ← 回放采集脚本 + 数据
│     ├─ 2026-10-09 … 2026-10-16\    ← 按日期的回放帧
│     ├─ lists\  templates\  _quarantine_*\
│     ├─ archive_grok_ps1_20261017\harvest_continuous_v3.ps1  ← 旧批采脚本
│     ├─ box_replay_harvest_missing\ ← box replay_harvest 中 Windows 缺的 384 个文件（1.24GB，按文件名+大小比对；原相对路径保留，未覆盖任何原文件）
│     ├─ box_logs\                   ← box majiang-data/logs
│     ├─ misc_box_screens\           ← box 散落的 tea_*/list_*/p1_*/p2_* 截图
│     └─ docs\                       ← majiang-bot-docs（旧 HANDOVER、mvp-blockers 等）、HARVEST_REPORT.json、routine_note.md
└─ 温州熟（红中放炮麻将）\          ← 暂停（on hold）
   ├─ HANDOVER.md  代打BOT\  采集\
```
旧路径：C:\Users\Administrator\agent-tools\tea_harvest（早期采集，保留勿删）。
相关旧数据：D:\抖音直播\温州麻将（熟 抖音素材，部分）、D:\archive_温州麻将202608（lab 存档，部分）、D:\HanaAgent。

## 2. Git
仓库：https://github.com/shenbin77/majiang-bot （私有）。全部分支已推送到 origin，box 上无未提交改动（2026-10-17 检查）。
| 分支 | head |
|---|---|
| main | 8f44474 |
| offline/tea-rules（茶主线，Windows clone 所在） | 5c73dd5 |
| offline/replay-and-hand14 | 588d670 |
| offline/templates-and-sampler | 8231252（本次补推 1 个提交） |
| offline/ceo-priority-5 | 20ad370 |
| offline/click-roi-tests | b2fbee3 |
| offline/recog-gold-and-rules | c99ad0d |
| offline/vision-brain-regress | 632bb47 |
无需 handover/* WIP 分支（没有未提交内容）。更新：`git fetch --all` 后按需 checkout。

## 3. 回放采集流程（茶，2026-10-19 更新）
当前路径：大厅 **比赛场 → 进入 → 战绩 → 目标战绩行详情 → 回放选项 → 每局回放**。战绩页底部“查看回放”是输入回放码的入口，不能当作战绩行。
- 当前脚本：`温州茶（传统麻将）\bot\harvest_tea.py`，使用 ADB 原生截图、模板定位、双帧确认和点击后页面验证；默认 dry_run。2026-10-19 已实测单局录屏，批量无人值守尚未验收。
- 旧 `harvest_continuous_v3.ps1` 在 `采集\archive_grok_ps1_20261017\`，含过时设备号和固定坐标，只作历史参考，不直接执行。
- 旧截图与新录屏均保留在 `采集\`；批采前先按日期／房号／局号去重，不重采已存在局。

## 4. 结算 OCR 状态
- 数字模板 13 个（0-9、+、-、±相关）。
- batch settle_batch_20261016：扫描 2039 帧，5 帧像结算，12 次 OCR 命中，4 次完整四座读数，8 个错误。
- 约 31 个房间不完整，大多缺结算帧 → 下一步补采结算画面。

## 5. 茶规则
底分进阶 2/4/6/8；16 局；杠算分；杠上花双翻；无中发；半清；清一色无序；2 人局无三背/跟风。

## 6. 红线（必须遵守）
- 绝不点 **充值**、**开始游戏**。
- 房卡为 0 → 停手。
- 绝不进入 **雷电游戏中心**。
- 出现 ANR → 点“等待”，再重新启动 `com.xm.zjgamecenter`。
- 温州熟 暂停（on hold）。
- **任何 ADB / 模拟器操作前必须先得到用户同意。**

## 7. 仅留在 box 上的数据（未复制）
原因：D: 空间充足（约 2.37TB 空闲），但 box→Windows 只能经 CopyFromBox 每块约 20MB 传输且连接间歇断开，45GB 约需 2300 次传输，不现实。需要时可分批迁移或从原始来源恢复。
| box 路径 | 大小 | 说明 |
|---|---|---|
| /workspace/majiang-data/archive_lab | 11G | 实验室存档，原自 Windows（现 Windows 仅剩约 3.8G 于 D:\archive_温州麻将202608） |
| /workspace/majiang-data/douyin_extra | 11G | 熟 抖音素材（非茶），原自 D:\抖音直播\温州麻将（现 Windows 约 4.1G） |
| /workspace/majiang-data/incoming | 22G | 上述两者的传输分片/重组 tar，冗余 |
| /workspace/majiang-data/hana_runtime | 809M | Hana 运行时 |
| /workspace/majiang_apk | 130M | 游戏 APK |
| /workspace/majiang-venv | — | Python venv，不需要 |

## 8. 下一步
1. 补采约 31 个房间缺失的结算帧（需用户同意后再动 ADB）。
2. 提升结算 OCR（修 8 个错误，扩充数字模板）。
3. 在 offline/tea-rules 上完善茶规则引擎与回放解析。
4. 熟 继续暂停，除非用户重启。

## 2026-10-18 此前开发范围（已被下方阿法优先决定覆盖）
- 温州茶第一版聚焦 **2 人局辅助打**：截图识牌、校验、给出建议，由人手动操作；沿用 `温州茶（传统麻将）/bot/tea_bot.py` 的离线识牌与决策链路。
- 4 人局回放仅可提供经核实的牌面视觉样本，规则、结算、决策样本不混入 2 人验收。四人玩法留待二人局稳定后另行扩展。
- 后续按“辅助建议 → 人确认后代点 → 全自动”逐级验证。当前 `TeaBot` 强制 `dry_run`；用户已授权模拟器操作，但未授权充值或消耗房卡，不能据此开始收费对局。

## 2026-10-18 阿法算牌恢复

- 从 2026-08-20 Chrome 下载记录找回原分享页，重新取得 `阿法算牌_v2.2.5_setup.exe`（264420779 字节）于 `C:\Users\Administrator\Downloads\`；本机 Defender 扫描未报威胁，安装包无数字签名。
- 已重装至原位置 `D:\温州麻将\阿法算牌\`，`阿法算牌.exe` v2.2.5 启动并显示主窗口。安装前卸载注册表备份在 `_backup_20261018_afa_restore\uninstall_before.reg`。
- 这是第三方商业程序，现有安装包不含可供我们修改的源码或开放 SDK 说明；当前 2 人温州茶的 16/17 张规则与识牌尚未用它验证。

## 2026-10-18 当前优先级

- 用户最新明确：以阿法算牌为参照，交付识牌与算牌最小版；沿用 `温州茶（传统麻将）/bot/` 的 2 人局链路，不重复开发新引擎。完整原版阿法未复刻。
- 使用 `morluto/rea` 的静态检查与适用的后续证据，见 `REA_阿法算牌_证据与MVP.md`；不能把资源提取等同于源码、协议或加密模型复原。
- 原版阿法的 2 人／4 人配置、财神、16/17 张手牌和实时识牌仍待实测；4 人局与自动代打另行验收。既有源码与采集数据保留。

## 2026-10-18 最新范围与前端证据

- 用户已重启温州熟目标：只做浙江游戏大厅的温州茶与温州熟，共用一个阿法风格的识牌/算牌桌面工具；其他麻将不做。上文“熟暂停”是历史状态。
- REA 快照哈希配合 Blutter Windows 已从原始 `app.so` 取得 Dart 3.8.1 对象池、部分 UI 文案/闭包及 x64 代码引用。证据见 `阿法算牌分析\Blutter_Windows前端解析_20261018.md`。原版 Dart 工程、页面树与完整协议仍未恢复。
- 茶的当前窗口与 2 人识牌链路可复用；熟的红中规则和视觉模板在 `温州茶（传统麻将）\代打BOT\majiang-bot\brain_v0\engine\wenzhou_rules.py`、`pc_bot\templates_hongzhong_v2\`，目标玩法真实手牌识别和统一窗口尚待验证。保持只读/dry_run。

## 2026-10-18 茶熟共用窗口的离线验证

- `温州茶（传统麻将）\bot\assist_window.py` 现为一个本地窗口，可切换茶 2 人／熟 4 人，抓图、导入截图、手工改牌、保存反馈；`assist_shu.py` 只接已有熟模板与规则引擎。它是自建辅助原型，不是原版阿法前端或加密模型的移植。旧窗口已备份在 `_backup_20261018_shu_unified_ui\`。
- 从 `D:\抖音直播\温州麻将\video_leidian_0_2026_08_21_10_40_55.mp4` 抽帧到 `阿法算牌分析\熟识牌对照_20261018\`：2 帧 14 张暗手逐张人工核对后给出定位建议；4 帧 13 张暗手识别完整并等待摸牌；一帧低置信度、一帧副露张数不符均暂停建议。尚无足够标注样本估算实战准确率，亦未做实战自动点击。
- 原版首页和识别/算法选择窗口的真实像素截图在 `阿法算牌分析\原版窗口_20261018\`。可作为流程参照；不建议继续为完整原版 UI、会员、后端或其他麻将扩张范围，优先验证浙江游戏大厅茶与熟识牌。
- 验证：`python -m pytest bot/tests -q -p no:cacheprovider` 41 passed；熟规则/Advisor/视觉目标测试 33 passed、155 subtests passed。原有茶窗口曾被简化为 400×320 导致测试导入错误，本轮恢复后通过。

## 2026-10-19 采集与训练状态

- 旧采集的 2026-10-12 至 16 日共有 36 个房间目录、555 个逐局目录、1639 张 `frame_*.png`；其中有损坏、重复、4 人局和不完整局，不能直接当作 555 个训练样本。`740195` 已有 16 局 48 帧，无需重采。
- 当前设备观测为 `emulator-5556`。已用模板和双帧复核进入 2 人房间 `833667` 的战绩详情，切换回放选项，录下第 1 局 30 秒可解码视频；见 `温州茶（传统麻将）\采集\2026-10-19\833667\r01\`。日期字形库已适配当日显示的 10 月 15–19 日列表，未知列表仍停止。
- 新视频中两张相邻亮牌帧分别逐张对照为 17/17 与 16/16，一些暗牌／遮罩帧保守弃权。它们来自同一局，不能计算实战准确率。先从旧数据筛选不同房间／局号并人工标注 20–30 个目标画面，再决定是否训练。
- 现用采集脚本尚缺旧数据去重和无人值守批跑验收；茶／熟辅助建议与自动代打仍须分别验证。当前相关测试 `bot/tests` 为 53 passed；单局点击成功不证明 100% 点击准确。
