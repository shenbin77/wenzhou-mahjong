# 离线回测报告 (2026-10-15 20:32:55 CST)

- schema: `backtest-report/v0`
- user_account: 晓红
- 数据源: ['/workspace/majiang-bot/brain_v0/data/backtest/analyze_r01_1015/replay_latest.json', '/workspace/majiang-data/replay_harvest/2026-10-15/r01/meta.json', '/workspace/majiang-bot/brain_v0/data/backtest/analyze_r01_1014/replay_latest.json', '/workspace/majiang-data/replay_harvest/2026-10-14/r01/meta.json', '/workspace/majiang-bot/brain_v0/data/backtest/analyze_r02_1014/replay_latest.json', '/workspace/majiang-data/replay_harvest/2026-10-14/r02/meta.json', '/workspace/majiang-bot/pc_bot/logs/decisions.jsonl']
- 局数 n_rounds: **3**
- 决策点数: **10**（actual_missing=10, errors=0）
- bot vs actual 一致率: **N/A**
- 一致率说明: N/A: 无 actual_discard/actual_action 真值（actual_missing）
- 向听 before 均值: 1.9
- 向听 after bot 均值: 2.0
- 向听 after actual 均值: None
- ukeire bot 均值: 43.7
- ukeire actual 均值: None
- ting_count 均值: 0.0

## 卡点

- 3局 harvest 帧均无 hand14：弃牌决策点仅来自 decisions.jsonl 或 hand13 听牌态
- 全部决策点 actual_missing：无 frame_diff/manual_gold 真值，agree_rate=N/A
- frame_diff 未命中：相邻 gameplay 帧无「多重集合恰好少1张」的 14→13 过渡

## 分局

- `346299-2026-10-15-r01`: decisions=3 hand14=0 hand13=3 missing=3 rate=None
- `302805-2026-10-14-r01`: decisions=3 hand14=0 hand13=3 missing=3 rate=None
- `302805-2026-10-14-r02`: decisions=1 hand14=0 hand13=1 missing=1 rate=None
- `decisions-adapt-decisions`: decisions=3 hand14=3 hand13=0 missing=3 rate=None

## 逐点

- [0] round=346299-2026-10-15-r01 turn=3 h14=False bot=None actual=None agree=None missing=True sh_before=2 sh_bot=None ukeire_bot=28 err=None
- [1] round=346299-2026-10-15-r01 turn=4 h14=False bot=None actual=None agree=None missing=True sh_before=1 sh_bot=None ukeire_bot=20 err=None
- [2] round=346299-2026-10-15-r01 turn=5 h14=False bot=None actual=None agree=None missing=True sh_before=1 sh_bot=None ukeire_bot=20 err=None
- [3] round=302805-2026-10-14-r01 turn=2 h14=False bot=None actual=None agree=None missing=True sh_before=2 sh_bot=None ukeire_bot=56 err=None
- [4] round=302805-2026-10-14-r01 turn=3 h14=False bot=None actual=None agree=None missing=True sh_before=2 sh_bot=None ukeire_bot=56 err=None
- [5] round=302805-2026-10-14-r01 turn=4 h14=False bot=None actual=None agree=None missing=True sh_before=2 sh_bot=None ukeire_bot=56 err=None
- [6] round=302805-2026-10-14-r02 turn=2 h14=False bot=None actual=None agree=None missing=True sh_before=3 sh_bot=None ukeire_bot=51 err=None
- [7] round=decisions-adapt-decisions turn=1 h14=True bot=8 actual=None agree=None missing=True sh_before=2 sh_bot=2 ukeire_bot=50 err=None
- [8] round=decisions-adapt-decisions turn=1 h14=True bot=8 actual=None agree=None missing=True sh_before=2 sh_bot=2 ukeire_bot=50 err=None
- [9] round=decisions-adapt-decisions turn=1 h14=True bot=8 actual=None agree=None missing=True sh_before=2 sh_bot=2 ukeire_bot=50 err=None
