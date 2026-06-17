# 权限、Sandbox 与审计实施记录

状态：P0 实施记录 + P1/P2 设计边界  
日期：2026-06-17  
范围：Ultra Studio Web Panel、Hermes API Server、agent tool policy、session/sandbox/audit 链路。

## 1. 目标

这份文档记录当前权限和执行隔离的真实实现，不把目标架构写成已完成能力。

P0 要证明：

```text
Login
  -> BFF 解析真实用户
  -> 注入 Principal
  -> API Session 绑定 Principal Scope
  -> Agent Turn 绑定 Principal + Sandbox Lease
  -> Tool Call 经过 PolicyChecker
  -> DecisionLog 记录 allow/deny
  -> Session/History 按用户隔离
```

## 2. 当前已实现

| 能力 | 状态 | 当前代码 |
|---|---|---|
| 本地登录 | implemented | `standalone-chat-panel/panelAuth.mjs` |
| 浏览器不传 principal | implemented | `standalone-chat-panel/panelBff.mjs` |
| BFF 注入 `X-Hermes-*` | implemented | `panelAuth.mjs`, `panelBff.mjs` |
| API session ACL | implemented | `gateway/api_server_sessions.py`, `gateway/session_scope_store.py` |
| ContextVar principal | implemented | `gateway/session_context.py`, `agent/ultra_security.py` |
| ContextVar sandbox lease | partial | `gateway/session_context.py`, `agent/ultra_security.py` |
| Tool policy gate | implemented | `hermes_cli/middleware.py`, `agent/ultra_security.py` |
| Decision JSONL | implemented | `agent/ultra_security.py` |
| API request audit log | partial | `gateway/api_server_audit.py` |
| Persistent sandbox lease | in progress | 本文后续实现 |
| durable approval record | not built | 只有现有 approval/clarify runtime |
| Asset ACL | not built in Hermes | 资产服务在外部/后续接入 |
| TokenRouter | specified, not built | P0 用进程内 PolicyChecker |

## 3. 信任边界

浏览器是不可信边界。浏览器只持有 BFF 颁发的不透明 token。

BFF 是当前 P0 的 identity boundary：

- 校验 token。
- 从 SQLite 用户表读取 `tenant_id/workspace_id/project_id/user_id/roles`。
- 向 API Server 注入 principal headers。
- 不允许浏览器直接提交 principal header。

API Server 是 session/resource boundary：

- 创建 session 时绑定 principal scope。
- list/get/messages/resume/chat/fork/delete 都必须按 scope 过滤或拒绝。
- 直接猜 `session_id` 不能绕过 ACL。

Agent Runtime 是 execution boundary：

- 每个 turn 使用 `ContextVar` 绑定 `Principal` 和 `SandboxLease`。
- Workflow Router 可以决定要调用什么工具，但不能批准权限。
- 工具执行前统一走 `PolicyChecker`。

## 4. Principal Contract

P0 Principal：

```json
{
  "tenant_id": "tenant-main",
  "workspace_id": "workspace-main",
  "project_id": "project-default",
  "user_id": "user-lif",
  "roles": ["owner", "creator", "member"],
  "session_id": "api_..."
}
```

规则：

- Principal 只能由 BFF/API Server 生成。
- 前端 localStorage 不是权限来源。
- prompt 中出现用户、workspace、asset 或 session id 不代表授权。
- 缺少 principal 的 server request 只能走 legacy/local fallback；多用户入口必须带完整 principal。

## 5. Sandbox Lease Contract

P0 Sandbox Lease：

```json
{
  "sandbox_id": "sbx_...",
  "tenant_id": "tenant-main",
  "workspace_id": "workspace-main",
  "project_id": "project-default",
  "session_id": "api_...",
  "owner_user_id": "user-lif",
  "status": "active",
  "expires_at": 1781710000.0
}
```

规则：

- `terminal` 和 `execute_code` 必须有 active lease。
- lease 必须匹配当前 principal 和 session。
- lease 过期、禁用或不匹配时 fail closed。
- local CLI fallback 可以继续工作；Web/API 多用户路径不能依赖 fallback。
- P0 lease 存在 `SessionDB` sidecar table，P1 再接真正 sandbox service。

## 6. Tool Policy

工具执行前统一走：

```text
tool_name + args + principal + sandbox_lease
  -> PolicyChecker.authorize()
  -> PolicyDecision
  -> DecisionLog JSONL
  -> allow 或 structured policy_denied
```

角色规则：

| action | roles |
|---|---|
| `media.generate` | `owner`, `admin`, `member`, `creator` |
| `sandbox.execute` | `owner`, `admin`, `member` + active lease |
| `file.write` | `owner`, `admin`, `member` |
| `browser.use` | `owner`, `admin`, `member` |

## 7. Audit Chain

P0 已有两类审计：

- API request audit：结构化 logger，含 request id、principal fingerprint、action/result/status。
- Tool decision log：`~/.hermes/logs/security_decisions.jsonl`，含 `decision_id/session_id/tool_call_id/action/result/sandbox_id`。

P0 目标链路：

```text
request_id
  -> session_id
  -> tool_call_id
  -> decision_id
  -> sandbox_id
```

P1 需要升级为 durable audit table：

```text
audit_events(
  audit_id,
  tenant_id,
  workspace_id,
  project_id,
  user_id,
  session_id,
  run_id,
  tool_call_id,
  decision_id,
  resource_type,
  resource_id,
  action,
  result,
  reason,
  created_at
)
```

## 8. P0 实施步骤

1. 已完成：SQLite 登录、BFF principal 注入。
2. 已完成：API session scope sidecar 和 session ACL。
3. 已完成：ContextVar principal/lease 绑定。
4. 已完成：PolicyChecker + DecisionLog。
5. 本轮实现：persistent sandbox lease sidecar。
6. 下一步：多用户 E2E 脚本和 asset/media job ACL 接入。

## 9. 验收标准

必须满足：

- Alice 和 Bob 登录后 session list 不互相可见。
- Bob 直接请求 Alice session messages 返回 404/403。
- Web/API principal 下的 `terminal` 没有 matching lease 时被拒绝。
- Web/API principal 下的 `terminal` 有 matching active lease 时允许进入工具调度。
- 每个 tool decision 写入 JSONL，且不记录 secret 参数值，只记录 arg keys。
- `video_generate` 和 `image_generate` 作为 `media.generate` 允许 creator 使用。

## 10. 非目标

P0 不做：

- 真正云 sandbox 池。
- Kubernetes/Firecracker 隔离。
- Vault/OPA/Redis TokenRouter。
- Asset Service ACL。
- Provider key delegation。
- 复杂 org admin 后台。

这些属于 P1/P2，但当前接口必须给它们留位置。
