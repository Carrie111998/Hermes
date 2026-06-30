# 10 媒体任务服务（Media Job Service）

状态：外部服务边界（external）—— Hermes 内只保留图像/视频 provider adapter 和工具注册基础；MediaJob 权威状态不在 Hermes runtime 内实现。当前 workspace 中的独立服务位于 `/Users/lifcc/Desktop/code/work/infra/ultrastudio-foundation/ultrastudio-media-job-service`，已提供 provider-neutral job envelope、状态读取、状态流转、TokenRouter 决策和队列发布；Asset Service finalize、后台 worker 轮询和产品 UI 事件流仍待接入。
日期：2026-06-30

来源：

- 文档：`docs/ultra-studio-product-specs/03-media-asset-contract.md`
  （§Media Job Envelope、§Required Job Tools、§Asset Lifecycle、§QA），
  `02-agent-runtime-contract.md`（§Event Stream、§Error Contract），
  `04-skill-tool-prompt-contract.md`（§Media Job Tools），
  `06-delivery-plan.md`（P0 项 5-7、Launch Gates），
  `docs/hermes-asset-library-backend-design.md`（§生成链路、
  `generation_jobs` 实体）
- 代码（本次会话已验证）：`tools/video_generation_tool.py`
  （`_handle_video_generate`、`_resolve_active_provider`、
  `check_video_generation_requirements`、`_format_model_caveats`、
  `_build_dynamic_video_schema`、`_normalize_reference_images`），
  `tools/image_generation_tool.py`、`agent/video_gen_provider.py`、
  `agent/video_gen_registry.py`、`agent/image_gen_provider.py`、
  `agent/image_gen_registry.py`、`agent/image_routing.py`、
  `plugins/video_gen/atlas/client.py`（`submit`、`poll`、`build_payload`、
  `extract_prediction_id`、`first_output_url`、`normalize_image_input`、
  `resolve_credentials`）、`plugins/video_gen/atlas/catalog.py`
  （`ATLAS_FAMILIES`）、`plugins/image_gen/atlas/`（`catalog.py`、
  `client.py`），以及 `plugins/image_gen/`、`plugins/video_gen/` 下的
  `fal`/`xai`/`openai` 提供商插件。
- 外部服务（本次会话已验证）：`/Users/lifcc/Desktop/code/work/infra/ultrastudio-foundation/ultrastudio-media-job-service/main.go`、
  `internal_response.go`、`main_test.go`，共享合约位于
  `/Users/lifcc/Desktop/code/work/infra/ultrastudio-foundation/ultrastudio-contracts/contracts.go`。

## 目的与范围

Media Job Service 将图像/视频/音频生成作为持久的、与提供商无关的作业运行。合约规则："提供商 API 不应直接向智能体暴露原始接口。请使用与提供商无关的作业信封"
（`03-media-asset-contract.md` §Media Job Envelope）。媒体作业的生命周期可能超过 websocket 重连、浏览器刷新或工作进程重启
（`02-agent-runtime-contract.md`）。

范围：作业创建/状态/取消/重试/完成、MediaJob 信封、提供商适配器、轮询、输出生成交付至 Asset Service，以及作业事件。模型/约束元数据见
`19-model-catalog-provider-constraints.md`；提示负载构造见
`13-prompt-compiler.md`；凭证策略见 `17-tokenrouter.md`。

边界规则：MediaJob、Asset、TokenRouter、usage/audit 是 Ultra Studio 产品基础服务，不能再放入 Hermes core。Hermes、Codex 或其他 runtime 只能通过薄 adapter 调用这些服务。

## 实现状态

| 状态 | 项目 | 引用 |
|---|---|---|
| 已实现（Implemented） | 支持配置驱动提供商/模型解析的智能体视频生成工具 | `tools/video_generation_tool.py`（`_read_configured_video_provider`、`_resolve_active_provider`、`_handle_video_generate`） |
| 已实现（Implemented） | 支持路由的智能体图像生成工具 | `tools/image_generation_tool.py`、`agent/image_routing.py` |
| 已实现（Implemented） | 提供商注册表层（Atlas、FAL、xAI、OpenAI），位于提供商 ABC 之后 | `agent/video_gen_registry.py`、`agent/image_gen_registry.py`、`plugins/video_gen/`、`plugins/image_gen/` |
| 已实现（Implemented） | Atlas 异步提交 + 轮询，支持预测 ID 提取与输出 URL 区分（轮询 URL 并非输出） | `plugins/video_gen/atlas/client.py`（`submit`、`poll`、`extract_prediction_id`、`first_output_url`、`_looks_like_media_output`） |
| 已实现（Implemented） | 服务端凭证解析（提示/UI 中无密钥） | `plugins/video_gen/atlas/client.py`（`resolve_credentials`）；启动门控 "Atlas credential path is explicit"（`06-delivery-plan.md`） |
| 已实现（Implemented） | 参考图像归一化（本地文件 → 数据 URI） | `plugins/video_gen/atlas/client.py`（`normalize_image_input`、`_image_to_data_uri`） |
| 已实现（Implemented） | 将每模型约束展示到工具 schema 和注意事项中 | `tools/video_generation_tool.py`（`_build_dynamic_video_schema`、`_format_model_caveats`）、`plugins/video_gen/atlas/catalog.py`（`ATLAS_FAMILIES` 时长/分辨率/音频） |
| 外部已实现（External implemented） | provider-neutral MediaJob 记录（job/context/provider/model/status/input/output/tokenrouter 字段） | `ultrastudio-foundation/ultrastudio-media-job-service` + `ultrastudio-contracts` |
| 外部已实现（External implemented） | `POST /v1/jobs`、`GET /v1/jobs`、`GET /v1/jobs/{id}`、`GET /v1/jobs/{id}/status` | `ultrastudio-media-job-service/main.go` |
| 外部已实现（External implemented） | `running/complete/fail/block/cancel/retry` 状态流转端点 | `ultrastudio-media-job-service/main.go` |
| 外部已实现（External implemented） | 创建时调用 TokenRouter `POST /v1/decisions`，保存 `tokenrouter_decision_id` 和 scoped token，默认响应擦除 scoped token | `decide`、`responseJob` |
| 外部已实现（External implemented） | 提交成功后发布队列消息，供 Worker Orchestrator 消费 | `enqueueJob` |
| Hermes 未实现（Not in Hermes） | `ultra_media_job_create/status/finalize` 不能在 Hermes core 中持久化作业；未来只允许薄 adapter 调外部服务 | 本次修正已移除 Hermes 本地实现 |
| 已规定，未构建（Specified, not built） | 输出注册到完整 Asset Service（缩略图、lineage、ACL、audit） | `09-asset-service.md` |
| 缺口（Gap） | 各提供商真实取消/重试语义（哪些 Atlas 路由支持 provider-side cancel） | 未指定 |

## 用户入口点

- 由 `workflow-router` 路由到媒体作业路径的聊天生成请求
  （`12-workflow-router.md`；当前：直接工具调用
  `tools/image_generation_tool.py` / `tools/video_generation_tool.py`）。
- 在失败作业上的 Inspector 重试/修复操作（计划中，
  `03-inspector-live-panel.md`）。
- 任务恢复重新连接活动作业（计划中，
  `07-tasks-session-history.md`）。
- 无直接面向用户的 API；所有内容均通过智能体工具和网关事件流转。

## 功能列表

| 功能 | 状态 |
|---|---|
| 通过 Atlas 模型路由进行文生视频和图生视频 | 已实现（Implemented）（`ATLAS_FAMILIES` `text_model`/`image_model` 路由） |
| 通过 Atlas/FAL/xAI/OpenAI 提供商进行图像生成 | 已实现（Implemented）（提供商插件 + 注册表） |
| 参考图像输入（上传 → 数据 URI） | 已实现（Implemented）（`normalize_image_input`） |
| 基于配置的提供商/模型选择及可用性检查 | 已实现（Implemented）（`check_video_generation_requirements`、`_resolve_active_provider`） |
| 向智能体展示模型注意事项消息（时长、分辨率、音频） | 已实现（Implemented）（`_format_model_caveats`） |
| 对提供商预测进行异步轮询 | 已实现（Implemented）（同轮次 `poll`）；跨轮次持久轮询计划中 |
| 支持状态查询的持久作业记录 | 外部已实现（External implemented）（`ultrastudio-media-job-service` JSON store + API） |
| 取消队列中/运行中的作业 | 外部状态端点已实现；provider-side cancel 能力矩阵仍待补 |
| 使用编译修复计划进行重试 | 外部状态端点已实现；Prompt Compiler repair plan 接入仍待补 |
| 完成：将输出注册为带有来源链路的资产 | 已规划（Planned）（必须由外部 Asset Service finalize，不在 Hermes 本地写 assets） |
| 向 UI 发送作业事件 | 已规划（Planned）（由产品层订阅 MediaJob/Asset 服务事件） |
| 信封中的种子捕获和可复现参数 | 已规划（Planned）（信封字段存在于规格中；并非所有提供商都返回种子） |
| TokenRouter 决策关联（`tokenrouter_decision_id`） | 外部已实现（External implemented）；完整用量闭环仍依赖 `17-tokenrouter.md` |

## 状态机

作业生命周期（`03-media-asset-contract.md`）：

```text
job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
job.created | job.running -> job.failed
job.created | job.running -> job.canceled        (where provider supports)
job.running -> job.timeout                       (maps to `job_timeout` error)
```

- `created -> running`：提供商接受提交
  （当前 `extract_prediction_id` 返回 ID）。
- `running -> succeeded`：轮询返回真实媒体输出 URL —— 轮询
  或状态 URL 绝不能被视为输出
  （`first_output_url` / `_looks_like_media_output` 编码此规则）。
- `succeeded -> asset.*`：finalize 交付给 Asset Service；直到
  `asset.ready` 之前，作业对 UI 而言未"完成"。
- `failed` 保留完整提供商错误并保持可检查状态
  （`03-media-asset-contract.md` §Acceptance）。
- 重试创建与旧作业关联的新作业；绝不修改失败记录。

## API 与事件

Hermes 内没有 `ultra_media_job_*` 权威工具实现。未来若 Hermes 作为
runtime 使用，只允许提供薄 adapter：把工具调用转发给外部
Media Job Service，并把服务返回的 job/asset/error 映射回 agent/UI。若
runtime 换成 Codex，同一套外部服务 API 应保持不变。

外部 Media Job Service 当前 API：

```http
POST /v1/jobs
GET  /v1/jobs?status=&include_internal=
GET  /v1/jobs/{id}
GET  /v1/jobs/{id}/status
POST /v1/jobs/{id}/running
POST /v1/jobs/{id}/complete
POST /v1/jobs/{id}/fail
POST /v1/jobs/{id}/block
POST /v1/jobs/{id}/cancel
POST /v1/jobs/{id}/retry
```

底层 `generate_video` / 图像生成 provider adapter 仍通过 Hermes 工具注册表注册（`model_tools.py` 分发）；通过
`plugins/*/atlas/client.py` `submit`/`poll` 对 Atlas API 进行提供商调用
（`ATLAS_API_KEY` 通过 `resolve_credentials` 在服务端）。这些 provider adapter 不是 MediaJob 状态源。

工具组状态（来自 `03-media-asset-contract.md` §Required Job Tools）：

| 工具 | 目的 |
|---|---|
| `ultra_media_job_create` | 外部服务 API 已有创建端点；Hermes adapter 未接入。 |
| `ultra_media_job_status` | 外部服务 API 已有状态端点；Hermes adapter 未接入。 |
| `ultra_media_job_cancel` | 外部服务 API 已有状态流转端点；provider-side cancel 能力矩阵未补。 |
| `ultra_media_job_retry` | 外部服务 API 已有状态流转端点；Prompt Compiler repair plan 未接入。 |
| `ultra_media_job_finalize` | 必须由外部 Asset Service 完成；Hermes 不写本地资产表。 |
| `ultra_media_constraints_get` | 未实现。 |

计划中事件：`media_job.created`、`media_job.updated`，然后是 `asset.ready`
（`02-agent-runtime-contract.md` §Event Stream）。事件源应来自外部服务或产品 BFF 投影，不来自 Hermes 本地账本。

## 数据模型

外部合约已在 `ultrastudio-contracts` 定义，核心字段包括：

```yaml
MediaJob:
  job_id:
  route_id:
  context:
  provider:
  model:
  media_type:
  mode:
  status:
  input_asset_refs:
  prompt:
  tokenrouter_decision_id:
  estimate_units:
  output_refs:
  error:
  created_at:
  updated_at:
```

边界说明：`hermes-asset-library-backend-design.md` 在 Asset Service 侧定义了
`generation_jobs` 表。当前决策是 Media Job Service 拥有 job 状态；
Asset Service 可保存 output/lineage 和可重建镜像，避免双写权威状态。

## UI 行为

（服务义务；渲染规范位于 `02-creative-chat-ui.md` 和
`03-inspector-live-panel.md`。）

- 每次作业提交产生一个作业卡片数据：ID、提供商/模型、
  media_type、状态、进度。
- 状态更新为推送式，而非浏览器轮询；刷新从持久状态重新水合
  （计划中）。
- 失败作业暴露类型化错误和提供商错误类别，供检查器制定修复计划。
- 输出仅在资产注册后渲染（`asset.ready`），防止虚假完成声明 —— 
  "智能体不能在没有事件、工件或分类账记录的情况下声称完成"
  （`00-index.md` §Top-Level Acceptance）。

## 权限与错误处理

作业在会话的用户/工作空间/项目范围下执行；输入资产引用必须通过
Asset Service 验证，且（如存在）在提交前通过 TokenRouter
权限/配额检查（`hermes-asset-library-backend-design.md`
§生成链路）。

类型化错误（来自 `02-agent-runtime-contract.md` §Error Contract）：
`missing_credential`、`unsupported_model_capability`、`invalid_asset_ref`、
`provider_rejected_input`、`quota_exceeded`、`job_timeout`、
`asset_upload_failed`。

当前已实现：缺失提供商/凭证产生显式工具错误
（`_missing_provider_error`、`check_video_generation_requirements`）；
Atlas 轮询错误向智能体暴露，而非伪装成功。

硬性规则（启动门控，`06-delivery-plan.md`）：无虚假媒体 URL、无硬编码作业结果、
无意外 FAL/Comfy 回退 —— 提供商切换由配置决定，绝非静默运行时回退。

## 验收标准

- 清晰的视频请求创建真实提供商作业，并返回真实输出或类型化阻断器
  （`04-skill-tool-prompt-contract.md` §Acceptance）。
- 轮询 URL 绝不会被注册为输出（由 `first_output_url` 行为进行回归防护）。
- 一旦外部 Media Job Service 接入产品层：浏览器在作业中途刷新保留作业；
  runtime adapter 的 `ultra_media_job_status` 返回与 UI 展示的相同状态。
- `finalize` 生成带有来源链路的资产 ID，关联作业、输入、模型、
  提示哈希、种子（`03-media-asset-contract.md` §来源链路（Lineage））。
- 失败作业保持可检查状态，提供商错误类别完整保留。
- 在支持的路由上取消会过渡到 `canceled`，无幻影输出。
- 任何代码路径都不会构造非由提供商返回的媒体 URL。

## 非目标

- 注册后拥有资产状态（由 Asset Service 拥有）。
- 提供商凭证存储或交换（TokenRouter 范围）。
- 提示构造逻辑（Prompt Compiler 范围）—— 作业服务接收已编译的负载。
- 媒体预处理/修剪管道（CometAPI 范围，
  `18-cometapi-media-gateway.md`）。
- 向智能体或 UI 暴露原始提供商仪表板或 API 形态。

## 待解决问题

1. Asset Service 是否保存 `generation_jobs` 可重建镜像，还是只保存
   output asset lineage；Media Job Service 是 job 状态的单一写入者。
2. 作业持久化后的轮询所有权：网关后台工作进程 vs
   每会话恢复轮询；工作进程重启时，进行中的轮询会发生什么？
3. 各 Atlas 路由的取消支持矩阵（`ATLAS_FAMILIES` 中的 Wan/Seedance/Kling 家族）
   未验证。
4. 种子可用性：哪些提供商返回种子，`seed` 是可复现性承诺所必需
   还是尽力而为？
5. 从当前 `generate_video`/图像工具合约映射到
   `ultra_media_job_create`：仓库惯例禁止重命名并保留别名 —— 
   一次性迁移工具名称，还是将当前名称作为 `ultra_*` 合约的实现保留？
6. TokenRouter 存在前各提供商/工作空间的并发限制 —— 
   P0 采用配置级上限还是无限制？
7. 音频作业（资产合约中存在 `audio_job` 类型）：哪个提供商首先支持，
   TTS（`tools/tts_tool.py`、`agent/tts_provider.py`）是并入此服务
   还是保持独立？
