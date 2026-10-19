# 麻将 Bot 项目交接文档

> **项目**: 浙江游戏温州放炮麻将自动代打
> **交接日期**: 2026-10-15
> **GitHub**: https://github.com/shenbin77/majiang-bot （私有库）
> **本地路径**: `D:\HanaAgent\majiang-bot`
> **目标玩法**: 温州红中放炮麻将（红中=固定财神，可替代任意牌）

---

## 一、项目概述

用 PC 雷电模拟器 + ADB 实现浙江游戏 App 内温州放炮麻将的自动代打。

**技术链路**:
```
雷电模拟器 (1600×900)
  → ADB 截图 (adb exec-out screencap)
  → OpenCV 牌面识别 (tile_vision.py + templates_hongzhong_v2/)
  → 本地决策引擎 (brain_v0/engine/: 向听/攻守/危险度/模拟)
  → ADB 点击出牌 (adb shell input tap)
  → 2秒一轮主循环，16局后自动停止
```

**与赤雀麻将馆完全隔离**，不混用代码、规则、策略。

---

## 二、目录结构

```
D:\HanaAgent\majiang-bot\
├── AGENTS.md                    # Agent 协作规则（命令、约束、变更记录）
├── .gitignore                   # 已排除: artifacts/ frames_*/ logs/ 等大文件
├── brain_v0/                    # 决策引擎（167 个 py 文件）
│   ├── engine/                  # 14 个核心引擎模块
│   │   ├── advisor.py           # 出牌建议（攻守策略）
│   │   ├── shanten.py           # 向听数计算（含红中财神逻辑）
│   │   ├── scoring.py           # 点数制计分（8月规则）
│   │   ├── danger.py            # 危险度评估
│   │   ├── simulator.py         # 蒙特卡洛模拟
│   │   ├── tiles.py             # 牌定义
│   │   ├── opponent_model.py    # 对手建模
│   │   ├── play_style.py        # 风格配置
│   │   ├── storage.py           # SQLite+JSONL 对局存储
│   │   ├── game_log.py          # 对局日志
│   │   └── ...
│   └── tests/                   # 16 个测试文件
├── pc_bot/                      # Bot 运行端（72 个 py 文件）
│   ├── bot.py                   # 主入口 (610行)，2秒主循环
│   ├── tile_vision.py           # 新识牌模块 (294行)，颜色感知
│   ├── observation.py           # 画面观察/状态机
│   ├── adb_transport.py         # ADB 传输层
│   ├── runtime_guard.py         # 运行 guard
│   ├── dataset_audit.py         # 数据集审计
│   ├── settle_ocr.py            # 结算分 OCR（未完成）
│   ├── templates_hongzhong_v2/  # ✅ 当前生效模板：34 个 PNG（63 含变体）
│   │   └── manifest.json
│   ├── templates_new/           # 旧模板（含错标，仅参考）
│   ├── _old_templates/          # 已废弃
│   ├── tests/                   # 7 个测试文件
│   ├── tools/                   # 采集/审计工具
│   └── logs/                    # 运行日志（git 已排除）
└── .codex_backups/              # Codex 备份（git 已排除）
```

---

## 三、已完成事项（带验证证据）

### 3.1 决策引擎 brain_v0 ✅

| 项目 | 状态 | 证据 |
|------|------|------|
| 向听数计算（含红中财神） | ✅ 通过 | `pytest brain_v0/tests` 131 passed, 13 skipped, 0 failed (2026-10-14) |
| 点数制计分（8月规则） | ✅ 通过 | 13/14 个真实结算局验证通过（第10局无结算画面） |
| 攻守策略 advisor | ✅ 通过 | 同上测试套件 |
| 危险度评估 danger | ✅ 通过 | 同上测试套件 |
| 蒙特卡洛模拟 simulator | ✅ 通过 | 同上测试套件 |
| 对局存储（SQLite+JSONL） | ✅ 已接入 | bot.py 有 round_start/每手/round_end 完整记录 |

**规则基线**（用户 2026-10-14 拍板）:
- 4人、16局、112张（万筒条各36 + 红中4）
- 13张起手，不能吃，可碰杠
- 红中=固定财神（可替代任意牌）
- 无扎码
- 点数制：底分1分；自摸每闲家输2分/赢6分；七小对/对对胡/清一色/杠上花/单吊/天胡/地胡一律2倍；杠分另计
- 公式：总分 = 底分×胡牌倍数 + 庄家1分 + 杠分

### 3.2 识牌模块重构 ✅（Codex 完成）

| 项目 | 状态 | 证据 |
|------|------|------|
| 新颜色感知方案 tile_vision.py | ✅ 完成 | 294行，替代旧灰度模板方案 |
| 34 个新模板 | ✅ 完成 | `templates_hongzhong_v2/` 34 PNG + manifest.json，63 含变体 |
| 实机 17/17 识别 | ✅ 通过 | 2026-10-14 用真实帧验证，置信度 0.86–0.99，margin > 0.14 |
| s8/s9/t7 模板 | ✅ 已包含 | 在 34 个模板内 |
| bot.py 接入新模块 | ✅ 完成 | `from tile_vision import TileVision` |

**旧方案已作废**: 旧31模板在实机框最高分仅 0.25–0.35，降阈值是错误方向。

### 3.3 下一局误报修复 ✅（Codex 完成）

| 项目 | 状态 | 证据 |
|------|------|------|
| check_xiayiju 重写 | ✅ 完成 | ROI收紧到 (1120,740)-(1490,885)，阈值 0.96，+暗桌面检测（灰度均值<95） |
| 排除误报位置 | ✅ 完成 | 注释明确排除游戏中的 (1069,676) 和大厅的 (943,732) |

### 3.4 托管误报修复 ✅（Hana 完成）

| 项目 | 状态 | 证据 |
|------|------|------|
| tuoguan_check 设为 False | ✅ 完成 | bot.py 第102行已确认 `"tuoguan_check": False` |
| 不再每2秒点 (800,450) | ✅ 完成 | 同上 |

### 3.5 基础设施 ✅

| 项目 | 状态 | 证据 |
|------|------|------|
| 雷电模拟器 + ADB | ✅ 就绪 | 路径 `D:\leidian\LDPlayer14\adb.exe`，设备 `emulator-5554`，分辨率 1600×900 |
| pc-server.py | ✅ 就绪 | `D:\HanaAgent\pc-server.py`，端口 8099，`/shot` `/tap` `/swipe` |
| win_agent v3.2 | ✅ 就绪 | C/D全盘读写 + 管理员执行 + 截图 + 键鼠 |
| MUSE连接助手 tray_app.py | ✅ 就绪 | 统一管理三服务（Hana小助手18789 + cloudflared + pc-server 8099） |
| GitHub 同步 | ✅ 完成 | `shenbin77/majiang-bot` 私有库，2026-10-15 推送 |

### 3.6 动作按钮模板（部分）

| 项目 | 状态 |
|------|------|
| btn_guo.png（过） | ✅ 已制作 |
| btn_peng.png（碰） | ❌ 未制作 |
| btn_gang.png（杠） | ❌ 未制作 |
| btn_hu.png（胡） | ❌ 未制作 |

---

## 四、待办事项（按优先级）

### P0 - 阻塞联调

| # | 事项 | 说明 | 负责人 |
|---|------|------|--------|
| 1 | **账号金币充值** | 当前仅 150 金币，进不了任何场（新手场需 300+）。**必须用户手动充值**（真钱操作） | 用户 |
| 2 | **联调实测** | 静态图认出14张手牌 → 开bot → 看前3轮日志出现识牌/向听/出牌 → 确认不再乱点 | Hana |
| 3 | **bot 进程恢复** | 最后日志（2026-10-14 22:55）显示 ADB 超时崩溃，当前无 Python 进程运行。需重启模拟器 + bot | Hana |

### P1 - 功能补全

| # | 事项 | 说明 |
|---|------|------|
| 4 | 碰/杠/胡按钮模板 | 从实机截图制作 `btn_peng.png` `btn_gang.png` `btn_hu.png` |
| 5 | 结算分 OCR | `settle_ocr.py` 已有框架，需正样本字形模板才能读分 |
| 6 | 下一局端到端验证 | 新 check_xiayiju 逻辑尚未在真实结算画面验证过一次 |

### P2 - 完整验收

| # | 事项 | 说明 |
|---|------|------|
| 7 | Shadow 模式 | 只识别记录、不点击，跑一局验证 |
| 8 | 监督一局 | 每次点击保存前后截图，连续10次正确出牌 |
| 9 | 完整16局 | 识牌→决策→点击→动作按钮→结算→下一局→第16局停止 |
| 10 | 目标玩法确认 | 当前验证多在温麻4人新手场，正式放炮麻将需单独验收 |

---

## 五、运行方法

```powershell
# 工作目录
cd D:\HanaAgent\majiang-bot

# 单次识别测试（不点击）
python pc_bot/bot.py --once --serial emulator-5554

# 只观察不操作
python pc_bot/bot.py --observe-only --serial emulator-5554 --max-rounds 16

# 正式运行（16局后自动停）
python pc_bot/bot.py --serial emulator-5554 --max-rounds 16

# 跑测试
python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider
```

**依赖**: `opencv-python`, `numpy`

**启动前检查**:
1. 雷电模拟器已启动，ADB 能看到 `emulator-5554`
2. 浙江游戏大厅已登录（包名 `com.xm.zjgamecenter`）
3. 账号金币 ≥ 300
4. 按进程命令行确认 bot 进程，勿用 `taskkill /F /IM python.exe`（会误杀 pc-server）

---

## 六、已知问题与坑

1. **ADB 设备不稳定**: `127.0.0.1:5557` 曾离线，`emulator-5554` 也偶发超时。bot.py 应做设备自动检测，不要硬编码。
2. **旧模板目录别用**: `templates_new/` 含错标，不能当标签真值。认准 `templates_hongzhong_v2/`。
3. **金币消耗**: 新手场每局有消耗，bot 长时间运行需关注余额。
4. **pc-server 隧道偶发 502**: MUSE连接助手重启可恢复。bot 本身走 ADB 直连，不依赖隧道。
5. **温麻4人 ≠ 目标玩法**: 新手场只用于验证流程，正式验收必须在放炮麻将房。
6. **不要多人同时改 bot.py**: 之前 Codex 和 Hana 并行改出过冲突。

---

## 七、关键决策记录

| 日期 | 决策 | 拍板人 |
|------|------|--------|
| 2026-10-14 | 弃云手机/屏幕助手App，改 PC雷电模拟器+ADB | 用户 |
| 2026-10-14 | 规则以8月点数制为准，10月番数制截图作废 | 用户 |
| 2026-10-14 | 温麻4人新手场只做流程验证，不做正式验收 | 用户 |
| 2026-10-14 | 完成16局后停止，不自动建房续桌 | 用户 |
| 2026-10-14 | 不做同桌多号串通 | 用户 |
| 2026-10-14 | 旧灰度模板方案作废，改颜色感知方案 | Codex 执行 |
| 2026-10-14 | tuoguan_check 设为 False | Hana 执行 |
| 2026-10-15 | 全部代码同步到 GitHub 私有库 | 用户指令 |

---

## 八、交接清单

- [x] 全部代码推送到 `https://github.com/shenbin77/majiang-bot`（私有）
- [x] 大文件已排除（artifacts/ frames_*/ logs/ 等 800MB 不在库内）
- [x] `.gitignore` 已配置
- [x] 本文档已交付
- [ ] **待用户**: 账号金币充值（≥300）
- [ ] **待 Hana**: 重启模拟器 + bot，做联调实测
- [ ] **待 Hana**: 制作碰/杠/胡按钮模板
- [ ] **待 Hana**: 完整16局验收

---

## 九、联系人与资料

- **回放资料**: `D:\archive_温州麻将202608\`（旧项目，15个md规则文档 + 录屏 + 291MB对局回放）
- **规则基线**: `D:\archive_温州麻将202608\02_游戏规则对齐.md`
- **用户16局实战**: 2026-10-14 用户亲自打完16局（房间302805），回放码如 72321415
- **远程控制**: winagent `https://winagent.clawskill.cloud` / 模拟器 `https://pcwin.clawskill.cloud/`

---

*文档生成: 2026-10-15 by Hana*
*代码状态: 以 GitHub main 分支为准*
