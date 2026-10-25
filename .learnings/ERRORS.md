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
