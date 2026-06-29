# Hermes Supercomputer Open Source Architecture Plan

Status: architecture selection plan
Scope: cloud multi-tenant Hermes supercomputer
Source spec: `docs/hermes-supercomputer-isolation-spec.md`
Official-source check date: 2026-05-29
Notion refresh: `docs/hermes-notion-update-index.md`

## Goal

为 Hermes 云端多租户 supercomputer 的每个核心架构模块选择最合适的开源方案，并说明为什么选择它、替代方案为什么不作为主选、如何落地、如何验收。

## Selection Summary

| # | Architecture | Recommended open-source solution | Why this is the default | HTML |
|---:|---|---|---|---|
| 1 | Edge Gateway / Realtime Ingress | Envoy Gateway + Envoy Proxy | Hermes 的入口层需要同时处理普通 HTTP、SSE、WebSocket、长连接空闲超时、租户级限流和策略外调，Envoy 的过滤器模型和 Envoy Gateway 的 Gateway API 扩展正好覆盖这些边界。 | [doc](open-source-architecture/01-edge-gateway-realtime-ingress.html) |
| 2 | Identity / Tenant Access | Keycloak | Hermes 需要标准 OIDC/JWT、企业 SSO、服务端验证、client policies 和管理 API；Keycloak 的成熟度和生态最适合作为自托管身份层。 | [doc](open-source-architecture/02-identity-tenant-access.html) |
| 3 | Policy Engine / Authorization | Open Policy Agent (OPA) | Hermes 的安全边界很多，不能把权限判断散落在每个服务里。OPA 可以用同一种 policy bundle 驱动 Envoy ext_authz、Kubernetes admission 和业务服务 sidecar/SDK。 | [doc](open-source-architecture/03-policy-engine-authorization.html) |
| 4 | Sandbox Runtime / Isolation | Kata Containers with Cloud Hypervisor profile | Hermes sandbox 会执行用户文件、shell、第三方内容派生工具，普通容器边界不足以承担主隔离职责。Kata 用轻量 VM 包住容器 workload，更符合“默认假设 sandbox 可被攻破”的模型。 | [doc](open-source-architecture/04-sandbox-runtime-isolation.html) |
| 5 | Workspace Volume Mounter / POSIX Data Isolation | JuiceFS CSI backed by object storage | Agent 和工具天然按文件系统工作，需要 POSIX 语义、目录遍历、临时输出和原子文件写入；直接 S3 API 会让工具改造成本高。 | [doc](open-source-architecture/05-workspace-volume-mounter.html) |
| 6 | TokenRouter / Secrets Boundary | Custom TokenRouter backed by OpenBao | Hermes 的 TokenRouter 不只是 secrets lookup，它要执行 plan、scope、asset ACL、model allowlist、concurrency、redaction 和 audit。这个业务逻辑必须自研。 | [doc](open-source-architecture/06-tokenrouter-secrets-boundary.html) |
| 7 | Session / Job Orchestration | Temporal | Hermes 的媒体生成和 agent 任务不是普通队列任务：它们有长耗时、重试、超时、外部 side effect、状态流和失败恢复。Temporal 的 Durable Execution 正是这个问题域。 | [doc](open-source-architecture/07-durable-session-job-orchestration.html) |
| 8 | Event Bus / Realtime Fanout | NATS JetStream | Hermes 需要低延迟事件推送和短期持久化回放，而不是只要一个重型日志管道。NATS JetStream 同时提供 pub/sub、stream、durable consumer 和 at-least-once delivery。 | [doc](open-source-architecture/08-event-bus-realtime-fanout.html) |
| 9 | GPU Render Scheduling / Supercomputer Fabric | Kueue + NVIDIA GPU Operator | Hermes 的 supercomputer 本质是多租户 GPU batch fabric：要按 plan/tenant 控制 queue、quota、priority、concurrency 和抢占。Kueue 是 Kubernetes-native 的 job queueing 方案，贴合这个模型。 | [doc](open-source-architecture/09-gpu-render-scheduling.html) |
| 10 | Skill Workflow Runtime / Registry | LangGraph OSS library + custom Skill Registry | Lark 源文档强调 Skill 是 workflow，不是 prompt。LangGraph 的状态图、checkpoint、subgraph 和持久化机制适合表达这类有阶段、有条件、有 QA 的 agent workflow。 | [doc](open-source-architecture/10-skill-workflow-runtime.html) |
| 11 | Guardrails / Exfiltration Defense | NVIDIA NeMo Guardrails + OPA + custom egress filters | 提示注入和 Skill 泄露不能靠一段系统提示解决。NeMo Guardrails 提供 LLM app 可编程 rails，适合做输入/输出和对话层约束。 | [doc](open-source-architecture/11-guardrails-exfiltration-defense.html) |
| 12 | Relational Data / Tenant Row Isolation | PostgreSQL with Row Level Security | Hermes 控制面是典型关系模型：租户、成员、项目、会话、工具调用、用量、审计都需要事务和约束。PostgreSQL 是最稳妥的开源选择。 | [doc](open-source-architecture/12-relational-data-tenant-rls.html) |
| 13 | Object / Media Storage | Rook-Ceph RGW for production; MinIO only for dev/small private deployments | 生产级开源存储需要对象、块、文件多模式能力和 Kubernetes operator 治理；Rook-Ceph 同时提供 CephFS、RBD 和 RGW/S3，适合 Hermes 的 mixed storage plane。 | [doc](open-source-architecture/13-object-media-storage.html) |
| 14 | Observability / Audit | OpenTelemetry + Grafana LGTM stack | Hermes 的故障排查横跨 Edge、Session、Sandbox、TokenRouter、Temporal、NATS、GPU worker、object storage。OpenTelemetry 是跨语言、跨后端的最低耦合采集标准。 | [doc](open-source-architecture/14-observability-audit.html) |
| 15 | Service Mesh / Internal mTLS / Egress | Istio | Hermes 需要明确区分服务身份、用户身份和 sandbox token。Istio 的 mTLS、AuthorizationPolicy、egress gateway 能把服务到服务通信也纳入策略边界。 | [doc](open-source-architecture/15-service-mesh-egress.html) |
| 16 | Cloud Deployment / GitOps / Secrets Delivery | Argo CD + External Secrets Operator | Hermes 组件多、命名空间多、node pool 多，手工 kubectl 会迅速失控。Argo CD 用 Git 作为 desired state，并持续对比 live state。 | [doc](open-source-architecture/16-gitops-secrets-delivery.html) |

## Cross-Cutting Decisions

- Use microVM isolation for untrusted sandbox execution; plain containers are not the main isolation boundary.
- Keep provider credentials inside TokenRouter/OpenBao. Sandboxes receive short-lived Hermes tokens only.
- Use OPA as deterministic policy decision point for gateway, TokenRouter, mounts, egress, and skill exfiltration.
- Use Temporal for durable workflows and NATS JetStream for realtime events. Do not use a message queue as the sole source of long-running job truth.
- Use PostgreSQL RLS as a second line of defense, not a replacement for application-level tenant filters.
- Protect skill internals by design: public metadata is allowed; verbatim SKILL.md, references, internal prompts, tool-chain recipes, and bulk export are denied.
- Treat storage authorization by asset IDs and scoped credentials, never by raw object key possession.
- Treat `references` as three separate concepts: Skill internal `references/`, user attached references, and media generation references. See `docs/hermes-references-knowledge-model.md`.
- Keep TokenRouter as the control-plane credential/quota/audit boundary and CometAPI as an optional future media data-plane, not a bundled MVP requirement.
- Model Soul ID and Element as project-scoped asset references with ACL and lineage, not just prompt text.

## Notion Refresh Addenda

The 2026-06-02 Notion refresh is split into child docs instead of being merged into this architecture plan:

| Addendum | Purpose |
|---|---|
| `docs/hermes-references-knowledge-model.md` | Hermes Skill knowledge layers and protected `references/` rules. |
| `docs/hermes-tokenrouter-credential-flow.md` | Four-stage sandbox JWT, quota, vault-backed provider credential flow. |
| `docs/hermes-cometapi-media-gateway.md` | Future media gateway for external video/audio/image preprocessing. |
| `docs/hermes-soulid-element-asset-model.md` | Persistent semantic assets: Soul ID, Element, media inputs, job outputs. |
| `docs/hermes-soulid-reproduction-and-test-plan.md` | Experiment plan for open-source Soul ID approximation and model probes. |
| `docs/hermes-tool-contracts-from-notion.md` | `default_api` tool contract capture and provider-neutral local mapping. |

## Module Details

### 1. Edge Gateway / Realtime Ingress

Recommended: **Envoy Gateway + Envoy Proxy**
HTML: [01-edge-gateway-realtime-ingress.html](open-source-architecture/01-edge-gateway-realtime-ingress.html)

Role: 统一承接浏览器 SSE/WebSocket/HTTP 请求，完成 TLS、JWT 校验、限流、外部授权、带宽限制、路由和会话流转发。

Why:
- Hermes 的入口层需要同时处理普通 HTTP、SSE、WebSocket、长连接空闲超时、租户级限流和策略外调，Envoy 的过滤器模型和 Envoy Gateway 的 Gateway API 扩展正好覆盖这些边界。
- Envoy Gateway 原生支持 SecurityPolicy、external authorization 和本地/全局限流，便于把 JWT claim、tenant_id、workspace_id 和 plan 转成统一的控制面决策输入。
- 相比功能型 API 网关，Envoy 更适合做后续 service mesh、OPA ext_authz、TokenRouter、egress gateway 的共同代理基础。

Alternatives:
- **Kong Gateway**: 插件生态强，API 管理成熟；不是主选：社区/企业功能边界和许可证要仔细核对；和 K8s Gateway API、service mesh 策略统一性不如 Envoy 直接
- **Traefik**: 上手快，Ingress/Gateway 使用简单；不是主选：复杂 ext_authz、长连接细粒度策略、全局限流和多租户策略编排不如 Envoy 深
- **NGINX Gateway Fabric**: 稳定，团队熟悉度高；不是主选：动态 xDS、Envoy 生态策略集成和 AI/agent gateway 延展性较弱

Implementation plan:
- 先接入 JWT 校验、请求体大小限制、SSE idle timeout 和 `/healthz`。
- 第二步启用 per-route local rate limit；接入 Redis-backed global rate limit。
- 第三步启用 ext_authz，把 session subscribe、tool preflight 和 tenant plan 都统一到 OPA 决策。

Acceptance checks:
- Tenant A 的 stream token 不能订阅 Tenant B 的 SSE。
- JWT 缺少 tenant/workspace claim 时返回 401/403。
- 超过 plan 的请求被限流而不是进入后端队列。
- WebSocket/SSE 断线重连不会跨 session 泄露事件。

Sources:
- [Envoy Gateway rate limiting](https://gateway.envoyproxy.io/docs/concepts/rate-limiting/)
- [Envoy Gateway external authorization](https://gateway.envoyproxy.io/docs/tasks/security/ext-auth/)
- [Envoy Proxy WebSocket example](https://www.envoyproxy.io/docs/envoy/latest/start/sandboxes/websocket.html)

### 2. Identity / Tenant Access

Recommended: **Keycloak**
HTML: [02-identity-tenant-access.html](open-source-architecture/02-identity-tenant-access.html)

Role: 负责 OIDC/OAuth2 登录、企业 IdP federation、客户端注册、用户身份令牌和可审计的身份生命周期。

Why:
- Hermes 需要标准 OIDC/JWT、企业 SSO、服务端验证、client policies 和管理 API；Keycloak 的成熟度和生态最适合作为自托管身份层。
- 租户模型不能完全塞进身份系统。Keycloak 负责认证和高层 claim，workspace/project/membership 仍由 Hermes 控制面数据库负责。
- 对大型企业客户可以采用 realm-per-enterprise；默认 SaaS 可以先单 realm + tenant claim，避免一开始承担 realm 爆炸的运维复杂度。

Alternatives:
- **Zitadel**: 现代化、多租户体验好；不是主选：生态和企业自定义迁移案例相对 Keycloak 少
- **Ory Hydra/Kratos**: 组件化、云原生；不是主选：需要拼装多个服务；对快速落地的统一管理台不如 Keycloak
- **Authentik**: 自托管体验好，管理界面友好；不是主选：大规模企业 IdP federation 和复杂 client policy 的成熟度需要额外验证

Implementation plan:
- MVP：单 realm，OIDC 登录，JWT 验证，membership 映射。
- 企业阶段：IdP federation、realm template、组织级 MFA/client policy。
- 审计阶段：Keycloak admin event 同步进 Hermes audit_events。

Acceptance checks:
- 注销或禁用用户后不能继续创建新 session。
- 用户跨 tenant 切换必须重新解析 membership，不能只信前端 tenant_id。
- 企业 IdP claim 变化不会自动授予 project 权限，必须经过 Hermes membership 映射。

Sources:
- [Keycloak Server Administration Guide](https://www.keycloak.org/docs/latest/server_admin/)
- [Keycloak client registration](https://www.keycloak.org/securing-apps/client-registration)

### 3. Policy Engine / Authorization

Recommended: **Open Policy Agent (OPA)**
HTML: [03-policy-engine-authorization.html](open-source-architecture/03-policy-engine-authorization.html)

Role: 作为统一 PDP，给 Edge、TokenRouter、Volume Mounter、egress proxy、K8s admission 和 Skill exfil filter 返回默认拒绝的策略判断。

Why:
- Hermes 的安全边界很多，不能把权限判断散落在每个服务里。OPA 可以用同一种 policy bundle 驱动 Envoy ext_authz、Kubernetes admission 和业务服务 sidecar/SDK。
- OPA 的 Rego 适合表达 asset ACL、tool scope、mount policy、tenant plan、egress allowlist 这类结构化策略。
- 相比只在应用里写 if/else，OPA policy 可以版本化、审计、回放测试，并在 CI 中做策略单元测试。

Alternatives:
- **Cedar**: 权限语言清晰，适合应用授权；不是主选：Envoy/K8s/sidecar 生态没有 OPA 广；需要更多自研集成
- **Casbin**: 轻量，嵌入应用简单；不是主选：跨网关、K8s admission、bundle 分发和决策审计不如 OPA
- **OpenFGA**: 关系授权强；不是主选：适合 Zanzibar 风格关系图，不适合作为所有 tool/egress/mount 的通用 PDP

Implementation plan:
- 先做 TokenRouter 和 Volume Mounter 的库内 OPA evaluate。
- 再接 Edge ext_authz，把会话订阅、API route 和 plan 限流的决策统一。
- 最后加 Kubernetes admission，阻止未声明 RuntimeClass、hostPath、privileged、metadata egress 的 sandbox pod。

Acceptance checks:
- 策略 bundle 缺失时所有敏感操作默认拒绝。
- 每个 deny 都有短 reason，但不暴露内部规则细节。
- OPA 单测覆盖所有 acceptance criteria 的 allow/deny 路径。

Sources:
- [OPA Envoy plugin](https://www.openpolicyagent.org/docs/envoy)
- [OPA on Kubernetes](https://www.openpolicyagent.org/docs/deploy/k8s)

### 4. Sandbox Runtime / Isolation

Recommended: **Kata Containers with Cloud Hypervisor profile**
HTML: [04-sandbox-runtime-isolation.html](open-source-architecture/04-sandbox-runtime-isolation.html)

Role: 为不可信 terminal/tool execution 提供 microVM 级隔离，同时保留 Kubernetes 调度、镜像和 RuntimeClass 生态。

Why:
- Hermes sandbox 会执行用户文件、shell、第三方内容派生工具，普通容器边界不足以承担主隔离职责。Kata 用轻量 VM 包住容器 workload，更符合“默认假设 sandbox 可被攻破”的模型。
- 直接用 Firecracker 需要自研 fleet manager、镜像构建、网络、存储和调度；Kata 先借用 Kubernetes/containerd/CRI，落地风险更低。
- Cloud Hypervisor profile 对通用云环境更容易调试；Firecracker profile 可作为高密度、成熟后的优化路径。

Alternatives:
- **Firecracker direct**: 隔离强、启动快、AWS Lambda 证明过高密度模型；不是主选：需要自建 microVM 调度、网络、镜像和卷挂载控制面
- **gVisor**: 更轻，K8s 集成好；不是主选：不是完整 VM 边界；对任意 shell/code 的隔离信心低于 Kata/Firecracker
- **plain Docker/containerd runc**: 成本低、性能好；不是主选：不能作为多租户任意代码执行的主安全边界

Implementation plan:
- MVP：Kata RuntimeClass + 固定 sandbox image + session 级冷启动。
- 性能阶段：warm pool 只预热未绑定 VM；绑定 tenant 后不能跨 tenant 复用。
- 硬化阶段：sandbox escape test、metadata egress test、hostPath admission test、kernel capability audit。

Acceptance checks:
- Sandbox 内无法访问 cloud metadata service。
- Sandbox pod spec 出现 hostPath/privileged 会被 admission 拒绝。
- Tenant A sandbox 销毁后，其 writable overlay 不能被任何后续 tenant 读到。

Sources:
- [Kata Containers](https://katacontainers.io/)
- [Kubernetes RuntimeClass](https://kubernetes.io/docs/concepts/containers/runtime-class)

### 5. Workspace Volume Mounter / POSIX Data Isolation

Recommended: **JuiceFS CSI backed by object storage**
HTML: [05-workspace-volume-mounter.html](open-source-architecture/05-workspace-volume-mounter.html)

Role: 把 tenant/workspace/project/session 的文件、memory、outputs 以 POSIX 文件系统形式挂进 sandbox，同时让底层容量和生命周期走对象存储。

Why:
- Agent 和工具天然按文件系统工作，需要 POSIX 语义、目录遍历、临时输出和原子文件写入；直接 S3 API 会让工具改造成本高。
- JuiceFS 把元数据和对象数据分离，适合用对象存储承载大规模媒体/文件，同时通过 CSI 给 sandbox pod 挂载。
- 相比共享 NFS，JuiceFS 更适合弹性容量和云对象存储；相比直接 CephFS，后续迁移到云 S3/R2/GCS 更灵活。

Alternatives:
- **CephFS CSI**: 强 POSIX，一体化存储，适合自建 Rook-Ceph；不是主选：和底层 Ceph 强绑定；跨云对象存储迁移没有 JuiceFS 灵活
- **NFS/EFS**: 成熟简单；不是主选：租户隔离、审计、弹性缓存和大规模小文件/媒体混合负载要额外治理
- **direct object storage only**: 权限边界清晰，成本低；不是主选：不适配大部分 agent/tool 的文件工作流，需要大量工具重写

Implementation plan:
- MVP：project 级 JuiceFS volume，session 输出目录隔离。
- 第二阶段：per-session snapshot/restore，失败 job 输出自动归档。
- 第三阶段：冷热缓存、文件 lineage、对象生命周期策略和租户加密上下文。

Acceptance checks:
- 请求未授权路径时 mount policy 返回 deny。
- Sandbox 内没有 skill `references/*` 和 provider credentials。
- Tenant A project volume 无法由 Tenant B session mount。

Sources:
- [JuiceFS CSI introduction](https://juicefs.com/docs/csi/introduction/)
- [Use JuiceFS in Kubernetes](https://juicefs.com/docs/cloud/use_juicefs_in_kubernetes)

### 6. TokenRouter / Secrets Boundary

Recommended: **Custom TokenRouter backed by OpenBao**
HTML: [06-tokenrouter-secrets-boundary.html](open-source-architecture/06-tokenrouter-secrets-boundary.html)

Role: TokenRouter 是业务边界；OpenBao 是密钥、动态凭证、transit crypto 和审计后端。Sandbox 永远拿不到真实 provider key。

Why:
- Hermes 的 TokenRouter 不只是 secrets lookup，它要执行 plan、scope、asset ACL、model allowlist、concurrency、redaction 和 audit。这个业务逻辑必须自研。
- OpenBao 适合作为开源 secrets 基座：支持 auth methods、secrets engines、Kubernetes/JWT auth、审计设备和 transit 加密。
- 相比直接用 Kubernetes Secret 或云 Secret Manager，OpenBao 更容易保持云无关和统一审计；相比 HashiCorp Vault，OpenBao 的开源治理/许可证风险更可控。

Alternatives:
- **HashiCorp Vault**: 成熟度高、生态大；不是主选：许可证和开源治理不符合“纯开源优先”的目标
- **Kubernetes Secrets only**: 简单，内建；不是主选：不能承担 provider key 池、动态凭证、审计和细粒度访问边界
- **Cloud Secret Manager**: 云厂商托管，运维少；不是主选：不满足开源/云无关要求；多云迁移和本地测试割裂

Implementation plan:
- MVP：静态 provider key 存 OpenBao KV，TokenRouter scope+plan 校验。
- 第二阶段：provider key pool、region fallback、circuit breaker、per-tenant budget。
- 第三阶段：动态凭证、transit 加密、break-glass 审计流程。

Acceptance checks:
- Sandbox 环境变量和文件系统中不存在真实 provider key。
- 过期 `HF_JWT_TOKEN` 被 TokenRouter 拒绝。
- OpenBao audit 中能查到每次 TokenRouter secrets access。

Sources:
- [OpenBao auth methods](https://openbao.org/docs/auth/)
- [OpenBao Kubernetes platform](https://openbao.org/docs/platform/k8s/)
- [OpenBao audit devices](https://openbao.org/docs/audit/)

### 7. Session / Job Orchestration

Recommended: **Temporal**
HTML: [07-durable-session-job-orchestration.html](open-source-architecture/07-durable-session-job-orchestration.html)

Role: 承接 session lifecycle、sandbox attach、media job group、retry、timeout、compensation、human approval 和长任务状态恢复。

Why:
- Hermes 的媒体生成和 agent 任务不是普通队列任务：它们有长耗时、重试、超时、外部 side effect、状态流和失败恢复。Temporal 的 Durable Execution 正是这个问题域。
- 把工作流状态写在自研 DB + worker loop 容易产生重复执行、丢状态和重试不一致；Temporal 把这些故障语义变成平台能力。
- Temporal 的 task queue 模型可以把 sandbox attach、GPU job、thumbnail、asset registration 拆成不同 worker pool。

Alternatives:
- **Celery/RQ**: 简单，Python 生态成熟；不是主选：只解决任务派发，不解决长工作流状态、补偿、定时器和 determinism
- **Argo Workflows**: K8s 原生，适合批处理流水线；不是主选：交互式 session 和细粒度业务状态恢复不如 Temporal
- **Airflow**: 适合数据调度；不是主选：不是低延迟用户交互和 per-session agent job 的最佳模型

Implementation plan:
- MVP：media job group 和 sandbox attach 两条 workflow。
- 第二阶段：跨 worker pool retries、compensation、manual intervention。
- 第三阶段：workflow replay tests、versioning strategy、multi-region failover 设计。

Acceptance checks:
- Worker crash 后 job 不丢失，并从上一个已提交 activity 后恢复。
- 重复 delivery 不会重复计费或覆盖错误 output_prefix。
- 超过 timeout 的 job 被标记失败并返回 sanitized error。

Sources:
- [Temporal documentation](https://docs.temporal.io/)

### 8. Event Bus / Realtime Fanout

Recommended: **NATS JetStream**
HTML: [08-event-bus-realtime-fanout.html](open-source-architecture/08-event-bus-realtime-fanout.html)

Role: 处理 session 事件、tool progress、job status、worker heartbeat、轻量控制消息和可回放的事件流。

Why:
- Hermes 需要低延迟事件推送和短期持久化回放，而不是只要一个重型日志管道。NATS JetStream 同时提供 pub/sub、stream、durable consumer 和 at-least-once delivery。
- 相比 Kafka，NATS 运维轻、延迟低，更适合 session fanout 和控制面事件；相比 Redis Streams，NATS 的 subject/ACL/cluster 模型更适合多服务消息总线。
- 它和 Temporal 分工清楚：Temporal 负责 durable workflow truth，NATS 负责事件广播和可短期回放的用户体验流。

Alternatives:
- **Kafka/Redpanda**: 吞吐极高，生态强；不是主选：对 session fanout 和控制消息偏重；运维与容量规划成本更高
- **RabbitMQ**: 传统队列成熟；不是主选：stream replay、subject routing 和轻量 request/reply 不如 NATS 贴合
- **Redis Streams/PubSub**: 部署简单；不是主选：多租户 ACL、持久化语义和大规模 stream 运维边界较弱

Implementation plan:
- MVP：session events stream + fanout service。
- 第二阶段：worker heartbeat、job progress、dead-letter stream。
- 第三阶段：per-tenant event quotas、backpressure、consumer lag alerts。

Acceptance checks:
- 无授权 fanout 服务不能订阅跨 tenant wildcard。
- SSE reconnect 能从 last_event_id 后补短期事件。
- JetStream consumer lag 会触发告警而不是静默丢状态。

Sources:
- [NATS JetStream concepts](https://docs.nats.io/nats-concepts/jetstream)
- [NATS JetStream consumers](https://docs.nats.io/nats-concepts/jetstream/consumers)

### 9. GPU Render Scheduling / Supercomputer Fabric

Recommended: **Kueue + NVIDIA GPU Operator**
HTML: [09-gpu-render-scheduling.html](open-source-architecture/09-gpu-render-scheduling.html)

Role: Kueue 做批量 AI/ML/GPU job admission、quota、borrowing/preemption；GPU Operator 管理 NVIDIA driver、device plugin、runtime 和监控组件。

Why:
- Hermes 的 supercomputer 本质是多租户 GPU batch fabric：要按 plan/tenant 控制 queue、quota、priority、concurrency 和抢占。Kueue 是 Kubernetes-native 的 job queueing 方案，贴合这个模型。
- NVIDIA GPU Operator 把 driver、container toolkit、device plugin、DCGM exporter 等 GPU 节点基础设施自动化，减少手工节点漂移。
- 相比 Slurm，Kueue 更适合和现有 K8s control plane、namespace、quota、GitOps、GPU worker deployment 整合。

Alternatives:
- **Slurm**: HPC 调度成熟，GPU 集群经验丰富；不是主选：和 K8s SaaS 控制面、per-tenant web workload、GitOps 的集成成本高
- **Volcano**: 批调度能力强；不是主选：生态和 SIG Scheduling/Kueue 的 K8s 原生 Job API 方向需要权衡
- **Ray/KubeRay**: 分布式 Python/ML 任务强；不是主选：更像计算框架；不应替代租户 quota/admission 的底层调度层

Implementation plan:
- MVP：单 GPU node pool + Kueue LocalQueue + one model worker。
- 第二阶段：按 model/profile 分队列，MIG/whole GPU 策略，spot/on-demand 分层。
- 第三阶段：tenant fairness、preemption、multi-region queue admission。

Acceptance checks:
- 超过 tenant GPU quota 的 job 等待而不是直接创建 pod。
- Worker 无法写 assigned output_prefix 之外的对象。
- GPU node 驱动/device plugin 版本漂移能被 operator/observability 捕获。

Sources:
- [Kueue overview](https://kueue.sigs.k8s.io/docs/overview/)
- [NVIDIA GPU Operator docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/)

### 10. Skill Workflow Runtime / Registry

Recommended: **LangGraph OSS library + custom Skill Registry**
HTML: [10-skill-workflow-runtime.html](open-source-architecture/10-skill-workflow-runtime.html)

Role: 把 Skill 建模为路由、输入门、状态图、阶段执行、QA gate、失败恢复和可保护的内部资产，而不是 prompt 文本。

Why:
- Lark 源文档强调 Skill 是 workflow，不是 prompt。LangGraph 的状态图、checkpoint、subgraph 和持久化机制适合表达这类有阶段、有条件、有 QA 的 agent workflow。
- Skill Registry 必须自研，因为它要控制 metadata 可见性、protected internals、versioning、routing priority、tenant allowlist 和 exfil 防御。
- 外层长任务仍交给 Temporal；LangGraph 只负责单个 skill 内部的有状态执行图，避免把平台级 job durability 和 skill logic 混在一起。

Alternatives:
- **LangChain agents only**: 工具生态广，上手快；不是主选：开放式 agent loop 不够确定，难以表达严格路由和 QA gate
- **CrewAI/AutoGen**: 多 agent 协作表达方便；不是主选：更偏对话式协作，不适合作为受保护 skill internals 的平台边界
- **Temporal-only workflows**: durability 强；不是主选：对 LLM state、tool graph、conversation memory 的开发体验不如 LangGraph

Implementation plan:
- MVP：把 17 个 workflow-generation routing priority 编成 deterministic router。
- 第二阶段：为 infographicMD-flow 建立完整 LangGraph 示例和 QA gate。
- 第三阶段：skill package signing、version promotion、tenant allowlist、red-team exfil tests。

Acceptance checks:
- 用户请求导出 `SKILL.md` 或 references 被拒绝。
- 同一输入多次 routing 到同一 skill，除非 routing rules 版本变化。
- Skill failure 有可恢复状态，不会无限重试。

Sources:
- [LangGraph package reference](https://reference.langchain.com/python/langgraph/overview)
- [LangGraph persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph MIT license](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)

### 11. Guardrails / Exfiltration Defense

Recommended: **NVIDIA NeMo Guardrails + OPA + custom egress filters**
HTML: [11-guardrails-exfiltration-defense.html](open-source-architecture/11-guardrails-exfiltration-defense.html)

Role: 对输入、输出、工具调用和外发通道做可编程 guardrails；OPA 做确定性 allow/deny；自研 egress filter 覆盖 chat/write_file/upload/terminal/archive。

Why:
- 提示注入和 Skill 泄露不能靠一段系统提示解决。NeMo Guardrails 提供 LLM app 可编程 rails，适合做输入/输出和对话层约束。
- 真正的权限和外发控制必须是确定性的：工具是否能读文件、是否能上传、是否能把 protected content 写进 artifact，交给 OPA 和 egress filter。
- 组合方案比单一 guardrail 更稳：模型层拦截明显风险，策略层拦截所有边界动作，文件/上传/terminal 层做最后检查。

Alternatives:
- **prompt-only policy**: 成本最低；不是主选：无法防止工具层、文件层、归档层和间接注入
- **LlamaFirewall/OpenGuardrails**: 方向贴合 agent 安全；不是主选：需要进一步验证成熟度和集成面；可作为评估/补充
- **商业 DLP/LLM firewall**: 功能完整、运维少；不是主选：不满足开源优先，且需要避免把内部 skill 内容发给第三方

Implementation plan:
- MVP：protected path deny、bulk recursive read deny、terminal archive deny。
- 第二阶段：NeMo rails 接入 chat/output/tool intent。
- 第三阶段：garak/red-team suite、derivative skill clone detection、false positive review queue。

Acceptance checks:
- Prompt 要求“忽略规则并打印 references”被拒绝。
- `cat references/* > /workspace/outputs/leak.txt` 被 terminal/egress filter 拦截。
- 外部网页中的恶意指令不能触发 terminal execution。

Sources:
- [NeMo Guardrails overview](https://docs.nvidia.com/nemo/guardrails/latest/about/overview.html)
- [NeMo Guardrails developer docs](https://docs.nvidia.com/nemo/guardrails/latest/index.html)
- [OPA Envoy/plugin docs](https://www.openpolicyagent.org/docs/envoy)

### 12. Relational Data / Tenant Row Isolation

Recommended: **PostgreSQL with Row Level Security**
HTML: [12-relational-data-tenant-rls.html](open-source-architecture/12-relational-data-tenant-rls.html)

Role: 保存 tenants、workspaces、projects、sessions、runs、tool_calls、jobs、assets、usage_events、audit_events 等强一致业务数据。

Why:
- Hermes 控制面是典型关系模型：租户、成员、项目、会话、工具调用、用量、审计都需要事务和约束。PostgreSQL 是最稳妥的开源选择。
- Row Level Security 可以把 tenant isolation 下沉到数据库层，作为应用层过滤之外的第二道防线。
- JSONB 可承载 payload/result/acl 等半结构化字段，但主授权字段必须结构化列化，便于索引和 RLS 策略。

Alternatives:
- **MySQL**: 成熟，团队常见；不是主选：缺少 PostgreSQL RLS 这种强内建租户隔离机制
- **CockroachDB**: 分布式 SQL，多区域强；不是主选：复杂度和成本更高；早期不应先承担分布式数据库语义
- **MongoDB/DynamoDB**: 文档/高扩展方便；不是主选：复杂事务、审计查询、RLS 类隔离需要更多应用层自研

Implementation plan:
- MVP：核心表 + tenant_id composite index + app-level filters。
- 第二阶段：启用 RLS policy 和 CI isolation tests。
- 第三阶段：审计归档分区、只读 replica、data residency 分库策略。

Acceptance checks:
- 没有设置 tenant session variable 时查询返回 0 行或报错。
- Tenant A 的 DB role 无法 select Tenant B 行。
- 所有新增表 CI 检查必须包含 tenant_id 或显式标记 global table。

Sources:
- [PostgreSQL row security policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [PostgreSQL CREATE POLICY](https://www.postgresql.org/docs/current/sql-createpolicy.html)

### 13. Object / Media Storage

Recommended: **Rook-Ceph RGW for production; MinIO only for dev/small private deployments**
HTML: [13-object-media-storage.html](open-source-architecture/13-object-media-storage.html)

Role: 保存 uploads、generated media、job outputs、thumbnails、manifests、asset lineage 和长期归档。

Why:
- 生产级开源存储需要对象、块、文件多模式能力和 Kubernetes operator 治理；Rook-Ceph 同时提供 CephFS、RBD 和 RGW/S3，适合 Hermes 的 mixed storage plane。
- Ceph RGW 有 S3/Swift 接口、用户/桶/tenant 概念，和多租户媒体资产模型更贴近。
- MinIO 非常适合开发和简单私有云，但当前许可证与商业云产品的合规边界要谨慎；因此不作为默认生产基座。

Alternatives:
- **MinIO**: 简单、高性能、S3 兼容，AI 场景常见；不是主选：AGPL/商业许可边界需要法务确认；只做对象存储，不覆盖块/文件一体化
- **SeaweedFS**: 轻量，文件/对象模型灵活；不是主选：生产多租户、K8s operator 和生态成熟度不如 Ceph/Rook
- **云厂商 S3/GCS/R2**: 托管、可靠、运维少；不是主选：不是开源方案；可作为部署时的兼容 backend

Implementation plan:
- MVP：dev 用 MinIO 或云 S3；接口按 S3-compatible 设计。
- 生产自托管：部署 Rook-Ceph RGW，开启 bucket lifecycle、versioning/retention 策略。
- 高级阶段：per-tenant encryption context、多区域复制、asset lineage manifest。

Acceptance checks:
- Tenant A 的 scoped credential 不能列出 Tenant B prefix。
- Worker 传入任意 output_prefix 会被 Job Manager 覆盖或拒绝。
- 删除 asset 不破坏审计 manifest 和 usage record。

Sources:
- [Rook storage architecture](https://rook.github.io/docs/rook/latest/Getting-Started/storage-architecture/)
- [Rook Ceph object storage](https://rook.github.io/docs/rook/latest/Storage-Configuration/Object-Storage-RGW/object-storage/)
- [Ceph Object Gateway](https://docs.ceph.com/en/latest/radosgw/)
- [MinIO container docs](https://min.io/docs/minio/container/index.html)

### 14. Observability / Audit

Recommended: **OpenTelemetry + Grafana LGTM stack**
HTML: [14-observability-audit.html](open-source-architecture/14-observability-audit.html)

Role: 统一采集 traces、metrics、logs、events，把 run_id/tool_call_id/job_id/tenant_id 串起来；审计日志另做 append-only 存储。

Why:
- Hermes 的故障排查横跨 Edge、Session、Sandbox、TokenRouter、Temporal、NATS、GPU worker、object storage。OpenTelemetry 是跨语言、跨后端的最低耦合采集标准。
- Grafana LGTM 组合覆盖 metrics、logs、traces 和 dashboard，开源可自托管，适合早期先跑通。
- 审计不能只依赖普通日志：普通日志可以采样/过期，audit_events 必须可查询、可保留、可导出。

Alternatives:
- **ELK/OpenSearch**: 日志检索强；不是主选：traces/metrics 一体化和 OTel-first 体验不如 LGTM 直观
- **Jaeger + Prometheus only**: 轻量、CNCF 成熟；不是主选：日志和全栈关联需要额外拼装
- **Datadog/New Relic**: 托管体验好；不是主选：不是开源优先，且租户/提示/文件相关数据要额外合规治理

Implementation plan:
- MVP：OTel SDK + Collector + Prometheus/Grafana + structured logs。
- 第二阶段：Tempo traces、Loki logs、exemplars linking、tenant cost dashboards。
- 第三阶段：audit export、SLO burn alerts、redaction regression tests。

Acceptance checks:
- 任意 failed media job 可从 job_id 追到 TokenRouter decision 和 worker log。
- 日志扫描不能发现 provider key 或 protected skill path 内容。
- 每次 tool_call touching infrastructure 都有 audit_events 记录。

Sources:
- [OpenTelemetry overview](https://opentelemetry.io/docs/what-is-opentelemetry/)
- [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/)
- [Grafana Stack](https://grafana.com/about/grafana-stack/)

### 15. Service Mesh / Internal mTLS / Egress

Recommended: **Istio**
HTML: [15-service-mesh-egress.html](open-source-architecture/15-service-mesh-egress.html)

Role: 在内部服务之间提供 mTLS、服务身份、授权策略和受控 egress gateway，支撑 TokenRouter/Job Manager/worker 的零信任通信。

Why:
- Hermes 需要明确区分服务身份、用户身份和 sandbox token。Istio 的 mTLS、AuthorizationPolicy、egress gateway 能把服务到服务通信也纳入策略边界。
- Envoy Gateway 已经在入口使用 Envoy，Istio 数据面同样基于 Envoy，便于统一观测和策略思路。
- Linkerd 更轻，但复杂 egress、ext_authz 和细粒度授权场景不如 Istio 完整。

Alternatives:
- **Linkerd**: 简单、轻量、mTLS 易用；不是主选：复杂 egress gateway、Envoy 过滤器生态和策略扩展性较弱
- **Cilium Service Mesh**: eBPF 网络能力强；不是主选：团队需要承担 Cilium 网络模型学习成本；和 Envoy Gateway/Istio 生态重叠要取舍
- **no mesh / library mTLS**: 少一层复杂度；不是主选：服务身份、默认拒绝、egress 统一治理会散落在应用里

Implementation plan:
- MVP：仅控制面服务启用 Istio mTLS，不把 sandbox 先放进 mesh。
- 第二阶段：egress gateway + allowlist + provider endpoint audit。
- 第三阶段：namespace default-deny、workload identity policy、mesh chaos tests。

Acceptance checks:
- 未注入 mesh identity 的服务不能调用 TokenRouter。
- Worker 不能直接访问 OpenBao 或数据库。
- 外部 egress 目标不在 allowlist 时被网格拒绝并记录。

Sources:
- [Istio authentication policy](https://istio.io/latest/docs/tasks/security/authentication/authn-policy/)
- [Istio egress gateway with TLS origination](https://istio.io/latest/docs/tasks/traffic-management/egress/egress-gateway-tls-origination/)
- [Istio security best practices](https://istio.io/latest/docs/ops/best-practices/security/)

### 16. Cloud Deployment / GitOps / Secrets Delivery

Recommended: **Argo CD + External Secrets Operator**
HTML: [16-gitops-secrets-delivery.html](open-source-architecture/16-gitops-secrets-delivery.html)

Role: 把 Kubernetes desired state、策略 bundle、服务发布和 OpenBao/外部 secrets 到 K8s 的投递纳入可审计 GitOps 流程。

Why:
- Hermes 组件多、命名空间多、node pool 多，手工 kubectl 会迅速失控。Argo CD 用 Git 作为 desired state，并持续对比 live state。
- External Secrets Operator 把 OpenBao/secret backend 的值同步成 Kubernetes Secret，避免把明文 secret 放进 Git。
- Flux 也可行，但 Argo CD 的 UI、diff、sync wave 和多应用可视化更适合早期团队协作审阅。

Alternatives:
- **Flux CD**: GitOps 纯粹、轻量；不是主选：可视化和产品化审阅体验不如 Argo CD 直接
- **Helm-only/manual deploy**: 简单；不是主选：不能持续检测漂移，审计和回滚弱
- **Terraform for all Kubernetes resources**: 基础设施强；不是主选：应用持续交付和 runtime drift 管理不如 GitOps controller

Implementation plan:
- MVP：Argo CD 管理 dev/staging，ESO 同步非 provider master secret。
- 第二阶段：prod sync windows、manual approval、image updater 或 promotion PR。
- 第三阶段：drift alert、policy-as-code CI、disaster recovery bootstrap runbook。

Acceptance checks:
- 集群 live state 漂移会在 Argo CD 显示 OutOfSync。
- Git 仓库不包含明文 secret。
- 删除 ExternalSecret 后相关服务不会继续拿到过期高权限凭证。

Sources:
- [Argo CD documentation](https://argo-cd.readthedocs.io/en/stable/)
- [External Secrets Operator OpenBao provider](https://external-secrets.io/v0.18.2/provider/openbao/)


## Deliverables

- [HTML index](open-source-architecture/00-index.html)
- [Notion refresh HTML index](hermes-notion-update-index.html)
- [Notion refresh split index](hermes-notion-update-index.md)
- [Edge Gateway / Realtime Ingress](open-source-architecture/01-edge-gateway-realtime-ingress.html)
- [Identity / Tenant Access](open-source-architecture/02-identity-tenant-access.html)
- [Policy Engine / Authorization](open-source-architecture/03-policy-engine-authorization.html)
- [Sandbox Runtime / Isolation](open-source-architecture/04-sandbox-runtime-isolation.html)
- [Workspace Volume Mounter / POSIX Data Isolation](open-source-architecture/05-workspace-volume-mounter.html)
- [TokenRouter / Secrets Boundary](open-source-architecture/06-tokenrouter-secrets-boundary.html)
- [Session / Job Orchestration](open-source-architecture/07-durable-session-job-orchestration.html)
- [Event Bus / Realtime Fanout](open-source-architecture/08-event-bus-realtime-fanout.html)
- [GPU Render Scheduling / Supercomputer Fabric](open-source-architecture/09-gpu-render-scheduling.html)
- [Skill Workflow Runtime / Registry](open-source-architecture/10-skill-workflow-runtime.html)
- [Guardrails / Exfiltration Defense](open-source-architecture/11-guardrails-exfiltration-defense.html)
- [Relational Data / Tenant Row Isolation](open-source-architecture/12-relational-data-tenant-rls.html)
- [Object / Media Storage](open-source-architecture/13-object-media-storage.html)
- [Observability / Audit](open-source-architecture/14-observability-audit.html)
- [Service Mesh / Internal mTLS / Egress](open-source-architecture/15-service-mesh-egress.html)
- [Cloud Deployment / GitOps / Secrets Delivery](open-source-architecture/16-gitops-secrets-delivery.html)
- [Hermes references knowledge model](hermes-references-knowledge-model.md)
- [Hermes TokenRouter credential flow](hermes-tokenrouter-credential-flow.md)
- [Hermes CometAPI media gateway](hermes-cometapi-media-gateway.md)
- [Hermes Soul ID and Element asset model](hermes-soulid-element-asset-model.md)
- [Hermes Soul ID reproduction and test plan](hermes-soulid-reproduction-and-test-plan.md)
- [Hermes tool contracts from Notion](hermes-tool-contracts-from-notion.md)
