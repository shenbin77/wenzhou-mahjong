# Learnings

## [LRN-20261027-002] “打牌快一些”不是新增倒计时五秒门禁

**Priority**: high
**Status**: promoted
**Area**: gameplay-latency

### 内容
用户纠正：其诉求只是让辅助器整体更快完成打牌，不是要求点击时画面倒计时必须剩余5秒。把口语中的“压进倒计时、不能低于5秒”解释成新的安全保留线，会把性能优化错误扩成行为门禁。

### 建议修复
先用真实延迟台账分解抓帧、识牌、稳定帧、决策、点击与点后验证耗时，只压缩实测瓶颈；不新增5秒保留线，不降低识牌阈值，也不删除点击前后复核。

### 元数据
- Source: correction
- Pattern-Key: gameplay-speed-not-countdown-gate
- Promoted-To: AGENTS.md

---

## [LRN-20261027-001] 多开管理器采用显式单设备启动而非周期刷新

**Priority**: high
**Status**: promoted
**Area**: launcher-ux

### 内容
用户纠正：“一键启动”不是批量拉起全部雷电，而是按某个离线设备行后，只启动该设备对应的一台雷电并打开一个辅助器；管理器不需要周期性后台刷新。旧实现每轮刷新销毁并重建设备行，会造成可见闪烁，也让自动行为超出用户预期。

### 建议修复
设备列表只在初始化、手动刷新和明确操作完成后更新；单行启动须先由 `ldconsole list2` 验证实例存在，再有限等待该 serial 上线并打开辅助器。重复、未知或不存在的实例全部拒绝，且不恢复跟随或全自动代打权限。

### 元数据
- Source: correction
- Pattern-Key: explicit-single-device-launch
- Promoted-To: AGENTS.md

---

## [LRN-20261026-001] 完整场次事实与零人工验收必须分开

**Priority**: high
**Status**: promoted
**Area**: validation

### 内容
用户纠正：昨晚代打实际已勉强走完整个16局场次。此前只写“16局未通过”容易被理解为从未打完；反过来把“打完”写成稳定通过也不成立。

### 建议修复
后续同时记录“是否完成过完整场次”和“是否满足当前SHA零人工稳定验收”。旧版本、停手、重拉、人工介入的完整场次只证明端到端可行，不能继承为N3通过。

### 元数据
- Source: correction
- Pattern-Key: evidence-vs-acceptance
- Promoted-To: AGENTS.md

---

## [LRN-20261025-001] 点后验证使用相对变化和副露状态

**Priority**: high
**Status**: promoted
**Area**: gameplay-validation

### 观察
用户指出碰、吃、杠会改变暗手绝对张数，不能用固定 13／14 张验证所有局面。实机又出现胡提示面板帧把左侧既有副露混入通用计数，导致 raw 14→15 假停。

### 规则
- “过”只要求副露不变，暗手相对 +0／+1。
- 吃／碰／明杠／加杠分别核对暗手增量与副露组变化。
- 两击间数牌以点击前 fresh 帧确认的暗手列为白名单，排除左侧既有副露。
- 任何相对变化或副露变化读不唯一时弃权并留证。

### 推广
已写入项目根 `AGENTS.md`，供后续所有实机动作验证复用。
