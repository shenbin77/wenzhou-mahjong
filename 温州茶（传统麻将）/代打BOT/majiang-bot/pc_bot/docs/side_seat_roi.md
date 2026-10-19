# 侧家手牌 ROI 方案（1600×900 回放帧）

> 状态：探针级假设，**不过线**。成功标准先看「检出张数/盒子」，分类准确率后置。  
> 对齐 `hand-record/v0`：`seats[].seat ∈ {bottom,right,top,left}`；侧家可继续 `tiles=[]` + `unknown_count`。

## 1. 与底家检测的差异

| 维度 | bottom（已有 TileVision） | right / left / top |
|------|---------------------------|--------------------|
| 朝向 | 正面立牌，面朝镜头 | 右/左：竖排侧立/透视；上：多背面或强压缩正面 |
| 几何 | 水平一条带，宽≈80–110px | 右/左：竖直条带，单张投影很窄；上：水平但高度矮 |
| 分隔 | 竖向暗边可切盒 | 侧立分隔变成**水平**缝；透视导致盒高不等 |
| 模板 | `templates_hongzhong_v2` 正面签名 | 同模板直接匹配会系统性失败（需透视矫正或侧视模板） |
| 金标 | 可读，已做底家金标 | 本批**不强行金标**，避免假精确 |

## 2. 候选 ROI 假设（参考分辨率 1600×900）

坐标为半开区间 `(x0,y0,x1,y1)`，来自 `replay_harvest/2026-10-14/r01/frame_02.png` 目视 + 边缘/亮区扫描。

| seat | ROI 假设 | 说明 |
|------|----------|------|
| **right** | `(1340, 240, 1440, 710)` | 右家竖排在**头像左侧**靠桌心；过右会裁到「啊泽哥」头像 |
| **top** | `(450, 45, 1150, 140)` | 对家水平条；回放里可能见背面（白+橙底）或压缩正面 |
| **left** | `(170, 240, 280, 710)` | 左家竖排在**头像右侧**靠桌心；过左会裁到「李哥/晓红」UI |
| bottom（对照） | `(0, 720, 1600, 900)` | 现有 `TileVision` 默认 `y≥0.8h` |

缩放：非 1600×900 时按 `sx=w/1600, sy=h/900` 线性缩放各角。

### 透视 / UI 干扰

- 回放中心常有播放条（暂停/倍速），勿扩 ROI 进中区。
- 左右头像+昵称+分值条与牌列紧邻，ROI 宜偏桌心 10–20px。
- 侧立牌面厚度可见、花色被压缩；上家常只见牌背 → **计数盒子优先于分类**。

## 3. 探针用法

```bash
cd /workspace/majiang-bot
.venv/bin/python pc_bot/tools/side_seat_probe.py \
  /workspace/majiang-data/replay_harvest/2026-10-14/r01/frame_02.png \
  /workspace/majiang-data/replay_harvest/2026-10-15/r01/frame_03.png
```

输出：

- 裁图：`pc_bot/logs/side_probe/<frame_stem>_{right,top,left}.png`
- stdout：每家候选盒数量（启发式，非生产检测器）

不调用分类、不写 bot 热路径。

## 4. 成功标准（分阶段）

1. **P0（本批）**：ROI 裁图肉眼可见侧家牌列；探针盒数对清晰帧 roughly 10–14（允许 ±3）。
2. **P1**：专用侧视/背面计数器稳定到 miss/extra ≤ 2。
3. **P2**：分类（需侧视模板或透视 warp）再进金标；此前 `unknown_count` 保持。

## 5. 与 hand-record / 金标的关系

- 底家 / 侧家探测：侧家仍可 `tiles: []` + `unknown_count: 13`。
- **少量可读金标（2026-10-15）**：right/left/top 仅目标视清晰的牌（不硬标 13 张）。
  - 金标：`pc_bot/logs/gold/side_seat_gold_YYYYMMDD.json`（`gold_by_index` + partial `tiles` / `unknown_count`）
  - 评测：`pc_bot/tools/side_seat_acc.py --gold-eval` → `pc_bot/logs/side_seat_acc_YYYYMMDD.md`
  - 预处理：right `rot90_scale`；left `rot270_scale`；top `scale`（可 `mode=none` 回退）
- `tile_id` 为 TARGET_IDS int；别名 `w/t/s/hongzhong` 仅人类可读。
