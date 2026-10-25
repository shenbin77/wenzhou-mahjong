# 项目状态板 STATUS（所有 AI 开工先读本页，收工必更新）

维护规矩：
1. 开工先读本页 + 《开发档案.md》最新条目；进度只认这两处，别信任何人的口头转述。
2. 收工在下方「交接日志」顶部加一条：时间 / 谁 / 做了什么 / 验证数字 / 下一步。没写交接 = 没交工。
3. 同一代码树同一时间只有一个落盘方；要动先在本页「谁在干什么」占位。

## 当前定版
- 活动分支：gpt/hardening-20261025；当前交付以最新 `git log`、`开发档案.md` 和本状态板为准，不再以 02c0d61 当最新代码。
- 提交链：332f228（两段式入口）→ da064e5（自动复盘链）→ 02c0d61（UI 线）→ 683cef9（安全硬化）→ 1a04d65/52656aa（多开隔离）→ 本轮设备/账号/实时日志 UI 收口。
- 16 局零人工：未达标，不得宣称完成（开发档案进度表 #12）

## 角色定位（2026-10-25 定）
- GPT（网页 GPT-6）：主力开发，当前唯一落盘方——写完、自测、提交归它。
- Codex：终审 + 硬骨头，额度回血后只接最难的 bug 与上线前总审，走文件派单。
- Grok：离线副线（金标/回测/数据），成果经 Hana 验收才进主线，不碰实机回路。
- Hana：调度、盯状态板、验收、实机盯场、向 BOSS 一页纸汇报；不与落盘方抢同一棵树。
- BOSS：只拍板（方向/开打/身份关）。
- 通知方式：Codex 由 Hana 直派（PC 直连 runner）；GPT 读本板（急事 BOSS 催一句）；Grok 由 BOSS 转达派单文件（_takeover 里 Hana 备好）。

## 谁在干什么（2026-10-25 14:17）
- **2026-10-25 16:xx：用户令 GPT 继续接管；Codex 14:57 后无新提交、工作区已跟踪文件稳定。GPT 恢复为当前唯一落盘方。** 本轮只推进未提交的 `lobby_seat.py` / 大厅模板 / 房卡门禁 / 对应测试，再由单线集成进辅助器；不重写 Codex 已提交的身份门、HUD 和主 BOT。
- **Codex：当前唯一落盘方，正在收口网页版 GPT 留下的设备连接 UI 增量。** 网页 GPT 必须暂停对当前工作区写入；后续只接不重叠的只读审计/离线数据任务，或另开独立 worktree。
- 启动器实测发现旧版辅助器 PID 34784 仍开着，执行 --all 按安全设计返回 2、拒绝混开；未终止进程、未启动新 UI、未重启雷电。在线设备仍为 5556/5558。用户/现场人员关闭旧辅助器窗口后再双击启动多开辅助器.bat。
- Hana（Muse）：调度、实机盯场和验收；下一步安排实机分级验收，本轮不改代码树。w5 没有 live 模板，手动指定财神菜单保留。
- Codex：额度已尽，用户令暂停审查。
- WorkBuddy：已停手（用户转达）。

## 卡点与待办
- 财神入库已提交：templates/caishen/manifest.json（63 条）+ w4/xi 两张 live 模板图，提交 2087281。模板审计未发现缺失文件或 SHA 不符；w5 有历史模板条目但**没有 w5 live 图**（语料为零），手动指定财神菜单保留。
- 本轮全量回归由 GPT 实跑 **1105 passed / 1 skipped**，日志 _takeover/gpt_multi_final_tests_20261025.log；最新定向多开/大厅/安全测试 **48 passed**。该数字是离线回归，不是实机代打验收。
- 实机验收整包未开：吃链 --claim-chi 首验 / 局间接缝 / 面板重启恢复 / 邀请拒绝首证 / SPEC-005 延迟 p95 复测 / 16 局零人工 / 房卡与登录标定帧。
- 帧资产与金标：金标真值仅 11 帧手牌 + 40 点击标注；C 池 flags 帧待复核升金标（见 _takeover/frame_assets/）。

## 交接日志（最新在上）
- 2026-10-25 18:04 GPT：接管 Grok/Muse 提交 `2db3575` 后只修两处已被真帧证明的实机误停，不重写现有 BOT。① 5558 普通出牌：实机 `tap_probe/20261025_171604_461642` 证明第一击后 `w9` 已从完整手牌消失、同时新摸入 `w6`，暗手仍14张；新增 `direct_discard_redraw_proof` + TapExecutor 的可选 `post_select_validator`，只有玩法/财神一致、目标牌恰少1张、且恰好多1张新牌时才把第一击判为已出牌并禁止第二击。② 5556 吃：实机 17:15 连续帧证明吃后副露 `[1,2,3]` 已出现、claim圆盘/候选面板均消失，但客户端把暗手整体压暗（正常 p70(V)=214 / bright≈77%，压暗 p70=100 / bright≈6.68%），导致旧识牌只读6张；新增**只用于吃后迁移验证**的压暗态强证据，要求合法顺子副露 +1组/+3张、圆盘0、面板false、固定ROI压暗，绝不降低全局识牌阈值。真帧离线复现两条均通过；TapExecutor+AutoPlayLoop+Chi 136 passed，Grok claim/下一局/大厅联合 172 passed，最终全量 `python -m pytest -q tests -p no:cacheprovider` → **1160 passed in 97.69s / 0 failed**，日志 `_takeover/gpt_takeover_full_20261025.log`。下一步：提交本轮5个 tracked 文件+文档，只重启两个辅助器加载新代码（不重启雷电），继续实机验证普通出牌/吃碰胡过；`round_transition` 仍因真实 `templates/round_transition/next_round.png` 缺失而 fail-closed，不得宣称跨局已实机通过。
- 2026-10-25 14:25 Codex：前端闭环审计确认“当前玩家/16局进度/累计积分”必须进入主界面。修复旧版把 `雷电5556` 当账号的迁移问题，设备占位身份自动失效并要求真实玩家标记；新增常显本场进度与积分栏，结算分尚未可靠识别时明确显示“待结算识别”。实际结算积分 OCR 待下一场真帧标定，属于16局正式验收门槛。最终全量 **1120 passed in 85.10s**。
- 2026-10-25 14:17 Codex：审计并接管网页版 GPT 未提交的设备连接 UI 增量。恢复独立账号标记，不用雷电号冒充玩家；serial 改由启动器自动固定，移除主界面雷电下拉/刷新；日志永久自动尾随，移除开关和手动刷新。相关专项 **69 passed**，全量独立 basetemp 复跑 **1118 passed in 91.19s**，编译/pyflakes/diff check 通过。玩家名自动 OCR 尚未实现；当前填真实游戏昵称并按 serial 记忆。窗口需关闭后重开，雷电无需重启。
- 2026-10-25 13:34 GPT：收到 BOSS「Hana 已停」后验证 `run_auto_play_dev.py` 无活动进程，两台雷电在线（emulator-5556 / emulator-5558）；先通过 Windows WM_CLOSE 仅关闭旧版无 serial 参数的辅助器进程，不重启雷电/不操作游戏。发现 AgentDock 短时命令拉起的 Pythonw GUI 会随命令会话结束被回收，改用长时受控会话分别启动**同一套** `assist_window.py --serial emulator-5556` 与 `--serial emulator-5558`。现场验证两套窗口均可见、响应正常：PID 14364（5556）、PID 35644（5558），Windows 交错排布。两条 GUI 会话为 session-e69181dad1cba66c0ecb71c5 / session-6d0be9f1758dfffbd9876957；退出会话会关闭相应窗口。**未开始 BOT 真实点击、未启动游戏、未消耗房卡**。下一步由用户在两个窗口分别绑定对应账号，后续按门禁开展实机测试；若要脱离 AgentDock 长会话持续运行，需从交互式 Windows/桌面快捷方式启动。无业务代码修改，本次无新测试运行。
- 13:24 GPT：最终安全复核新增两道 fail-closed：跨设备目录里的错 serial 绑定会被清除，不允许窗口悄悄切到另一台模拟器；多开启动器无法加载 psutil/检查进程时拒绝启动，防止与旧版全局配置 UI 混开。代码提交 52656aa。全量日志 _takeover/gpt_multi_final_tests_20261025.log：**1105 passed / 1 skipped in 101.95s**；定向回归 48 passed；py_compile 与 git diff --check 通过。启动器现场检查发现旧 UI PID 34784，因此 --all 安全拒绝，没启动新 UI/没重启雷电；设备 5556/5558 在线。下一步先关闭旧辅助器，再启动每设备独立窗口；自动找位/入座/开局、房卡3/8实帧标定、16局零人工及其余实机验收仍未完成。
- 13:20 GPT：收尾提交完成。分支 gpt/hardening-20261025；多开/只读大厅桥接提交 1a04d65，财神 manifest（63 条）+ w4/xi live 模板提交 2087281；状态板与开发档案随本收尾提交更新。全量 pytest 日志 _takeover/gpt_multi_final_tests_20261025.log：**1104 passed / 0 failed**；资产审计/大厅预检/多开隔离专项 **25 passed**；manifest 引用文件存在且 SHA 匹配。仍未完成：自动找位/入座/开始游戏的真实点击执行器、房卡 3/8 字形与余额实帧标定、自有账号避碰、吃链/局间/面板恢复/邀请拒绝/SPEC-005 p95 等实机复验、16 局零人工验收。未 push；没有清理临时目录或采集数据。下一步由 Hana 安排实机分级验收，旧 UI 需关闭后重新启动以加载新代码；不得把离线全绿等同实机通过。
- 12:51 Hana：建本状态板。此前今日成果：自动复盘链 da064e5、UI 线 02c0d61、帧资产化交付、财神 w4/xi 挖矿+A/B 验证通过并 promote 到工作区、GPT 审查文件 gpt_review_out.md 已落盘（语料门收紧等意见质量高）。下一步：等 GPT 收工交接，再排实机验收。


## 2026-10-25 17:40 收尾（接手 muse）
- claim 来牌恢复：discard_vision.read_opponent_sparkle_tile + action_policy.resolve_incoming 兜底；真帧 20261025_165416_996466 → t6 score 0.8845 / margin 0.4166（门槛 0.86/0.10），回归测试 tests/test_sparkle_recovery.py（含无闪光/多闪光/错位/低置信负例）。
- 新模块 round_transition.py：局间「下一局」三重门（模板≥0.90 + 右下几何/尺寸/橙色占比 + 结算场景信号且非等候室），100–250ms 复核帧，单击、永不重复，点后需“按钮消失+新局证据”，否则 post_verify_uncertain 永久停手；第16局/无按钮→terminal 不点；报告写 session/round_transition.jsonl。
  - **未完成：templates/round_transition/next_round.png 缺失**——dev_auto_play 4.7k 帧扫描无真实局间页，未伪造模板；缺模板时模块拒绝点击（template_missing）。采到局间帧后：python round_transition.py crop <frame> x0 y0 x1 y1。
- 房卡策略 round_transition.room_card_policy：>0 允许、0 禁止开新局、≤3 警告、读不出允许并记 room_cards_unknown；不阻拦入座。room_hud_digits 仍缺数字 3、8 模板。
- assist_window：全自动状态「全自动跟随中 · BOT 已接管识牌/决策/点击」、按钮「停止全部」/「关闭全自动（恢复只读推荐）」；全自动因 gate_turn 结算停手→后台执行下一局→next_round() 簿记→重开跟随→新局财神读出后自动 _start_full_auto（门禁全保留）。
- 控制文件续接：python full_auto_control.py resume|stop emulator-5556 → D:\温州麻将\_control\full_auto_<serial>.cmd，窗口 500ms 消费一次，resume 仍走全部门禁。
- auto_play_loop 仅新增 summary.stop_gate 字段（无算法改动）。
- 17:15 实机停手分析：
  1. 5556 chi_panel_missing：单候选吃已直接完成（副露 1-2-3 万读出正确），但吃后游戏把 13 张暗手置灰、只亮 1 张，识牌对置灰牌 score 0.37–0.49 → 暗手读成 6 张，L3 判 16→6≠−2。轮询窗口不是问题（6 帧均已成吃），真正缺口是“吃后置灰手牌”的识别/校验；按 HARD DON'T 未改阈值/验证器，保持 fail-closed。
  2. 5558 tap_aborted：第一次点 9万 box 与 fresh 帧一致（非旧几何）；点后帧 9万已离手、右侧出现新摸 6万 → 牌已直接打出/回合已推进，而验证器期望“抬起12px”，故拒绝第二击。正确 fail-closed；后续需让点后验证接受“目标牌已离手+暗手变化”为成功证据。
- 测试：目标集 389 passed；全量 1157 passed（本次无既有失败）。
- 仍需实机：下一局模板采集与多局续局（2–3 局、16 局 soak）、吃/碰/胡/杠实机闭环、上述两类停手修复。
