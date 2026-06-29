文档路径：docs/ultra-studio-zh/infra-design/08-hermes-fork-isolation-migration.md

# Hermes Fork 隔离与多租户控制面迁移 Spec

状态：迁移执行规格
日期：2026-06-22
适用范围：`/Users/lifcc/Desktop/code/work/infra/her/hermes-agent` 当前 fork 到 Ultra Studio / VideoAgent 产品层的拆分迁移

## 1. 背景与问题

当前 `hermes-agent` fork 不是一个难以拆解的巨大分叉。按正确口径计算，从 `merge-base(upstream/main, HEAD)` 到当前 `HEAD`：

| 指标 | 当前值 |
|---|---:|
| fork 点 | `a2d7f538d49c7cc282c25ebcc803c8349cae9cff` |
| 自己新增 commit | 43 |
| 纯增量 | 430 files, +72813 / -4310 |
| 新增文件 | 423 |
| 修改 upstream 既有文件 | 7 |
| `.md/.html` 文档文件 | 279 |
| 非 docs 代码/配置/测试新增文件 | 53 |
| 当前未提交 `git status --porcelain` 条目 | 68 |

结论：可外置面很大，fork 接入面很窄。升级 Hermes 的长期冲突风险主要集中在 7 个 upstream 既有文件，而不是 430 个文件。

## 2. 目标

本 spec 的目标不是重新设计 tool，而是把 Ultra Studio 的多租户、鉴权、会话隔离和产品运行时从 Hermes fork 里拆出来，让 Hermes 回到可直接更新的 upstream runtime。

目标状态：

- Hermes upstream 保持干净，可通过 `git fetch upstream && git switch --detach upstream/main` 更新。
- Ultra Studio 拥有自己的控制面：Auth、Tenant、Workspace、Project、Session、Asset、MediaJob、Quota、Audit、Lineage。
- Hermes 只作为受控执行器，接收已校验的 run envelope 和有限 scope。
- 模型输出不能决定租户、权限、资产归属、任务归属或凭证路由。
- 所有跨租户资源访问都在 Ultra 服务端 fail closed。
- 当前 fork 里的可用实现被分层回收，而不是被重写或混进 Hermes core。

非目标：

- 不把 Hermes 改造成 Ultra Studio 后端。
- 不把多租户鉴权放到 prompt、skill 或 tool allowlist 里解决。
- 不在 P0 实现完整云平台、市场、企业组织架构或复杂计费。
- 不把未实现的 TokenRouter、Asset Service、MediaJob Service 伪装成已落地能力。

## 3. 当前已实现能力盘点

### 3.1 多租户 / 鉴权 / 会话隔离

这部分是最有价值的回收对象。

| 文件 | 当前职责 | 迁移判断 |
|---|---|---|
| `agent/ultra_security.py` | principal、安全上下文、sandbox lease、tool/API 授权决策 | 抽成 Ultra control-plane policy 模块，Hermes 内只保留调用钩子 |
| `gateway/principal_headers.py` | 解析 `X-Hermes-*` principal headers | 转为 BFF 到 Hermes 的内部签名 envelope，禁止公网客户端直传 |
| `gateway/session_acl.py` | session/response ownership 判断 | 保留逻辑，迁到 Ultra session service 或 shared auth 包 |
| `gateway/session_scope_store.py` | session scope 与 sandbox lease 存储 | 迁到 Ultra DB schema，Hermes 本地只保留 session id 映射 |
| `gateway/session_context.py` | 把 tenant/workspace/project/user/roles 绑定到 agent run | 需要 upstream 接入点，收敛成 thin adapter |
| `gateway/api_agent_runner.py` | API 模式 agent runner，注入 scope 与 sandbox lease | 可变成 Ultra 调 Hermes 的 runner wrapper |
| `gateway/api_server_audit.py` | API 请求和授权决策 audit log | 迁到 Ultra audit service，Hermes 只发事件 |
| `gateway/api_server_*.py` | sessions/runs/chat/SSE/jobs/responses API 拆分 | 评估拆成 Ultra BFF API，Hermes 只暴露最小 agent run API |
| `hermes_cli/middleware.py` | LLM/tool/API execution middleware | upstream hook 候选，否则维持极薄 patch |

### 3.2 Standalone Chat Panel

| 文件 | 当前职责 | 迁移判断 |
|---|---|---|
| `standalone-chat-panel/panelAuth.mjs` | SQLite 本地账号、密码 hash、token session、tenant/workspace/project 字段 | 作为本地 dev auth 原型迁出到产品 repo |
| `standalone-chat-panel/panelBff.mjs` | Bearer 校验、转发 principal headers、WebSocket/SSE 桥接 | 作为 Ultra BFF P0 原型迁出 |
| `standalone-chat-panel/src/*` | 登录、历史、模型选择、面板 UI | 迁到独立 Ultra UI repo，不留在 Hermes |

### 3.3 Ultra Studio Web UI

| 文件 | 当前职责 | 迁移判断 |
|---|---|---|
| `web/src/components/chat/*` | dashboard chat 组件 | 产品 UI，迁出 |
| `web/src/hooks/useGatewayChat.ts` | Hermes gateway chat hook | 产品适配层，迁出或只保留 upstream 通用 hook |
| `web/src/pages/UltraStudioChatPage.tsx` | Ultra Studio 页面 | 产品页面，迁出 |

### 3.4 Atlas 媒体 provider

| 文件 | 当前职责 | 迁移判断 |
|---|---|---|
| `plugins/image_gen/atlas/*` | Atlas 图像生成 provider | 可做独立 Hermes plugin 或 Ultra media service adapter |
| `plugins/video_gen/atlas/*` | Atlas 视频生成 provider | 可做独立 Hermes plugin 或 Ultra media service adapter |

P0 如果只给内部使用，可以先作为外部 plugin 保留。生产多租户版本不应让 Hermes 直接持有全局 Atlas key，应该经 TokenRouter 或 Ultra media service 调用。

### 3.5 Skill 路由

| 文件 | 当前职责 | 迁移判断 |
|---|---|---|
| `hermes_cli/ultra_studio_skills.py` | Ultra / VideoAgent skill allowlist | 产品 preset，迁出为 profile distribution 或 setup 脚本 |
| `hermes_cli/skills_config.py` | skill 配置命令接入 | 不进 Hermes core，改为外部 profile 配置 |
| `hermes_cli/subcommands/skills.py` | `hermes skills video-agent` 接入 | 删除或外置，避免 upstream 冲突 |

## 4. 当前只存在于文档的能力

下列能力在 Ultra 文档中已经定义，但当前 43 个 commit 的代码文件里没有对应服务实现。不得在排期、demo 或验收里标记为已实现。

| 能力 | 当前状态 | P0 处理 |
|---|---|---|
| TokenRouter | PRD/spec-only | P0 可用 mock 或单租户 key adapter，但必须标注非生产 |
| Asset Service | PRD/spec-only | P0 需要最小真实资产表和 object storage adapter |
| MediaJob Service | PRD/spec-only | P0 需要真实 job 表、状态机和失败恢复 |
| Tenant 数据模型 | 部分字段散落于 panel/auth/session scope | 需要 Ultra DB source of truth |
| Quota | spec-only | P0 可只记录 usage，不做计费 |
| Lineage | spec-only | P0 记录输入资产、prompt hash、provider、output asset |
| 运营级 Audit Ledger | 部分 API audit 已有 | P0 先做 append-only audit_events |

## 5. 目标架构

### 5.1 代码仓库边界

建议目标目录：

```text
infra/her/
  hermes-agent-upstream-main/       # 干净 upstream，用于更新和验证
  hermes-agent/                     # 当前 fork，作为迁移来源
  ultra-studio/                     # 新产品仓库
    apps/web/                       # 产品 UI
    apps/bff/                       # Auth / BFF / API gateway
    services/control-plane/         # tenant, workspace, project, session, policy
    services/media/                 # asset, media_job, tokenrouter adapter
    packages/hermes-runner/         # 调 Hermes 的薄封装
    packages/hermes-profile/        # profile distribution / blank slate 配置
    plugins/atlas-media/            # 可选 Hermes plugin
    docs/                           # Ultra 中文/产品/基建文档
```

如果短期不建新 repo，可以先在 `infra/her/ultra-studio/` 建顶层目录，不能继续把产品代码写进 `hermes-agent`。

### 5.2 运行时关系

```text
Browser/UI
  -> Ultra BFF/Auth
  -> Policy + Tenant DB
  -> Create Hermes Run Envelope
  -> Hermes worker/profile
  -> Ultra services: Asset, MediaJob, TokenRouter, Audit
  -> Event stream back to UI
```

Hermes 只接收已经由 Ultra BFF 校验过的 envelope：

```json
{
  "run_id": "run_...",
  "principal": {
    "tenant_id": "tenant_...",
    "workspace_id": "workspace_...",
    "project_id": "project_...",
    "user_id": "user_...",
    "roles": ["creator"]
  },
  "session_id": "sess_...",
  "allowed_capabilities": ["asset.read", "media_job.create"],
  "input": {
    "message": "...",
    "asset_ids": ["asset_..."]
  }
}
```

Hermes 不允许从用户 prompt、前端 body 或模型输出中重新解释 principal。

## 6. Trust Boundary

| 边界 | 信任等级 | 规则 |
|---|---|---|
| Browser/UI | 不可信 | 只能提交用户意图和文件，不能提交可信 `tenant_id` 或 roles |
| Ultra BFF/Auth | 可信入口 | 唯一创建 principal/session/run envelope 的入口 |
| Ultra DB | source of truth | tenant、membership、asset、job、quota、audit 归这里 |
| Hermes worker | 受控执行器 | 可被 prompt injection 影响，不能作为授权源 |
| Model output | 不可信 | 只能作为建议，不能直接决定资源访问 |
| Tool/API/MCP | enforcement point | 每次调用按 principal 和 resource owner 校验 |
| Atlas/外部 provider | 外部依赖 | 只通过 TokenRouter/media service 访问，不暴露全局 key |

关键规则：

- 用户传来的 `tenant_id` 只能作为选择意图，必须由 session membership 重新校验。
- Hermes 传来的 `asset_id/job_id/session_id` 必须二次校验 owner scope。
- 没有 scope 的请求默认拒绝，不做 local fallback。
- 权限失败返回明确错误，并写 audit event。
- 任何跨租户数据缺失不能用空结果静默降级，必须返回 `403` 或 `404 by policy`。

## 7. 最小数据模型

P0 不需要完整企业组织架构，但需要把 source of truth 放在 Ultra DB。

| 表 | 最小字段 | 说明 |
|---|---|---|
| `users` | `id`, `email_or_username`, `password_hash` 或 external id, `created_at` | 本地可先 password auth，未来接 OAuth |
| `tenants` | `id`, `name`, `plan`, `created_at` | tenant 是最高隔离边界 |
| `workspaces` | `id`, `tenant_id`, `name` | workspace 属于 tenant |
| `projects` | `id`, `tenant_id`, `workspace_id`, `name` | project 是多数资源默认 scope |
| `memberships` | `tenant_id`, `workspace_id`, `project_id`, `user_id`, `role` | authorization 输入 |
| `hermes_sessions` | `id`, `tenant_id`, `workspace_id`, `project_id`, `user_id`, `hermes_session_id`, `status` | Ultra session 与 Hermes session 映射 |
| `runs` | `id`, `session_id`, `status`, `input_hash`, `started_at`, `ended_at` | 每次 agent run |
| `assets` | `id`, `tenant_id`, `workspace_id`, `project_id`, `owner_user_id`, `uri`, `mime`, `status` | 所有素材和输出 |
| `media_jobs` | `id`, `tenant_id`, `project_id`, `asset_id`, `provider`, `status`, `error` | 媒体生成状态机 |
| `sandbox_leases` | `id`, `session_id`, `tenant_id`, `status`, `expires_at` | 当前 fork 已有概念，迁到 Ultra |
| `audit_events` | `id`, `tenant_id`, `actor_user_id`, `action`, `resource_type`, `resource_id`, `decision`, `reason`, `ts` | append-only |

最小索引：

- 所有 tenant-scoped 表必须有 `tenant_id`。
- `assets(id, tenant_id)`、`media_jobs(id, tenant_id)`、`hermes_sessions(id, tenant_id)` 必须可快速校验。
- `audit_events(tenant_id, ts)` 用于追查。

## 8. 授权模型

P0 可以从简单 RBAC 起步：

| Role | 权限 |
|---|---|
| `owner` | tenant 内全部管理权限 |
| `admin` | workspace/project 管理、资产和任务管理 |
| `creator` | 创建 session、上传资产、创建 media job、读取自己可见资产 |
| `viewer` | 读取可见 session 和资产，不能创建 job |

每个 API handler 必须执行：

```text
authenticate request
resolve principal from server-side session
load resource by id
verify resource.tenant_id == principal.tenant_id
verify membership grants action
write audit decision
execute or reject
```

禁止模式：

- 从 request body 直接信任 `tenant_id`。
- 从 Hermes prompt 或模型工具参数决定 resource owner。
- 找不到资源时返回全局搜索结果。
- 权限失败时把数据过滤成空数组但不记录原因。

## 9. Hermes 接入点处理

当前真正修改 upstream 既有文件只有 7 个。处理策略如下：

| 文件 | 当前风险 | 目标处理 |
|---|---|---|
| `.gitignore` | 低 | 产品产物迁出后尽量还原 |
| `gateway/platforms/api_server.py` | 高，API server 入口会持续和 upstream 冲突 | 只保留 upstream 原始入口；Ultra BFF 调用 Hermes 标准 API；如缺 hook，提交通用 upstream PR |
| `gateway/session_context.py` | 中，高价值 | 收敛成通用 `run_scope` / `session metadata` 注入点 |
| `hermes_cli/dashboard_auth/routes.py` | 中 | 产品 auth 迁到 Ultra BFF，Hermes dashboard auth 不承载 tenant auth |
| `hermes_cli/middleware.py` | 高价值 | 抽象成通用 execution middleware/hook，避免 Ultra 专名 |
| `tests/gateway/test_api_server_toolset.py` | 低 | 随接入点迁移改到对应测试 |
| `tests/gateway/test_session_env.py` | 低 | 随 `session_context` 改成 upstream 通用测试或迁出 |

目标不是保留 7 个 patch，而是把它们分成三类：

| 类别 | 处理 |
|---|---|
| Hermes 通用能力 | 做 upstream PR，例如 session metadata、request context、execution middleware |
| Ultra 产品能力 | 搬到 Ultra BFF/control-plane |
| 过渡 shim | 短期保留在 fork，必须少于 300 行，有删除计划 |

## 10. 迁移阶段

### Phase 0：冻结与基线

目标：防止 fork 面继续扩大。

动作：

- 冻结 `hermes-agent` 中 Ultra/VideoAgent 新功能写入。
- 保留当前 fork 作为 migration source。
- 建立 clean upstream worktree 作为升级基线。
- 记录当前 43 commits、7 modified upstream files、68 dirty entries。

验收：

```bash
base=$(git merge-base upstream/main HEAD)
git diff --shortstat "$base"..HEAD
git diff --name-status "$base"..HEAD | awk '$1=="M"{print $2}'
git status --short
```

### Phase 1：文档与 UI 迁出

目标：先把最大体积、最低耦合的部分迁出。

迁出：

- `docs/ultra-studio-*`
- `docs/lark-source`
- `docs/notion-source`
- `docs/open-source-architecture`
- `standalone-chat-panel`
- `web/src/pages/UltraStudioChatPage.tsx`
- `web/src/components/chat/*`

验收：

- Ultra 文档可以在新位置 `npm run docs:dev`。
- Standalone panel 可以独立启动。
- Hermes upstream worktree 不需要这些文件也能运行官方测试。

### Phase 2：控制面抽取

目标：把多租户鉴权从 Hermes gateway 内部抽成 Ultra 控制面。

迁出或重写为 Ultra service：

- `principal_headers`
- `session_acl`
- `session_scope_store`
- `api_server_audit`
- `ultra_security`
- `panelAuth`
- `panelBff`

新服务职责：

- 登录和 token session。
- 创建 principal。
- 绑定 tenant/workspace/project/user。
- 创建 run envelope。
- 校验 asset/job/session ownership。
- 写 audit event。

验收：

- 不经过 Ultra BFF，不能创建带 tenant scope 的 Hermes run。
- 手动伪造跨租户 `asset_id` 返回 `403` 或 `404 by policy`。
- audit_events 能看到 allow/deny。

### Phase 3：Hermes worker 接入收敛

目标：Hermes 只留下运行时接入，不拥有业务状态。

处理：

- Hermes profile 使用 Blank Slate 起步。
- 禁止生产 worker 暴露任意 Terminal/File 给用户意图。
- Hermes run 只接收 Ultra BFF 生成的 envelope。
- Hermes 输出通过事件流回传给 Ultra，而不是直接写产品 DB。

验收：

- Hermes profile 删除后，Ultra DB 中的 users/assets/jobs/audit 不丢。
- Hermes 升级后，只需要重跑 worker smoke，不需要 merge 产品 UI 和 docs。
- `gateway/platforms/api_server.py` 里的 Ultra 专名消失或只剩通用 hook。

### Phase 4：Atlas / Media 能力产品化

目标：避免 Hermes 直接持有多租户生产凭证。

短期：

- Atlas provider 可以作为外部 plugin 或 dev adapter。
- P0 使用单租户 key 时必须标记为 dev/prototype。

长期：

- UI 创建 `media_job`。
- Media service 通过 TokenRouter 选择 key/provider。
- Worker/Atlas 返回 output asset。
- Asset service 写入 lineage 和 audit。

验收：

- job 状态刷新后可恢复。
- provider error 能落库并展示。
- output asset 带 `tenant_id/project_id/lineage`。

### Phase 5：消灭 fork 接入点

目标：升级 Hermes 时不再处理产品 diff。

动作：

- 对通用 hook 提 upstream PR。
- 对产品逻辑删除 Hermes patch。
- 对暂时不能上游化的接入点保留最小 patch，并单独列清单。

验收：

```bash
git diff --name-status upstream/main..HEAD
```

结果应只剩：

- 通用 upstream PR 分支。
- 或一份小于 300 行的 Ultra adapter patch。
- 不包含 docs、UI、TokenRouter、Asset、MediaJob、tenant product logic。

## 11. P0 使用链路

P0 用户链路：

```text
login
  -> choose workspace/project
  -> upload asset
  -> create session
  -> create media job request
  -> Ultra BFF creates Hermes run envelope
  -> Hermes worker plans/calls allowed service APIs
  -> media service calls Atlas
  -> output asset saved
  -> UI receives events and shows result
```

关键点：

- 用户使用的是 Ultra Studio，不是裸 Hermes。
- Hermes 不直接暴露给公网用户。
- Hermes 不直接决定 tenant、quota、asset owner。
- 工具调用只是执行动作，每次都要由 Ultra 服务端鉴权。

## 12. 验证矩阵

| 场景 | 预期 |
|---|---|
| A 用户读取 B 用户 session | 拒绝，audit `deny` |
| A tenant 读取 B tenant asset | 拒绝，不能返回空成功 |
| 伪造 `X-Hermes-Tenant-Id` | BFF 忽略或拒绝 |
| Hermes 模型输出另一个 `tenant_id` | 服务端拒绝 |
| Atlas key 缺失 | job failed，错误可见，不能静默降级 |
| 刷新浏览器 | session/job/asset 状态可恢复 |
| Hermes profile 删除 | Ultra DB 业务数据不丢 |
| 升级 Hermes upstream | 产品 docs/UI/control-plane 不参与 merge |

建议测试命令：

```bash
python -m pytest \
  tests/agent/test_ultra_security.py \
  tests/gateway/test_api_server_principal_scope.py \
  tests/gateway/test_api_server_session_acl.py \
  tests/gateway/test_principal_headers.py
```

迁移到新 repo 后，应新增 Ultra 侧测试：

```bash
pytest tests/auth tests/policy tests/sessions tests/assets tests/media_jobs
npm test
npm run build
```

## 13. 决策清单

| 决策 | 结论 |
|---|---|
| 多租户鉴权是否靠 tool 调整解决 | 否。tool 是执行出口，不是 trust boundary |
| Hermes 是否作为产品后端 | 否。Hermes 是受控 agent runtime |
| 当前 fork 是否不可拆 | 否。只有 7 个 upstream 修改点 |
| 当前已实现的核心价值 | session ACL、principal scope、API audit、authenticated panel/BFF 原型 |
| 当前未实现的核心服务 | TokenRouter、Asset Service、MediaJob Service、Quota、Lineage |
| 下一步优先级 | 先迁出文档/UI，再抽控制面，再收敛 7 个 Hermes 接入点 |

## 14. Done-When

本迁移完成的定义：

- `hermes-agent-upstream-main` 可以直接更新到 upstream main。
- Ultra Studio 产品代码不在 Hermes checkout 下继续增长。
- 多租户 source of truth 在 Ultra DB。
- 所有 `asset_id/job_id/session_id` API 都做 tenant ownership 校验。
- Hermes run 只接收 Ultra BFF 生成的 scoped envelope。
- 生产 worker profile 不暴露任意 Terminal/File 给终端用户。
- TokenRouter、Asset、MediaJob、Audit、Lineage 的实现状态在文档和代码中一致，不把 spec-only 当成 done。
- 回归测试覆盖跨租户拒绝、会话 ownership、伪造 principal、provider failure、刷新恢复。

## 15. 立即执行建议

按风险和收益排序：

1. 新建 `infra/her/ultra-studio/`，初始化产品 repo 或顶层目录。
2. 搬迁 `docs/ultra-studio-zh` 和相关 PRD/source archive。
3. 搬迁 `standalone-chat-panel`，保留本地 auth/BFF 原型。
4. 从 fork 里抽出 `principal/session/audit` 逻辑，形成 Ultra control-plane 包。
5. 为 Hermes 定义最小 run envelope，并写跨租户拒绝测试。
6. 把 7 个 upstream 修改点逐个归类：upstream hook、Ultra BFF、过渡 shim。
7. 清理 `hermes_cli/skills video-agent` 这类产品 preset，不继续写进 Hermes core。
8. 每完成一阶段，用 clean upstream worktree 跑官方测试，再跑 Ultra smoke。
