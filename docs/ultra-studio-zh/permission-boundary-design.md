# 权限边界与零信任执行设计

状态：权限与执行 contract
日期：2026-06-15

## 目的

这份文档定义 Ultra Studio 的权限边界。目标是把权限判断从 Prompt、UI、Workflow Router 和 Worker 中剥离，统一收敛到身份、资源、策略、审批和审计边界。

## 核心原则

权限判断不属于 Prompt。
权限判断不属于 UI。
权限判断不属于 Workflow Router。
权限判断不属于 Worker。

权限只来自：

- Gateway / Identity Provider 断言的 Principal。
- Resource Service 的 ACL。
- PolicyChecker / TokenRouter 的 decision。
- Approval Gateway 的 durable decision。

## 信任边界

```text
Browser
  untrusted; can submit intent, never identity authority

Gateway / Session
  trusted to bind authenticated user to session

Workflow Router
  trusted to produce intent, not permission

Prompt Compiler
  trusted to compile validated intent into tool args, not permission

PolicyChecker / TokenRouter
  trusted to authorize capability, quota, model and credential grant

Asset Service
  trusted to authorize asset read/use/update/delete/revoke

Worker
  trusted only to execute authorized job envelope
```

## Current vs Target

当前 Hermes fork 不是完整零信任云架构。

| 能力 | 当前状态 | 目标 |
|---|---|---|
| TokenRouter | specified, not built | 控制面服务，负责 policy、quota、credential、decision log。 |
| CometAPI | future | 媒体数据面，负责帧采样、长视频、多模态预处理。 |
| Asset Service | specified, not built | 资产状态、ACL、lineage、audit 的资源权威源。 |
| PolicyChecker | not formalized | P0 用进程内实现，接口兼容未来 TokenRouter。 |
| Provider Key | 服务端 env / adapter | 不进入浏览器、worker payload、日志或资产上下文。 |
| Approval | partial | 需要 durable approval record 和可恢复 pending 状态。 |

文档中不能把目标能力写成已实现能力。状态必须写清楚：`implemented`、`partial`、`specified, not built` 或 `future`。

## Principal

服务端生成 Principal。前端传来的身份字段不可信。

```json
{
  "user_id": "usr_x",
  "tenant_id": "ten_x",
  "workspace_id": "ws_x",
  "project_id": "proj_x",
  "session_id": "sess_x",
  "roles": ["owner"]
}
```

P0 self-host 可以把 `tenant_id`、`workspace_id`、`project_id` 设为默认常量，但数据模型和 API 仍要保留这些字段，避免后续迁移重写。

## 授权接口

所有高价值工具调用先经过授权。

```ts
type AuthorizeToolCallRequest = {
  request_id: string
  principal: Principal
  session_id: string
  tool_name: string
  action: string
  model?: string
  asset_refs: AssetRef[]
  estimated_cost?: CostEstimate
  requested_scopes: string[]
}

type AuthorizeToolCallResult =
  | {
      status: "allow"
      decision_id: string
      allowed_scopes: string[]
      credential_grant_id?: string
      expires_at: string
    }
  | {
      status: "deny"
      decision_id: string
      error_code:
        | "resource_denied"
        | "quota_exceeded"
        | "scope_denied"
        | "model_denied"
        | "credential_unavailable"
      user_message: string
    }
  | {
      status: "needs_approval"
      decision_id: string
      approval_id: string
      reason: string
    }
  | {
      status: "queued"
      decision_id: string
      reason: "concurrency_limited" | "queued_by_policy"
      position?: number
    }
```

没有 `decision_id` 的 tool call 不进入 worker。

## Asset 权限

asset id 不是权限凭证。

每次使用资产前必须检查：

```text
asset exists
asset.project_id matches principal.project_id
asset.status == ready
asset not revoked
principal has read/use
```

prompt 里出现 asset id 不算授权。只有结构化 `asset_refs[]` 才能进入 preflight。

P0 资源权限最小动词：

| 动词 | 用途 |
|---|---|
| `read` | 浏览、预览、检查详情。 |
| `use` | 作为生成输入或 reference。 |
| `update` | 修改名称、标签、collection。 |
| `delete` | 删除或 tombstone。 |
| `revoke` | 让资产不能再被 mention 或生成使用。 |

## Capability Scope

会话和工具调用必须声明能力范围。

```text
image.generate
video.generate
asset.read
asset.use
asset.download
asset.delete
skill.reference.read
external.publish
```

Workflow Router 可以请求这些 scope，但不能批准这些 scope。批准由 PolicyChecker / TokenRouter 返回。

## Worker Envelope

Worker 只能接受授权后的 job envelope。

```json
{
  "job_id": "job_x",
  "decision_id": "dec_x",
  "principal_scope": {
    "tenant_id": "ten_x",
    "workspace_id": "ws_x",
    "project_id": "proj_x"
  },
  "tool_name": "atlas.video.generate",
  "asset_refs": [],
  "credential_grant_id": "grant_x"
}
```

Worker 禁止：

- 从环境或 payload 中读取 provider key。
- 重新解释 prompt 里的 asset id。
- 执行没有 `decision_id` 的 job。
- 在 ACL、quota、credential 不确定时继续执行。

## 错误模型

不能 silent drop。不能 warning 后继续。

必须返回结构化错误或状态：

| code | 含义 |
|---|---|
| `resource_denied` | 没有资源读或用权限。 |
| `asset_not_ready` | 资产还不能作为输入。 |
| `asset_revoked` | 资产已撤销。 |
| `quota_exceeded` | 额度不足。 |
| `concurrency_limited` | 并发限制命中。 |
| `queued_by_policy` | 被策略排队。 |
| `requires_approval` | 需要人工审批。 |
| `credential_unavailable` | 无法取得 provider 授权。 |
| `model_denied` | 当前用户或计划不能使用该模型。 |
| `scope_denied` | 当前 session 不具备工具能力。 |

Agent 可以不理解内部策略，但必须收到稳定错误码、用户可读说明和可追踪 ID。

## 审批边界

以下动作至少需要明确确认；P1 可升级为团队审批：

- 高成本媒体生成。
- 删除、撤销、批量导出。
- 外部发布。
- 下载受限资产。
- 重试会再次消耗额度的失败任务。

Approval record 必须包含：

```text
approval_id
decision_id
principal
action
target_refs
status: pending | approved | rejected | expired
created_at
decided_at
```

## 审计链

每条生成链必须能串起来：

```text
session_id
  -> run_id
  -> tool_call_id
  -> policy_decision_id
  -> approval_id?
  -> provider_job_id
  -> output_asset_id
  -> usage_event_id
```

P0 可以先写 SQLite 或 JSONL，但字段不能少。审计记录不能包含 provider key、完整内部 prompt、vault 路径或跨租户对象 id。

## P0 最小实现

P0 不需要完整 TokenRouter 服务，但要实现同形接口：

1. `Principal` 从服务端 session 生成。
2. `PolicyChecker` 进程内执行。
3. 所有 media job 创建前调用 `authorize_tool_call`。
4. asset 使用前做 `read/use/status/revoked` 检查。
5. 高风险动作生成 approval record。
6. provider key 只在服务端 adapter 内可见。
7. 每次 tool call 写 decision log。
8. UI 显示结构化错误，不展示假成功。

## 反模式

| 反模式 | 为什么禁止 |
|---|---|
| UI 判断最终权限 | 前端可被篡改，只能展示后端允许的动作。 |
| Workflow Router 判定权限 | Router 应只管意图和字段，不管授权。 |
| Prompt 文本携带 asset id | 容易被 prompt injection 绕过。 |
| Worker 直接拿 provider key | 破坏零信任边界，日志和 payload 有泄露风险。 |
| 失败时 warning 后继续 | 会造成越权、假成功和审计断链。 |
| capability 只靠工具可见性 | 动态工具集不是加密绑定的授权。 |

## 验收标准

- 未授权 asset id 在 prompt 中出现不会被使用。
- 没有 `decision_id` 的 job 不会进入 worker。
- 缺少 quota、credential 或 ACL 时 fail closed。
- 高风险动作刷新页面后仍能看到 pending / expired 状态。
- failed job 可以追到 policy decision 和用户可读错误码。
- provider key 不出现在前端 bundle、日志、job payload、asset context。
