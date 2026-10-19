# 阿法算牌 Windows 前端解析：REA 定位 + Dart 3.8.1 对象池

日期：2026-10-18。目标文件为 `D:\温州麻将\阿法算牌\data\app.so`，SHA256 `d08f2c6b4f3aa91ecd3ea7f18c64754f4408cc0effb43cc984ad2536e716d703`。原安装文件只读；全部新产物在 D 盘。

## 工具与结果

1. REA `inspect_dart_aot` 证据 `ev_d9c874cb56d949f61dfc743cefcb47d0cbf7679791df4cbf6ddfe26f7e07f799` 取得快照哈希 `830f4f59e7969c70b595182826435c19`、4 个快照段和字符串池。Linux Ghidra 对同一 ELF 报 0 个恢复的过程，这只是 Ghidra 对 Dart AOT 的能力边界。
2. [blutter-windows](https://github.com/1903247335/blutter-windows) 的 `--detect-only` 对**原始 Windows 安装目录**识别为 `Dart 3.8.1 / windows x64 / no-compressed-pointers`，哈希与 REA 一致。[reFlutter 的引擎哈希表](https://github.com/Impact-I/reFlutter/blob/main/enginehash.csv) 也将该哈希列在 Flutter 3.32 系列；同一哈希对应多个 Flutter 补丁版本，不可据此确定唯一补丁号。
3. 本机 VS 2022 Build Tools 编译匹配的 Dart VM 解析器成功；`--no-analysis` 和完整模式均退出码 0。对象池 `pp.txt` 约 1.65 MB、嵌套对象 `objs.txt` 约 1.05 MB、按库拆分的汇编 381 个文件。只读原始输出分别在 `D:\Tools\REA\analysis\blutter_afa_probe\` 和 `D:\Tools\REA\analysis\blutter_afa_full\`；解析器源码在 `D:\Tools\REA\blutter-windows\`。

## 可用于前端复刻的证据

| 观察 | 原始位置 | 可确认的范围 |
| --- | --- | --- |
| 识别设置相关对象含“玩家数量”“手牌数量”“碰杠吃选择”“规则”“保存”“识别设置” | `blutter_afa_full\pp.txt` 约 30888–30936 行 | 文案与相邻闭包/对象存在；实际页面层级、控件默认值和点击行为尚需运行时核对。 |
| 识别详情附近有“当前手牌”“自己碰杠区”“自己牌池”及上/下/对家牌池、“麻将窗口名”“识别中”“识别已停止”；同段还有 `final_result`、`persistent_peng/gang/chi` | `pp.txt` 约 22696–22780 行 | 这些字段与界面候选位置明确，尚未得到完整进程 JSON 协议。 |
| 对象池还有“识别结果预览”“计算结果”“自动同步手牌”和 `v4_4_recognition_player_count/tile_count` | `pp.txt` 约 24373、30528–30995、35456 行 | 可作为运行时页面核对清单，不能仅凭邻近关系推断路由。 |
| x64 汇编引用 `mahjongRules`、`handCards` 与计算模式字符串 | `blutter_afa_full\asm\afh.dart` 约 201、217 行；`asm\zfh.dart` 约 354、393、698 行 | 已从纯字符串推进到部分代码引用；还不能断言具体 HTTP 请求结构或规则调用结果。 |

## 原版窗口的只读核对

AgentDock 查到两个原版进程；按返回的窗口 ID 用 Computer Use 的 UI Automation **只读**获取了控件树。首页窗口显示 `阿法算牌 v2.2.5`、“搜索”“设置”、各地麻将入口，以及“首页 / 练习 / 我的”三个底部标签；另一窗口显示“阿法v4.4 新算法页”“阿法v3.2 3.0拆分版”“阿法v2.5 沿用2.0识别，UI升级版”三个选择按钮。它证实了运行中的导航表面和算法版本入口，但尚未进入识别详情页，因此对象池推断的设置/结果流程仍待交互核对。窗口截图捕获两次超时；一次尝试点击 v4.4 入口返回 `coordinate input geometry is unavailable`，重新读取控件树确认窗口未变化。此处仅以 UIA 文本为证据，没有页面像素证据。

## 解开到什么程度

`app.so` **可以解析到对象池、部分类型/闭包地址及 x64 汇编引用**，不等于解回原 Dart/Flutter 源工程。此程序大量名称被混淆；某些方法在 Windows 分支输出 `size: -0x1`，缺完整函数体。原前端不能直接修改后独立运行。下一步应以以上字符串和地址为线索，在原版窗口逐页核对识别设置、识别详情、结果展示和状态变化，再为浙江游戏大厅的温州茶/熟实现同等功能。原版模型和后端仍未复原。

此轮未新启动原版客户端、未调用其登录/算牌服务、未操作模拟器，也未将对象池中可能含私有信息的原文复制进项目报告。新工具的运行状态由退出码 0、产物存在和上述代码引用交叉验证。
