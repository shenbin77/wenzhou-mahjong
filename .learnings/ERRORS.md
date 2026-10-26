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
