# 茶 2 自动故障包与 Codex 巡检契约（2026-10-26）

## 1. 本机即时检测

| 场景 | 报警阈值 | 额外门槛 |
|---|---:|---|
| 已有我方可执行证据但无动作 | 5 秒 | 至少 3 张不同原生帧，状态签名一致 |
| 普通识别／门禁异常长期不变 | 15 秒 | 至少 3 张不同原生帧；排除等对手、摸牌、发牌、等候室和好友等待 |
| “下一局”已被客户端接收但新局证据迟迟不来 | 20 秒 | 至少 3 张不同帧；只告警，不补第二击 |

检测器只观察日志和鼠标，不点击游戏、不重启模拟器、不修改代码。

## 2. “是不是用户手点”的证据等级

1. `event=action`：BOT 执行层动作；保留 `action_id/action_source`（新事件应由执行层写入）。
2. `external_advance_suspected`：可信本家牌河严格追加，期间没有 BOT action；只能证明外部推进，不能判断操作者。
3. `human_action_likely`：在第 2 条基础上，推进前后 2.5 秒内还观察到同 serial、已核验雷电 HWND 内的鼠标左键按下。属于“高度疑似人工手点”，仍不冒充绝对真值。

`human_input_monitor.py` 不记录键盘、不记录其它窗口；serial 只有通过官方 `ldconsole list2` 的实例 index 与 `emulator-<port>` 公式精确互证后才标记 `verified`，否则保持 `unknown`。`list2` 由进程内单个后台刷新任务维护，两台共享同一快照；采点线程绝不等待该外部命令。可信旧快照仅在有界窗口内继续使用，首次无缓存或缓存超龄立即 fail-closed 为 `unknown`。

## 3. 故障包

生产目录固定为 `D:\温州麻将\_incidents\tea2\`。每个 incident 原子生成：

- `incident.json`：首故障、最新故障、版本、进程头、最近 30 条白名单结构化事件和脱敏证据引用；session 与 evidence 使用哈希/相对引用，不外发昵称目录、窗口标题或屏幕绝对坐标；
- `README.md`：人读摘要；
- 根 `index.json`：`open`／`verification_pending`／`resolved` 索引；
- `watchdog_status.json`：守护线程、设备映射和最近复盘状态；
- `human-input\*.jsonl`：仅目标雷电窗口内的鼠标按下。

不复制截图、原始日志、密钥、Token、Cookie、密码、聊天或无关文件。相同历史 fingerprint 幂等更新且已 resolved 不会被重扫重开；同一会话后续不同 line/fingerprint 可独立生成 open 包。代码修复并完成离线回归、但同路径实机尚未复验时，使用 `mark_incident_verification_pending()`，既不让巡检重复修旧记录，也不谎称已解决；实机同路径不再复现后才用 `mark_incident_resolved()` 写入解决说明。

## 4. 产品接线

管理器持有唯一 `IncidentWatchdog`：

```python
watcher = IncidentWatchdog(
    logs_root,
    incident_root,
    serials,
    ldconsole=ldconsole_path,
    interval_s=2.0,
)
threading.Thread(target=watcher.run, daemon=True).start()
# serial 集合变化或真正退出程序：
watcher.stop()
```

隐藏主界面、托盘托管和悬浮球运行期间不停止守护；在线 serial 集合变化时先 `stop/join` 旧实例，再创建新实例。同一对象 stop 后不复用。

CLI 默认路径与产品一致：

```powershell
python incident_watchdog.py --serial emulator-5554 --serial emulator-5556
```

`--once` 只做一次增量复盘，不启动鼠标观察线程，供诊断或定时巡检使用。

## 5. Codex 自动修复闭环

同一会话的巡检只消费 `tea2/index.json` 中新的 open incident：读取首故障证据 → 修第一原因 → 定向测试 → 全量回归 → 安全重启辅助器 → 实机观察 → 标记 resolved。状态无变化时保持安静，不重复通知。

Codex 巡检的撤除条件不是“测试全绿”，而是当前提交依次通过 N1 完整 1 局、N2 连续跨 3 局、N3 完整 16 局零人工，并且对应场次没有 open incident、没有危险误点、没有人工代打。撤除 Codex 巡检后，本地只读日志与故障包守护继续保留。
