# Changelog

<!-- SCHEMA: {"ts":"ISO-8601","action":"add|promote|extract|resolve","type":"learning|error|feature","id":"entry ID","summary":"<=100字","target":"可选"} -->

```jsonl
{"ts":"2026-10-20T18:00:00+08:00","action":"add","type":"error","id":"ERR-20261020-001","summary":"pytest默认C盘临时目录耗尽，改用工作区D盘basetemp"}
{"ts":"2026-10-20T18:05:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-001","summary":"工作区D盘basetemp下新测试7项全部通过"}
{"ts":"2026-10-20T18:06:00+08:00","action":"add","type":"error","id":"ERR-20261020-002","summary":"茶系候选元组经JSON变数组，导致返回对象与sidecar不相等"}
{"ts":"2026-10-20T18:10:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-002","summary":"候选改用JSON原生数组后bot全量通过，最终98项"}
{"ts":"2026-10-25T03:36:00+08:00","action":"add","type":"error","id":"ERR-20261025-001","summary":"AgentDock context和动态MCP搜索返回-32603，MUSE交接改走明确文档"}
{"ts":"2026-10-25T03:36:00+08:00","action":"add","type":"learning","id":"LRN-20261025-001","summary":"动作点后必须按相对变化结合既有副露核算，禁止固定13或14张"}
{"ts":"2026-10-26T13:20:00+08:00","action":"add","type":"error","id":"ERR-20261026-001","summary":"PowerShell对象表达式内嵌分号导致目录盘点命令解析失败"}
{"ts":"2026-10-26T13:20:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-001","summary":"拆分ignore检查和hashtable构造后继续只读盘点"}
{"ts":"2026-10-26T13:42:00+08:00","action":"add","type":"learning","id":"LRN-20261026-001","summary":"完整16局场次事实与当前SHA零人工稳定验收必须分开记录"}
{"ts":"2026-10-26T13:42:00+08:00","action":"promote","type":"learning","id":"LRN-20261026-001","summary":"端到端完成事实与验收状态分栏记录","target":"AGENTS.md"}
{"ts":"2026-10-26T17:45:00+08:00","action":"add","type":"error","id":"ERR-20261026-002","summary":"目录整理移动真实证据后单亮牌测试仍引用旧路径"}
{"ts":"2026-10-26T17:45:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-002","summary":"测试改指归档证据路径后64项通过"}
{"ts":"2026-10-26T18:45:00+08:00","action":"add","type":"error","id":"ERR-20261026-003","summary":"核心窗口新增方法直接调用导致5个轻量替身全量回归失败"}
{"ts":"2026-10-26T18:55:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-003","summary":"可选生命周期钩子增加callable守卫后全量1661项通过"}
{"ts":"2026-10-26T19:27:00+08:00","action":"add","type":"error","id":"ERR-20261026-004","summary":"后台adb和ldconsole探测弹控制台，且全机cmdline扫描拖慢管理器"}
{"ts":"2026-10-26T19:35:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-004","summary":"无窗口子进程加EnumWindows候选扫描后重复启动稳定，全量1733项通过"}
{"ts":"2026-10-26T19:20:00+08:00","action":"add","type":"error","id":"ERR-20261026-005","summary":"误用full_auto_control不存在的status子命令"}
{"ts":"2026-10-26T19:21:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-005","summary":"改用控制文件消费与follow_session stop事件验证停止"}
{"ts":"2026-10-26T19:43:00+08:00","action":"add","type":"error","id":"ERR-20261026-006","summary":"嵌套pytest basetemp父目录不存在导致14项setup失败"}
{"ts":"2026-10-26T19:44:00+08:00","action":"resolve","type":"error","id":"ERR-20261026-006","summary":"创建已忽略父目录后同组58项通过"}
```
