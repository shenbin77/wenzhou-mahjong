# Errors

## [ERR-20261020-001] pytest temporary directory exhausted

**Priority**: high
**Status**: resolved
**Area**: infra

### 摘要
SPEC-002 新测试首次执行时，pytest 默认使用的 C 盘临时目录无法继续追加小型 JSONL 文件。

### 错误信息
```
OSError: [Errno 28] No space left on device
```

### 上下文
- 操作：`python -m pytest tests\test_recorder.py -q -p no:cacheprovider`
- 默认临时目录：`C:\Users\Administrator\AppData\Local\Temp\pytest-of-Administrator`
- 项目工作目录：`D:\温州麻将\温州茶（传统麻将）\bot`

### 建议修复
本项目测试显式使用工作区内的 `D:\温州麻将\.tmp` 作为 `TEMP`、`TMP` 与 pytest `--basetemp`；不自动清理用户临时文件。

### 元数据
- Reproducible: yes

---

## [ERR-20261027-002] PowerShell 分号未阻断失败后的提交

**Priority**: medium
**Status**: resolved
**Area**: tools

### 摘要
提交前把 `git diff --cached --check` 与 `git commit` 用 PowerShell 分号串联；前者发现新文件末尾多余空行并返回失败后，后者仍继续执行，导致需要立即清理并修正本地提交。

### 建议修复
验证门禁与提交必须分成独立工具调用，或显式检查 `$LASTEXITCODE` 后再提交；不能依赖 PowerShell 分号短路。此次已删除多余空行、重新执行 `git diff --check`，并修正尚未推送的本地提交。

### 元数据
- Reproducible: yes

---

## [ERR-20261027-001] 对抗测试被更早门禁遮蔽而假绿

**Priority**: high
**Status**: resolved
**Area**: tests

### 摘要
17 张自摸未知财神的零点击测试使用全黑图，先被 L1 模板门拒绝，因此没有覆盖真实“胡／过模板均命中后，L2 胡失败又回退自动过”的危险路径。全量测试为绿，但独立安全审查仍能复现可执行 `pass`。

### 建议修复
验证深层安全门时必须显式让所有前置门通过，并断言目标分支与最终点击调用；不能把更早的 fail-closed 当作被测门禁的证据。对抗用例现强制胡／过模板均 `matched`，未知财神或非明确茶 2 的 17 张自摸候选在自动过回退前直接阻断。

### 解决结果
代打专项 276 项、相关安全复审 389 项通过；独立复现返回 `action='' / executable=False`，最终组合全量 1819 项通过。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-008] TapExecutor 专用通道测试未传证据目录

**Priority**: low
**Status**: resolved
**Area**: tests

### 摘要
新增超时解散 dry-run 测试直接构造 `TapExecutor`，未显式传 `session_dir`，先被既有证据目录安全门拒绝，尚未进入被测视觉门禁。

### 错误信息
```
TapAborted: execute_claim 必须显式传 session_dir，或先赋值 executor.session_dir
```

### 建议修复
所有独立构造 `TapExecutor` 的专用通道测试都传入 pytest `tmp_path`，不使用生产会话根目录。

### 解决结果
两项测试均改为传 `session_dir=tmp_path`；既有证据目录安全门保持不变。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-009] sorted 生成器与关键字参数缺少分组

**Priority**: low
**Status**: resolved
**Area**: code

### 摘要
昵称模板排名首次实现把生成器表达式与 `reverse=True` 直接并列传给 `sorted`，Python 要求显式包住生成器，`py_compile` 在测试前即阻断。

### 错误信息
```
SyntaxError: Generator expression must be parenthesized
```

### 建议修复
多行 `sorted(generator, keyword=...)` 始终给生成器额外一层圆括号，并先跑 `py_compile`。

### 解决结果
生成器已显式分组，随后重新执行编译与定向测试。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-010] 猜测了不存在的前端测试文件名

**Priority**: low
**Status**: resolved
**Area**: tests

### 摘要
积分前端接线后定向回归时直接写了不存在的 `tests/test_assist_window.py`，pytest 在收集前退出；未影响另一条并行测试。

### 错误信息
```
ERROR: file or directory not found: tests/test_assist_window.py
```

### 建议修复
运行前先用 `rg --files tests` 核对真实测试文件名，再选与改动对应的 `test_assist_full_auto.py` 和 `test_assist_hud.py`。

### 解决结果
已按真实文件清单改跑相关前端测试。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-011] 递归清理已验证临时目录被执行策略拒绝

**Priority**: low
**Status**: resolved
**Area**: tools

### 摘要
离线分析生成的 `_offline_probe_logs` 已先解析并确认位于目标实盘会话目录内，但当前命令执行策略仍拒绝 `Remove-Item -Recurse`；未改用跨 shell 或其它绕过方式。

### 错误信息
```
exec_command failed: ... Remove-Item ... -Recurse -Force ... rejected: blocked by policy
```

### 建议修复
由允许的文件清理能力删除该精确临时目录，或保留为已忽略的离线探针日志；不得扩大删除范围。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-012] 独立 majiang-bot 全量测试受 Windows 默认 GBK 读取影响

**Priority**: medium
**Status**: resolved
**Area**: tests

### 摘要
按项目根命令运行独立 `代打BOT/majiang-bot` 回归时，486 项已通过、13 项跳过，唯一失败来自测试用 `Path.read_text()` 未指定编码，系统默认 GBK 无法读取导出的 UTF-8 manifest；与本次茶2主线改动文件无交集。

### 错误信息
```
UnicodeDecodeError: 'gbk' codec can't decode byte 0xbe ...
pc_bot/tests/test_export_tile_samples.py:181
```

### 建议修复
先以 `PYTHONUTF8=1` 复跑确认环境归因；后续在独立子仓库把测试读取显式改为 `encoding='utf-8'`，但本轮茶2收口不跨边界修改。

### 处理结果
设置 `PYTHONUTF8=1` 后同一套件复跑为 `487 passed, 13 skipped, 206 subtests passed`，确认是 Windows 默认文本编码的测试环境差异，不是本轮茶2运行时代码回归。独立子仓库源码未在本轮跨边界修改。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-013] 三按钮总结版式被错误当作 16/16 局数证据

**Priority**: high
**Status**: resolved
**Area**: safety-gate

### 摘要
独立安全审查发现 `read_match_end().present` 只证明底部三按钮几何，不证明顶部局数为 `16/16`；旧接线却把它作为 `full_match_verified=True` 传给积分识别器。若未来出现三按钮的提前或异常结算，存在误记整场总分并返回大厅的风险。

### 处理
删除调用方布尔门；`read_match_score()` 改为在同一1600×900原生帧自行复核三按钮版式与顶部 `16/16` 固定局数字形。新增对抗用例，把真实 `4/16` HUD 粘到真实三按钮整场总结帧，必须拒绝读取积分。

独立复审结论为 PASS：验证失败只会停手，不持久化积分、不点击返回大厅；固定 ROI 漂移最多造成安全假阴性。

### 元数据
- Reproducible: yes

---
## [ERR-20261025-001] AgentDock context internal error

**Priority**: medium
**Status**: pending
**Area**: integration

### 摘要
尝试通过 AgentDock 给 MUSE 发送双端接棒任务时，bootstrap context 与动态 MCP 搜索均返回服务端内部错误。

### 错误信息
```
Mcp error: -32603: Internal error
```

### 上下文
- 操作：`agentdock_context`、`mcp_tool_search`
- 目标：读取 AgentDock 能力并找到 MUSE/ACP 任务发送入口
- 影响：不能通过该接口确认任务已送达；不影响本地代码、测试与日志

### 建议修复
AgentDock 服务恢复后重试 context；恢复前用明确的本地交接文档/用户转发，且先停止现有双端进程，避免双操盘。

### 元数据
- Reproducible: yes

---
## [ERR-20261020-002] JSON sidecar tuple/list mismatch

**Priority**: medium
**Status**: resolved
**Area**: tests

### 摘要
茶系候选新增为 Python 元组列表后，JSON sidecar 读取为数组列表，破坏了既有“返回对象等于落盘对象”的测试契约。

### 错误信息
```
Differing items: candidates tuples != candidates JSON lists
```

### 上下文
- 操作：运行 `bot/tests` 全量回归
- 结果：93 passed, 2 failed

### 建议修复
在辅助分析结果边界将茶系 `(score, tile)` 元组显式转换为 JSON 原生数组；记录器仍兼容元组和数组输入。

### 元数据
- Reproducible: yes

---
## [ERR-20261026-001] PowerShell hashtable中嵌套命令分隔符导致解析失败

**Priority**: low
**Status**: resolved
**Area**: tools

### 摘要
目录容量盘点命令在 hashtable 属性表达式里直接嵌入带分号的 `git check-ignore` 调用，PowerShell 报 `Missing closing ')' in expression`，只读盘点未执行。

### 错误信息
```
ParserError: Missing closing ')' in expression.
```

### 上下文
- 操作：汇总项目临时目录大小并同时判断 Git ignore 状态
- 影响：没有执行删除或写入，项目内容未受影响

### 建议修复
把 ignore 检查拆成独立语句，先保存 `$ignored`，再构造 hashtable；复杂 PowerShell 表达式不在对象字面量中混用命令和分号。

### 元数据
- Reproducible: yes

---
## [ERR-20261026-002] 目录整理后测试证据路径失效

**Priority**: medium
**Status**: resolved
**Area**: tests

### 摘要
项目整理把 `capture_5556.png` 移入 `_archive/vision-probes-20261023-24/`，但单亮牌回归仍引用根目录旧路径，导致 7 个用例在 fixture 阶段报 `FileNotFoundError`。

### 错误信息
```
FileNotFoundError: D:\温州麻将\capture_5556.png
```

### 上下文
- 操作：运行账本、单亮、局间和点击器定向回归
- 结果：154 passed, 7 errors；算法用例尚未执行到断言
- 根因：归档移动没有同步更新测试证据引用，不是中文路径或 OpenCV 读取失败

### 建议修复
归档历史证据时同步用 `rg` 查找所有引用；测试应指向可追溯的归档相对路径。路径更新后 `tests/test_single_bright_cut.py` 64 项通过。

### 元数据
- Reproducible: yes

---
## [ERR-20261026-003] 核心窗口新增方法打破轻量测试替身

**Priority**: high
**Status**: resolved
**Area**: tests

### 摘要
跨局等待和窗口控制接入后，`TeaAssistWindow.stop()` 与 `_tick()` 直接调用新增方法；SPEC-005 的轻量 `_FakeWindow` 只绑定被测方法，没有这些成员，导致全量回归失败。

### 错误信息
```
AttributeError: '_FakeWindow' object has no attribute '_cancel_accepted_round_observation'
AttributeError: '_FakeWindow' object has no attribute '_process_window_control'
```

### 上下文
- 操作：运行 `python -m pytest -q tests -p no:cacheprovider`
- 结果：1652 passed, 5 failed
- 影响：生产算法未执行错误点击，但全仓兼容契约未通过，不能交付

### 建议修复
核心 UI 生命周期方法调用可选扩展钩子时使用 `getattr`/`callable` 守卫，并始终执行全量回归；不要只靠给单个测试替身补属性掩盖耦合。

### 解决结果
为可选扩展钩子增加 `getattr`/`callable` 守卫；SPEC-005 定向 35 项通过，最终全量 1661 项通过。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-004] 后台设备探测弹出控制台并拖慢管理器

**Priority**: high
**Status**: resolved
**Area**: desktop-runtime

### 摘要
新版管理器与双 serial 鼠标观察器周期调用 `adb devices` / `ldconsole list2` 时未设置 `CREATE_NO_WINDOW`，桌面表现为两个控制台程序持续闪烁；管理器同时通过 psutil 读取全机每个进程的命令行，遇到无关进程时可阻塞十余秒，导致辅助器迟迟不打开。

### 错误信息
```
用户现场：两个程序一直闪
faulthandler：psutil._pswindows.cmdline -> app_entry.verified_window_argv
```

### 上下文
- 操作：重启新版多开管理器并加载唯一 incident watchdog
- 影响：无游戏点击、无模拟器重启，但桌面闪烁且管理器自动打开下一台的响应被拖慢

### 建议修复
所有周期性 Windows 子进程显式使用 `CREATE_NO_WINDOW`；辅助器占用扫描先用 `EnumWindows` 按真实窗口标题缩小 PID 集，再只验证候选进程，禁止全机逐个读取 cmdline。

### 解决结果
后台探测不再产生控制台窗口；`collect_device_rows()` 从十余秒阻塞降为约 0.4 秒。重复启动实测保持 1 个管理器、2 个辅助器；全量 1733 项通过。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-005] 用不存在的 status 子命令验证全自动控制

**Priority**: low
**Status**: resolved
**Area**: tools

### 摘要
安全停止后尝试运行 `full_auto_control.py status`，但该工具的真实接口只支持 `stop` 与 `resume`，命令按设计返回 `ValueError`。

### 建议修复
控制命令必须先从源码/`--help` 核对；停止结果改由控制文件是否被消费及会话末尾 `event=stop` 双重验证。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-006] pytest 嵌套 basetemp 的父目录不存在

**Priority**: low
**Status**: resolved
**Area**: tests

### 摘要
在仓库根指定 `--basetemp=.pytest_tmp/tools-final` 时，父目录 `.pytest_tmp` 尚不存在，pytest 不能递归创建嵌套路径，14 个依赖 `tmp_path` 的用例在 setup 阶段报 `FileNotFoundError`；断言未执行。

### 建议修复
使用嵌套 basetemp 前先创建已忽略的父目录，或直接使用一级唯一目录。创建父目录后同一组 58 项通过。

### 元数据
- Reproducible: yes

---

## [ERR-20261026-007] Python OpenCV 直接读取中文绝对路径失败

**Priority**: medium
**Status**: resolved
**Area**: tools

### 摘要
一次性实机弹窗门禁脚本把含中文的绝对路径直接交给 `cv2.imread`，当前 Windows Python/OpenCV 组合未能打开文件；脚本在任何 ADB 点击前安全退出。

### 错误信息
```
imread_('D:\温州麻将\_takeover\...\emulator-5554_before.png'): can't open/read file
reference missing/bad size
```

### 建议修复
让 Python 从已设置为项目根的工作目录使用纯 ASCII 相对路径，或用 `Path.read_bytes` 加 `cv2.imdecode` 读取；实机操作脚本必须继续保持“读图失败即零点击”。

### 解决结果
改用 `Path.read_bytes` + `cv2.imdecode` 后，两台原生帧均以消息区像素差与唯一青色按钮双门禁通过；分别点击一次并验证弹窗消失。

### 元数据
- Reproducible: yes

---
