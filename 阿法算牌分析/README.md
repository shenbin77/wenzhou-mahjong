# 阿法算牌只读分析记录（2026-10-18）

本目录保存对已安装阿法算牌的只读分析输出。原始安装目录在 `D:\温州麻将\阿法算牌`；未修改或调用原版登录、计算服务。主结论与证据边界见 `D:\温州麻将\REA_阿法算牌_证据与MVP.md`。

## 产物

| 文件 | 内容 |
| --- | --- |
| `rea_linux_appso_analysis.json` | Linux REA 对原始 `app.so` 的 Ghidra 分析：12 段、4166 个字符串、0 个恢复的过程。 |
| `rea_linux_appso_selected_strings.json` | 对识牌、算牌、运行状态线索的 33 项字符串搜索结果；不能据此还原调用链。 |
| `rea_flutter_aot.json` | REA Dart AOT 解析结果，输入是封装原始 `app.so` 的合成分析容器，不是原版 APK。 |
| `unified_runner_contract.json`、`get_shoupai_unified_contract.json`、`mahjong_runtime_init_contract.json` | 对麻将侧全部 3 个 Python 3.11 `.pyc` 的静态字节码提取；前者是启动分发与事件外壳，后两者分别只转调入口和声明包。其余 24 个 `.pyc` 属于内嵌斗地主模块。 |
| `rea_emit_runtime_status_by_address.json`、`rea_emit_runtime_status_core.json` | REA 对 Cython 运行状态函数的地址级分析；键值协议仍未完整解析。 |
| `appso_elf_sections.txt`、`appso_elf_symbols.txt` | 原始 ELF 的段与符号表；仅有 Dart 快照对象符号。 |
| `Blutter_Windows前端解析_20261018.md` | 以 REA 快照哈希定位 Dart 3.8.1，进一步解析 Windows `app.so` 的对象池与代码引用；原始产物在 D 盘工具目录。 |
| `REA_阿法算牌_证据与MVP.baseline.md` | 更新主报告前的基线备份。 |

`Dockerfile.rea-linux` 是曾尝试的容器路线；由于 Docker 的数据盘位于已满的 C 盘，构建失败。现用 D 盘上的 `REA-Debian` WSL 发行版。`rea_emit_runtime_status.json` 保存一次用方法名查找 Ghidra 过程失败的记录，后续改用已校验的方法表地址。

## C 盘空间与回退

分析开始时 C 盘临时目录及 Docker 数据盘承压。已将下列可恢复的临时目录从 C 盘**移动**到 `D:\Tools\REA\analysis\c_temp_archive`，没有删除：

| 原位置 | 归档名 | 当时大小 |
| --- | --- | ---: |
| `C:\Users\Administrator\.agentdock\tmp\nsr24E4.tmp` | `nsr24E4.tmp` | 1.06 GB |
| C 盘临时目录中的 `DiagOutputDir` | `DiagOutputDir` | 0.97 GB |
| `C:\Users\Administrator\AppData\Local\Temp\32zxapvm` | `32zxapvm` | 0.36 GB |
| C 盘临时目录中的两个 `rea-ghidra-*` 工作目录 | 同名目录 | 合计 0.12 GB |

若需回退，先确认原路径仍不存在，再把归档中的目录逐一移回；对未记录完整原路径的两个临时目录，应先核对当时工具的临时目录设置。移动后 C 盘仍仅有约 0.60 GB 可用：Windows 自动管理的 `C:\pagefile.sys` 已扩到约 12.1 GB。`C:\hiberfil.sys` 约 12.8 GB、Docker 的 `docker_data.vhdx` 约 4.8 GB。三者都没有手工删除或移动；休眠文件和分页文件属于系统配置，Docker VHD 需通过 Docker 的迁移或清理功能处理。

另有 `C:\Users\Administrator\AppData\Local\Temp\DockerDesktopUpdates` 约 0.33 GB，因 Docker Desktop 正在运行，未动。后续 REA 分析的临时目录应在命令级设为 `D:\Tools\REA\analysis\tmp`；不要把长期项目产物写进 C 盘临时目录。

## 验证

- 茶 2 人辅助 `bot/tests`：37 passed。
- 现有算牌子仓库 `brain_v0/tests pc_bot/tests`：设置 `PYTHONUTF8=1` 后 442 passed、13 skipped、206 subtests passed。未设置时有 1 项因 Windows 默认 GBK 读取 UTF-8 测试清单失败；已按相同命令在 UTF-8 环境复核通过。测试过程还出现测试坏图所预期的 `libpng error: PNG input buffer is incomplete` stderr，但退出码为 0。
- 未执行原版客户端登录、在线算牌调用、模拟器实战或自动打牌。
