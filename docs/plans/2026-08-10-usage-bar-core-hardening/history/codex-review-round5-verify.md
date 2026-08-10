# Codex 评审 R5 — 修复复核轮（2026-08-10 12:5x）

复审对象：R4 REVISE 6 项最小修改清单的落地情况（代码 + 测试逐项核验，
非仅计划声称）。执行：本地 Codex CLI read-only（专用 CODEX_HOME 绕行用户
config.toml 的 `[agents]` schema 不兼容）。

## 逐项结论（全部 VERIFIED，行级依据）

1. VERIFIED — `_credential_fingerprint` 忽略 persisted fingerprint，域分隔
   `sha256("usage.accounts/v1|" + runtime_token)`（usage_contract.py）。
2. VERIFIED — `_canonical_endpoint` 剔除 userinfo/query/fragment、默认端口
   折叠、IPv6 brackets；顺带修掉默认端口未折叠的既有 bug。
3. VERIFIED — `_is_retryable_fetch_error`：timeout/transport/5xx → stale；
   401/403/429 → error，绝不 stale。
4. VERIFIED — `USAGE_FETCH_TIMEOUT_SECONDS=6s` 统一 contract 路径全部
   provider（openrouter 双请求各 3s，总 6s ≤ 6.5s deadline）。
5. VERIFIED — statusbar 迁移仅在持久化集合与旧默认精确匹配（长度+成员
   双比较）时剥离 `context-usage`；一次性 marker 幂等（statusbar-prefs.ts:43-70）。
6. VERIFIED — 测试在库：persisted-fingerprint 轮换
   （test_usage_contract.py:468）、endpoint 等价类（:498）、profile 隔离
   （:517）、late worker 不污染（:526）、6 任务 ≤4 worker（:546）、分状态
   stale 语义（:579）、四 provider timeout 预算（test_account_usage.py:310）、
   Desktop 迁移 6 项（statusbar-prefs.test.ts:35-79）。

注：评审环境 read-only 策略拒绝执行 pytest/vitest，本轮结论基于实现、调用
路径与测试断言逐行复核；测试真实运行输出见 CHECKPOINT（写者侧执行：
后端 24 passed、Desktop 19 passed）。

## 总结论：APPROVE
