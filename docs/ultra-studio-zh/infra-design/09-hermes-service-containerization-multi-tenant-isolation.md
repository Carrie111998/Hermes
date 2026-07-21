文档路径：docs/ultra-studio-zh/infra-design/09-hermes-service-containerization-multi-tenant-isolation.md

# Hermes 服务化、容器化与多用户隔离

状态：生产架构执行规格  
日期：2026-07-15  
适用范围：把当前单租户、个人 Agent 形态的 Hermes 演进为 Ultra Studio 可托管的多用户服务

## 1. 结论

推荐采用“控制面常驻、执行面按需创建”的结构：

- Hermes 作为常驻 Agent 服务，负责对话、模型调用、session、审批和工具路由。
- 普通聊天不创建 sandbox。
- 第一次调用 `terminal`、`execute_code`、文件读写等执行类工具时，才创建或唤醒 session sandbox。
- Hermes 不直接持有 Docker socket 或 Kubernetes 管理权限，只调用受限的 Sandbox Manager API。
- 第一阶段按 tenant/workspace 部署 Hermes Cell；状态外移完成后，再演进为共享无状态 Worker Pool。
- 每个 session 默认独占物理 sandbox；同一 session 的子 Agent 可以显式共享，但不同 tenant、project 或 session 禁止共享。

这不是新增第三套架构，而是补全当前仓库已经出现的 principal、session scope、sandbox lease、多后端 environment 和 TokenRouter 边界。

## 2. 目标与非目标

### 目标

- Hermes 服务启动时不创建执行沙箱。
- 多用户请求可安全映射到 tenant、workspace、project、user 和 session。
- 所有不可信 shell、代码和文件修改都在独立 sandbox 中发生。
- Hermes、sandbox、provider credential 和用户文件之间有可验证的边界。
- sandbox 创建失败时明确失败，不回退到宿主机执行。
- 支持从每 tenant Cell 平滑迁移到共享 Worker Pool。

### 非目标

- 不把 Hermes 本身当作用户代码沙箱。
- 不让 Prompt、模型输出或浏览器直接声明可信身份和角色。
- 不把统一 API key 当作完整的多租户认证系统。
- 不在 sandbox 内保存静态 provider key。
- 不允许未审核的第三方 Plugin 直接进入共享 Hermes Python 进程。

## 3. 当前仓库事实

| 领域 | 当前事实 | 生产判断 |
|---|---|---|
| 镜像 | `Dockerfile` 把代码放在 `/opt/hermes`，把可写状态集中到 `/opt/data` | 可作为服务镜像基础 |
| Compose | gateway/dashboard 使用 `network_mode: host`，并共享 `~/.hermes:/opt/data` | 适合个人部署，不适合 SaaS 多租户 |
| 运行用户 | 镜像由 root 启动 s6 初始化，再降权到 `hermes` 用户 | 生产版应改为 initContainer 初始化 PVC，主容器从启动即非 root |
| 本地状态 | `hermes_state.py` 默认使用一个 `/opt/data/state.db` | 可用于单 Cell，不适合多个副本共享写入 |
| 身份 | `X-Hermes-Tenant-Id` 等 header 可把 principal scope 注入 Agent turn | 只是内部桥接，不能信任公网客户端直传 |
| 授权 | `agent/ultra_security.py` 已有角色、tool risk 和 sandbox lease 检查 | 可复用合同，策略 source of truth 应迁到控制面 |
| Lease | `gateway/session_scope_store.py` 生成逻辑 `sbx_*` lease | 尚未绑定真实 workload UID、RuntimeClass 或 image digest |
| 环境 | `BaseEnvironment` 已支持 Docker、Modal、Daytona、SSH 等 backend | 适合新增 Sandbox Service adapter |
| Session 映射 | 普通 `task_id` 当前会折叠成共享的 `default` environment | 多租户生产阻塞项 |
| Docker backend | Hermes 进程直接调用 Docker CLI | 生产上应由独立 Sandbox Manager 取代 |

## 4. 目标拓扑

```text
Browser / Client
  |
  v
Edge Gateway + OIDC/JWT Auth
  - TLS
  - rate limit
  - JWT verification
  - strip untrusted X-Hermes-* headers
  |
  v
Session Router / Ultra Control Plane
  - tenant membership
  - workspace/project ACL
  - run envelope
  - quota and policy
  |
  v
Hermes Service / Hermes Cell
  - conversation loop
  - model calls
  - approvals
  - tool routing
  - no host shell
  |
  +---------------------> TokenRouter / Provider Gateway
  |                         - real provider credentials
  |                         - quota and usage
  |
  v
Sandbox Manager
  - create / attach / exec / terminate
  - lease -> workload binding
  |
  v
gVisor or Kata Sandbox
  - terminal / execute_code / file operations
  - scoped workspace
  - deny-by-default egress
```

持久状态独立于 Hermes 容器：

```text
PostgreSQL       principal, membership, session, message, lease, audit
Redis            lock, rate limit, short-lived coordination
Object Storage   uploads, outputs, artifacts, snapshots
Workspace Store  sandbox POSIX working set
Vault/OpenBao    provider and service credentials
```

## 5. 多用户部署模型

### 5.1 每用户一个 Hermes 容器

优点：

- 隔离最直观。
- 最大程度兼容当前 `/opt/data`、SQLite、memory、plugins 和 profile 模型。
- 出现状态串用时影响范围最小。

缺点：

- 容器数量和常驻内存随用户增长。
- 升级、调度和冷启动成本较高。
- 企业 workspace 内协作需要额外的共享数据服务。

适合私有部署、小规模内部用户和第一轮安全验证。

### 5.2 每 tenant/workspace 一个 Hermes Cell

这是推荐的第一版 SaaS 形态。

每个 Cell 拥有独立的：

- `/opt/data` 或 PVC。
- Hermes config/profile。
- SQLite bridge 状态。
- Plugin/Skill allowlist。
- 缓存和进程空间。

Cell 内可以服务同一 tenant/workspace 的多个用户，但 session、memory、asset 和 sandbox 仍必须按 user/project/session 校验。

### 5.3 共享 Hermes Worker Pool

这是长期规模化形态，不应在状态尚未外移时直接采用。

前置条件：

- Session/message 已迁移到 PostgreSQL。
- 文件和产物已迁移到 object storage/workspace service。
- memory、approval、job、audit 不依赖本地文件。
- Hermes worker 不保存跨请求可变全局状态。
- 任意 worker 都能从 run envelope 恢复同一 session。

## 6. 状态所有权

| 状态 | 唯一 owner | Hermes 是否可持久化副本 |
|---|---|---|
| 用户身份、tenant membership | Identity / Control Plane | 否 |
| Session、message、run | Session Service / PostgreSQL | 只允许请求内缓存 |
| Sandbox 状态与物理 workload | Sandbox Manager | 只保存 opaque `sandbox_id` |
| Provider credential | Vault + TokenRouter | 否 |
| Asset metadata、ACL、lineage | Asset Service | 否 |
| 二进制文件和 artifact | Object Storage / Workspace Service | 否 |
| Tool policy、quota | Policy / TokenRouter | 可缓存版本化只读 bundle |
| Prompt cache、单次 Agent runtime | Hermes worker | 是，但必须绑定 session |

任何两个层同时声称同一状态为 source of truth，都视为架构缺陷。

## 7. Hermes 服务容器合同

生产 `hermes-service` 镜像应与个人自托管镜像区分。

### 镜像要求

- 镜像按 digest 部署。
- `/opt/hermes` 只读。
- 不包含或不暴露 `docker-cli`。
- 不挂载 `/var/run/docker.sock`、containerd socket 或宿主根目录。
- 禁止 `terminal.backend=local`。
- 禁止运行时修改核心 Python environment。
- 只安装审核过的内置 Plugin。
- 依赖和系统包在构建期固定。

### Pod/容器安全要求

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10000
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]

automountServiceAccountToken: false
```

同时要求：

- `/tmp` 使用有容量限制的 tmpfs/emptyDir。
- 设置 CPU、memory、PID 和 ephemeral-storage limit。
- 使用独立 ClusterIP/内部网络，不使用 host network。
- 只允许访问 Control Plane、PostgreSQL、Redis、TokenRouter、Sandbox Manager 和必要的模型网关。
- 健康检查区分 liveness、readiness 和 dependency degradation。
- shutdown 先停止接新 run，再等待活跃 turn 到安全边界后退出。

### PVC 初始化

当前镜像依赖 root 处理 UID/GID 和 `/opt/data` 权限。生产版建议：

1. initContainer 以受限权限创建/chown tenant PVC。
2. Hermes 主容器从启动开始以 UID 10000 运行。
3. 主容器不再拥有 UID remap、chown 任意路径或安装系统包的能力。

## 8. 身份与请求隔离

### 外部身份

浏览器提交 OIDC access token：

```json
{
  "sub": "user_123",
  "tenant_id": "tenant_a",
  "workspace_id": "workspace_1",
  "roles": ["member"],
  "exp": 1780000000
}
```

Edge/Auth 必须：

1. 验证 issuer、audience、signature、`exp` 和 `nbf`。
2. 从服务端 membership 数据库重新确认 tenant/workspace/project 权限。
3. 删除客户端传入的所有 `X-Hermes-*` header。
4. 创建内部签名 run envelope，或由可信内网代理重新注入 principal header。
5. Hermes API 只接受来自 BFF/Control Plane 的 mTLS 或服务身份。

### Run Envelope

```json
{
  "run_id": "run_...",
  "session_id": "sess_...",
  "principal": {
    "tenant_id": "tenant_...",
    "workspace_id": "workspace_...",
    "project_id": "project_...",
    "user_id": "user_...",
    "roles": ["member"]
  },
  "allowed_capabilities": ["chat.run", "sandbox.execute"],
  "budget": {
    "max_tool_calls": 30,
    "max_cost_units": 100
  },
  "input": {
    "message": "...",
    "asset_ids": []
  }
}
```

Hermes 不允许从 prompt、模型输出或前端 body 重建 principal。

## 9. 数据隔离

所有持久业务记录必须显式包含：

```text
tenant_id
workspace_id
project_id
created_by
created_at
```

| 数据 | P0/Cell 模式 | 共享 Worker 模式 |
|---|---|---|
| Session/message | Cell 内独立 `state.db`，外层 scope 校验 | PostgreSQL + tenant filter + RLS |
| Memory | Cell 独立目录，按 user/project 分区 | 独立 Memory Service，资源级 ACL |
| Upload/output | tenant 独立目录或 bucket prefix | Asset Service + object ID 授权 + signed URL |
| Cache | Cell 内缓存 | Redis key 含 tenant/project，但缓存不能替代授权 |
| Workspace | 每 session 独立路径/volume | Sandbox Manager 分配的 session workspace |
| Audit | Cell 内 append-only bridge | 集中 append-only audit store |

对象 key、路径、session ID 和 sandbox ID 都只是 locator，不是授权凭证。

## 10. 按需 Sandbox 生命周期

### 状态机

```text
none -> creating -> ready -> attached -> idle
idle -> attached
idle -> snapshotting -> terminated
terminated -> restoring -> attached
any -> failed
```

### 请求流程

```text
普通聊天
  -> Hermes 直接完成，不创建 sandbox

第一次 terminal / execute_code / file mutation
  -> Policy authorize
  -> SandboxManager.ensure(principal, session_id, profile)
  -> 创建 gVisor/Kata workload
  -> 持久化 lease -> workload binding
  -> exec 并流式返回 stdout/stderr

后续执行调用
  -> 重新校验 principal 和 lease
  -> attach 同一个 session sandbox

空闲超时
  -> 保存允许保留的 artifacts
  -> terminate workload
```

### Sandbox Manager 最小 API

```text
POST   /sandboxes/ensure
POST   /sandboxes/{id}/exec
POST   /sandboxes/{id}/files/upload
GET    /sandboxes/{id}/files/download
POST   /sandboxes/{id}/snapshot
DELETE /sandboxes/{id}
GET    /sandboxes/{id}/status
```

Lease 必须绑定：

```text
sandbox_id
tenant_id
workspace_id
project_id
session_id
owner_user_id
runtime_class
workload_uid
image_digest
resource_profile
state
expires_at
last_active_at
```

### Sandbox 安全要求

- 每 session 独占 writable overlay。
- 禁止不同 tenant/project/session 复用已绑定的 warm sandbox。
- 禁止 hostPath、privileged、host PID、host IPC、host network 和 runtime socket。
- 默认拒绝 egress，只允许 TokenRouter、artifact service 和受控包代理。
- 禁止 cloud metadata、Kubernetes API、Vault、数据库和 provider 直连。
- sandbox 只持有短期 scoped token，不持有真实 provider key。
- OOM、disk full、timeout 和 workload eviction 返回结构化错误。

## 11. Tool、Plugin 与 Skill 边界

| 能力 | 运行位置 | 规则 |
|---|---|---|
| LLM 调用、session、审批 | Hermes | 可信控制面逻辑 |
| terminal、execute_code、file mutation | Sandbox | 必须经过 lease 与 policy |
| read_file | Sandbox/Workspace Service | 不能读取 Hermes 宿主路径 |
| 不可信 MCP server | Sandbox 或独立 MCP worker | 不进入 Hermes 进程 |
| Browser/Computer Use | 独立 Browser Context Service | Cookie 和 context 按 project 隔离 |
| 审核过的系统 Plugin | Hermes 镜像 | 构建期固定版本 |
| tenant 安装的可执行 Plugin | Sandbox/plugin worker | 禁止 import 到共享 Hermes Python 进程 |
| 纯提示类 Skill | Hermes | references 和权限按 tenant/project 过滤 |

## 12. 错误与降级策略

| 故障 | 必须行为 |
|---|---|
| Sandbox Manager 不可用 | 返回 `sandbox_unavailable`，不执行本地 shell |
| Lease 不存在/过期/不匹配 | 返回 403/typed policy error |
| Auth/Policy 不可用 | 敏感操作 fail closed |
| TokenRouter 不可用 | provider 调用失败，不回退静态 key |
| Workspace restore 失败 | 明确报告缺失文件，不创建空白结果冒充恢复成功 |
| Hermes worker 退出 | run 可从 durable session/event state 恢复 |
| 审计写入失败 | 高风险操作默认拒绝，break-glass 必须独立审批 |

生产配置中必须删除所有“sandbox 失败后自动改用 local backend”的路径。

## 13. 迁移路线

### P0：Hermes Cell

- 新建不含 Docker socket/docker-cli 的 `hermes-service` 镜像 target。
- 每 tenant/workspace 一个 Hermes Cell 和独立 `/opt/data`。
- Auth Gateway 验证 JWT，只有内部可信层创建 principal。
- 禁止 hosted 模式使用 `terminal.backend=local`。
- 修复普通 session 全部映射到 `default` environment 的行为。
- 增加跨 user/session 的状态与文件隔离测试。

### P1：Sandbox Control Plane

- 实现 Sandbox Manager。
- 新增 `SandboxServiceEnvironment` adapter，替代 Hermes 直接调用 Docker。
- 逻辑 lease 绑定真实 gVisor/Kata workload。
- 每 session 独立 workspace 和 artifact snapshot。
- 接入 deny-by-default egress、TokenRouter 和短期 token。
- 建立 sandbox TTL、reaper、quota 和容量告警。

### P2：共享 Worker Pool

- Session/message/memory/approval/audit 外移到持久服务。
- Asset 和 workspace 完成 object storage 化。
- Hermes worker 变成无状态、可水平扩展的执行器。
- 任意 worker 可从 run envelope 恢复任意 session。
- PostgreSQL RLS、mTLS、OPA policy bundle 和全链路审计上线。
- 按 workload 风险选择 gVisor 或 Kata RuntimeClass。

## 14. 验收矩阵

| 场景 | 预期结果 |
|---|---|
| 用户只聊天 | 不创建 sandbox |
| 第一次调用 terminal | 创建当前 session 独占 sandbox |
| 同一 session 再次执行 | 复用同一有效 lease/workload |
| Tenant A 猜测 Tenant B session ID | API、DB 和 Sandbox Manager 全部拒绝 |
| 两个普通 session 同时执行 | workload、workspace、进程和 env 均不同 |
| Sandbox 创建失败 | 返回 `sandbox_unavailable`，宿主机无命令执行 |
| Sandbox 尝试访问 metadata | 网络层拒绝并产生 audit event |
| Sandbox 尝试直接访问 provider | 拒绝，只能通过 TokenRouter |
| Sandbox 内搜索 provider key | 环境和挂载文件中不存在 |
| Hermes 容器被 prompt injection 影响 | 无 Docker socket、无宿主 shell、无跨租户直接数据权限 |
| Worker 在响应中途退出 | 客户端从 event cursor/session projection 恢复 |
| Tenant Plugin 包含可执行代码 | 进入隔离 worker，不 import 到 Hermes 主进程 |

## 15. 发布门槛

在标记为“生产多用户隔离已完成”前，必须有本次部署环境的新鲜证据：

- Hermes service 容器以非 root、只读 root filesystem 运行。
- Hermes 容器内不存在可用 Docker/containerd socket。
- 两个不同 tenant/session 的物理 sandbox UID 和 workspace 不同。
- PostgreSQL/API 跨租户访问测试返回 deny。
- metadata、private CIDR、Vault、数据库和 provider 直连测试均失败。
- sandbox 中无法读取真实 provider key。
- sandbox 不可用时没有 local fallback。
- 日志、trace、event 中没有 secret，并能按 `run_id -> tool_call_id -> sandbox_id` 追踪。
- 资源耗尽、终止、恢复和清理均有结构化结果与告警。

## 16. 开放问题

1. 第一版 Cell 的粒度是 tenant、workspace 还是 user？默认建议 workspace，强隔离客户可选 tenant/user。
2. Session sandbox 的默认空闲 TTL 和 artifact 保留期是多少？
3. P1 首选 gVisor 还是直接使用 Kata？需要以兼容性、冷启动、I/O 和威胁模型 POC 决定。
4. Browser context 是否和 terminal sandbox 共生命周期，还是始终由独立 Browser Service 管理？
5. 哪些系统 Plugin 可以进入 Hermes 镜像，谁负责审核、签名和撤销？
6. 共享 Worker Pool 前，现有 SQLite/本地 memory 的迁移和双写窗口如何关闭？

## 17. 相邻文档

- [Hermes Fork 隔离与多租户控制面迁移](08-hermes-fork-isolation-migration)
- [基础设施边界图](02-boundary-map)
- [控制面设计](03-control-plane-design)
- [执行面设计](04-execution-plane-design)
- [数据面设计](05-data-plane-design)
- [安全与运维设计](06-security-ops-design)
- [沙箱生命周期](../product-specs/components/14-sandbox-lifecycle)
- [TokenRouter](../product-specs/components/17-tokenrouter)

