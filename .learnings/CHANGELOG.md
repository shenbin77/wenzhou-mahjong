# Changelog

<!-- SCHEMA: {"ts":"ISO-8601","action":"add|promote|extract|resolve","type":"learning|error|feature","id":"entry ID","summary":"<=100字","target":"可选"} -->

```jsonl
{"ts":"2026-10-20T18:00:00+08:00","action":"add","type":"error","id":"ERR-20261020-001","summary":"pytest默认C盘临时目录耗尽，改用工作区D盘basetemp"}
{"ts":"2026-10-20T18:05:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-001","summary":"工作区D盘basetemp下新测试7项全部通过"}
{"ts":"2026-10-20T18:06:00+08:00","action":"add","type":"error","id":"ERR-20261020-002","summary":"茶系候选元组经JSON变数组，导致返回对象与sidecar不相等"}
{"ts":"2026-10-20T18:10:00+08:00","action":"resolve","type":"error","id":"ERR-20261020-002","summary":"候选改用JSON原生数组后bot全量通过，最终98项"}
```
