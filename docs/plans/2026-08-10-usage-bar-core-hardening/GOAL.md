# GOAL — Usage Bar 内建化完善 (M0→M3)

唯一 writer：本 session。父会话只读验收。

## 总目标

按 M0→M1→M2→M3 完善内建 usage/account limits 链路
(`usage.accounts -> ContextUsagePanel -> statusbar context-usage`)：

- **M0** 固化当前基线、审计 dirty diff、建立可恢复控制面与安全锚点、评估上游同步。
- **M1** 恢复真实账号额度信息：安全稳定账号名、当前账号 badge、plan、reset
  countdown、window detail、source、freshness、stale、provider-specific error；
  加强请求可靠性（deadline、in-flight dedupe、Retry-After、negative
  cache/circuit breaker）。
- **M2** 完整入口整合进 Command Center（状态栏保留为快捷层）；逐步恢复
  Provider/Model/Task 维度；不恢复旧 1681 行插件。
- **M3** 补充 provider、可靠性与 E2E（Nous credits、transient stale、in-flight
  dedupe、主题/缩放/离线/键盘/reduced-motion）。

## 本轮边界

本轮完成 **M0 + M1/M2 高保真 mockup**，停在 `AWAITING_MOCKUP_APPROVAL`。
不实施新的 M1/M2 生产 UI；可固化/评审/测试已在现行 release 中运行的 dirty 修复。

## 设计与架构约束（不可违反）

1. 真相来源分离：provider quota / credential health / local analytics 不混算。
2. 不平均不同时间窗口；不把 cooldown/429 当作使用百分比。
3. 安全账号名稳定，不由 cooldown/priority/数组顺序临时决定；不暴露内部 label。
4. contract v1 走可选 additive 字段兼容旧客户端；语义破坏需显式 version 决策。
5. stale 仅用于可重试传输/上游故障；401/403 不得用 stale 掩盖认证失效。
6. provider 单请求预算 ≤ 全局 deadline；分析线程 deadline 后仍运行、in-flight
   dedupe、Retry-After、negative cache/circuit breaker。
7. 状态栏紧凑；完整分析进 Command Center；不搬回旧插件 640px 巨面板。
8. xAI 无可靠官方接口时只能标 independent local estimate，不标 official quota。

## 安全约束

- 不读取/输出任何 token/API key/refresh token/credential label/邮箱；凭证值永远 `[REDACTED]`。
- 不修改 `config.yaml`、`.env`、凭证池；不启用旧插件；不 push、不 deploy。
- 不向用户提问；客观 blocker 写入 STATE/CHECKPOINT 后继续不受阻项。
