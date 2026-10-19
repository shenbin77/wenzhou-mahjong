# 动作按钮模板缺口（2026-10-15）

| 模板 | 状态 | 证据 |
|------|------|------|
| btn_guo.png | 有 | 青色圆形「过」 |
| btn_xiayiju.png | 有 | 结算「下一局」 |
| btn_hu.png | **缺** | 搜遍 frames_raw/live、replay_harvest、captures、stash、相关分支；无实战「胡」钮帧 |
| btn_gang.png | **缺** | 同上 |

bot.py CONFIG 已引用 `btn_hu.png` / `btn_gang.png`，缺文件时动作检测无法匹配。需真打出现动作条时截 1600×900 再裁。
