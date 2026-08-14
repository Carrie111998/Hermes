# EXECUTION BRIEF（原始任务书，2026-08-10 父会话注入）

你是本任务唯一 writer。运行模型应为 Kimi K3（provider `kimi-coding`，model `k3`）。父会话只负责只读验收，不会并发写仓库。

## 工作目录

`C:\Users\Admin\AppData\Local\hermes\hermes-agent`

## 总目标

按 M0→M1→M2→M3 完善内建 usage/account limits：

- M0：固化当前基线、同步上游、建立可恢复控制面；
- M1：恢复真实账号额度信息（安全稳定账号名、当前账号、plan、reset、detail、source、freshness、stale、reason），并加强请求可靠性；
- M2：把完整入口整合进 Command Center，保留状态栏为快捷层；逐步恢复 Provider/Model/Task 维度，但不恢复旧 1681 行插件；
- M3：补充 provider、可靠性和 E2E（含 Nous credits、transient stale、in-flight dedupe、主题/缩放/离线/键盘/reduced-motion）。

## 本轮强制边界

本轮先完成 **M0 + M1/M2 高保真 mockup**，然后停在 `AWAITING_MOCKUP_APPROVAL`，由父会话验收。未经父会话确认，不得实施新的生产 UI 设计。

可以固化/评审/测试当前已有 dirty UI 修复，因为它们已在现行 release 中运行；但不得在本轮新增 M1/M2 生产 UI。

## 必做流程（摘要）

1. 加载并遵守 hermes-agent / systematic-debugging / test-driven-development /
   writing-plans / requesting-code-review / better-accessibility。
2. 复核仓库规则、git status/log/worktree、当前 release receipt。
3. 维护本目录控制面（GOAL/PLAN/STATE/CHECKPOINT/EXECUTION_BRIEF/history）。
4. 实施计划先经本地 Codex CLI read-only 多轮评审直到 APPROVE，覆盖：安全、
   凭证隔离、cache identity、deadline/worker 生命周期、profile scope、
   向后兼容、发布/回滚、测试矩阵。
5. 逐文件事实审计 dirty diff，识别噪声，不误收无关变更。
6. M0 固化：安全锚点；不 reset --hard；release backup 验证后归档仓库外；
   任务自有修复整理为可审查 commit；干净 checkpoint 后评估 origin/main 同步，
   冲突跨大量无关模块立即 abort 登记 blocker；不 push、不 deploy。
7. 重跑并记录真实输出：后端 pytest（usage_contract + usage_accounts +
   account_usage）、vitest（context-usage-panel + statusbar-visibility）、
   Desktop typecheck/build、git diff --check、可行时 package/release smoke。
8. M1/M2 高保真 mockup：状态栏快捷摘要、account limits popover（Codex 1/2、
   当前 badge、plan、reset countdown、window detail、source、freshness、
   stale、provider error）、Command Center→Usage 顶部 Account Limits、
   状态栏关闭时经 Command Palette/Command Center 可达、light/dark、
   窄/常规视口、reduced-motion 与键盘焦点说明。产物放 mockup/。
9. 不读取/输出任何凭证；凭证值永远 [REDACTED]。
10. 不改 config.yaml/.env/凭证池；不启用旧插件；不 push/deploy。
11. 不向用户提问；blocker 写入 STATE/CHECKPOINT 后继续。

## 设计与架构约束

真相来源分离；不平均不同时间窗口；cooldown/429 不作使用百分比；安全账号名
稳定；contract v1 additive 优先；stale 仅用于可重试故障，401/403 不用 stale
掩盖；provider 单请求预算 ≤ 全局 deadline；状态栏紧凑，完整分析进 Command
Center；xAI 只作 independent local estimate。

## 本轮完成门

- STATE.json = AWAITING_MOCKUP_APPROVAL（除非客观 blocker）；
- 报告 session ID、建议 title、HEAD/commit/dirty、上游同步状态、测试命令与结果、
  mockup/截图绝对路径、Codex 评审结论、父会话验收清单；
- 不宣称 M1/M2/M3 已实现。
