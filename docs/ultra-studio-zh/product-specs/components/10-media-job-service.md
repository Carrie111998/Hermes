# 10 媒体任务服务（Media Job Service）

状态：部分实现（partial）—— 同步图像/视频生成工具、提供商注册表及 Atlas 提交/轮询客户端已实现；P0 本地 `ultra_media_job_create/status/finalize`、SQLite `media_jobs` / `assets` / `media_events` 已实现；取消、重试、后台轮询、网关事件流和完整 Asset Service 仍未实现。
日期：2026-06-29

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
  `fal`/`xai`/`openai` 提供商插件；P0 本地作业层：
  `agent/ultra_media_store.py`、`tools/ultra_media_job_tool.py`、
  `tests/tools/test_ultra_media_job_tool.py`

## 目的与范围

Media Job Service 将图像/视频/音频生成作为持久的、与提供商无关的作业运行。合约规则："提供商 API 不应直接向智能体暴露原始接口。请使用与提供商无关的作业信封"
（`03-media-asset-contract.md` §Media Job Envelope）。媒体作业的生命周期可能超过 websocket 重连、浏览器刷新或工作进程重启
（`02-agent-runtime-contract.md`）。

范围：作业创建/状态/取消/重试/完成、MediaJob 信封、提供商适配器、轮询、输出生成交付至 Asset Service，以及作业事件。模型/约束元数据见
`19-model-catalog-provider-constraints.md`；提示负载构造见
`13-prompt-compiler.md`；凭证策略见 `17-tokenrouter.md`。

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
| 已实现（Implemented） | P0 本地 MediaJob 记录（`job_id`、`session_id`、`run_id`、`tool_call_id`、provider/model、状态、错误、输出） | `agent/ultra_media_store.py` |
| 部分实现（Partial） | `ultra_media_job_create / status / finalize` 工具组 | `tools/ultra_media_job_tool.py`；`cancel` / `retry` 未实现 |
| 部分实现（Partial） | `media_job.created` / `media_job.updated` / `media_job.failed` / `asset.ready` 事件记录 | `agent/ultra_media_store.py` 的 `media_events` 表；尚未接入 gateway/UI event stream |
| 部分实现（Partial） | 将生成输出注册为本地 asset（`finalize` → `assets` 行 + lineage metadata） | `agent/ultra_media_store.py`；这不是完整 Asset Service |
| 已规定，未构建（Specified, not built） | 作业在工作进程/会话中断后存活 | `06-delivery-plan.md` P2 门控 |
| 缺口（Gap） | 各提供商的取消/重试语义（哪些 Atlas 路由支持取消） | 未指定 |

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
| 支持状态查询的持久作业记录 | 已实现（Implemented）（本地 SQLite `media_jobs` + `ultra_media_job_status`） |
| 取消队列中/运行中的作业 | 已规划（Planned）（`ultra_media_job_cancel`） |
| 使用编译修复计划进行重试 | 已规划（Planned）（`ultra_media_job_retry`；与 `prompt-repair` 技能配对） |
| 完成：将输出注册为带有来源链路的资产 | 部分实现（Partial）（`ultra_media_job_finalize` 写本地 `assets`；缩略图/完整 Asset Service 未实现） |
| 向 UI 发送作业事件 | 已规划（Planned）（当前只写本地 `media_events`） |
| 信封中的种子捕获和可复现参数 | 已规划（Planned）（信封字段存在于规格中；并非所有提供商都返回种子） |
| TokenRouter 决策关联（`tokenrouter_decision_id`） | 已规划（Planned）；依赖于 `17-tokenrouter.md` |

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

已实现（智能体工具层面）：`ultra_media_job_create`、
`ultra_media_job_status`、`ultra_media_job_finalize` 包装当前
`image_generate` / `video_generate`，并写入本地 SQLite。底层
`generate_video` / 图像生成工具
通过工具注册表注册（`model_tools.py` 分发）；通过
`plugins/*/atlas/client.py` `submit`/`poll` 对 Atlas API 进行提供商调用
（`ATLAS_API_KEY` 通过 `resolve_credentials` 在服务端）。

工具组状态（来自 `03-media-asset-contract.md` §Required Job Tools）：

| 工具 | 目的 |
|---|---|
| `ultra_media_job_create` | 已实现：创建本地 MediaJob，调用当前图像/视频 provider，可自动 finalize。 |
| `ultra_media_job_status` | 已实现：返回持久作业、输出资产、错误和本地事件。 |
| `ultra_media_job_cancel` | 未实现。 |
| `ultra_media_job_retry` | 未实现。 |
| `ultra_media_job_finalize` | 部分实现：将输出注册为本地资产和来源链路；缩略图/完整资产服务未实现。 |
| `ultra_media_constraints_get` | 未实现。 |

计划中事件：`media_job.created`、`media_job.updated`，然后是 `asset.ready`
（`02-agent-runtime-contract.md` §Event Stream）。在持久作业落地前，
作业进度通过 `tool.progress` 传递给 UI。

## 数据模型

已实现：无持久化 —— 作业状态在工具调用的内存流中
仅存在于轮次持续期间。

计划中：MediaJob 信封（原文字段，
`03-media-asset-contract.md`）：

```yaml
MediaJob:
  job_id:
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
  negative_prompt:
  provider_constraints:
  seed:
  tokenrouter_decision_id:
  output_assets:
  error:
```

边界说明：`hermes-asset-library-backend-design.md` 在 Asset Service 侧定义了
`generation_jobs` 表；将此表与此信封（单一写入者 + 事件镜像）协调
是与 `09-asset-service.md` 共享的开放问题。

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
- 一旦持久作业落地：浏览器在作业中途刷新保留作业；
  `ultra_media_job_status` 返回与 UI 展示的相同状态。
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

1. 持久化作业存储位置：网关侧数据库 vs Asset Service
   `generation_jobs` —— 谁是单一写入者？
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
