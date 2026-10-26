# SPEC-005 回改复审报告

- 复审日期：2026-10-21
- 复审基线：`_backups/spec005_20261020_231616`
- 必读材料：`_spec005_final_review.md`、`_spec005_rework_out.txt`、`SPECS/SPEC-005-realtime-assist.md`
- 审查原则：回改交卷仅作线索；结论以当前磁盘源码、基线 diff、独立测试和本次复测为准。
- 裁决：**复审打回**

## 一、结论摘要

终审第八节第 1、4、7 条已落实；第 2、3、5、6 条未完整落实。第 10 条三套测试全部通过，红线也全部守住，但两项主路径缺陷仍阻止放行：

1. 财神识别失败后出现的下拉框写入显示文本（例如 `1万 (w1)`），而 `_caishen_selection()` 只接受内部标签（例如 `w1`），所以下拉选择不会传入下一帧分析；自动识别恢复成功时，已展开的手选行也不会被收起。现有专项测试直接向 `analyze_bytes()` 传内部标签，绕过了这段实际 UI 接线。
2. 延迟分段仍不是真实的“纯识牌 / advisor”边界：`analyze_snapshot()` 内已经依次执行 `bot.see()` 与 `bot.think()`，返回后才标记 `recognize`。窗口以分析耗时反推 capture 起点，且在 Python 调用返回后立即记 `render_done`，没有等待 Tk 完成绘制；状态栏的 `total` 也只等于 capture + recognize + advise，没有覆盖轮询等待和 Tk 完成渲染。

第 8、9 条按本轮指示不纳入复审裁决，状态如实记为：**待试车证据补齐**。

## 二、基线 diff 核对

基线四文件的 SHA-256 对比如下：

| 文件 | 基线 SHA-256 | 当前 SHA-256 | 结果 |
|---|---|---|---|
| `bot/assist_window.py` | `F05229B7A1726B1D4533262B58CDC1AA376FE55D13133370B74C521FAFA2861B` | `7E4D98008CC864058432024742FCD07475C49C495295E4C950535733FF9CA5ED` | 已改；`git diff --no-index --stat` 为 744 行新增、301 行删除 |
| `bot/assist_tea.py` | `033E046B602ED09727789B60CF8B4453C358C51C158D5D55EE17C43BEBC89D7A` | 同左 | 一致 |
| `bot/assist_shu.py` | `7CB17E9F96F76E1FE04C3898A9BDE94CBF841699537E3B490868FD0C422563A3` | 同左 | 一致 |
| `bot/recorder.py` | `6F8EFF1C7AA389070152468472FBEAD3790D0CB50C0A92AB5CF565A68B9EBD89` | 同左 | 一致 |

相对该基线，`live_state.py`、`live_capture.py`、`bench_spec005_latency.py` 和 `tests/test_spec005_live_v2.py` 为新增交付物；本轮未修改任何源码。

## 三、终审第八节第 1–7 条逐条复审

### 1. `_show()` 驱动四类 UI 更新：符合

- `_show()` 更新 `LiveRefreshState` 后明确调用 `_refresh_view(state)`：`温州茶（传统麻将）/bot/assist_window.py:656-673`。
- `_refresh_view()` 顺序调用手牌牌面、推荐块、Top-3 和灰化/财神可见性四类更新：`温州茶（传统麻将）/bot/assist_window.py:675-680`。
- 专项测试验证 `_show()` 调用刷新入口，并验证该入口触发四类更新：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:371-395`。

### 2. 手选财神接入分析链且四场景测试齐：不符

已完成部分：

- 窗口把手选值传给 `_process_frame()`，再传入 `capture_result()`：`温州茶（传统麻将）/bot/assist_window.py:542-586`。
- `analyze_bytes()` 处理自动识别、手选兜底和画面冲突弃权：`温州茶（传统麻将）/bot/live_capture.py:157-201`。

未完成部分：

- 失败后展开的 `ttk.Combobox` 使用 `values=list(TILE_CHOICES)`，即显示文本键：`温州茶（传统麻将）/bot/assist_window.py:712-723`；但 `_caishen_selection()` 只接受 `TILE_CHOICES.values()` 内部标签：`温州茶（传统麻将）/bot/assist_window.py:557-567`。独立探针以首项 `1万 (w1)` 选择后得到 `("", False)`，实际下拉链路未接通。
- `_apply_visibility()` 只有在 `caishen_guard` 或财神为空时才调用 `_refresh_caishen_rows()`：`温州茶（传统麻将）/bot/assist_window.py:701-710`。自动识别成功时两个条件均为假，已经展开的手选行不会收起。
- 三个财神测试直接传入内部标签，且“手选后可建议”只断言状态属于 `{advise, hu, wait, abstain}`，仍允许弃权；没有验证自动成功时 UI 不展开、自动失败时 UI 展开、下拉选择进入下一帧并产生建议：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:215-245`。

### 3. `continuous_round` 四处清空并补齐四场景回归：不符

实现本身已在四处清空：

- `_on_mode_change()`：`温州茶（传统麻将）/bot/assist_window.py:372-377`。
- `next_round()`：`温州茶（传统麻将）/bot/assist_window.py:454-464`。
- `start()` 与 `stop()`：`温州茶（传统麻将）/bot/assist_window.py:509-526`。

但要求的四场景回归没有完整落地。现有测试只有“stop/start 清锁、next_round 清锁、mode_change AST 含字段”三组：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:284-308`；没有用两帧 HUD 指纹验证“同局恢复”，也没有把“停止后新局”作为独立接受路径验证。故本条按完整回改要求判不符，而不是否认四处清空已经实现。

### 4. 开窗完成前置输入后自动连续观察：符合

- `_edit_account()` 在账号变更后立即尝试自动启动：`温州茶（传统麻将）/bot/assist_window.py:293-299`。
- `_on_confirm()` 在确认玩法后尝试自动启动：`温州茶（传统麻将）/bot/assist_window.py:380-389`。
- `_poll_recorder()` 每 500ms 重试前置条件；`_maybe_autostart()` 同时要求账号非空、玩法已确认，随后调用 `start()`，没有绕过门禁：`温州茶（传统麻将）/bot/assist_window.py:467-487`。

### 5. 延迟分段为真实边界并覆盖真实周期/Tk 完成渲染：不符

- `analyze_bytes()` 在调用完整 `analyze_snapshot()` 后才标记 `recognize`：`温州茶（传统麻将）/bot/live_capture.py:157-190`。
- `analyze_snapshot()` 在一次调用内先 `bot.see(data)`、再 `bot.think(hand)`：`温州茶（传统麻将）/bot/assist_tea.py:122-127`。因此当前 `recognize_ms` 仍包含 advisor 主耗时，`advise_ms` 主要只剩财神复核和候选归一化：`温州茶（传统麻将）/bot/live_capture.py:190-214`。
- `_process_frame()` 在截图和保存之后才开始 `total_ms`，并把这个分析耗时写为 `cycle_ms`：`温州茶（传统麻将）/bot/assist_window.py:569-595`。`_on_frame()` 又用该值从 render 起点反推 capture 起点：`温州茶（传统麻将）/bot/assist_window.py:620-636`，不是实际记录的 capture 起点时间戳。
- `render_done` 只是在 `_show()` 返回后立即取 `perf_counter()`，未调用 `after_idle()` 或 `update_idletasks()` 等待 Tk 完成绘制：`温州茶（传统麻将）/bot/assist_window.py:639-650`。
- `FrameTiming.total_ms` 仅为三段相加：`温州茶（传统麻将）/bot/live_state.py:36-56`；状态栏 p50/p95 使用该 total：`温州茶（传统麻将）/bot/assist_window.py:757-771`，没有覆盖轮询等待与 Tk 完成渲染。
- 官方 bench 仍以 `tracker.record(timing)` 记录，不传 `period_ms`：`温州茶（传统麻将）/bot/bench_spec005_latency.py:61-78`。本次按参数一次成功复跑后，输出中的 `period_p50`、`period_p95` 均为 `null`。

### 6. `live_state`、`live_capture`、v2 接线独立测试覆盖：不符

- 新增文件 `tests/test_spec005_live_v2.py` 确实覆盖了名义上的分段、p50/p95、stale、旧建议清除、低置信、Top-3、财神、轮次锁、玩法切换和只读白名单，见 `温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:65-453`。
- 但“分段测试”只验证两个 mark 存在及先后顺序，没有证明 `bot.think()` 落入 advise 段：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:81-93`。
- 财神测试绕过真实 Combobox 值映射，未发现本报告第三节第 2 条的主路径故障：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:215-245`。
- `_show()` 与 `_refresh_view()` 被拆成两个单元验证，没有对真实 `_show() → 四个 renderer` 做一次端到端 spy；连续观察自动启动也没有专项回归：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:311-395`。

因此“有测试文件且 20/20”属实，但测试未能证明两个关键回改目标，不能据此判本条符合。

### 7. bench 会话目录可配置且默认避开旧 `.pytest_tmp`：符合

- 支持 `SPEC005_BENCH_SESSION_DIR`，默认使用系统 `TEMP/TMP`，并提供 `--session-dir`：`温州茶（传统麻将）/bot/bench_spec005_latency.py:44-58,89-104`。
- 本次独立命令一次成功：`python bench_spec005_latency.py --mode tea_2p --runs 20 --out <系统临时目录>/result.json --session-dir <系统临时目录>/session`；输出文件和会话目录均成功生成。
- 专项测试也覆盖默认目录：`温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py:431-453`。

## 四、第 10 条与全量回归

所有测试均使用全新的系统临时 `--basetemp`，禁用 cacheprovider，并设置 `PYTHONDONTWRITEBYTECODE=1`。

| 检查 | 命令摘要 | 结果 |
|---|---|---|
| 新增 SPEC-005 专项 | `python -m pytest tests -q -p no:cacheprovider --basetemp <tmp> -k spec005` | **20 passed, 106 deselected in 1.70s** |
| bot 全量 | `python -m pytest tests -q -p no:cacheprovider --basetemp <tmp>` | **126 passed in 21.40s** |
| 子仓库全量 | `python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider --basetemp <tmp>` | **446 passed, 13 skipped, 206 subtests passed in 11.67s** |

新增测试文件为 `温州茶（传统麻将）/bot/tests/test_spec005_live_v2.py`，文件名含 `spec005`。因此终审第 10 条的数量与全量回归要求：**符合**。

## 五、红线复核

| 红线 | 结论 | 证据 |
|---|---|---|
| `recorder.py` 与基线 SHA 一致 | **符合** | 基线与当前均为 `6F8EFF1C7AA389070152468472FBEAD3790D0CB50C0A92AB5CF565A68B9EBD89`。 |
| 识牌阈值 `0.82/0.055` 未动 | **符合** | `温州茶（传统麻将）/bot/tile_vision.py:127-130` 仍为 `min_score=.82, min_margin=.055`。 |
| 无点击/发键通路 | **符合** | v2 新链路的外部命令只有 `dumpsys activity activities` 与 `exec-out screencap -p`：`温州茶（传统麻将）/bot/live_capture.py:107-124`；对 `assist_window.py`、`live_capture.py`、`live_state.py`、`bench_spec005_latency.py` 搜索 `tap/swipe/keyevent/send_keys/pyautogui/input text` 无命中。 |
| `dry_run` 完好 | **符合** | 茶分析仍显式 `TeaBot(dry_run=True)`：`温州茶（传统麻将）/bot/assist_tea.py:122-123`；`TeaBot` 拒绝非 dry-run：`温州茶（传统麻将）/bot/tea_bot.py:115-131`；子仓库入口仍 `Bot(dry_run=True)`：`温州茶（传统麻将）/代打BOT/majiang-bot/pc_bot/bot.py:536-542`。 |

## 六、capture-to-capture 新口径复测

本轮不做真机试车。使用同一离线保存帧，沿当前生产分析链运行 21 次 capture，取相邻 capture 实际起点的 20 个间隔；每帧处理完成后按当前 `LatencyTracker` 的实际 800ms 调度间隔等待。该口径测的是实际 capture-to-capture 周期，不再把“处理完成后再等 800ms”写成周期 800ms。

| 指标 | 结果 |
|---|---:|
| capture-to-capture p50 | **978.0 ms** |
| capture-to-capture p95 | **996.5 ms** |
| 同次探针处理耗时 p50 | 177.1 ms |
| 同次探针处理耗时 p95 | 193.3 ms |
| 最终调度等待 | 800 ms |
| 状态 | 21/21 `advise`（同一离线帧） |

这组数字只用于验证“实际周期”口径，不是 SPEC 验收所需的“手牌变化 → Tk 完成新建议渲染”，也不替代真机 20 个有效决策点。

## 七、第 8、9 条状态

- 第 8 条（真机一局不少于 20 个有效决策点、手牌变化到 Tk 完成渲染的 p50/p95）：**待试车证据补齐**。
- 第 9 条（真实亮牌窗口完整截图，四项同屏）：**待试车证据补齐**。

两项按本轮要求不纳入复审裁决，也没有进行 ADB 实战、点击、发键、充值或房卡消耗。

## 八、剩余回改项

1. 修正失败手选下拉的值映射：Combobox 选择必须转换为内部 label 后传入下一帧；自动识别恢复成功时必须收起已展开的手选行。
2. 补齐四个真实 UI 链路测试：自动成功不展开、自动失败展开、从失败下拉手选后下一帧可建议、画面与手选冲突仍弃权；“可建议”不得继续允许 `abstain`。
3. 为 `continuous_round` 增加真实 HUD 指纹的“同局恢复”和“停止后新局”接受路径回归，不只检查字段被赋为 `None`。
4. 把茶/熟的识牌与 advisor 拆成真正可独立调用、独立计时的边界；不要在完整 `analyze_snapshot()` 返回后才标记 recognize。
5. 记录实际 capture 起点，不用分析耗时反推；总延迟需覆盖轮询等待，并在 Tk `after_idle` 或等价完成点落下 render 完成时间。状态栏 p50/p95 应使用该端到端口径。
6. 让 bench 自身采集并输出 capture-to-capture `period_p50/p95`；当前参数化目录已合格，但官方输出仍为 `null`。
7. 修正/新增测试，使其能抓住上述分段与 UI 接线问题；完成后再复跑专项、bot 全量和子仓库全量。
8. 第 8、9 条按计划在真人试车局补证，当前维持“待试车证据补齐”。

综上，SPEC-005 本轮不得改为“复审通过”。
