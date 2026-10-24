# 文件管理基线（2026-10-24）

## 当前结论

- 活动测试固定在三个目录：`温州茶（传统麻将）/bot/tests/`、`温州茶（传统麻将）/代打BOT/majiang-bot/brain_v0/tests/`、`温州茶（传统麻将）/代打BOT/majiang-bot/pc_bot/tests/`。
- 当前活动测试共 58 个文件；收集到 1,137 个测试用例（主辅助器 644，子仓库 493）。
- `tests_deprecated/`、`_backups/`、`.codex_backups/` 中的测试不属于默认回归入口。
- `采集/` 约 22 GB，是实战证据，禁止按缓存清理。
- 根仓库存在大量历史删除和未跟踪文件，尚不具备安全批量提交条件。

## 目录规则

| 类型 | 位置 | 处理规则 |
|---|---|---|
| 活动源码 | `温州茶（传统麻将）/bot/` | 保留；WorkBuddy 与 Codex 不并发写同一文件 |
| 活动规则引擎 | `温州茶（传统麻将）/代打BOT/majiang-bot/` | 保留；按子仓库单独测试和提交 |
| 活动测试 | 上述三个 `tests/` 目录 | 保留；错误断言应修正，不以删除测试掩盖问题 |
| 实战证据 | `温州茶（传统麻将）/采集/` | 保留；候选、金标、实机验收分层记录 |
| 回滚副本 | `_backups/`、`.codex_backups/` | 本地保留并忽略，不参与测试收集和提交 |
| 可再生成缓存 | `.pytest_cache/`、`.pytest_tmp/`、`__pycache__/` | 无 pytest 运行时可删除 |
| 本地工具工作区 | `_dfull/`、`_dtest/`、`_ds_test/`、`_acc_bt/`、`_preflight/` | 本地保留并忽略，不作为产品源码 |

## 已完成

1. 已备份根 `.gitignore` 到 `_backups/20261024_file_management/.gitignore.before_codex`。
2. 已加入测试缓存、临时目录、测试输出、回滚副本、本地工具工作区和本地凭据的忽略规则。
3. 已验证 Git 能正确忽略 `.pytest_tmp/`、`.tmp/`、`test_out/`、`_backups/`、`_echobird_key.txt` 与误生成的 `%SystemDrive%/`。

## WorkBuddy 停写后执行

1. 重新跑两套全量测试并记录精确基线。
2. 删除可再生成缓存；不删除 `.tmp/`，除非逐项确认内容。
3. 将根目录一次性 `_probe/_verify/*_out` 文件归档到单一诊断目录。
4. 对 727 个历史 tracked deletion 单独复核，决定正式删除或恢复；禁止批量猜测。
5. 更新 `SPECS/QUEUE.md` 与 `STAGE.md`，使测试数字和实际开发状态一致。
