# Codex 评审 Round 2 — VERDICT: CHANGES_REQUESTED (2026-08-10)

## 遗留问题

1. **[中] R1#2 singleflight 与"旧 future 不复用"语义矛盾** — join/负缓存/新建
   任务没有明确状态机；需定义 deadline 后 in-flight entry 的保留/移除、后续
   调用行为与清理条件，并统一测试。
2. **[中] R1#5 回滚制品完整性仍未成为发布门槛** — SHA-256 清单/构建 commit/
   版本/兼容范围被延后为"M1 收尾动作"，仍允许未校验回滚包时发布。应将制品
   校验 + 一次按序恢复演练明确列为发布阻塞条件。
3. **[低] R1#1 的当前实现描述失真** — 实现已改为忽略持久化 fingerprint、基于
   runtime_token 哈希（usage_contract.py:118, `del entry`）；剩余任务只是补
   域分隔与隔离测试。PLAN 表述须修正，避免错误基线。

## 已通过

R1#3 异常分类与测试要求、R1#4 精确集合迁移与测试均妥善回应；statusbar 表述
与当前实现一致。

## Round 2 对应的 PLAN 修订

- 数据面 P0 改为"补域分隔前缀 + 轮换隔离测试"（实现基线已修正）。
- singleflight 四态状态机写入可靠性面第 3 条（无 entry→发起；在飞且
  未过期→join；在飞且过期→不 join、fresh→stale→negative 服务、semaphore
  有余量才替代 fetch、旧 future settle 不写缓存；settle→移除）。
- 发布前提升级为全部阻塞条件：可靠性 1–4 + profile 隔离 + 制品 MANIFEST +
  恢复演练证据。
