# Hermes 真实资产库产品 PRD

Status: draft for implementation planning
Date: 2026-06-12
Owner: Hermes / Ultra Studio

## 1. 一页结论

Hermes 资产库要做成“项目级创作资产系统”，不是普通图库。第一版必须先打通真实链路：

```text
上传/生成请求
  -> media_input / media_job
  -> asset record + object storage
  -> lineage / prompt context / ACL / audit
  -> Chat 与 Asset Library 可搜索、可复用、可 @
  -> 后续生成用结构化 asset_ref 传入
```

本 PRD 采用一个分层落地方案：

- P0 做真实入库闭环：上传素材、Atlas 图片/视频生成结果、job 状态、资产详情、Collection 基础管理、Chat `@` 选择和复用。
- P1 做资产库工作效率：Smart Group 创建器、详情完整上下文、可执行复用动作、批量操作、基础语义搜索。
- P2 做高级创作资产：Character / Element 完整生命周期、去重版本、推荐、计费用量、协作审计增强。

核心决策：

- 资产状态由 Asset Service 统一管理；Object Storage 只存二进制；Search Index 是可重建索引。
- Chat 里的 `@Luna` 不能只是文本匹配，必须变成结构化 mention token，并在提交前通过 ACL/status 校验。
- Atlas、FAL、OpenAI、xAI 等 provider 都包进 provider-neutral `MediaJob`，前端和 Agent 不直接处理 provider 私有返回。
- P0 存储基线采用 MySQL/GORM、多库实例预留、Redis 缓存/分布式锁、阿里云 OSS media/result/temp 三类 bucket。

## 2. 输入资料与对比结论

| 来源 | 已确认要求 | 对本 PRD 的影响 |
|---|---|---|
| 用户前面整理的 PDF P0/P1/P2 补项 | 真实资产入库、上传入口、Collection、Smart Group、角色创建、真实搜索、详情上下文、可执行动作、权限审计、真实预览 | 作为产品需求完整范围；按 P0/P1/P2 分期落地 |
| `docs/hermes-web-mvp-prd.md` | Web MVP 要证明真实 Agent、真实上传、真实 media job、TokenRouter 边界、资产复用；禁止 fake job | P0 必须先完成真实上传和生成结果入库 |
| `docs/ultra-studio-product-specs/03-media-asset-contract.md` | 每个上传/生成媒体都是 typed product object；MediaJob envelope 记录 provider/model/prompt/input/output/lineage | 定义本 PRD 的资产类型、job 模型和详情页字段 |
| `docs/ultra-studio-research-analysis/01-p0-mvp-vertical-slice.md` | P0 是窄真实创作闭环，先不做完整平台和完整 Character 生命周期 | 把 Character 训练、完整 Smart Group 语义能力放到 P1/P2，但 P0 预留入口 |
| `docs/hermes-asset-library-backend-design.md` | 已有资产服务、索引、Collection、Smart Group、mention、ACL、audit 后端设计 | 本 PRD 复用该设计，并补齐产品流程、前端交互和验收 |
| 当前代码 | `/api/chat/uploads` 只保存文件并返回 path/name/mime/size；`useGatewayChat` 支持附件和事件；Atlas provider 已有图片/视频客户端 | 第一阶段应扩展现有入口，不重建 Web Chat |

## 3. 目标与非目标

### 3.1 产品目标

1. 用户上传或生成的素材会自动进入资产库，并获得稳定 `asset_id`。
2. 用户能在 Chat 中通过 `@` 找到可用资产、合集、角色和元素，并把它们作为结构化引用传入后续生成。
3. 用户能在资产库里查看真实预览、来源、prompt、参数、模型、seed、lineage、权限和审计记录。
4. 用户能创建和管理 Collection，并把多选资产加入或移出 Collection。
5. 用户能把当前筛选保存为 Smart Group，并在后续动态评估。
6. 后端能拒绝未授权、已撤销、未 ready 的资产引用，避免 prompt 注入绕过权限。
7. 图片/视频生成走真实 job 状态，不出现 CSS 假图、静态假 job 或硬编码结果。

### 3.2 非目标

P0 不实现以下能力：

- 完整云多租户计费系统。
- 全量视觉 embedding 和高级相似图搜索。
- 公开资产市场或跨 workspace 分享。
- 多 provider 的复杂路由策略 UI。

P0 可以预留字段和接口，但不能在 UI 上展示“已 ready”的假能力。

## 4. 用户与核心场景

### 4.1 用户角色

| 角色 | 需求 |
|---|---|
| 创作者 | 上传参考图、生成图片/视频、复用角色/场景/产品素材 |
| 项目协作者 | 在同一 project 内查找、归档、加入合集、复用资产 |
| 管理者 | 控制 workspace/project 权限，追踪谁创建和复用了资产 |
| Agent / Skill | 根据用户意图搜索资产、选择合法引用、创建 media job |

### 4.2 关键用户故事

1. 作为创作者，我上传一张产品图后，它立即进入资产库，显示处理状态，ready 后可以被 `@` 引用。
2. 作为创作者，我在 Chat 输入“用 @Luna 做一个 5 秒天台夜景视频”，系统解析 `@Luna` 为结构化角色引用，并创建真实 video job。
3. 作为创作者，我选中 12 个生成结果，创建 Collection “618 campaign”，后续可以在 Chat 里 `@618 campaign` 选择使用。
4. 作为创作者，我把筛选条件“视频 + Atlas + 最近 7 天 + ready + prompt 包含 product”保存为 Smart Group。
5. 作为协作者，我打开资产详情，可以看到它由哪个 job 生成、输入引用是什么、完整 prompt 和参数是什么。
6. 作为管理者，我撤销一个资产后，任何 Chat prompt 即使裸写 asset id 也不能继续使用它。

## 5. 信息架构

### 5.1 主要入口

| 入口 | 功能 |
|---|---|
| Chat composer | 上传、`@` mention、结构化提交、展示 job card |
| Asset Library page | 浏览、搜索、筛选、批量选择、Collection/Smart Group 管理 |
| Asset Inspector | 预览、上下文、lineage、权限、审计、复用动作 |
| Job card | 生成状态、失败原因、输出资产、重试/下载/保存为 Element |

### 5.2 导航结构

```text
Ultra Studio
  Chat
  Assets
    All
    Uploads
    Generated
    Elements
    Characters
    Collections
    Smart Groups
  Jobs
  Settings
```

P0 可以先把 Assets 作为 Chat 右侧 Inspector 的扩展页或单独 route，建议保留独立 route：`/assets`。

## 6. 核心对象定义

### 6.1 Asset

Asset 是所有可复用创作素材的统一记录。

| 字段 | 说明 |
|---|---|
| `asset_id` | 稳定 ID，例如 `asset_...` |
| `account_id` / `account_uuid` / `workspace_id` / `project_id` | KubeDL 账号、项目和隔离边界 |
| `type` | `media_input`、`image_job`、`video_job`、`audio_job`、`element`、`character` |
| `media_type` | `image`、`video`、`audio`、`mesh`、`document` |
| `status` | `uploading`、`processing`、`ready`、`failed`、`archived`、`revoked`、`deleted` |
| `source` | `upload`、`generation`、`save_as_element`、`character_training` |
| `object_key` | 对象存储 key，不直接暴露本地路径 |
| `thumbnail_key` | 缩略图 key |
| `prompt` / `negative_prompt` | 生成上下文 |
| `endpoint` / `model` / `model_route` | provider-neutral 生成路由 |
| `params_json` | steps、CFG、LoRA、duration、resolution 等 |
| `seed` | 可复现 seed，provider 不返回时为空 |
| `owner_user_id` / `created_by_source` | 创建者、console 或 apikey 来源 |
| `created_at` / `updated_at` | 时间 |

### 6.2 MediaJob

MediaJob 是 provider-neutral 的生成任务。

| 字段 | 说明 |
|---|---|
| `job_id` | Hermes job ID |
| `session_id` / `run_id` / `tool_call_id` | 与 Agent run 关联 |
| `provider` / `model` | Atlas/FAL/xAI/OpenAI 等 |
| `media_type` / `mode` | 图片/视频、t2i/i2v/t2v 等 |
| `status` | `created`、`queued`、`running`、`succeeded`、`failed`、`cancelled`、`timeout` |
| `input_assets` | 输入资产结构化引用 |
| `prompt` / `negative_prompt` | 编译后的 prompt |
| `provider_constraints` | duration、resolution、aspect ratio、audio 等限制 |
| `seed` / `params_json` | 可复现参数 |
| `provider_job_id` | 上游 prediction/task id |
| `output_assets` | 输出 asset_id 列表 |
| `tokenrouter_decision_id` | 权限/计费/路由决策记录 |
| `error` | typed error 和 provider error class |

### 6.3 Collection

Collection 是静态人工合集。

| 字段 | 说明 |
|---|---|
| `collection_id` | 稳定 ID |
| `name` / `description` | 名称和说明 |
| `owner_user_id` | 创建者 |
| `members` | 通过 `collection_members` 保存 asset_id |
| `visibility` | project/workspace/private |

Collection 成员关系必须静态存储，不从筛选条件动态推导。

### 6.4 Smart Group

Smart Group 是保存的动态筛选规则。

P1 至少支持：

- prompt 条件。
- endpoint / model 条件。
- source 来源。
- created_at 时间范围。
- media_type。
- status。
- collection 包含条件。
- 相似度阈值。

Smart Group 存 `query_json`，打开时实时评估；不把成员物化成静态表。

### 6.5 Reference: Element / Character

| 类型 | 定义 | P0 处理 |
|---|---|---|
| `element` | 可复用物体、场景、风格、产品素材 | 支持从 ready asset 保存为 Element |
| `character` | 普通角色概念，可能由单张或多张素材构成 | P0 可创建 queued/training/ready 状态，但不假装训练完成 |

生成时传 `character_id`、`element_id` 等结构化参数，而不是 prompt 文本。

## 7. 前端交互 PRD

### 7.1 Chat 上传流程

当前已有 `/api/chat/uploads` 和 `uploadChatAttachment(file)`，P0 要改成资产上传：

```text
用户拖拽/选择文件
  -> 前端预校验 mime/size
  -> POST /api/assets/uploads
  -> 返回 asset_id + upload status
  -> composer 展示附件 chip
  -> asset.processing / asset.ready 事件更新 UI
  -> 提交 prompt 时 attachments[] 传 asset_id
```

P0 可以兼容旧 `/api/chat/uploads`，但返回体必须新增 `asset_id`。旧的 `path` 只作为本地 attach 兼容，不作为后续生成权限依据。

### 7.2 Chat `@` mention 流程

```text
用户输入 @
  -> GET /api/assets/mentions?q=lun&context=chat_prompt
  -> 前端展示分组结果：Assets / Characters / Elements / Collections / Smart Groups
  -> 用户选择 Luna
  -> composer 内保存 mention token
  -> 提交时发送 text + mentions[] + attachments[]
  -> 后端 validate refs
  -> Prompt Compiler 编译 provider payload
```

提交 payload 示例：

```json
{
  "text": "用 @Luna 做一个 5 秒天台夜景视频",
  "mentions": [
    {
      "span": [2, 7],
      "entity_type": "character",
      "entity_id": "char_luna",
      "asset_ref_type": "character",
      "operation": "use"
    }
  ],
  "attachments": [
    {
      "asset_id": "asset_upload_123",
      "role": "image_reference"
    }
  ]
}
```

交互规则：

- 同名结果必须让用户选择，不能自动猜。
- `revoked` 不出现在可选结果里。
- `processing` / `training` 可以展示，但禁用使用，说明原因。
- `@collection` 和 `@smart_group` 需要先打开预览并确认，避免一次隐式展开大量资产。
- 文本里裸写 `asset_123` 不等于授权引用；后端只接受结构化 mention。

### 7.3 Asset Library 页面

页面布局：

```text
Top toolbar: Search / Upload / New Collection / Save Smart Group / View switch
Left rail: Type, Status, Source, Endpoint, Time, Collection filters
Main grid/table: Asset cards
Right inspector: Preview, Context, Lineage, ACL, Audit, Actions
```

Card 必须展示：

- 真实图片/视频/音频预览，视频可播放。
- status chip。
- type 和 media_type。
- provider/model。
- 创建时间和 owner。
- Collection 标记。
- 主要动作：Inspect、Reuse、Add to Collection、Download。

默认空状态不展示样例资产，只给上传入口和创建提示。

### 7.4 Asset Detail / Inspector

详情至少包含：

- Preview：原图/视频播放器/音频播放器/失败占位/处理中占位。
- Context：API version、endpoint、model route、full prompt、negative prompt、seed、steps、CFG、LoRA、params、request id、run id、session id。
- Lineage：输入 asset、source job、输出 asset、derived_from、saved_as、character_source。
- Permissions：owner、workspace/project ACL、allowed operations。
- Audit：created、uploaded、used、downloaded、added_to_collection、revoked、deleted。
- Actions：Reuse as reference、Save as Element、Create Character、Add to Collection、Download、Archive/Delete、Copy reproducible prompt。

### 7.5 Collection 创建与管理

P0 必须支持：

- 新建 Collection。
- 重命名 / 删除 Collection。
- 多选资产加入 Collection。
- 从 Collection 移除资产。
- Collection 详情展示成员。
- Chat `@collection` 搜索和确认选择。

### 7.6 Smart Group 创建器

P1 支持“保存当前筛选为智能分组”：

```text
用户设置筛选
  -> 点击 Save Smart Group
  -> 填写 name/description
  -> 系统展示 preview hit count 和前 N 个结果
  -> 保存 query_json
  -> 左侧 Smart Groups 出现该分组
```

规则：

- 保存前必须 preview。
- `query_json` 校验失败要显示字段级错误。
- 打开 Smart Group 时实时评估，不保存静态成员。

### 7.7 Character 创建流程

P1/P2 支持从上传图或生成图创建角色：

```text
资产详情 -> Create Character
  -> 选择类型：character / product mascot
  -> 选择来源图和训练参数
  -> 创建 reference job
  -> 状态 queued -> training -> ready / failed
  -> Chat 后续通过 @ 或 picker 选择，作为 character_id / element_id 传入
```

P0 可以先开放按钮和 `queued` 状态，但如果没有真实训练 provider，不得自动变成 `ready`。

## 8. 后端架构 PRD

### 8.1 服务边界

```text
Web React
  -> Existing Dashboard / Gateway
  -> Go Asset Service (assetd)
       -> MySQL/GORM
       -> Redis
       -> Object Storage
       -> Search Indexer
       -> Audit Ledger
  -> Go Media Job Service
       -> Provider adapters: Atlas/FAL/xAI/OpenAI
       -> Worker / poller
  -> TokenRouter / Policy Checker
       -> ACL, quota, credential, audit decision
```

P0 本地版建议：

- DB：MySQL/GORM，P0 单 asset DB，预留多库实例。
- Cache/lock：Redis，用于缓存、分布式锁、active job set。
- Object storage：阿里云 OSS，media/result/temp 三类 bucket，接口上抽象为 `object_key`。
- Thumbnail：本地生成并保存 `thumbnail_key`。
- Search：MySQL FULLTEXT 或 keyword 表 + 关键字段硬过滤；embedding 字段预留。
- Events：复用现有 `/api/events` 或 Gateway event stream。

Cloud 版迁移：

- DB：MySQL 多库拆分。
- Object storage：继续 OSS 或兼容对象存储。
- Search：MySQL FULLTEXT + vector index 或专用 vector index。
- Worker：队列 + durable poller。

### 8.2 模块划分

| 模块 | 职责 | 建议位置 |
|---|---|---|
| HTTP API | upload/list/detail/collection/smart group/mentions/media jobs | `services/asset-library/internal/httpapi` |
| App services | 上传、资产、合集、mention、media job 用例和事务边界 | `services/asset-library/internal/app` |
| Domain | Asset、MediaJob、Collection、SmartGroup、Reference、typed errors | `services/asset-library/internal/domain` |
| Store | MySQL/GORM repository，migration，transaction runner | `services/asset-library/internal/store/mysqlstore` |
| Redis | cache、distributed lock、active job set | `services/asset-library/internal/redisstore` |
| Object store | OSS media/result/temp bucket，返回 object_key 和 preview/download reader | `services/asset-library/internal/object/ossstore` |
| Provider adapter | Go 原生 Atlas image/video adapter | `services/asset-library/internal/provider/atlas` |
| Search indexer | metadata、keyword、FTS、embedding pipeline | `services/asset-library/internal/search` |
| Asset policy | ACL/status/use 校验 | `services/asset-library/internal/authz` |
| Audit | append-only audit event | `services/asset-library/internal/audit` |
| Events | outbox 和 Dashboard/Gateway 事件发布 | `services/asset-library/internal/events` |

原则：

- 不新增 core model tool，优先通过 service-gated tool 或 plugin/skill 暴露。
- 不把 provider key 传给前端、prompt、sandbox。
- 不让搜索索引成为资产真相来源。

### 8.3 API 设计

#### Upload

```http
POST /api/assets/uploads
Content-Type: image/png
X-Hermes-Filename: product.png
```

返回：

```json
{
  "asset_id": "asset_01h...",
  "name": "product.png",
  "mime_type": "image/png",
  "size": 123456,
  "status": "processing",
  "object_key": "media-assets/20260612/...",
  "thumbnail_url": null
}
```

P0 可以单步上传；P1 可升级为 `uploads/init` + `uploads/complete`。

#### Asset list/detail

```http
GET /api/assets?project_id=...&media_type=image&status=ready&q=luna&cursor=...
GET /api/assets/{asset_id}
GET /api/assets/{asset_id}/lineage
GET /api/assets/{asset_id}/audit
```

#### Collection

```http
POST   /api/assets/collections
GET    /api/assets/collections
PATCH  /api/assets/collections/{collection_id}
DELETE /api/assets/collections/{collection_id}
POST   /api/assets/collections/{collection_id}/members
DELETE /api/assets/collections/{collection_id}/members/{asset_id}
```

#### Smart Group

```http
POST /api/assets/smart-groups/preview
POST /api/assets/smart-groups
GET  /api/assets/smart-groups
GET  /api/assets/smart-groups/{group_id}/assets
PATCH /api/assets/smart-groups/{group_id}
DELETE /api/assets/smart-groups/{group_id}
```

#### Mention

```http
GET /api/assets/mentions?q=lun&project_id=...&types=asset,character,collection&context=chat_prompt
```

返回项必须包含：

- `entity_type`
- `entity_id`
- `label`
- `subtitle`
- `thumbnail_url`
- `status`
- `allowed_operations`
- `disabled_reason`

#### Media job

```http
POST /api/media-jobs
GET  /api/media-jobs/{job_id}
POST /api/media-jobs/{job_id}/cancel
POST /api/media-jobs/{job_id}/retry
```

P0 需要至少实现 create/status；finalize 是 worker 内部动作，不暴露给前端。cancel/retry 可返回明确 `not_supported`，不能静默成功。

### 8.4 数据模型

P0 最小表：

```text
assets
asset_lineage
media_jobs
collections
collection_members
asset_acl
asset_audit_events
asset_search_index
```

P1/P2 增加：

```text
smart_groups
asset_references
reference_jobs
asset_versions
asset_usage_events
```

关键约束：

- `assets.asset_id` 全局唯一。
- `collection_members(collection_id, asset_id)` 唯一。
- `asset_acl(asset_id, subject_type, subject_id, permission)` 唯一。
- `media_jobs.output_asset_id` 必须指向 `assets`。
- `asset_lineage.parent_asset_id` 和 `child_asset_id` 必须存在。
- `deleted/revoked` 资产不能被 `use`。

### 8.5 搜索与索引

搜索分两层：

1. 硬过滤：project、type、media_type、status、endpoint、source、collection、time。
2. 软召回：prompt FTS、tag/keyword、embedding similarity、prompt cluster。

P0：

- MySQL FULLTEXT 或 keyword 表。
- prompt、filename、manual tags、endpoint、model、source 硬过滤。
- index pending 时显示“索引中”，但列表不能空白丢数据。

P1：

- prompt embedding。
- tag/keyword extraction。
- prompt soft cluster。
- 相似度阈值用于 Smart Group。

## 9. 生成链路

### 9.1 上传入库

```text
POST /api/assets/uploads
  -> validate mime/size
  -> write object
  -> insert assets(status=processing, type=media_input)
  -> create ACL(owner/project read/use)
  -> audit upload.created
  -> async metadata/thumbnail
  -> asset.ready or asset.failed
```

### 9.2 生成入库

```text
Chat prompt + mentions + attachments
  -> validate asset refs
  -> Prompt Compiler builds provider payload
  -> TokenRouter/Policy checks
  -> create media_job
  -> provider submit/poll
  -> materialize output object
  -> insert output asset
  -> insert lineage input -> output
  -> audit job.succeeded and asset.created
  -> emit media_job.updated and asset.ready
```

Atlas 注意事项：

- `urls.get`、`status`、`poll`、`/prediction/` 这类控制 URL 不能当成输出媒体。
- provider poll 错误必须保留为 retryable/failed 状态，不能把 job 标成 succeeded。
- `ATLAS_API_KEY` / `LLM_API_KEY` 只在后端读取，不能进入前端或资产详情。

### 9.3 复用生成

```text
用户点 Reuse 或选择 @asset
  -> composer 插入结构化 ref
  -> submit 时后端再次校验 read/use/status
  -> 编译为 provider 支持的 image_url / asset_ref / character_id / element_id
```

所有复用写 audit：

- `asset.used`
- `mention_resolved`
- `job.input_asset_added`
- `output.derived_from`

## 10. KubeDL 身份桥、权限与审计

### 10.1 身份与资源权限

身份源是 kubedl/Dashboard：先完成 JWT/cookie、account member、RBAC、user 状态校验，再用签名内部 header 把 `account_id`、`account_uuid`、`user_id`、`roles`、`source`、`request_id` 传给 `assetd`。`assetd` 只信内部签名，不信浏览器同名 header。

资产库自己判断资源权限。权限动词：`read`、`use`、`update`、`delete`、`revoke`；主体：user、project、workspace、service_account。

规则：

- 所有资产、job、collection 都带 `account_id/account_uuid`，跨 account 默认不可见。
- List API 只返回可 read 资产。
- Mention API 只返回可 read 资产，并标出是否可 use。
- 生成 submit 时必须重新检查 use 权限。
- 浏览、预览、下载、合集管理不触发余额检查；以资产继续生成时才进入 aiproxy 计费/余额链路。
- Revoked/deleted 资产不能继续生成。
- Unauthorized asset id 即使出现在 prompt 文本里也无效。

### 10.2 审计事件

至少记录：

- `asset.uploaded`
- `asset.created_from_job`
- `asset.used`
- `asset.downloaded`
- `asset.added_to_collection`
- `asset.removed_from_collection`
- `asset.revoked`
- `asset.deleted`
- `mention_resolved`
- `media_job.created`
- `media_job.failed`
- `media_job.succeeded`

审计字段：

- actor_user_id
- workspace_id / project_id
- asset_id / job_id / collection_id
- action
- request_id / run_id / session_id
- metadata_json
- created_at

## 11. 错误策略

必须使用 typed errors：

| Error | 场景 |
|---|---|
| `upload_mime_not_allowed` | 上传 mime 不允许 |
| `upload_too_large` | 文件超过大小 |
| `asset_not_found` | asset id 不存在或不可见 |
| `asset_access_denied` | 无 read/use/update 权限 |
| `asset_not_ready` | processing/training 未完成 |
| `asset_revoked` | 已撤销 |
| `smart_group_query_invalid` | Smart Group query 无效 |
| `collection_expand_requires_confirmation` | Collection/Group 需要用户确认 |
| `missing_credential` | provider key 缺失 |
| `provider_rejected_input` | provider 拒绝参数或素材 |
| `job_timeout` | 生成超时 |
| `output_materialize_failed` | 输出下载/落库失败 |

规则：

- 生成链路遇到任意输入资产错误，整体 fail closed。
- 不允许 warning 后跳过某个输入继续生成。
- 不允许 provider 失败后返回假图或假视频。
- 不支持的操作返回 `not_supported`，不要静默无效。

## 12. 分期计划

### P0: 真实资产库最小可用版

目标：一条真实创作闭环可以跑通，并能在资产库复用。

范围：

1. 扩展上传接口，返回 `asset_id`，写入 `assets`。
2. 上传素材真实预览、mime/size 校验、失败状态。
3. Atlas 图片/视频生成包装成 `media_jobs`。
4. 输出落 OSS result bucket，生成 `image_job` / `video_job` asset。
5. 保存 prompt、endpoint、model、params、seed、source、owner、session/run/tool_call、lineage。
6. Asset Library 基础页面：列表、筛选、详情、真实预览。
7. Collection CRUD 和多选加入/移除。
8. Chat `@` mention：搜索资产/Collection/Element/Character，结构化提交。
9. Reuse、download、archive/delete 的真实动作。
10. ACL 和 audit 最小实现。

验收：

- 上传一张图片后，刷新页面仍能在资产库看到同一个 `asset_id`。
- 生成一条图片/视频后，输出资产出现在资产库，并能打开详情看到 job、prompt、model、lineage。
- Chat 选择 `@asset` 后，后端收到 `mentions[]`，不是只收到文本。
- 撤销一个资产后，mention 不再出现，直接提交该 asset_id 会失败。
- 没有 Atlas key 时返回 `missing_credential`，不展示假结果。

### P1: 完整资产管理与搜索

范围：

1. Smart Group 创建器和 preview。
2. prompt embedding、tag/keyword extraction。
3. endpoint 硬过滤 + prompt soft cluster + source/time 条件组合。
4. Asset detail 完整上下文：API version、model route、negative prompt、steps、CFG、LoRA、request/run/session ids。
5. 可执行复用动作：保存为 Element、建角色、加入合集、复制可复现 prompt。
6. 批量删除、批量下载、批量加合集。
7. Element 创建和复用。
8. Character 创建流程进入 queued/training/failed/ready 真实状态。

验收：

- “保存当前筛选为 Smart Group”后，重新打开命中结果与 preview 规则一致。
- 详情页可复制可复现 prompt，并包含完整参数。
- 批量操作有明确成功/失败结果。
- Character 如果没有训练完成，不能作为 ready 引用使用。

### P2: 高级资产智能化

范围：

1. Character / Element 真实训练或绑定 provider 接入。
2. 去重与版本：同 prompt 多次生成、重复上传文件检测。
3. 智能推荐：根据当前 prompt 推荐角色、场景、产品。
4. 计费/用量：资产关联生成成本、耗时、provider usage。
5. 协作：分享合集、评论、收藏、锁定资产。
6. 云多租户：MySQL 多库 + OSS 生命周期 + vector index + TokenRouter 完整落地。

## 13. 实施建议

前端新增 `AssetLibraryPage`、`AssetGrid`、`AssetCard`、`AssetInspector`、`AssetFilters`、`CollectionDialog`、`SmartGroupDialog`、`MentionMenu`、`useAssetLibrary`、`useAssetMentions` 和 `assetApi`。修改 `useGatewayChat.ts`、Chat composer、Chat side panel、`chatUpload.ts`，让附件携带 `asset_id`，提交携带 `mentions[]`，并订阅 `media_job.updated` / `asset.ready`。

前端可以缓存筛选、分页、mention token、card 事件状态和 Smart Group 草稿；不能把 ACL、asset ready/use、lineage、provider job completion 当作客户端真相。

后端 P0 先做：Go module + `assetd` HTTP server、KubeDL 身份桥、MySQL/GORM schema + repository、Redis lock/cache、OSS 三 bucket、上传入库、资产 list/detail/lineage/audit、Collection API、mention API、MediaJob create/status、Atlas 输出 materialize + asset + lineage、Gateway event 推送。

兼容策略：旧 `/api/chat/uploads` 可以继续返回 `path`，但必须新增 `asset_id`；旧附件可显示，新生成链路只信 `asset_id`；Go `assetd` 原生封装 Atlas provider；OSS internal key 和浏览器 header 不进权限判断。

安全策略：object key 使用系统生成 ID；KubeDL 身份 header 必须签名；下载检查 read；生成检查 use；裸 asset id 不授权；删除/撤销后索引和 mention 结果失效；上传内容 sniff 放入 P1。

## 14. 验收测试计划

后端测试覆盖 KubeDL 签名身份、Upload 成功/失败、List/detail 权限、Collection CRUD、Mention ACL/status、MediaJob 成功/失败、Lineage 正确性、Revoke 后不能 use。

前端测试覆盖上传状态更新、`@` 搜索和 token 删除、Collection 确认、详情上下文、视频播放、failed job 占位。

端到端 smoke：

1. 启动 Web。
2. 上传图片并等待 asset ready。
3. 在 Chat 里 `@` 选择该图片作为参考。
4. 发起 Atlas 图片或视频生成。
5. 看到 job running。
6. 输出 ready 后出现在资产库。
7. 打开详情确认 prompt、model、lineage。
8. 创建 Collection 并加入输出。
9. 撤销输出，确认不能再 `@` 使用。

## 15. 开放问题

1. P0 DB 使用 MySQL/GORM 独立 asset 库，按 aiproxy 多库模式预留扩展。
2. 本地 preview/download 走受权限保护的 route，还是短期 signed URL。P0 建议前者。
3. `media_jobs` 由 Asset Service 拥有，还是 Media Job Service 拥有并镜像。P0 建议单库两表。
4. Character P0 显示 queued 入口还是先隐藏。建议显示但明确训练 provider 未接入。
5. 语义 embedding 的模型和存储。P0 先 FTS + metadata。
6. TokenRouter P0 最小范围。local 可用 in-process policy checker，但保持 fail closed contract。

## 16. 最小任务拆解

| 序号 | 任务 | 交付 |
|---|---|---|
| 1 | Asset store | `assets`、`media_jobs`、`collections` 等 P0 表 |
| 2 | 上传入库 | `/api/assets/uploads` 返回 `asset_id` |
| 3 | 资产读取 | list/detail/lineage/audit |
| 4 | 组织管理 | Collection CRUD + members |
| 5 | Chat 复用 | mention API + `@` structured submit |
| 6 | 生成入库 | MediaJob wrapper + Atlas output asset |
| 7 | UI | grid/filter/detail/preview |
| 8 | 安全审计 | read/use/revoke + audit events |
| 9 | 验证 | 上传 -> @ -> 生成 -> 入库 -> 复用 smoke |

## 17. 成功标准

第一版完成时，用户能用真实数据完成这条路径：

```text
上传产品图
  -> 资产库出现 media_input
  -> Chat @ 选择该图
  -> 生成图片/视频
  -> 资产库出现 image_job/video_job
  -> 查看完整上下文和 lineage
  -> 加入 Collection
  -> 再次 @ Collection 或资产复用
```

任何一步失败，都必须有真实状态、错误码和审计记录；不允许用静态数组、CSS 假图、假 job 或静默 fallback 掩盖。
