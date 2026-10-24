# WorkBuddy 并行任务交接（2026-10-24）

目标：加速温州茶 2 人自用辅助与代打。Codex 正在维护实时主链路，请 WorkBuddy 只做下列独立任务。项目入口为 `D:\温州麻将\AGENTS.md`、`D:\温州麻将\HANDOVER.md`、`D:\温州麻将\SPECS\QUEUE.md`。已保存截图、日志与录屏不得覆盖或删除。

## 当前已核实状态

- `bot/tests` 最新全量结果为 **180 passed / 4 failed**。四个失败仍是 `test_spec005_live_v2.py` 的既有测试替身问题：`test_window_records_capture_start_and_tk_idle_in_refresh_latency`、`test_stop_invalidates_the_last_live_advice`、`test_offline_on_frame_never_seeds_continuous_round`、`test_show_calls_refresh_view_with_the_new_state`。不要把它们算成新回归。
- 茶 2 已有 ADB 原生截图、只读连续识牌、单次人工确认的 `TapExecutor`；**没有可实战托管的连续代打**。碰、杠、胡、过的自动动作尚未验证。不能因为离线通过就启动自动点击。
- 2026-10-24 手选八筒的实战财神模板已目视复核入库；同局 5 帧识成八筒，旧二筒样本仍识成二筒。其它局仍可能读不出财神，必须弃权。
- Codex 已修改但可能尚未被旧辅助器进程加载：`bot/assist_window.py`（手选按钮）、`bot/assist_tea.py`、`bot/tea_bot.py`、`bot/live_capture.py`、`bot/recorder.py`（副露组进入候选与记录）。这些文件由 Codex 维护，WorkBuddy 不要同时写。

## 任务 A：实战识牌候选核对（只读）

读取 `D:\温州麻将\温州茶（传统麻将）\采集\assist_sessions\` 中的原生 1600×900 截图，特别是 `20261023_235121` 与 `diagnostics/20261024_tea2_probe`。输出到新文件 `D:\温州麻将\温州茶（传统麻将）\采集\workbuddy_review_20261024\frame_review.csv`，每帧记录原路径、SHA-256、房号/局数是否可读、画面是否压暗、财神候选、暗手数量、副露组数量与每组牌、无法确认处。模型判断只是**候选**，不得自动入生产模板或当作金标。优先找清晰、非压暗、不同局的漏识样本；把需要用户核实的牌面单列。

## 任务 B：碰杠胡过画面清单与只读检测

只在已有截图/录屏中找出茶 2 的行动按钮与等待状态。先写 `D:\温州麻将\温州茶（传统麻将）\采集\workbuddy_review_20261024\action_inventory.md`：每类至少给原图路径、SHA-256、可见文案/图标、原生坐标、前后帧和不确定性。样本足够时才在新文件 `D:\温州麻将\温州茶（传统麻将）\bot\tea2_action_vision.py` 及独立测试文件 `D:\温州麻将\温州茶（传统麻将）\bot\tests\test_tea2_action_vision.py` 实现**只读分类或弃权**；不要调用 ADB tap、不要猜按钮固定坐标、不要改现有识牌或决策入口。无法找到可靠样本就只交清单。

## 任务 C：修复四个 SPEC-005 旧测试替身

WorkBuddy 可独占编辑 `D:\温州麻将\温州茶（传统麻将）\bot\tests\test_spec005_live_v2.py`，仅修测试替身缺失的方法/字段，使上述四项真正测试现有窗口契约；不为通过测试改运行时代码。先备份该文件并记录 SHA-256。工作目录设为 `D:\温州麻将\温州茶（传统麻将）`，完成后跑 `python -m pytest bot/tests/test_spec005_live_v2.py -q -p no:cacheprovider --basetemp C:/Users/Administrator/AppData/Local/Temp/workbuddy_spec005_20261024`，报告新增/减少的失败和每项原因。Codex 在此期间不编辑这个测试文件。

## 并发边界与交付

- 不操作雷电模拟器或手机，不开始新游戏，不充值，不自动点击，不停止或重启辅助器进程。
- 不改 `bot/assist_window.py`、`bot/assist_tea.py`、`bot/tea_bot.py`、`bot/live_capture.py`、`bot/recorder.py`、`bot/tap_executor.py`、财神生产 manifest。任务 B 的新文件与任务 C 的测试文件可独立写。
- 每项交付写明：已验证事实、截图来源、改动文件、测试结果、未验证项。不要宣称代打已可用。
