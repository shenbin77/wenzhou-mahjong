# 2 人茶识牌候选样本（2026-10-19）

从已有回放里挑出 25 张可解码的 1600×900 帧图，分布在 10 个逐局目录。它们只是待标注候选，尚未逐张核对画面房号、逐牌人工标注，也不能作为 25 个独立局面或实战准确率。

路径均相对 `采集/`：

| 逐局目录 | 候选文件 |
| --- | --- |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r01` | `frame_01.png`, `frame_02.png`, `_ac.png` |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r02` | `frame_01.png`, `frame_02.png`, `_ac.png` |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r03` | `frame_01.png`, `frame_02.png`, `frame_03.png`, `_ac.png` |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r04` | `frame_03.png` |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r05` | `frame_01.png`, `frame_02.png`, `frame_03.png` |
| `box_replay_harvest_missing/tea/2026-10-13/207547/r06` | `frame_01.png`, `frame_02.png`, `_ac.png` |
| `2026-10-15/room_p2a/r01` | `frame_01.png`, `frame_02.png`, `frame_03.png` |
| `2026-10-15/room_p2a/r03` | `frame_01.png`, `frame_02.png`, `frame_03.png` |
| `2026-10-15/room_p2a/r12` | `frame_01.png` |
| `2026-10-16/740195/r10` | `frame_01.png` |

抽看图像时，`207547` 目录的画面顶部显示房号 `964065`；`room_p2a` 的部分画面显示 `543271`。这些只是抽查结果，不能自动推广到同目录每张图。还有不同目录的手牌区域完全相同，按文件路径切训练集和验证集会泄漏样本。

下一步逐张核对画面房号、2 人模式与 17 张手牌真值；按实际画面房间和独立牌局去重分组。核对之前不把候选帧用于训练或准确率统计。
