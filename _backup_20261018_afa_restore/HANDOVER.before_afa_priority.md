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
│     ├─ harvest_continuous_v3.ps1  match_template.py  uiauto_helper.py …
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

## 3. 回放采集流程（茶）
路径：大厅 **比赛场 → 进入 → 我的比赛场 → 详情 → 战绩 → 查看回放**，逐局截帧。
- 每一步用 cv2 模板匹配定位按钮：`match_template.py`，阈值 **0.82**。
- 主脚本：`采集\harvest_continuous_v3.ps1`（.bak_* 为历史版本）。
- 找不到模板 → 输出 `TEMPLATE_MISS` 并**停止**；**没有坐标盲点兜底**（故意移除，防止误点充值/开始游戏）。
- 模板：`采集\templates\`（ui 子目录含 `WIRED.md`，说明哪些模板已接入脚本）。

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

## 2026-10-18 当前开发范围
- 温州茶第一版聚焦 **2 人局辅助打**：截图识牌、校验、给出建议，由人手动操作；沿用 `温州茶（传统麻将）/bot/tea_bot.py` 的离线识牌与决策链路。
- 4 人局回放仅可提供经核实的牌面视觉样本，规则、结算、决策样本不混入 2 人验收。四人玩法留待二人局稳定后另行扩展。
- 后续按“辅助建议 → 人确认后代点 → 全自动”逐级验证。当前 `TeaBot` 强制 `dry_run`；用户已授权模拟器操作，但未授权充值或消耗房卡，不能据此开始收费对局。

## 2026-10-18 阿法算牌恢复

- 从 2026-08-20 Chrome 下载记录找回原分享页，重新取得 `阿法算牌_v2.2.5_setup.exe`（264420779 字节）于 `C:\Users\Administrator\Downloads\`；本机 Defender 扫描未报威胁，安装包无数字签名。
- 已重装至原位置 `D:\温州麻将\阿法算牌\`，`阿法算牌.exe` v2.2.5 启动并显示主窗口。安装前卸载注册表备份在 `_backup_20261018_afa_restore\uninstall_before.reg`。
- 这是第三方商业程序，现有安装包不含可供我们修改的源码或开放 SDK 说明；当前 2 人温州茶的 16/17 张规则与识牌尚未用它验证，只能作为候选对照工具，不能直接替换本地识牌和决策链路。
