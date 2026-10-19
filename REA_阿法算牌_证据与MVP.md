# 阿法算牌：REA 证据与识牌算牌最小版

2026-10-18（同日更新：本机已建成 Ghidra 12.1.4 + JDK 21 + REA 6.5.0 深度分析环境，详见下节）。目标是温州茶 2 人局的截图识牌、手工改牌、算牌建议与本地留存。完整阿法客户端、会员功能、加密识牌模型、远程算法和 4 人规则不在当前已验证范围。

版本边界：exe 的 Windows 版本资源为 `2.2.5+1`；`app.so` 字符串池含 `v4.4.1`、`/calculator_v4_4_1`、多个 `calc_v44_*`/`calc_v45_*` URL 与 `v4_4_recognition_*` 配置键。它们证明安装包内有这些版本标识与资源，不能单凭字符串确定当前运行页面、实际调用的接口或整个产品只有一个版本。`get_shoupai_v25_2` 是本地引擎文件名；v4 与 v2/v3 的运行时调度关系尚未证实。

## 原版证据

| 项目 | 已观察到的证据 | 不能据此推断 |
| --- | --- | --- |
| 启动器 | REA `inspect-artifact`：`D:\温州麻将\阿法算牌\阿法算牌.exe` 为 141312 字节 PE，SHA256 `9237a90fc04a3bf4f7f91a7bbe2440b0411640668d1e114986a04d3c5ee10c77`，证据 `ev_8e233630e1f6f9d8a069bd698293fabfcb21614af598e4dc45ae82ffe4087435`。`inspect-managed-artifact` 证据 `ev_2ea35fa8dc0dec3d2ae688e784de6d9d3687b9535e35ac9ead4e5f3e973c13ff` 确认无 .NET CLI 元数据。 | 启动器包含完整前端源码。 |
| 应用主体 | REA `inspect-artifact`：`data\app.so` 为 10027936 字节 ELF，SHA256 `d08f2c6b4f3aa91ecd3ea7f18c64754f4408cc0effb43cc984ad2536e716d703`，证据 `ev_d0888f24b6a0a595a47684eb220921ff318ef5ef39ca789a5e9542b86db61bd1`。`data\flutter_assets\AssetManifest.json` 有麻将牌图片、规则页图片、识别配置资源。 | 可从资源表恢复 Dart 源码或真实识别精度。 |
| 识牌配置 | 安装资源 `assets\recognition_v4\af_v4_profiles.json` 声明通用桌面、微乐、雀神三个 profile，描述模板与 YOLO 候选、多帧融合。 | 这些声明证明对应算法已在当前版本被调用，或适配温州麻将。 |
| 后端线索 | 对 `app.so` 做只读字符串提取，出现 `https://www.afsuanpai.com/flutterapi_v2/` 下 `calc_v44_tongyong/baoding/shanxikoudian/xinxiang_buketing|no_tingpai_secure.php`（4 地方规则 × 2 模式）与 `calc_v45_calculate_secure.php`、`login.php`、`session_status.php` 等路径；另有 `v4.4.1`、`v4_4_recognition_*` 配置键（玩家数/牌数/含吃碰杠/微乐微调/显示缩放）。 | 请求方法、参数、响应、鉴权或任一调用链已经验证。未调用这些接口，也未抓取登录凭证。后端未见温州茶规则适配。 |
| 启动器深度分析（Ghidra） | REA 6.5.0 `analyze --provider ghidra`（Ghidra 12.1.4，证据 `ev_0b2209a77796839e6465d8463b0f0d00d2afde3215e4e84313ab28bcea791958`）：exe 为 x86_64 PE 原生应用，8 段、331 函数、243 字符串；`.text` 仅 24KB；字符串仅含 `\flutter_assets`、`\app.so`、`flutter_windows.dll` 及 FlutterDesktop* 导入。字符串搜索证据 `ev_832213ccc77694c73cf7a8b1909732b355ffb6610b0d790e520b76dc20fb5014`。 | 启动器含业务逻辑。已证实它只是标准 Flutter Windows 壳。 |
| 识牌核心深度分析（Ghidra） | REA 6.5.0 `analyze --provider ghidra`：`runtime\scripts\get_shoupai_v25_2_core.cp311-win_amd64.pyd`（SHA256 `4501721f0827b20d6333cff8b85a0f0cee87c35e10a9ed4f42497be359146472`，证据 `ev_05ed690f85a90504905b52df28ddd4568590b8f8cfc003525d7a149dcd1227a2`）为 Cython 编译的 x86_64 PE，8 段、551 个 Ghidra 过程、489 个已定义字符串。方法表文档字符串含 `resolve_model_path`、`normalize_yolo_tile_name`、`_bbox_iou` 等名称；Ghidra 的过程多为泛化地址名，名称与过程的对应需逐一校验。 | 未证明原版的识牌精度、完整算法或运行时实际执行了哪些路径。 |
| 识牌引擎算法设计（方法表文档字符串） | 通过解析 pyd 的 PE 方法表（PyMethodDef 32字节条目、flags=0x82、name/func/doc 三指针，`.data` 段文件偏移 0x7b400 起）提取 60 个方法的完整中文文档字符串——即原版作者自己写的算法注释。核心设计：①双模型架构：模型1+模型2 双 YOLO 交叉校验；`_special_pair_decision` 同位置冲突特判（m2 的 5d≥0.90 vs m1 的 7d≥0.70 选 m1；m1 字牌≥0.50 优先；发财 vs 1条：m2 的 1b≥0.85 才信真一条否则判发财）；`比对两个模型手牌`：模型2 的 1b 强制纠正模型1、8d 缺失补充、其余以模型1为准；`_双模型NMS融合`：合并→NMS保最高置信→按 x 排序。②区域几何：手牌仅取窗口下方 1/3，牌池仅取上方 2/3（`筛选手牌`/`提取牌池牌`）。③缺位补牌：`_estimate_expected_spacing` 估算相邻中心距→`_is_size_ratio_ok` 面积比校验→`_calc_dynamic_match_tolerance` 动态容差（原注释：避免固定 40px/60px 在不同分辨率失真）；`提取底部区域候选`按面积降序，`current < previous*0.80` 后截断，截断后为碰杠牌。④碰杠吃状态机：`peng_gang_cards_jisuan` 对比当前/上次手牌，维护整局 persistent_peng/gang/chi。⑤癞子：手牌排序分析中“分析间距，插入癞子填补空缺”。⑥其余：`找出麻将窗口`按窗口标题定位；单实例锁；启动心跳/运行状态 JSON 供 Flutter 消费；`is_chi_cards` 两牌数值差≤2 即可吃。 | 文档字符串描述的设计意图；具体实现细节（阈值组合、边界处理）需逐函数反汇编验证，尚未全部验证。YOLO 权重本身仍加密。 |
| YOLO 模型 | `yolov8n.afm`（22552003 字节）与 `model_management\page_v2_5_external_test\models\mahjong_yolo_train5_last.afm`（6217357 字节）头部均为自定义魔数 `AFM1`，非标准 ONNX/TorchScript；REA `inspect-artifact` 拒绝该格式（`unsupported binary format`）。 | 已解开其权重结构或类别表；加密细节未验证。 |
| 模板资源 | `runtime\scripts\af_majiang3.0\templates\pics_my_shoupai\` 含 34 个 PNG（b1-9/c1-9/d1-9 筒条万 + ew/nw/sw/wd/ww/rd 东南西北中发白），另有背面/碰杠/上家下家对家分区模板；`templates_queshen` 为雀神变体。 | 这些模板适配温州茶皮肤。 |
| 运行时脚本 | `get_shoupai_unified.pyc` 与 `mahjong_runtime\unified_runner.pyc`（Python 3.11 魔数 `a70d0d0a`）经只读 marshal 检查：统一入口支持 v2、v2_5、v2_5_2、v2_5_2_fast、v3_2、v3_3、v4_1_generic_yolo 七个模式；含高 DPI 感知与事件上报。`get_shoupai_unified.pyc` 仅导入并调用统一入口。 | 分发脚本以外的执行行为、识牌结果协议已完整还原。 |
| 引擎家族盘点 | `runtime\scripts\` 下 5 个 Cython 引擎：v2 `get_shoupai_core`（312320 B，sha16 `4f90623464c53ddf`）、v2.5 `get_shoupai_v25_core`（382976 B，`2e46959b8bd93769`）、v2.5.2 `get_shoupai_v25_2_core`（524800 B，`4501721f0827b20d`）、v2.5.2_fast（597504 B，`3c28365bbd3ac69e`）、v3 `af_majiang3.0\get_shoupai3_core`（680960 B，`444af6bcf637ad61`）。v3 引擎函数侧重配置解析（窗口变体/queshen profile/玩家数/牌数/缩放/两人模式/NMS），v2.5.2 侧重双模型融合。 | 家族差异已逐函数对比。 |
| 后端参数骨架 | `app.so` 静态字符串含请求参数键：`hand_cards`、`hand_tiles`、`chi_cards`、`dipai_cards`（底牌）、`pool_cards`、`played_cards`、`hidden_cards`、`shangjia_cards`、`xiajia_cards`、`ting_tiles`、`advance_tiles`、`laizi`、`player_count`、`tile_count`、`use_count`、`action_list`、`size_mode`、`detection_count`。 | 这些是实际请求字段或调用链已验证。未发请求。 |
| 辅助组件 | `scripts\get_windows.exe`（10822845 B，sha16 `ed9a267521df2554`）为 PyInstaller 打包的 Python 311 窗口枚举工具。`runtime\scripts\af_douzero\` 内嵌开源 DouZero 斗地主 AI（`DouZero_For_HLDDZ_FullAuto-main`）+ 模板 anniu/pics2/pics3。`flutter_assets` AssetManifest 共 137 项资源（含各地方玩法图标：长沙/东北/福建/广东/贵州/江西/四川/武汉/山西口店，无温州茶）。 | 斗地主模块与麻将主线无关。 |
| v4 调度线索 | `af_v4_profiles.json` 声明通用桌面 `preferred_source: v2`、微乐/雀神 `preferred_source: v3`；`unified_runner.pyc` 支持多个模式，默认参数含 `v2_5_2`；`app.so` 含 `v4_4_recognition_*` 字符串。 | 温州茶一定走某个 profile、v4 层实际如何标准化/融合、何时降级，均未验证。 |

### 2026-10-18 环境更新与 Windows 边界

同日用户要求按 REA 推荐链继续深度分析。已在本机完成：Ghidra 12.1.4（`D:\DevTools\ghidra12\ghidra_12.1.4_PUBLIC`）、Temurin JDK 21（`D:\DevTools\jdk21\jdk-21.0.12.1+1`）、REA 6.5.0 源码构建（`D:\DevTools\rea`，含从 `rea-agents@6.5.0` tarball 恢复的 Windows 原生控件）。`rea doctor` 全部 Ghidra 检查项 healthy（版本/headless/win_x86_64 反编译器/JDK 21/原生控件）。

边界：Windows 上 Ghidra P0 只接受 x86/x64 原生 PE（`inspectWindowsP0TargetSupport`，源码 `src/ghidra/GhidraProvider.ts`）；后来已在本机 WSL Linux 中导入原始 ELF，结果见下节。`inspect-dart-aot` 只接受 APK，所以此前把同一份 `app.so` 放入合成容器供解析；该容器不是原版 APK。

### 2026-10-18 本机 Linux REA 复核

- 新建 Debian WSL 发行版 `REA-Debian` 于 `D:\Tools\REA\wsl-debian`，在其中安装 JDK 21、Ghidra 12.1.4 和 REA 6.5.0；原始 `data\app.so` 的 SHA256 保持 `d08f2c6b4f3aa91ecd3ea7f18c64754f4408cc0effb43cc984ad2536e716d703`。
- REA Linux Ghidra 分析原始 ELF：12 段、4166 个已定义字符串、**0 个恢复的过程**。证据 `ev_f7b68b6e22e623c55812002333e8c9b7ab75a3685967fee3b35962cadaa42d60`，原始结果在 `阿法算牌分析\rea_linux_appso_analysis.json`。这解决了 Windows 无法导入 ELF 的环境问题，但 Ghidra 对 Dart AOT 快照仍无法提供函数级前端结构。
- REA 字符串搜索证据 `ev_166270e8b02cfa9f1638a9fbf3486654b045ba1e754388bf2cff6d7fdc07fccf`：33 项命中包括 `/calculator_v4_4_1`、`recognition_result`、`hand_cards`、`player_hand_cards`、`runtime_status`、若干 `calc_v44_*` 与 `calc_v45_*` URL。只证明字面量存在，不能确定页面路由组织、字段所属消息或实际 HTTP 调用。
- REA Dart AOT 资源解析证据 `ev_d9c874cb56d949f61dfc743cefcb47d0cbf7679791df4cbf6ddfe26f7e07f799`：解析出四个快照段和字符串池；输入是由原始 Windows `app.so` 构造的分析容器，不是原版 Android 安装包。完整结果在 `阿法算牌分析\rea_flutter_aot.json`。
- 麻将侧 3 个 `.pyc` 已全部只读提取：`unified_runner.pyc` 确认入口函数 `parse_unified_args`、`resolve_script_path`、`resolve_core_module`、`run_core_module` 与 `main`；`emit_event` 的固定键为 `event`、`status`、`script`、`timestamp_ms`。另两个文件只是入口转调与包声明。其余 24 个 `.pyc` 属于内嵌斗地主模块。该层是**启动分发层**，不是完整识牌结果协议。结果在 `阿法算牌分析\*_contract.json`。
- REA 对 Cython `emit_runtime_status` 的包装函数与实现分别获得证据 `ev_69fb0d11d176de895c98339fc2d3dd0c232a5d190486659251946914cefc8ed8`、`ev_975e0119a1c18202128e6bdb093ed9480e227c02adb03b7b39e280a57ac1e717`。实现反编译显示会建立 Python 字典并写入条目，但 Cython 对象的键值尚未可靠解析，不能宣称已得完整 JSON schema。

结论：前端页面树、Dart 状态管理、多帧融合阈值、后端签名和完整 Python 进程协议**仍未复原**。最小版沿用独立茶 2 人识牌与算牌代码，不以这些静态线索伪装成原版实现。

### 2026-10-18 继续解析 Windows Flutter AOT

REA 给出的快照哈希被用于匹配 Dart 3.8.1 Windows x64 解析器。本机 [blutter-windows](https://github.com/1903247335/blutter-windows) 对原始 `app.so` 的检测、对象池读取和 x64 汇编分析均已完成；取得识别设置/详情的对象与部分代码引用。**这修正了“Ghidra 0 个过程即无法继续分析”的判断**，但不代表原 Dart 源码或可改用的原版前端已经恢复。命令、原始产物、证据定位和剩余边界见 `阿法算牌分析\Blutter_Windows前端解析_20261018.md`。

旧记录（2026-10-18 早）：当时 `rea doctor` 显示本机没有 Ghidra、IDA、Hopper。该状态已被上文环境更新取代；`inspect-dart-aot` 只接受 APK 的限制不变。频道、消息、实时同步属于之前混入的另一类客户端需求，本项目没有相应证据，也没有加入最小版。

## 最小版及验收

现有 `温州茶（传统麻将）\bot\assist_tea.py`、`tile_vision.py`、`tea_bot.py` 和茶规则引擎承担识牌与算牌。窗口 `assist_window.py` 提供模拟器抓图、导入已有截图、修正识错手牌后重算；每次截图与手工修正另存本地记录，不发游戏点击。打开 `温州茶（传统麻将）\启动2人茶局辅助.cmd` 使用；先勾选 2 人局并选择实际财神。手工修正牌用 `w1`–`w9`（万）、`t1`–`t9`（筒）、`s1`–`s9`（条）、`dong/nan/xi/bei/bai`，空格分隔 16 或 17 张。

离线验收样本仅有 `采集\2026-10-16\740195\r08\frame_02.png` 与 `frame_03.png` 的 17 张牌；不能据此声称所有牌桌皮肤、明牌组、4 人局、实时实战识牌均可用。输入含糊或不合规则时应停在“暂停建议”。本工具是受原版启发的独立实现，未复制原版加密模型或私有服务算法。

同图离线复核见 `阿法算牌分析\对照_20261018\对照结果.md`：两张真实回放帧各 17/17 与现有逐张期望标签一致，实际财神按画面选“北”；原版阿法的同图识牌输出未取得，不能判定两套结果是否一致。

复原原版所需的下一批证据是：原版 2 人温州茶界面实际规则设置、同一截图的原版识别输出、可合法保存且去敏的请求/响应、多个真实对局样本。比较时逐项记录识牌、向听、建议、无法判断，不把未知写成一致。

来源：[REA 官方仓库](https://github.com/morluto/rea)（2026-10-18 读取）；其命令和本机输出以以上证据 ID 为准。
