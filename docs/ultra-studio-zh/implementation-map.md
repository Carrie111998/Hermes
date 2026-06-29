# Ultra Studio 当前实现地图

状态：当前代码事实 + P0 缺口清单  
日期：2026-06-29

## 目的

这份文档把 Ultra Studio / Hermes Agent 的现有代码、已有规格和下一步缺口放到同一张地图里。它不是新的目标架构；目标架构仍以
[设计主线](00-design-spine) 为准。这里回答三个落地问题：

- 当前仓库里哪些能力已经有真实代码。
- 哪些能力只有相邻机制，还没有接成 Ultra Studio 产品闭环。
- 下一步应该先补哪几个连接点，才能跑通 P0。

状态词沿用中文文档入口的规则：

| 状态 | 含义 |
|---|---|
| implemented | 仓库里有真实代码或可运行路径。 |
| partial | 有相邻机制或原型，但没有接到 Ultra Studio runtime。 |
| spec-only | 只有设计文档，没有运行时代码。 |
| external | 不应继续放在 Hermes core；属于 Ultra Studio 产品层或独立基础服务。 |
| stale-doc-risk | 文档仍引用已经迁走或不存在的路径，需要更新。 |

## 当前真实链路

当前仓库能证明的是 Hermes runtime 基座和部分创意能力，不是完整 Ultra Studio 产品：

```text
Browser / Hermes web
  -> dashboard chat upload / gateway websocket
  -> Hermes session and agent runtime
  -> skill discovery / creative allowlist helpers
  -> image/video tool registry
  -> Atlas image/video provider adapters
  -> provider output returned to the chat/tool call
```

P0 目标链路还没有完整闭合：

```text
User Message
  -> Product UI
  -> Gateway / Session
  -> Workflow Router
  -> Prompt Compiler
  -> Policy Preflight
  -> Media Job Service
  -> Atlas provider
  -> Asset Service
  -> Event Stream
  -> Inspector / History / Reuse
```

缺口主要在中后段：结构化 workflow intent、持久 MediaJob、资产注册、统一事件、审计/用量/lineage。

## 实现地图

| 能力 | 当前状态 | 代码证据 | 文档证据 | 下一步 |
|---|---|---|---|---|
| Hermes Web Shell | implemented | `web/src/App.tsx`、`web/src/pages/ChatPage.tsx` | [02 创作聊天界面](product-specs/components/02-creative-chat-ui) | 保持作为 runtime 基座，不把它误写成完整 Ultra Studio 产品。 |
| Ultra Studio Legacy Chat Page | stale-doc-risk | `web/src/pages/UltraStudioChatPage.tsx` 明确标注未被 `web/src/App.tsx` 引用 | [08 迁移](infra-design/08-hermes-fork-isolation-migration) | 文档里标为 legacy/migration source，不再当当前入口。 |
| Standalone Panel / BFF | stale-doc-risk / external | 当前仓库没有 `standalone-chat-panel/` 目录 | `permission-sandbox-audit-implementation.md`、[08 迁移](infra-design/08-hermes-fork-isolation-migration) 仍有旧引用 | 更新旧文档；产品 BFF 应放 Ultra Studio 产品层。 |
| Gateway Chat / Session | partial | `web/src/hooks/useGatewayChat.ts`、`web/src/lib/gatewayClient.ts`、`gateway/api_server_*.py` | [Agent 运行时合约](product-specs/02-agent-runtime-contract) | 统一 Hermes gateway events 与 Ultra media/job/asset event envelope。 |
| Chat Upload | partial | `web/src/lib/chatUpload.ts`、`hermes_cli/dashboard_uploads.py` | [03 媒体与资产合约](product-specs/03-media-asset-contract) | 从聊天附件升级为类型化 `media_input` 资产。 |
| Principal Headers / Session Scope | implemented for gateway boundary | `gateway/principal_headers.py`、`gateway/session_scope_store.py` | [权限边界](permission-boundary-design) | 接到产品 BFF 后保持 fail-closed；不要让浏览器直接伪造 principal。 |
| Local Policy Checker | partial | `agent/ultra_security.py` | [权限边界](permission-boundary-design)、[17 TokenRouter](product-specs/components/17-tokenrouter) | P0 可先用同形接口；TokenRouter 不在 Hermes core 内补完整服务。 |
| Creative Skill Allowlist | implemented helper | `hermes_cli/ultra_studio_skills.py`、`hermes_cli/subcommands/skills.py` | [11 技能注册表](product-specs/components/11-skill-registry) | 接到 profile/runtime，使默认 Ultra 模式只暴露聚焦技能。 |
| workflow-router skill | partial | `skills/creative/workflow-router/SKILL.md` | [12 工作流路由器](product-specs/components/12-workflow-router) | 产出稳定结构化 handoff，不只停留在 skill 文本。 |
| media-qa / prompt-repair | partial | `skills/creative/media-qa/SKILL.md`、`skills/creative/prompt-repair/SKILL.md` | [04 技能、工具与提示词合约](product-specs/04-skill-tool-prompt-contract) | 接到失败作业和 Inspector，而不是只作为手动技能。 |
| Prompt Compiler | spec-only | 未发现稳定 runtime 编译器 | [13 提示词编译器](product-specs/components/13-prompt-compiler) | 把 workflow handoff 编译为工具参数，禁止自然语言裸传资产 ID。 |
| Atlas Image Provider | implemented provider | `plugins/image_gen/atlas/__init__.py`、`plugins/image_gen/atlas/client.py` | [10 媒体任务服务](product-specs/components/10-media-job-service) | 包进 MediaJob 信封，输出交给 Asset Service finalize。 |
| Atlas Video Provider | implemented provider | `plugins/video_gen/atlas/__init__.py`、`plugins/video_gen/atlas/client.py` | [10 媒体任务服务](product-specs/components/10-media-job-service) | 保留真实轮询和错误；不要把 poll/status URL 当输出 URL。 |
| Media Job Service | spec-only with provider pieces | provider submit/poll 已有；无 `ultra_media_job_*` 持久工具组 | [10 媒体任务服务](product-specs/components/10-media-job-service) | 下一步 P0 优先补 `create/status/finalize` 最小信封。 |
| Asset Service | spec-only | 当前上传仅是聊天附件；无资产表、资产 API 或来源链路端点 | [09 资产服务](product-specs/components/09-asset-service) | 补最小 `assets`/`asset_lineage`/download path，使输出可 inspect/reuse。 |
| Inspector / Live Panel | partial | `web/src/components/chat/ChatInspector.tsx`、`ToolCall.tsx`、`PendingPromptPanel.tsx` | [03 检查器 / 实时面板](product-specs/components/03-inspector-live-panel) | 显示 job_id、asset_id、provider/model、输入/输出和 typed errors。 |
| Approval Gateway | partial | chat pending prompt / approval 机制存在 | [15 人工审批网关](product-specs/components/15-human-approval-gateway) | 给高风险或高成本媒体操作增加 durable decision record。 |
| Audit / Provenance Ledger | spec-only | 有 API server audit 相邻代码，但未串起 session/run/tool/job/asset/usage | [16 观察与溯源账本](product-specs/components/16-observation-provenance-ledger) | P0 至少写入 run/tool/job/asset 关联字段。 |
| TokenRouter / Usage Accounting | external | Hermes repo内为 spec；用量闭环属于 Ultra Studio foundation/control plane | [17 TokenRouter](product-specs/components/17-tokenrouter) | 通过边界集成，不在 Hermes runtime 里复制产品控制面。 |

## 现在还差什么

按 P0 顺序看，剩余工作不是再补更多页面，而是补中间的权威状态源：

1. 入口边界：明确当前产品入口是 Hermes web `/chat` 的临时基座，还是外部 Ultra Studio BFF。旧 `standalone-chat-panel` 文档必须降级为迁移记录。
2. 结构化路由：让 `workflow-router` 输出可测试的 intent/handoff，包含 media type、mode、input asset roles、缺字段和下一步工具。
3. Prompt Compiler：把 handoff 编译成 provider-neutral 工具参数，资产引用必须是结构化字段。
4. MediaJob 最小信封：增加 `job_id/session_id/run_id/tool_call_id/provider/model/status/error/output`，先支持 create/status/finalize。
5. Asset 最小记录：上传和生成输出都要产生 `asset_id`、download path、mime、status、lineage。
6. 事件统一：UI 不能只看到工具文本流；需要 `media_job.*` 和 `asset.*` 事件投影。
7. Inspector 绑定：右侧面板按 `job_id/asset_id` 展示真实提供商、模型、输入、输出、错误、来源链路。
8. 审计和用量 seam：P0 至少保留 `policy_decision_id/tokenrouter_decision_id/usage_event_id` 字段，完整服务留在 Ultra 控制面。

## 不要继续误判的点

- `docs/ultra-studio-zh/README.md` 和 [设计主线](00-design-spine) 已经能解释目标链路，但它们不是当前实现验收表。
- [10 媒体任务服务](product-specs/components/10-media-job-service) 已经诚实标出持久 MediaJob 和 `ultra_media_job_*` 是 spec-only。
- [09 资产服务](product-specs/components/09-asset-service) 已经诚实标出资产服务是 spec-only。
- `web/src/pages/UltraStudioChatPage.tsx` 是迁移遗留页，不是当前路由入口。
- `standalone-chat-panel/` 在当前仓库不存在；引用它的文档需要更新为历史/外部边界。
- TokenRouter、Asset Service、Media Job Service、worker/orchestration 不应该默认塞回 Hermes core。Hermes 是 runtime 基座，Ultra Studio 产品层和基础服务应独立。

## 推荐下一步

第一步先补 P0 的最小持久状态，而不是继续扩写大架构：

```text
chat upload
  -> media_input asset
  -> workflow-router handoff
  -> ultra_media_job_create
  -> Atlas provider
  -> ultra_media_job_finalize
  -> output asset
  -> inspector/history
```

建议拆成两个连续 PR：

1. 文档和入口校准：更新 stale panel/legacy page 说明，保证文档站从本页能跳到每个实现证据。
2. P0 runtime slice：实现最小 MediaJob + Asset Registry，把 Atlas 输出注册为可检查、可下载、可复用的资产。

完成这两步后，再接 TokenRouter/usage/audit 才不会变成没有状态源的控制面。

## 验证命令

文档变更后至少运行：

```bash
cd docs/ultra-studio-zh
npm run docs:build
```

运行时代码变更后再加：

```bash
pytest
npm --prefix web run typecheck
```
