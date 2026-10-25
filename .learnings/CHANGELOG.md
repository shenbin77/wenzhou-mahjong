# Changelog

<!-- SCHEMA: {"ts":"ISO-8601","action":"add|promote|extract|resolve","type":"learning|error|feature","id":"entry ID","summary":"<=100字","target":"可选"} -->

```jsonl
{"ts":"2026-10-20T18:00:00+08:00","action":"add","type":"error","id":"ERR-20261020-001","summary":"pytest默认C盘临时目录耗尽，改用工作区D盘basetemp"}
{"ts":"2026-10-20T18:05:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-001","summary":"工作区D盘basetemp下新测试7项全部通过"}
{"ts":"2026-10-20T18:06:00+08:00","action":"add","type":"error","id":"ERR-20261020-002","summary":"茶系候选元组经JSON变数组，导致返回对象与sidecar不相等"}
{"ts":"2026-10-20T18:10:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-002","summary":"候选改用JSON原生数组后bot全量通过，最终98项"}
{"ts":"2026-10-25T03:36:00+08:00","action":"add","type":"error","id":"ERR-20261025-001","summary":"AgentDock context和动态MCP搜索返回-32603，MUSE交接改走明确文档"}
{"ts":"2026-10-25T03:36:00+08:00","action":"add","type":"learning","id":"LRN-20261025-001","summary":"动作点后必须按相对变化结合既有副露核算，禁止固定13或14张"}
```
