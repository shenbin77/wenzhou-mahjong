# 温州茶牌面原图提取 — 调查结论（2026-10-17）

## 结论：APK/热更包静态提取此路不通（资源全加密）

### 1. 热更服务器（可用）
- 主域名 `gxb-api.hzxuanming.com` 被 egress 代理拦（400），**备用 `gxb-api-tx.hzxuanming.com` 可用**
- 麻将模块 appid=1089，当前版本 **1.0.0.723**，共 **1765 个文件**
- manifest 已存：`manifest-1089-1.0.0.723.json`
- 文件 CDN：`https://gxb-cos.hzxuanming.com/yj/files/<hashpath>`（`?t=` 时间戳非必需）

### 2. 牌面图集位置（已定位，未能解密）
| 文件 | manifest 路径 | 大小 |
|---|---|---|
| MahFace.png (+.plist) | res/cocosStudio/MahjongNew/MahLayer/Image/ | 144KB |
| MahFace_2.png / _3 / _4 | 同上 | _2=163KB |
| MahFace_TaiZhou | 同上 | 只有 plist，无 png（MISSING）|

加密文件已存：`MahFace.png.encrypted` / `MahFace.plist.encrypted`

### 3. 加密方案（静态无法破解）
- 自定义头：`89 77 88 89 82`（5字节魔数）+ 1字节类型（01=png, 02=plist）+ 加密载荷
- PNG 魔数 `89 50 4E 47...` 对不上，XOR 反推无规律 → 疑似 AES/流密码，密钥在 native 层或运行时内存
- 抽查其他小图（jinhuaui.png 等）同样加密 → **整个热更包统一加密**
- 之前静态分析报告只记录了 Lua 用 AES（`universe::network::Encryption::decryptLua`），图片加密是新增发现

### 4. winagent 状态（2026-10-17 01:00 左右）
- `https://winagent.clawskill.cloud` 隧道通（无 token 返回标准 401 `{"ok":"bad token"}`）
- **带有效 token 的任何请求（query/header、GET/POST）都被断连接（HTTP 000）**
- 服务端源码（`~/workspace/muse-connector-open/src/win_agent.py`）中无"真token断连接"逻辑 → 问题在隧道/网络层，非预期行为
- 约 1 小时前（00:53）前一个 subagent 还能正常调用 → 之后发生的

### 5. 建议后续（按优先级）
1. **用户在PC上重启 winagent**（托盘右键重启服务 / 重跑 restart 脚本），恢复后：
   a. 首选：从模拟器内存/运行时dump解密后的 MahFace 图集（游戏运行时已解密）
   b. 备选：进游戏实战，定向截缺的 11 张（筒7/8/9、条4-9、南、白）
2. **穷举现有 1728 张截图**：之前只抽查了部分，全量扫一遍可能找到南/白和高点数牌
3. **程序化合成**：用已有的 21 张推导缺失牌（南与东/西/北同字体；筒条按标准点数排列；白为空白）——精度低于实拍
4. 热更包以后版本若换加密方式可再试；当前版本静态无解
