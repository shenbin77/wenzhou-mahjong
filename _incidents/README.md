# 茶 2 故障包目录

- 正式自动巡检入口：`D:\温州麻将\_incidents\tea2\`。
- 根目录现有 `02c12875f03d3b50`、`45b2b22f57da49bc` 与 `index.json` 是接线前对 2026-10-26 旧会话做的一次只读试运行证据，保留、不删除。
- 正式管理器只写 `tea2\index.json`、`tea2\watchdog_status.json`、`tea2\human-input\*.jsonl` 与各 incident 子目录。
- incident 只保存白名单结构化事件和原始证据路径，不复制截图、原始日志、密钥、Cookie、聊天或无关文件。
