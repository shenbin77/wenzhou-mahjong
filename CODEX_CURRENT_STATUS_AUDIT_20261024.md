# Codex 当前状态审计（2026-10-24）

## Summary

WorkBuddy 的 P0 修复大部分确实存在且单元测试通过；其中财神自动识别主流程已经接入“开始跟随”，用户实测反馈较准确。整体交付边界仍是“安全失败的开发版本”，不是“已经可以全权代打”。最明显的产品缺口是：桌面辅助、自动循环、统一台面状态和跟局日志没有接成一个入口。

## Validation

- 已执行并通过：`python -m pytest bot/tests -q -p no:cacheprovider` → `689 passed in 60.23s`。
- 已执行但失败：子仓库按原命令 → `1 failed, 486 passed, 13 skipped`；失败原因为 Windows 默认 GBK 读取 UTF-8 manifest。
- 已执行并通过：设置 `PYTHONUTF8=1` 后子仓库 → `487 passed, 13 skipped, 206 subtests passed`。
- 已核对：`CLAIM_TAP_CHANNEL_READY=False`；生产循环没有 `execute_claim()` 调用；没有真实 `follow_session.jsonl`，现有文件均来自测试目录。
- 未执行：ADB、模拟器点击、harvest、真机整局验收；本次审计没有这类授权，也未提交任何代码。

## P0 审计结论

1. 模板默认、claim 超龄、未知来牌来源、副露逻辑位和跨局弃牌基线均已按 fail-closed 方向修正。
2. countdown 的 unreadable/low/expired 在普通出牌门禁会 wait；但行动圆盘早退分支没有经过这套统一门禁，开 CLAIM 前必须修。
3. 财神 heldout/LOSO 缺证据时会返回未通过；但生产 manifest 仍允许在未带 `--require-heldout` 时写入 provisional 条目，不能把“已入库”当作“跨源已验收”。
4. 当前没有 CLAIM 真点旁路：常量关闭，且 `execute_claim()` 只有实现和测试调用，没有生产调用。自动出牌 CLI 仍可通过显式 `--enable --no-dry-run` 点手牌，所以必须继续标记为开发入口。
5. follow-session schema 能解释普通帧的识牌、门禁和建议；行动圆盘帧缺完整 TableState，普通桌面跟随也不写该日志，尚不足以复盘每一局。
6. 下一次真机只读取证优先级：第一是跑满一局并验证财神自动换局；第二是收集碰/杠（尤其中间抬高的杠）及行动圆盘模板、倒计时真帧。两项都不开 CLAIM。

## Repository state

- 根仓库：`main...origin/main [ahead 15]`。
- P0 五个收口提交已在本地 commit；follow logger、主窗口、自动循环等仍有未提交改动。
- 根工作区历史脏文件很多，包含大量删除和未跟踪采集/脚本；只能按明确文件清单分批提交，不能 `git add -A`。

