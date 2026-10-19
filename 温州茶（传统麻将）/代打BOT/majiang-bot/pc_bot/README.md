# 温州红中放炮：本地识牌、观察与离线验证

主工程固定在 `D:\HanaAgent\majiang-bot`，复用旧 `D:\archive_温州麻将202608` 的本地引擎，旧目录不改写。目标为112张牌、红中财神可代任意牌的温州放炮；温麻4人仅为牌面采样来源，不能替代目标场证据。

用户已确认有人民币结算，当前命令行入口仅只读观察，不启动自动实战、点击、充值或资金操作，也不产生实时弃牌建议。算法测试通过不代表收益、胜率或完整实战已验证。

## 已交付

- `tile_vision.py` 与 `templates_hongzhong_v2/`：34种视觉牌面、63个真实像素样本；manifest保留来源、坐标、旋转、哈希。目标引擎仅接收27种数牌与红中；歧义、未知牌、超4张拒识。
- `adb_transport.py`：原始ADB帧、显式PNG模式、严格解码。5557采样实例关闭后会留旧注册；当前目标实例为 `emulator-5554`，后续仍须当场确认设备。
- `brain_v0/engine/`：旧引擎复用，财神组合枚举改为精确分组动态规划，支持已确认副露数。外部已见计数不包含自己暗手。
- `runtime_guard.py` 与 `bot.py`：完整识牌门禁、已胡禁止弃牌、动作确认、结算去重，16局检查先于下一局。当前入口不启用实战动作；未知杠类型与缺目标动作模板仍待验收。

## 命令

从项目根执行，参数均来自当前入口：

```powershell
python pc_bot/bot.py --once --serial emulator-5554
python pc_bot/bot.py --observe-only --serial emulator-5554 --max-rounds 16 --duration 7200 --capture-dir pc_bot/artifacts/observations
python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider
python -m py_compile pc_bot/bot.py pc_bot/runtime_guard.py pc_bot/tile_vision.py pc_bot/adb_transport.py
```

观察截图为无损PNG，采集状态在输出目录 `status.json`，日志在 `logs/bot_*.log`，观察模式不写自动战绩数据库。登录、等待页不算完成对局。目标场结算模板还未采集，自动计局未实机验收，不把 `--max-rounds 16` 当作已证明会正确计满16局。

模板重建入口为 `tools/rebuild_templates.py`，先查看它的实际参数。勿直接重跑旧 `collect_v2.py`：它会递归删除采集目录且使用错误模板预标注。

## 验证与未覆盖项

2026-10-14：全套298项测试、13项跳过、180个子测试通过。视觉56张去重留出裁片均正确；两个温麻样本各17张正确，仅是同批皮肤视觉QA。六条来自小尺寸弃牌旋转，仍缺全尺寸手牌留出。

10个0–4红中冷性能案例均通过，首次本地决策约26–184ms；旧版3次20秒超时已消除。原始截图在采样实例初测约411ms，目标实例首次约3961ms，尚未证明持续低延迟。

还未验收：新目标App皮肤、真实13／14张与副露、胡／碰／不同杠类型／过、牌河跟踪、真实结算和连续16局。1000局不能替代规则、标签和效果的独立检验；不得承诺保本、正收益或多号线性收益。

备份位于项目根 `.codex_backups/20261014_170036_takeover/`；引擎修复前另存 `artifacts/engine_validation/before_perf/`。回退仅恢复确认涉及的源文件，不覆盖新对局数据，不使用破坏性Git命令。
