# 父会话验收 Round 1 — 修订记录（2026-08-10）

> 本文件的结论优先于 `codex-review-round5-verify.md`：R5 的"预算闭环"判断
> 被父验收 A2 修正——闭环的是 phase timeout 配置收敛，不是 wall-clock。

## A1 — stale classifier 过宽（已修复，TDD）

- 问题：`_is_retryable_fetch_error` 对整个 `httpx.TransportError` 放行，
  包含 `UnsupportedProtocol`/`LocalProtocolError` 等本地配置/调用错误。
- RED：`uv run pytest tests/agent/test_usage_contract.py::test_stale_classifier_excludes_protocol_and_programming_errors -q`
  → 1 failed（`UnsupportedProtocol` 被判 retryable），12:5x 实测。
- 修复：白名单收窄为 `TimeoutException / NetworkError / RemoteProtocolError /
  ProxyError` + 5xx；其余（含 401/403/429、LocalProtocolError、
  UnsupportedProtocol、程序错误）一律不 stale。
- GREEN：同上命令 → 1 passed；`test_usage_contract.py` 全量 17 passed。
- 429 未改成 stale；Retry-After negative cache 仍属 M1。

## A2 — wall-clock 口径修正（计划/状态层）

- 采纳父会话独立 Codex review：httpx timeout 标量是 phase 级；Codex native
  resolver refresh 可达 20s+认证锁；6.5s 是面板/contract 等待上限，不是
  worker 生命周期上限。
- M0 真实闭环口径改写进 PLAN 可靠性面与 STATE；wall-clock / in-flight
  生命周期 / Codex refresh 长调用 / 进程级并发上限并入 M1 发布阻塞
  （与 singleflight + process-wide semaphore + negative cache + worker
  bounded stress 同项闭环）。
- 测试改名 `test_provider_fetch_phase_timeout_budget_within_deadline`，
  docstring 明确只证明配置的 phase timeout 数值。

## A3 — artifact quality gate（已修复）

- `git diff --check 299d5d7d9..HEAD` 父会话实测失败（CRLF + patch 尾随空格）。
- 处理：计划目录全部文本工件规范为 LF；`pre-m0-dirty.patch` 移至
  `C:\Users\Admin\AppData\Local\hermes\cache\usage-bar-core-hardening\pre-m0-dirty.patch`，
  SHA-256/字节数记录于 CHECKPOINT/STATE，repo 内删除；range diff-check 复跑。

## A4 — 控制面一致性

- STATE 去除自引用 hash（`head_now`/`control_plane_commit` 不再指向承载
  自身的 commit），改 `head_before_acceptance_fix` + `head_resolution`；
  状态设 `AWAITING_PARENT_REVIEW`，不自行宣称 APPROVED。

## B — mockup 修订

进度条语义统一为 fill=remaining；360px 错误行垂直堆叠；Command Center
补 Task 区；`source: provider_reported` 改为用户文案 `Official provider
data`；动作改真实 button/a；palette rows 可聚焦；stale live region
`role=status`；index 明确标注键盘/焦点行为为 production acceptance target；
360px DOM assertion（scrollWidth==clientWidth、全部 action 可 Tab）留存证据；
全部截图重生成。
