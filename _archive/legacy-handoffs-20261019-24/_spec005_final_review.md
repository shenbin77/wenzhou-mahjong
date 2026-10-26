# SPEC-005 实时辅助窗 v2：终审与全量回归

- 审查日期：2026-10-21
- 审查基线：`_backups/spec005_20261020_231616`
- 实现报告：`_spec005_impl2_out.txt`（仅作线索，结论均以磁盘、源码和本次测试为准）
- 裁决：**打回**

## 一、结论摘要

红线守住，既有回归通过，但 SPEC-005 的核心交付尚未成立：

1. 正常新帧只更新文字变量，没有调用牌面、推荐大牌和 Top-3 的统一渲染入口。`_refresh_view()` 只有定义、全文件无调用；因此 R3 实际不可见，R4 也没有落到窗口，R5 的灰化与旧图清理不完整。证据：`温州茶（传统麻将）/bot/assist_window.py:593-616`。
2. “财神识别失败后手选”没有接入分析链路。手选值只进入任务上下文，`_process_frame()` 调用 `capture_result()` 时不传财神；而 `analyze_bytes()` 固定以空财神调用茶分析。下拉即使选择，也无法让后续帧获得建议。证据：`assist_window.py:411-429,521-545`；`live_capture.py:157-194`。
3. 窗口不是“开窗即自动连续刷新”：初始化明确 `continuous=False`，必须人工确认、填账号并点开始。证据：`assist_window.py:136,395-409,487-494`。
4. 当前版删除了旧版 `start/stop` 对 `continuous_round` 的清空；`next_round`、玩法切换也不清该锁。第一次锁定 HUD 后停止再开始或进入下一局，可能把新局与旧指纹比较并立即停止。证据：现版 `assist_window.py:330-340,370-374,447-457,487-501`；基线 `assist_window.py:487-498`。
5. 没有新增任何 SPEC-005 单元测试。`pytest -k spec005` 实际为 `106 deselected`；实现报告所称“专项 77”是 6 个既有测试文件，不含 `live_state.py`、`tile_render.py`、`live_capture.py` 或 v2 UI 接线测试。
6. 验收要求的“真机一局不少于 20 个决策点、手牌变化到界面更新”的延迟证据不存在。留存的真机 20 帧全部是 `abstain`，离线 20 次只是同一冻结帧重复回放；计时也没有覆盖轮询等待与 Tk 渲染。
7. 验收要求的亮牌局面完整窗口截图不存在。现有 `.pytest_tmp/render_probe.png` 仅为 316×114 的牌面合成图，`test_out/spec005_probe/*.png` 是 1600×900 原始游戏帧，均不是同时包含牌面、推荐高亮、财神和延迟栏的窗口截图。

以上均为主路径缺陷或缺失验收证据，不能以“有条件通过”放行。

## 二、基线 diff 与交付物核对

基线目录有 `assist_window.py`、`assist_tea.py`、`assist_shu.py`、`recorder.py` 四个文件。磁盘对比结果：

| 文件 | 对比结果 |
|---|---|
| `bot/assist_window.py` | 已改；相对基线 672 行新增、301 行删除 |
| `bot/assist_tea.py` | 与基线 SHA-256 完全一致：`033E046B602ED09727789B60CF8B4453C358C51C158D5D55EE17C43BEBC89D7A` |
| `bot/assist_shu.py` | 与基线 SHA-256 完全一致：`7CB17E9F96F76E1FE04C3898A9BDE94CBF841699537E3B490868FD0C422563A3` |
| `bot/recorder.py` | 与基线 SHA-256 完全一致：`6F8EFF1C7AA389070152468472FBEAD3790D0CB50C0A92AB5CF565A68B9EBD89` |
| `bot/live_state.py` | 新增 |
| `bot/tile_render.py` | 新增 |
| `bot/live_capture.py` | 新增 |
| `bot/bench_spec005_latency.py` | 新增 |
| SPEC-005 测试文件 | **未新增** |

实现报告称曾更新 `SPECS/QUEUE.md`，但终审开始时磁盘上的队列没有 SPEC-005 行；本次按终审要求补入“终审打回待回改”。

## 三、R1–R9 逐条审查

| 要求 | 判定 | 证据与说明 |
|---|---|---|
| R1 显示与记录解耦 | **部分符合** | `_on_frame()` 先 `_show()`、后 `_record_result()`，且 recorder 仍自行做双稳定帧状态机，方向正确（`assist_window.py:578-591`；`recorder.py:362-460`）。但 `_show()` 只更新文字变量，未调用 `_refresh_view()`，不构成“每处理完一帧立即渲染”完整画面（`assist_window.py:593-616`）；窗口也未自动开始连续刷新（`assist_window.py:136,487-494`）。 |
| R2 延迟可观测 | **部分符合** | 有滚动统计和最近帧文字（`live_state.py:37-100`；`assist_window.py:693-701`），也写了 3 秒逻辑（`live_state.py:195-205`；`assist_window.py:703-721`）。但 `_tick()` 以 `last_render_key is not None` 为前提，而该 key 只在未被调用的 `_render_hand()` 中赋值（`assist_window.py:665-678,706-721`），所以正常运行时“X 秒前更新”和红色超时警告不启动。另三段边界不真实：`analyze_snapshot()` 已同时完成识牌和 advisor（`assist_tea.py:122-184`），返回后才标记 `recognize`，所谓 `advise` 实际只覆盖财神二次识别和候选归一化（`live_capture.py:173-194`）。 |
| R3 牌面渲染 | **不符** | 辅助函数确实实现了模板牌面、1.6 倍推荐、财神金框和理由（`tile_render.py:21-26,114-161,190-221`），单独的合成图也可生成；但窗口主路径 `_show()` 不调用唯一的 `_refresh_view()`，故新帧不会刷新这些图（`assist_window.py:593-616`）。 |
| R4 Top-3 候选 | **部分符合** | 数据源正确复用 `recorder.normalize_candidates()`，显示状态再截 Top-3（`live_capture.py:144-154`；`live_state.py:207-217`；`recorder.py:92-126`）。但候选图渲染入口未被主路径调用，窗口没有实际更新（`assist_window.py:611-616,686-688`）。 |
| R5 状态诚实 | **部分符合** | `LiveRefreshState` 对等待、缺财神、无定位、低置信和一般弃权建立了非 actionable 状态，并能把过期建议清空（`live_state.py:182-205,241-275`）。但窗口未调用 `_apply_visibility()`，灰化和手选行不会随新状态更新；3 秒 stale 分支也被 `last_render_key` 条件挡住（`assist_window.py:637-663,703-721`）。文字建议会更新，但图形区不能保证不残留旧状态。 |
| R6 财神自动与失败手选 | **部分符合** | 茶调用 `detect_caishen`，熟固定红中，标题也有来源显示（`live_capture.py:129-141`；`assist_window.py:618-624`）。失败手选不成立：手选值没有传给 `_process_frame/capture_result/analyze_bytes`（`assist_window.py:411-429,521-545`），且失败后的展开由未被主路径调用的 `_apply_visibility()` 触发（`assist_window.py:637-663`）。 |
| R7 窗口形态 | **部分符合** | 默认 440×560、置顶、原生可拖动、保存位置，玩法在顶部且切换会清显示（`assist_window.py:38-40,117-127,180-198,370-374,860-870`）。但轮次指纹锁没有随玩法切换、下一局或重新开始而清空，破坏切换/重启后的本局状态（`assist_window.py:330-340,370-374,447-457,487-501`）。 |
| R8 反馈行 | **符合** | “我实际打的牌”、可用/不对/识错、记录目录按钮均保留（`assist_window.py:242-259`），反馈仍追加到独立 `feedback.jsonl`，打开目录行为保留（`assist_window.py:48-64,844-858`）。 |
| R9 只读与不改阈值 | **符合** | 新链路的 ADB 子进程仅 `dumpsys` 与 `exec-out screencap -p`（`live_capture.py:107-124,266-291`），未发现点击、发键或输入调用。茶分析仍强制 `TeaBot(dry_run=True)`（`assist_tea.py:122-123`）；`tea_bot.py:117-128` 仍拒绝非 dry-run；子仓库 `pc_bot/bot.py` 未改且入口仍 `Bot(dry_run=True)`（`pc_bot/bot.py:542`）。阈值仍为 `.82/.055`（`bot/tile_vision.py:127-130`）。 |

## 四、红线核查

| 红线 | 结论 | 核查依据 |
|---|---|---|
| templates 未改 | **符合（按现有基线可核范围）** | 茶/熟模板目录没有文件晚于 2026-10-20 23:16:16 基线；茶模板最新文件为 2026-10-19 19:33:13。子仓库 `pc_bot/templates_hongzhong_v2` 对 `git status --short` 无改动。基线包未包含模板副本，因此无法做模板目录的基线哈希逐文件对照，但无实施期写入迹象。 |
| 识牌阈值 0.82/0.055 未改 | **符合** | `bot/tile_vision.py:127-130` 仍为 `min_score=.82, min_margin=.055`，该文件最后修改时间 2026-10-19 09:38:48，早于基线。 |
| recorder JSONL 契约未变 | **符合** | 当前 `recorder.py` 与基线 SHA-256 完全一致；schema 仍为 `assist_record/v4`（`recorder.py:18,323-353`）；本次 recorder 测试包含在 106/106 与专项清单 77/77 中并通过。 |
| 无点击/发键通路被启用 | **符合** | 新增/修改的 v2 文件未出现 tap/click/keyevent/send_keys/pyautogui；ADB 只读调用见 `live_capture.py:107-124`。 |
| `bot.py` / `tea_bot.py` dry_run 未受影响 | **符合** | 两文件修改时间均早于基线；`tea_bot.py:117-128` 保持强制 dry-run，`pc_bot/bot.py:254-261` 的点击仍受 dry-run 保护，入口 `pc_bot/bot.py:542` 仍显式启用 dry-run。 |

## 五、独立测试结果

### 1. bot 全量

命令（使用全新系统临时 `--basetemp`，避免仓库旧 `.pytest_tmp` ACL 干扰）：

```text
python -m pytest tests -q -p no:cacheprovider --basetemp <系统临时目录>
```

结果：**106 passed in 20.15s**。

首次直接使用项目默认 basetemp 时得到 `58 passed, 48 errors`，48 个均为 pytest 清理旧 `.pytest_tmp/basetemp/.../frame.png` 时的 `PermissionError [WinError 5]`，发生于 fixture setup，不是代码失败；换全新系统临时目录后 106/106，故最终回归数字采用后者。

### 2. 实现报告所列 6 文件清单

```text
python -m pytest tests/test_recorder.py tests/test_assist_tea.py
  tests/test_assist_shu.py tests/test_caishen_vision.py
  tests/test_tea_2p_gold_manifest.py tests/test_tea_bot.py
  -q -p no:cacheprovider --basetemp <系统临时目录>
```

结果：**77 passed in 10.12s**。数字属实，但这些都是基线前已有测试，不是新增 SPEC-005 专项测试。

### 3. 新增 SPEC-005 专项测试

```text
python -m pytest tests -q -p no:cacheprovider --basetemp <系统临时目录> -k spec005
```

结果：**106 deselected in 0.25s**，即 **0 项收集、0 项执行**。`bot/tests` 没有实施期新增测试文件，未满足验收第 1 条的“新增刷新状态机与延迟统计单元测试全过”。

### 4. 子仓库全量补充回归

按根 `AGENTS.md` 命令另跑：

```text
python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider --basetemp <系统临时目录>
```

结果：**446 passed, 13 skipped, 206 subtests passed in 11.93s**。

## 六、延迟复测

### 实现方留存数字复核

`test_out/spec005_latency_tea2p_live.json`：

- capture p50/p95：403.8 / 446.7 ms
- recognize p50/p95：50.4 / 55.3 ms
- advise p50/p95：0.0 / 0.1 ms
- total p50/p95：452.6 / 494.0 ms
- **20/20 状态均为 `abstain`**

`test_out/spec005_latency_tea2p_offline.json`：

- capture p50/p95：20.3 / 22.3 ms
- recognize p50/p95：146.4 / 167.2 ms
- advise p50/p95：4.8 / 5.6 ms
- total p50/p95：170.4 / 192.3 ms
- **20/20 状态为 `advise`，但都是同一张冻结帧回放**

磁盘数字与实现报告一致。

### Codex 独立复测（同一离线冻结帧，20 次）

直接运行原脚本时，首轮因其硬编码仓库内 `.pytest_tmp/spec005_bench/.../logs` 而触发 `PermissionError [WinError 5]`。随后保持相同 `SegmentClock → grab_screen(FileRunner) → analyze_bytes → LatencyTracker` 边界，仅把日志放进系统临时目录：

| 段 | p50 | p95 |
|---|---:|---:|
| capture | 20.7 ms | 24.2 ms |
| recognize（按当前脚本标签） | 154.6 ms | 176.9 ms |
| advise（按当前脚本标签） | 5.1 ms | 6.2 ms |
| total | **182.2 ms** | **203.7 ms** |

- 20/20 为 `advise`；最终 `interval_ms=800`。
- 与实现方离线数字接近，可复现“同一冻结帧的处理耗时”。
- 不认可其为 SPEC 目标“手牌变化到建议更新”的端到端延迟：没有轮询等待、Tk 渲染、真实手牌变化时间点，也没有 20 个真机决策点。
- 三段标签也不准确：`analyze_snapshot()` 内部已执行 `bot.see()` 和 `bot.think()` 后才返回；`clock.mark("recognize")` 在其返回之后，所以 advisor 主耗时被计入 recognize，advise 仅余财神二次识别与候选归一化。
- 实现方把“现场抓图 + 非目标画面快速 abstain”当作真机端到端 p95 的口径不成立。真机文件的 20 帧没有一个有效建议。

因此，虽然离线静态处理 p95 明显小于 1.5 秒，**SPEC 的真机端到端验收仍未完成**。

## 七、验收条款核对

| 验收项 | 结果 |
|---|---|
| 新增刷新状态机与延迟统计测试；全量不少于 106/106 | **不通过**：全量 106/106，但新增专项测试为 0 |
| 真机一局不少于 20 个决策点，给出端到端 p50/p95 | **不通过**：真机 20 帧全 abstain；离线为同帧重复，且未测手牌变化到 UI 更新 |
| 亮牌局面窗口截图，四项齐全 | **不通过**：未找到合格窗口截图 |
| Codex 终审 | **打回** |

## 八、必须回改项（逐条可执行）

1. 在 `_show()` 更新 `LiveRefreshState` 后调用 `_refresh_view(state)`；确保每个正常帧都刷新手牌牌面、推荐块、Top-3、灰化状态和财神失败手选行。新增测试必须证明 `_show()` 会触发四类 UI 更新，而不只测纯图片函数。
2. 将手选财神真正接入茶分析链：自动识别成功时以画面为准；仅自动识别失败时显示下拉，并把已选财神传入下一帧分析。增加“自动成功不展开、自动失败展开、手选后可建议、画面与手选冲突仍弃权”测试。
3. 恢复并统一清理 `continuous_round`：至少在 `start()`、`stop()`、`next_round()`、`_on_mode_change()` 清空。增加“同局恢复、停止后新局、下一局、切模式”回归。
4. 落实“开窗即连续刷新”。在不绕过账号与玩法画面门禁的前提下，完成会话前置输入后应自动进入连续观察，不再要求额外点击“开始”；若产品决定必须手动开始，应先修订 SPEC，而不是用当前行为宣称符合。
5. 修正延迟分段：把纯识牌与 advisor 计算拆成可独立计时的调用边界；总延迟还须覆盖轮询等待和 Tk 完成渲染的时点。状态栏显示实际 capture-to-capture 周期，不能把“处理完成后再等待 800 ms”标成实际周期 800 ms。
6. 为 `live_state.py`、`live_capture.py` 和 v2 窗口接线新增独立测试，至少覆盖：分段计时、p50/p95、3 秒 stale 红警、旧建议清空、低置信灰化、Top-3 同源、手选财神、轮次锁重置、玩法切换、只读命令白名单。
7. 让 `bench_spec005_latency.py` 的会话/日志目录可由参数指定，默认不要依赖可能带旧 ACL 的仓库 `.pytest_tmp`；复跑命令应一次成功。
8. 经用户明确授权后，在目标茶 2 人真机一局采集不少于 20 个**有效决策点**，测“手牌变化 → Tk 完成新建议渲染”的 p50/p95；不得用 abstain 快退帧或同一离线帧替代。
9. 保存一张真实亮牌局面的完整窗口截图，必须同屏可见牌面行、推荐放大/高亮、财神标记和延迟/更新时间栏。
10. 回改后重跑并报告：新增 SPEC-005 专项测试、bot 全量 106+、子仓库全量回归；不得只复用本次 77 个旧用例。

在 1–7 完成、8–9 取得合格实测材料、10 全绿前，SPEC-005 保持“终审打回待回改”。
