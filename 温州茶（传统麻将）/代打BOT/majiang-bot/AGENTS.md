# majiang-bot — Agent 协作规则

PC 端温州红中放炮识牌与代打研究项目：Python、ADB、OpenCV 与本地决策引擎。运行说明见 [pc_bot/README.md](pc_bot/README.md)；当前参数与行为以 `pc_bot/bot.py` 为准。

## 命令

工作目录固定为 `D:\HanaAgent\majiang-bot`。以下入口与参数已按实际文件核对：

```powershell
# 单次截图、计数和识别，不点击屏幕
python pc_bot/bot.py --once --serial emulator-5554
# 当前入口只读观察，不操作实战或资金
python pc_bot/bot.py --observe-only --serial emulator-5554 --max-rounds 16
# 已按实际测试目录执行通过
python -m pytest brain_v0/tests pc_bot/tests -q -p no:cacheprovider
```

依赖为 `opencv-python` 与 `numpy`，见 `pc_bot/README.md`。本根目录没有统一的 lint、构建或 CI 配置；不能把未执行的检查写成通过。

## 目录与约定

- 真正目标始终是温州红中放炮麻将，红中是可替代任意牌的财神（用户于 2026-10-14 纠正）。其余规则以目标玩法规则页为准；温麻4人仅用于牌面采样，不能作为目标玩法的规则或实战验收证据。
- `pc_bot/bot.py` 是运行入口，`brain_v0/engine/` 是决策引擎；只改任务涉及的文件。
- 已验证模板位于 `pc_bot/templates_hongzhong_v2/`，旧 `templates_new/` 含错标，不能当作标签真值；每个模板必须保留截图来源与像素哈希；日志、截图与采集数据为运行产物，不手改既有记录。
- 重启前按进程命令行确认 bot，按 PID 停止；`D:\HanaAgent\pc-server.py` 和其他 Python 服务必须保留。
- 验证运行时检查新日志的 `[TICK]`、识牌与异常记录，并区分计数正确和分类正确；代码或参数改变后须重新启动进程才能生效。

## 变更记录

- 2026-10-14：根据项目规则模板创建，命令已核对入口与 argparse 参数；运行验收以当次日志为准。
