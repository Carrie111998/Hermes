# PLAN — Usage Bar 内建化完善 (M0→M3)

> 评审对象：本文档。评审方式：本地 Codex CLI read-only 多轮，直到 APPROVE。
> 评审必须覆盖：安全、凭证隔离、cache identity、deadline/worker 生命周期、
> profile scope、向后兼容、发布/回滚、测试矩阵。

## 现状事实（2026-08-10 复核）

- 分支 `codex/hermes-custom-migrate-v020`，HEAD `299d5d7d9 feat: add canonical
  multi-account usage bar`，相对 `origin/main` ahead 2 / behind 29。
- 内建链路：`agent/usage_contract.py::build_usage_contract` → tui_gateway
  `usage.accounts` → Desktop `ContextUsagePanel` → statusbar `context-usage`。
- 旧插件 `desktop-plugins-disabled/model-usage-bar/plugin.js`（1681 行）仅作 UX
  参考，不重新启用。
- 现行修复（已在 release 中运行，本轮固化；含 Codex round-1 评审修订）：
  - `agent/usage_contract.py`：cache identity（profile 路径 + provider +
    entry_id + source + canonical endpoint + **当前 runtime token 的 sha256**；
    persisted fingerprint 可能滞后轮换，不作锚点）。endpoint 归一化剔除
    userinfo/query/fragment（非身份且可能携带 secret/临时签名）、折叠默认端口、
    IPv6 补 brackets。identity 含 secret 派生指纹——不是明文泄漏，但属于
    secret-derived identifier，**禁止日志化或外传**。60s fresh / 600s stale
    LRU(128) 进程缓存、6.5s 全局 deadline 下 ≤4 worker 并发 fetch；stale 仅
    掩盖可重试故障（timeout / transport error / 5xx），401/403/429 一律
    表面化为 error，不用 stale 掩盖。env 引用凭证 runtime 重读（信任前提：
    凭证池文件为可信配置，`env:<VAR>` 引用无 allowlist）。Codex 过期 token
    单账号走 native resolver 且绕过缓存。
  - `agent/account_usage.py`：`USAGE_FETCH_TIMEOUT_SECONDS = 6.0` 统一收敛
    contract 路径全部 provider 的 HTTP phase timeout（httpx 各阶段标量，
    非 wall-clock；Codex/Anthropic/Kimi 6s；OpenRouter 两次顺序请求各 3s）。
  - Desktop：statusbar `context-usage` 移出默认隐藏集；一次性迁移**仅在持久化
    集合与旧默认完全一致时**剥离 context-usage（无法逐条区分"默认隐藏"与
    "显式隐藏"，自定义集合一律视为有意保留；残余风险：恰好复现旧默认集合的
    显式隐藏会被覆盖一次，已如实记录）。`hidden` 由 gateway open 门控；
    `AccountUsageQuota` 增加 additive 可选字段 `fetched_at`/`stale`。
  - 测试：contract 16 项（含 persisted-fingerprint 轮换、endpoint 等价类、
    profile 隔离、late worker 不污染、>4 jobs 并发上限、401/403/429/5xx/
    connect 的 stale 语义）+ account_usage 7 项（含逐 provider timeout 预算
    断言）+ Desktop 19 项（含 migration 6 项单测）。
- 已剔除噪声：`package-lock.json` 的 `peer:true` 删除（npm 版本差异），已还原。

## M0 — 固化基线（本轮执行）

1. 安全锚点：分支 `backup/usage-bar-hardening-pre-m0-20260810`（指向锚点 HEAD）
   + `history/pre-m0-dirty.patch` 全量快照。不 reset --hard，不丢工作。
2. release 备份验证与归档：`apps/desktop/release-backup-usage-20260810-114009/`
   为今晨 release 构建的 electron-builder win-unpacked 备份（765MB），用途=当前
   release 的回滚包；移出仓库至
   `C:\Users\Admin\AppData\Local\hermes\cache\release-backups\`。
3. 重跑 focused 基线（真实输出记录进 CHECKPOINT）：
   - `scripts/run_tests.sh tests/agent/test_usage_contract.py
     tests/tui_gateway/test_usage_accounts.py tests/agent/test_account_usage.py -q`
     （run_tests.sh 为 CI 等价 hermetic wrapper，等价于 brief 中的两条 pytest）
   - `npx vitest run --project ui src/app/shell/context-usage-panel.test.tsx
     src/app/shell/statusbar-visibility.test.tsx`（apps/desktop）
   - Desktop typecheck + build（按仓库实际脚本）
   - `git diff --check`
4. 将任务自有修复整理为可审查 commit（后端契约/缓存 1 个，desktop statusbar 1 个，
   或按评审意见合并为 1 个）。
5. 干净 checkpoint 后评估 `origin/main` 同步：先试 merge/t rebase --no-commit 探测
   冲突面；若冲突跨越大量无关模块，立即 abort 并登记 blocker，不猜测式解决。
   不 push。

## M1 — 真实账号额度信息 + 请求可靠性（下轮，需 mockup 批准后）

### 数据面（后端，contract v1 additive）

- **P0（Codex R1#1/R2#3）cache identity 的 credential 分量**——✅ M0 已闭环：
  忽略持久化 `secret_fingerprint`，直接域分隔哈希
  `sha256("usage.accounts/v1|" + runtime_token)`（防跨用途哈希混淆）；
  "同一 env 引用 + 旧 fingerprint + token 轮换 → 两次 identity 不同、旧缓存
  不可见"回归测试在库。
- `AccountUsageQuota` 已有 additive 字段：`plan`、`source`、`fetched_at`、
  `stale`、`reason`、`details[]`；windows 带 `label/used_percent/reset_at/detail`。
- 补齐 per-account 稳定展示名：新增可选 `display_name`（由
  `sanitized_account_id` 同源哈希派生，如 `Codex 1/2` 中的序号来自
  `(provider, entry_id)` 稳定排序，不受 cooldown/priority/数组顺序影响）。
- 当前账号标记：新增可选 `is_current`（与路由层当前选用凭证同源的稳定判定，
  由后端计算，前端不猜）。
- freshness 语义：`fetched_at` + `stale` 已足够；前端只展示，不推断。
- stale 仅挂在传输类故障（timeout/connect/5xx）；401/403 →
  `status=unavailable, reason="Credential token expired"/auth`，不回退 stale。

### 可靠性面

- 已实现（M0，父验收 R1 修订后口径）：**面板/contract 等待上限 6.5s**
  （`wait(timeout=_FETCH_DEADLINE_SECONDS)` 保证 build 按时返回）、单次 build
  ≤4 workers、HTTP phase timeout 收敛（`USAGE_FETCH_TIMEOUT_SECONDS=6s` 是
  httpx 的 connect/read/write/pool **各阶段**标量，不是整个调用的
  wall-clock 上限；OpenRouter 双请求各 3s 同理）、late worker 不写 usage
  cache、错误分类器（timeout/network/remote-protocol/proxy/5xx → stale；
  401/403/429/UnsupportedProtocol/LocalProtocolError → error，绝不 stale；
  有/无缓存场景均锁定）、>4 jobs 并发上限测试。
  **未闭环（父验收 A2）**：httpx 标量按阶段生效，一次调用可多阶段/多请求，
  wall-clock 无上限；单 Codex 过期 token 的 native resolver refresh 默认
  可达 20s 且认证锁可能更久——contract 外层 6.5s 及时返回，但 daemon
  worker 继续运行。worker 生命周期须随 M1 singleflight + 进程级 semaphore
  一并闭环。
- 待补（**1–3 为 M1 发布前阻塞条件**，Codex R1#2/#3）：
  1. in-flight dedupe（singleflight，R2#1 状态机明确如下）：in-flight registry
     每个 cache_key 至多一个 entry `(future, deadline)`——
     - 无 entry → 发起 fetch，登记 entry（deadline = now + 6.5s 全局预算）；
     - entry 在飞且 now < deadline → **join 同一 future**（并发构建共享结果，
       这是 singleflight 的唯一 join 窗口）；
     - entry 在飞且 now ≥ deadline → **不 join**：本次构建立即按
       fresh→stale→negative cache 顺序服务；仅当进程级 semaphore 有余量时
       发起替代 fetch 并登记为新 entry（旧 entry 被替换即孤儿化：其 future
       settle 时不写任何缓存、直接丢弃——"旧 future 不被复用"仅指此态）；
     - entry settle → **compare-and-remove**：仅当 registry 当前仍指向该 entry
       （identity/generation 匹配）才移除；被替换的 orphan future 晚完成时
       不写缓存、不删除/不修改 registry 中的替代 entry（R3#1 竞态闭合）。
     进程级 semaphore 封顶并发 fetch 数，连续刷新不再每次新建独立 executor
     累积 late workers。测试按四态分别锁定行为。
  2. negative cache / circuit breaker：连续失败的 cache_key 短窗口负缓存
     （如 30s）；429 尊重 Retry-After 设置负缓存时长（封顶 120s）；
     401/403 语义细化为 `status=unavailable` + auth reason（M0 为 error，
     已满足"不被 stale 掩盖"底线）。
  3. **worker 生命周期 wall-clock 闭环（父验收 A2 强化）**：httpx phase
     timeout 不等于 wall-clock；Codex native resolver refresh 可达 20s+认证
     锁。M1 必须把 in-flight 生命周期、Codex refresh 长调用、进程级并发上限
     与 singleflight + process-wide semaphore + negative cache + worker
     bounded stress 一起闭环：连续多轮 deadline 压测下活跃 worker 数有界、
     旧 future 不被新请求复用且最终释放（含 native refresh 路径）。

### 测试矩阵（M1）

M0 已在库：cache identity 轮换隔离（含旧 persisted fingerprint）、profile
隔离、endpoint 等价类、late worker 不污染、>4 jobs 并发上限、错误分类
（timeout/connect/5xx→stale 带 `fetched_at`；401/403/429→error 不读 stale）、
逐 provider timeout 预算、statusbar 迁移 6 项（精确匹配旧默认才剥离 /
自定义集合保留 / 幂等 / 损坏 JSON）。

M1 待补：

- 稳定 display_name / is_current 的确定性（打乱输入顺序、cooldown 状态不变名）。
- 401/403 → `unavailable` + auth reason 的语义细化（M0 为 error）。
- in-flight dedupe：N 次并发 build 只触发 1 次 provider 调用。
- singleflight 竞态（R3#1）：旧 future 在替代 entry 登记后才完成 → 旧结果
  被丢弃、替代 entry 仍可被 join、registry 不被误删（generation 比对）。
- negative cache：失败后 30s 内不再发起请求；429 Retry-After 窗口被尊重。
- worker 有界性：连续 K 轮 deadline 超时后活跃 worker 数 ≤ 进程上限，旧
  future 最终释放（R1#2）。

## M2 — Command Center 整合 + 维度恢复（下下轮）

- Command Center 新增 `Usage` 区：顶部 Account Limits（复用
  ContextUsagePanel 的数据源，非状态栏 640px 巨面板）。
- 状态栏保持快捷摘要（单行 + popover）。
- 状态栏整体关闭时：Command Palette（`usage` 关键词）与 Command Center 均可达。
- 逐步恢复 Provider/Model/Task 维度：数据源为 local analytics（与 provider
  quota 分开展示，不混算）；每维度独立小节，独立 loading/error。
- 可达性：popover 可键盘打开/关闭（Esc 返还焦点）、焦点环可见、
  reduced-motion 下无位移动画、live region 播报 stale/error 状态变化。

## M3 — provider 补全 + E2E

- Nous credits 余额（若 portal 暴露接口；否则独立 local estimate 标注）。
- xAI：无官方接口 → independent local estimate，明确不标 official。
- E2E：transient stale 展示、in-flight dedupe、light/dark、窄/常规视口、
  离线、键盘全流程、reduced-motion。

## 发布/回滚（Codex R1#5 / R2#2 / R3#2 修订）

### 发布门禁清单（唯一权威，R3#2；全部为阻塞条件，无例外）

1. M1 可靠性面 1–3（singleflight、negative cache/Retry-After、worker 有界性
   压测）实现完成。~~provider timeout 收敛、基础错误分类器~~（M0 已闭环）。
2. ~~credential 域分隔前缀 + env 轮换隔离测试~~（M0 已闭环，R1#1/R2#3）。
3. ~~profile 隔离测试~~（M0 已闭环）。
4. worker 有界性压测通过（R1#2）；~~late-worker 不写缓存~~（M0 已闭环）；
   不误删 registry（随 singleflight 落地）。
5. M1 错误分类增量：401/403→unavailable 细化、429→Retry-After 负缓存
   （M0 已锁定 stale/非 stale 边界）。
6. 双向兼容测试通过：新客户端连旧后端（缺字段降级 M0 展示）、旧客户端连
   新后端（忽略未知字段）。
7. 回滚包制品校验完成：SHA-256 清单、构建 commit、版本号、配置兼容范围
   落盘到回滚包旁 `MANIFEST.md`。
8. 一次按既定顺序的恢复演练完成，证据记录进本目录 `history/`。

任一缺失不得发布，维持 M0 已固化版本运行。
- 兼容性证据：contract v1 全部为可选 additive 字段，但须补双向兼容测试——
  新客户端连旧后端（缺字段时 UI 降级为 M0 展示）、旧客户端连新后端（未知
  字段被忽略）。两者通过才允许"滚动发布"表述成立。
- 回滚包登记：`hermes/cache/release-backups/release-backup-usage-20260810-114009/`
  （来源：2026-08-10 11:40 release 构建的 win-unpacked 备份）；SHA-256 清单、
  构建 commit、版本号与配置兼容范围写入包旁 `MANIFEST.md`（发布阻塞，见上）。
- 回滚顺序：先回滚 Desktop（UI 依赖 contract 字段），再回滚后端；每步后跟
  smoke：① 后端 `usage.accounts` 契约测试；② Desktop vitest focused 两件；
  ③ 手动打开 statusbar popover 确认渲染。
- 后端/前端 commit 可独立 revert（后端缓存/并发层失败时 revert 回串行无缓存
  版）。
- statusbar 迁移修正（R1#4，✅ M0 已落地）：一次性迁移收窄为"存储值与已知
  旧默认集合精确匹配才剥离 context-usage"；用户自定义集合原样保留；6 项迁移
  单测在库。已知残余风险：恰好逐条复现旧默认集合的显式隐藏会被覆盖一次
  （无法区分，已如实记录）。回滚不尝试恢复被剥离的隐藏偏好（不可区分）。

## 风险登记

| 风险 | 缓解 |
|---|---|
| behind 29 的上游同步冲突跨模块 | merge-tree 探测零冲突；实际 merge 因 live-checkout guard 推迟 |
| cache identity 泄漏凭证 | 无明文 secret；含 secret 派生的域分隔哈希——禁止日志化/外传 cache key |
| cache identity 漂移（env 轮换） | R1#1：identity 改用 runtime_token 域分隔哈希 + 轮换隔离测试 |
| late worker 污染状态/累积 | worker 纯化 + singleflight + 进程级并发上限 + 有界性压测 |
| stale 掩盖认证失效 | 错误分类器：401/403 永不回退 stale（R1#3 测试锁定） |
| Kimi/Codex 端点变更 | snapshot 解析失败 → error status，不崩 contract |
