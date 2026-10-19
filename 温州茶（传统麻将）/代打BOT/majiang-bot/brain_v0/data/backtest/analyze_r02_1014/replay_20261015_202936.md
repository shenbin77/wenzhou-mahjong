# 回放分析报告

- 生成时间: 2026-10-15 20:29:36 CST
- 输入: explicit:/workspace/majiang-data/replay_harvest/2026-10-14/r02
- dry_run: False
- JSON: `/workspace/majiang-bot/brain_v0/data/backtest/analyze_r02_1014/replay_20261015_202936.json`

## 规模
- 帧数: **3** (ok=3, 有牌=1, 完整=1, gameplay=1)
- hand14/hand13: 0 / 1
- 估计局数: **1**

## 识牌完整率
- complete_rate: **1.0**

## 红中 / 起手
- gameplay 红中数分布: `{0: 1}`
- 起手红中分布: `{'0': 1}`
- 起手向听均值: **3.0**

## 胜率 / 胡牌率
- 有结算分的局: 0
- score>0 局数: 0
- win_rate: **None**
- 备注: 无法从结算帧/settle OCR 推出胜率：本批无可靠 settle_score

## 复跑命令
```
python -m tools.replay_analyze
# 或指定目录:
python pc_bot/tools/replay_analyze.py --input /workspace/majiang-data/replay_harvest
```
