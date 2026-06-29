# UltraStudio 多租户基础架构总纲

状态：基础架构总纲
日期：2026-06-12
范围：UltraStudio 作为多租户 creative agent 产品，Hermes 只作为 agent runtime 基座。

## 1. 一页结论

UltraStudio 的定位是多租户 creative agent workspace。Hermes 提供 agent runtime 基座，视频生成只是其中一类工作流：

```text
用户登录
  -> 进入 tenant / workspace / project
  -> 打开真实 agent chat
  -> 上传素材并入库为 typed asset
  -> agent 通过 workflow router 选择 skill / tool
  -> 创建 durable media job
  -> TokenRouter 做凭证、配额、ACL、模型策略检查
  -> worker / Atlas 执行
  -> 输出注册为 asset
  -> UI 通过 event stream 展示状态和结果
  -> audit / usage / lineage 可追踪
```

核心边界：

- Hermes 负责 agent loop、skills、tool calling、gateway/TUI/web chat 基础能力、模型/provider 抽象。
- UltraStudio 负责 tenant/workspace/project、资产库、媒体 job、TokenRouter、权限/配额/审计、sandbox、产品 UI。
- 多租户状态不能藏在 prompt、前端 local state、Hermes 本地 session 目录或 provider 返回里，必须由 UltraStudio 的服务和数据库拥有。
- 媒体生成必须是真实 job 闭环。不能用 fake job、自动开跑、硬编码视频 brief 或前端伪状态来证明系统成立。

## 2. 当前证据

本总纲收敛下列已有材料：

| 材料 | 可采用结论 | 注意事项 |
|---|---|---|
| `docs/ultra-studio-product-specs/` | UltraStudio 是产品表面、agent runtime、media job、asset library、memory、marketplace、skill runtime 的组合。 | 产品规格不是运行时代码。 |
| `docs/ultra-studio-infra-design/` | 需要 control plane、execution plane、data plane、security/ops 分层。 | 黄色状态表示设计存在，不代表已 wired。 |
| `docs/hermes-web-mvp-prd.md` | MVP 要跑通可验证、可隔离、可审计的多租户 agent 纵向闭环。 | 不能把 cloud 多租户能力误认为本地 Hermes 已有能力。 |
| `docs/hermes-real-chat-agent-ui.md` | `/chat` 应是真实 Hermes gateway client，不是视频 demo surface。 | 不自动生成视频，不造假 job。 |
| `docs/hermes-tokenrouter-credential-flow.md` | sandbox 只拿短期 scoped token，provider key 由 TokenRouter + vault 持有。 | cloud mode 不允许 fallback 到本地 provider key。 |
| `docs/hermes-asset-library-backend-design.md` | asset service 拥有资产、ACL、lineage、collection、smart group、reference。 | object storage 不拥有产品语义。 |

## 3. 目标和非目标

### 3.1 目标

P0 只证明一件事：UltraStudio 可以安全地跑通一条真实 creative agent 生成链路。

必须满足：

- 用户身份和 project 边界来自服务端，不信任前端传入的 `tenant_id`。
- 上传素材变成 `media_input` asset，不作为裸文件路径塞给模型。
- Agent 能基于真实 session/run/tool call 创建 media job。
- Job 使用 structured asset refs，不靠 prompt 中裸写 asset id。
- TokenRouter 在 provider 调用前检查 token、quota、model allowlist、asset ACL、status、revocation。
- Provider key 只在服务端 TokenRouter/vault 边界内出现。
- 输出注册成 asset，并记录 lineage、usage、audit。
- UI 可以在刷新后恢复 session、job、asset 状态。

### 3.2 非目标

P0 不做：

- 完整云平台、计费系统、复杂套餐商业化。
- 多 region GPU 调度、复杂 batch render farm。
- 完整 CometAPI 媒体数据平面。
- Soul ID 底层训练承诺。
- Higgsfield 内部 API 名称复刻。
- 普通 Docker 作为 hostile multi-tenant 安全边界。
- fake job runner、hardcoded demo prompt、自动开跑生成。

## 4. 选定架构形状

项目形状：agent/workflow system + service framework + event log/projection。

主状态模型：event log + durable projections。

原因：

- Agent 和媒体任务都是长周期过程，必须能断线重连、失败追踪、重试和审计。
- UI 不应该直接拥有 job 状态；UI 订阅事件并读取 projection。
- Provider 返回不是产品状态源。Provider job status 需要被归一化为 UltraStudio 的 `media_job` 和 `asset` 状态。
- 多租户安全不是 prompt 规则，必须在服务边界和数据库边界执行。

## 5. Hermes 与 UltraStudio 职责分工

| 领域 | Hermes 基座 | UltraStudio 产品层 |
|---|---|---|
| 对话循环 | AIAgent、prompt、tool dispatch、model adapter。 | session/run/tool_call 产品记录，workspace/project 绑定。 |
| Skills | SKILL.md 加载、slash command、tool instruction。 | workflow router、skill profile、付费/权限/asset-aware execution。 |
| 工具 | terminal、file、browser、provider abstraction。 | tenant-scoped tools、asset refs、media job tools、approval policy。 |
| Gateway | 本地/消息平台 gateway、TUI/web 基础事件。 | 多租户 edge ingress、session auth、event replay、UI projection。 |
| Provider | 模型和工具 provider 抽象。 | TokenRouter、quota、vault credential exchange、usage ledger。 |
| 文件 | 本地工作目录、Hermes home、task files。 | object storage、workspace volume、asset promotion、ACL。 |
| 记忆 | Hermes memory / context。 | tenant/project-scoped memory policy、marketplace、asset provenance。 |

原则：

- 不把 UltraStudio 多租户状态塞进 Hermes core。
- 不为了一个产品 workflow 增加新的 Hermes core model tool。
- UltraStudio capability 优先做成产品服务、gateway API、skill/tool adapter 或插件。
- Hermes 原有本地便利路径在 cloud mode 必须经过 policy gate。

## 6. 多租户实体模型

最小实体：

```text
Tenant
  Workspace
    Project
      Session
        Run
          ToolCall
          MediaJob
      Asset
      Collection
      SmartGroup
      MemoryScope
      TaskFileRoot
```

关键实体字段：

| 实体 | 必备字段 | 说明 |
|---|---|---|
| `tenants` | `id`, `name`, `status` | 计费、安全和隔离顶层边界。 |
| `users` | `id`, `oidc_sub`, `email`, `status` | 身份来自 IdP，授权仍看 membership。 |
| `workspace_memberships` | `tenant_id`, `workspace_id`, `user_id`, `role` | 工作区权限真相。 |
| `projects` | `tenant_id`, `workspace_id`, `id`, `name`, `status` | 文件、资产、记忆、job 的默认边界。 |
| `sessions` | `tenant_id`, `workspace_id`, `project_id`, `id`, `created_by`, `status` | Chat 会话边界。 |
| `runs` | `session_id`, `id`, `client_message_id`, `status`, `model`, `created_at` | 每次用户提交形成一个 run。 |
| `tool_calls` | `run_id`, `id`, `tool_name`, `status`, `args_ref`, `result_ref`, `error_code` | 审计和重放边界。 |
| `assets` | `tenant_id`, `workspace_id`, `project_id`, `id`, `type`, `status`, `object_key` | 产品级媒体对象。 |
| `asset_acl` | `asset_id`, `subject_type`, `subject_id`, `permission` | asset id 不是权限凭证。 |
| `media_jobs` | `run_id`, `tool_call_id`, `id`, `provider`, `model`, `status`, `output_asset_id` | 生成任务强状态。 |
| `usage_events` | `tenant_id`, `workspace_id`, `project_id`, `run_id`, `job_id`, `credits_delta` | 配额和账务依据。 |
| `audit_events` | `actor`, `action`, `resource`, `decision`, `reason`, `request_id` | 安全审计依据。 |

规则：

- 所有 tenant-scoped 表必须带 `tenant_id`，并以 Postgres RLS 作为第二道防线。
- `project_id` 是文件、asset、job、memory 的默认边界。
- `asset_id`、`job_id`、`session_id` 都不是授权凭证，每次访问必须查 membership 和 ACL。
- 跨 project 复用 asset 必须显式授权或复制 lineage。

## 7. 服务边界

### 7.1 Edge / Session Service

职责：

- 认证浏览器请求。
- 绑定 tenant/workspace/project。
- 创建/恢复 session。
- 接收 prompt、slash command、upload admission。
- 维护 event stream auth 和 replay cursor。

不负责：

- 不理解 prompt。
- 不持有 provider key。
- 不直接改 asset lineage。
- 不执行长任务。

P0 API：

```text
POST /api/sessions
POST /api/sessions/{session_id}/resume
POST /api/sessions/{session_id}/prompt
GET  /api/sessions/{session_id}/events
POST /api/uploads
GET  /api/jobs/{job_id}
GET  /api/assets/{asset_id}
```

### 7.2 Agent Runtime / Workflow Router

职责：

- 把用户请求变成 intent、missing_fields、asset_roles、tool_plan。
- 选择 skill/workflow。
- 调用 asset-aware tools。
- 在需要审批、缺字段、资产未 ready 时暂停。

输出结构：

```yaml
WorkflowPlan:
  intent:
  workflow_skill:
  missing_fields:
  asset_roles:
  provider_constraints:
  approval_requirements:
  tool_plan:
```

规则：

- 上传文件不能自动触发生成。必须有用户意图和 tool plan。
- 如果模型能力、素材类型或预算不满足，返回 typed error 或 structured question。
- Agent 最终回复不能声称完成，除非有 job、asset、artifact 或明确失败证据。

### 7.3 Asset Service

职责：

- 拥有资产、ACL、lineage、collection、smart group、character、element、soul_id。
- 把上传和生成输出注册成 typed asset。
- 提供 picker、mention resolve、search、signed preview/download。

资产类型：

```text
media_input
image_job
video_job
audio_job
element
character
soul_id
task_file
```

规则：

- Object storage 只保存二进制，不拥有产品语义。
- `task_file` 不是自动产品资产，必须显式 promote/register。
- mention/picker 只返回当前用户有 `read` 或 `use` 权限的 asset。
- revoked/deleted/not_ready asset 不能进入 media job。

### 7.4 Media Job Service

职责：

- 创建 durable job。
- 驱动 provider submit/status/download/finalize。
- 归一化 provider 状态。
- 把成功输出注册回 Asset Service。
- 记录 provider_job_id、tokenrouter_decision_id、usage_event_id。

Job envelope：

```yaml
MediaJob:
  job_id:
  tenant_id:
  workspace_id:
  project_id:
  session_id:
  run_id:
  tool_call_id:
  provider:
  model:
  media_type:
  mode:
  status:
  input_assets:
  prompt:
  provider_constraints:
  tokenrouter_decision_id:
  output_assets:
  error:
```

规则：

- Provider API 不暴露给 agent。
- Provider raw payload 只作为受控 debug/audit 材料保存。
- Job 失败必须保留 inspectable failure reason。

### 7.5 TokenRouter

职责：

- 验证 scoped Hermes token。
- 检查 tenant/workspace/project/session/tool scopes。
- 检查 quota、concurrency、model allowlist。
- 检查所有 input asset 的 ACL、status、revocation。
- 从 vault 读取 provider credential 并代理 provider 调用。
- 写 usage event 和 sanitized audit event。

失败策略：

| 失败 | 行为 |
|---|---|
| token 缺失、过期、scope 不足 | 401/403，不调用 provider。 |
| quota store 不可用 | generation fail closed。 |
| vault 不可用 | provider unavailable，不 fallback 到 sandbox key。 |
| model 不在 allowlist | `unsupported_model_capability` 或 `model_not_allowed`。 |
| asset 越权 | `asset_access_denied`。 |
| asset 未 ready | `asset_not_ready`。 |
| audit 写失败 | 高风险调用 fail closed。 |

### 7.6 Sandbox / Workspace Volume

职责：

- 为 session/run 提供受控执行环境。
- 挂载授权 project/session 文件集。
- 提供短期 scoped token。
- 限制网络、进程、文件、环境变量。

规则：

- cloud mode 下 sandbox 不能持有 provider key。
- sandbox 不能访问别的 project volume。
- sandbox 不能直接访问 Atlas/provider API。
- sandbox 不能直接提交 Kubernetes/GPU job。
- 本地 dev 可以用 mock provider，但必须显式标记为 dev mode。

P0 可以先用受限本地/容器闭环验证产品语义，但不能把普通 Docker 宣称为 hostile multi-tenant 隔离。

### 7.7 Event / Projection / Audit

职责：

- 事件流负责 UI 实时性和重连恢复。
- Projection 负责 UI 查询。
- Audit 负责安全和事后追踪。

必须事件：

```text
message.start
message.delta
message.complete
status.update
tool.start
tool.progress
tool.complete
tool.error
media_job.created
media_job.updated
asset.ready
approval.requested
approval.resolved
audit.recorded
```

规则：

- UI 状态来自事件和 projection，不来自 worker log。
- Worker log 可观察，但不是产品状态源。
- 每个 failed job 必须能从 `job_id` 追到 `run_id`、`tool_call_id`、TokenRouter decision、worker log、usage event。

## 8. P0 最小纵向闭环

P0 只做一条链路：

```text
真实用户
  -> 真实 workspace/project
  -> 真实 session
  -> 上传 1 个图片或视频素材
  -> asset status ready
  -> prompt.submit
  -> workflow router 生成 tool plan
  -> ultra_media_job_create
  -> TokenRouter policy check
  -> mock provider 或显式 live Atlas
  -> media_job succeeded/failed
  -> output asset ready 或 failed inspectable
  -> UI event stream 可恢复
```

P0 推荐工作包：

| 顺序 | 工作包 | Done-when |
|---:|---|---|
| 1 | Tenant/workspace/project/session 数据骨架 | 前端改 URL/payload 不能访问别的 project session。 |
| 2 | Real chat session 接入 | `/chat` 能 create/resume/prompt.submit，并流式显示真实事件。 |
| 3 | Upload to asset | 上传后得到 `media_input` asset，其他 tenant 无法读取。 |
| 4 | Asset picker / structured refs | Agent/tool 接收 `asset_id` refs，不接收裸本地路径。 |
| 5 | Media job envelope | 创建 job 记录，状态可查，失败可 inspect。 |
| 6 | TokenRouter MVP | expired token、越权 asset、禁用 model、quota 缺失全部 fail closed。 |
| 7 | Provider adapter | 默认 mock provider；live Atlas 只能显式 opt-in，`ATLAS_API_KEY` 只在服务端。 |
| 8 | Output registration | 成功输出注册成 asset，并记录 lineage。 |
| 9 | Audit/usage trace | `job_id` 能查到 run、tool call、policy decision、worker、usage。 |

## 9. P1 / P2 路线

### P1

- Approval Gateway：高风险操作、付费超预算、外部发布、删除资产都要 durable approval。
- Better Workflow Router：支持 missing fields、asset role classification、provider constraints。
- Asset Library 完整搜索：collection、smart group、thumbnail、embedding index。
- Project Files：task files、promotion、download、preview、lineage。
- Worker reliability：retry、cancel、timeout、idempotency key。
- Observability：trace id 串起 session、run、tool_call、job、provider、asset。

### P2

- microVM/Kata sandbox control plane。
- Temporal durable orchestration。
- NATS JetStream 或等价 event replay。
- CometAPI 媒体数据平面。
- 多 provider media gateway。
- 多 region / GPU pool / Kueue 调度。
- per-tenant encryption context。
- marketplace 和 skill runtime 付费/权限治理。

## 10. 验收矩阵

| 类别 | 验收 |
|---|---|
| 身份隔离 | Tenant A 用户不能通过改 URL、payload、asset id 访问 Tenant B 的 session、asset、job。 |
| 数据隔离 | 所有 tenant-scoped 表有 `tenant_id`，RLS 策略开启并有负例测试。 |
| 文件隔离 | 上传 object key 带 tenant/workspace/project 分区，但授权不依赖 object key。 |
| 凭证隔离 | sandbox env、mounted files、prompt、tool result 中搜索不到 provider key。 |
| TokenRouter | expired token、model deny、quota missing、asset revoked 都在 provider 调用前拒绝。 |
| Job 可恢复 | 浏览器刷新后仍能看到 active job 和最终状态。 |
| 失败可见 | Provider 失败、上传失败、asset not ready 都有 typed error 和 UI recovery path。 |
| Lineage | output asset 记录 parent asset、job、prompt hash、provider、model、run。 |
| Usage | 每次 provider 调用产生 usage event，失败策略明确。 |
| Audit | 高风险 deny/allow 都写 audit，日志不含 secret。 |

## 11. 实现规则

- 默认 mock provider，live Atlas 必须显式开启。
- `ATLAS_API_KEY` 只能由服务端 TokenRouter/provider adapter 读取，不能发到前端、sandbox、prompt 或 task file。
- 新 API 使用 snake_case；外部边界如果必须 camelCase，在 adapter 层转换。
- 不吞错误。用户可见缺数据、缺结果、失败生成必须返回 typed error。
- 没数据就空状态，不编造 asset/job/provider 结果。
- 不为了 demo 自动创建 job。
- 不把 provider raw URL 当最终视频输出，必须经过 status/finalize 和 asset registration。
- 不让 prompt 中裸写的 asset id 绕过 Asset Service/TokenRouter。
- 不新增 Hermes core tool 来承载 UltraStudio 产品语义。

## 12. 开放问题

P0 前必须定：

- Cloud P0 的身份源：先用本地 dev auth，还是接 OIDC/Keycloak。
- P0 数据库：直接 Postgres + RLS，还是先 SQLite 验证产品语义再迁移。
- Event stream：复用 Hermes gateway event，还是先定义 UltraStudio session event facade。
- Provider 路由：P0 live Atlas 只支持一个模型，还是支持 provider/model allowlist 表。
- Sandbox：P0 是否允许 dev-only Docker/local runner，同时明确不声明为生产多租户安全边界。
- Asset storage：本地 object store、S3-compatible、还是先文件系统模拟 object storage API。

建议默认：

- P0 用 Postgres schema 先定多租户真相，避免 SQLite 迁移时重做权限模型。
- Provider 默认 mock，live Atlas opt-in。
- Event facade 先做薄层，不重写 Hermes agent loop。
- Asset Service 先做强一致 metadata + 本地/S3-compatible object adapter。
- Sandbox P0 做 dev-only runner，生产隔离进入 P2，不提前承诺安全等级。
