# Hermes 资产库 Go 后端设计

状态：后端架构设计稿
日期：2026-06-12
范围：真实资产入库、生成任务、合集、智能分组、角色/元素引用、搜索、权限、审计、Chat `@` 结构化引用。
实现语言：Go

## 1. 目标

资产库后端要成为上传素材和生成结果的唯一状态源。所有上传、图片生成、视频生成、复用、下载、加入合集、撤销删除，都必须进入同一个 Go 服务，并产生稳定 `asset_id`、真实对象存储、lineage、prompt 上下文、权限和审计记录。

第一版不是图库，也不是前端静态状态。它要支持这条真实链路：

```text
上传/生成请求
  -> Go Asset Service
  -> MySQL + Redis + Aliyun OSS
  -> media_input / image_job / video_job asset
  -> Chat 和 Asset Library 通过 asset_id / reference_id 复用
  -> ACL / status / audit 全程校验
```

## 2. 当前事实

| 领域 | 现状 | 设计结论 |
|---|---|---|
| 前端 | React Web 已存在 Chat 和附件上传调用 | 前端继续调用 `/api/assets/*`、`/api/media-jobs/*`，不直接碰 provider。 |
| 旧上传 | `/api/chat/uploads` 只返回 path/name/mime/size | Go 服务必须返回 `asset_id`；旧接口只做兼容桥。 |
| 生成 provider | Atlas image/video 客户端已有 Python 实现 | Go 后端需要独立 Atlas adapter，不能依赖前端或旧 path。 |
| 实时通道 | 现有 Dashboard 有 `/api/events` / `/api/ws` | P0 可通过 Dashboard 代理事件；Go 服务内部先实现 event outbox。 |
| 文档要求 | PRD 要真实入库、Collection、Smart Group、`@`、ACL/audit | Go 服务按 Asset Service + MediaJob Service 切边界。 |
| 代码仓库 | 当前没有 Go module | 新增独立 Go service，避免和 Hermes Python runtime 混在一起。 |
| aiproxy 存储参考 | `aip/pkg/repositories/mysql/client.go`、`aip/pkg/storage/oss/transfer.go` | 采用 MySQL/GORM 多库、Redis、OSS 三 bucket。 |
| kubedl 身份参考 | `kubedl/console/backend/pkg/middleware/auth.go`、`handlers/account.go`、`serviceauth.go` | 登录、account、member、RBAC 以 kubedl 为权威源；assetd 只接收签名身份上下文。 |

## 3. 架构选择

选择：独立 Go 服务 `assetd`，由现有 Dashboard/Gateway 代理到它。

```text
React Web
  -> Existing Dashboard / Gateway
     -> proxy /api/assets/*
     -> proxy /api/media-jobs/*
     -> proxy /api/asset-events
  -> Go assetd
     -> Asset app services
     -> MySQL/GORM
     -> Redis
     -> Aliyun OSS: media/result/temp buckets
     -> Atlas provider adapter
```

为什么不是把资产库写进现有 Python 后端：

- 用户已明确后端用 Go。
- 资产库包含 durable job、对象存储、审计、权限和未来 worker，更适合作为独立服务边界。
- 旧 Python Dashboard 可以继续负责登录、Chat shell、WebSocket；资产状态不再由 Python 文件路径承担。

P0 集成方式：

- Go 服务监听本地端口，例如 `127.0.0.1:8787`。
- Dashboard/kubedl 完成 JWT/cookie、account member、RBAC/user 状态校验后，把 `/api/assets/*`、`/api/media-jobs/*` 代理到 Go，并注入签名身份 header。
- 旧 `/api/chat/uploads` 临时调用 Go 上传接口，并保留旧 `path` 字段给旧前端兼容。

## 4. Go 项目结构

建议新增：

```text
services/asset-library/
  go.mod
  cmd/assetd/main.go
  internal/config/
  internal/domain/
  internal/httpapi/
  internal/app/
  internal/store/mysqlstore/
  internal/object/ossstore/
  internal/redisstore/
  internal/provider/atlas/
  internal/search/
  internal/events/
  internal/identitybridge/
  internal/authz/
  internal/audit/
  internal/testkit/
```

包职责：

| 包 | 职责 |
|---|---|
| `cmd/assetd` | composition root：读配置、建 DB、建 HTTP server、启动 worker、优雅关闭。 |
| `internal/domain` | Asset、MediaJob、Collection、SmartGroup、Reference、状态机、typed errors。无 DB/HTTP/provider。 |
| `internal/app` | 用例层：上传、资产查询、合集、智能分组、mention、media job、引用创建。拥有事务边界。 |
| `internal/httpapi` | HTTP handler、JSON DTO、错误码到 HTTP 状态映射。不能直接 SQL 或 provider。 |
| `internal/store/mysqlstore` | GORM repository、多库实例、migration、事务执行。 |
| `internal/redisstore` | Redis 缓存、分布式锁、活跃 job 集合、短 TTL 状态。 |
| `internal/object/ossstore` | 阿里云 OSS 上传、远程结果转存、media/result/temp bucket 路由。 |
| `internal/provider/atlas` | Atlas 请求/轮询/输出解析。provider 字段不泄露到公开 API。 |
| `internal/search` | FTS、关键词、未来 embedding 投影。索引不是资产真相源。 |
| `internal/events` | outbox、SSE/代理事件、事件序列化。 |
| `internal/identitybridge` | 校验 kubedl 签名身份 header，生成 `Principal`。不做资产 ACL。 |
| `internal/authz` | read/use/update/delete/revoke 策略校验。 |
| `internal/audit` | append-only 审计写入和查询。 |
| `internal/testkit` | fake provider、临时 DB、临时 object store、HTTP test server。 |

Go 规则：

- `context.Context` 作为阻塞/IO 函数第一个参数，不存进 struct。
- handler 只做 transport 翻译；应用层拥有事务和状态转换。
- provider adapter 返回 Go typed error，HTTP 层统一映射。
- goroutine 必须由 `assetd` 或 worker manager 拥有，支持 shutdown。
- 接口定义在消费者侧，只定义小接口，例如 `ObjectStore`、`Publisher`、`Provider`。

## 5. 核心状态所有权

| 状态 | 唯一 owner | 说明 |
|---|---|---|
| 资产 | `AssetService` / `assets` table | UI、Chat、worker 都只读这里。 |
| 对象字节 | `OSSStore` | media bucket 存上传，result bucket 存生成输出，temp bucket 存临时中转；不拥有产品语义。 |
| 生成任务 | `MediaJobService` / `media_jobs` table | provider submit/poll/finalize 都写这里。 |
| Lineage | `AssetService` / `asset_lineage` | 输入输出关系、保存为 Element、角色来源。 |
| Collection | `CollectionService` / `collection_members` | 静态成员关系。 |
| Smart Group | `SmartGroupService` / `query_json` | 动态查询，不保存成员。 |
| Search | `SearchIndexer` / `asset_search_index` | 可重建投影，不能覆盖资产真相。 |
| ACL | `authz.Policy` / `asset_acl` | 每次 read/use/update/delete/revoke 都检查。 |
| Audit | `AuditService` / `asset_audit_events` | create/use/download/revoke/delete 等操作必须落审计。 |

## 6. Domain 模型

### 6.1 Asset

```go
type AssetType string
const (
    AssetMediaInput AssetType = "media_input"
    AssetImageJob   AssetType = "image_job"
    AssetVideoJob   AssetType = "video_job"
    AssetAudioJob   AssetType = "audio_job"
    AssetElement    AssetType = "element"
    AssetCharacter  AssetType = "character"
)

type AssetStatus string
const (
    AssetUploading  AssetStatus = "uploading"
    AssetProcessing AssetStatus = "processing"
    AssetReady      AssetStatus = "ready"
    AssetFailed     AssetStatus = "failed"
    AssetArchived   AssetStatus = "archived"
    AssetRevoked    AssetStatus = "revoked"
    AssetDeleted    AssetStatus = "deleted"
)
```

核心字段：

```text
asset_id, workspace_id, project_id, type, media_type, status,
title, original_filename, mime_type, size_bytes,
prompt, negative_prompt, endpoint, api_version, model_route, provider,
params_json, seed, source, object_key, thumbnail_key,
account_id, account_uuid, owner_user_id, created_by_source,
created_by_api_key_id, created_at, updated_at
```

规则：

- `ready` 媒体资产必须有 `object_key`。
- `revoked/deleted` 不可用于生成。
- `failed` 保留详情和错误，不可用作输入。
- `archived` 默认隐藏，但详情可查。

### 6.2 MediaJob

状态：

```text
created -> queued -> running -> succeeded
created/running -> failed
created/running -> cancelled
running -> timeout
```

字段：

```text
job_id, workspace_id, project_id, session_id, run_id, tool_call_id,
provider, endpoint, model_route, media_type, mode, status,
prompt, negative_prompt, params_json, seed,
input_assets_json, provider_job_id, provider_request_json,
provider_response_json, output_asset_ids_json,
tokenrouter_decision_id, error_code, error_message,
created_by, created_at, updated_at
```

规则：

- provider submit 前必须先插入 `media_jobs(status=created)`。
- provider 接收后才能进入 `running`。
- 只有真实输出已落 object store 且 output asset 已创建，才能进入 `succeeded`。
- Atlas prediction/status/poll URL 不能当输出媒体。
- retry 创建新 job，不能复写旧 failed job。

### 6.3 Collection

Collection 是静态合集：

```text
collections(collection_id, workspace_id, project_id, name, description, created_by, timestamps)
collection_members(collection_id, asset_id, added_by, added_at)
```

删除合集只删 membership，不删资产。

### 6.4 Smart Group

Smart Group 保存动态规则：

```json
{
  "prompt": {"contains": "product"},
  "endpoint": ["atlas/kling-video"],
  "source": ["generation"],
  "created_at": {"from": "2026-06-01", "to": "2026-06-10"},
  "media_type": ["video"],
  "status": ["ready"],
  "collection_id": "col_campaign_a",
  "similarity_threshold": 0.78
}
```

P0 如果没有 embedding，`similarity_threshold` 返回 `semantic_pending`，不能假装语义搜索已执行。

### 6.5 Reference

只保留：

```text
element
character
```

```text
asset_references(reference_id, workspace_id, project_id, type, kind, name,
source_asset_id, status, provider_reference_id, metadata_json, created_by, timestamps)
```

规则：

- `element` 可以从 ready asset 立即创建为 ready。
- `character` 可以是 `queued/training/failed/ready`，但没有真实 provider 或本地流程完成时不能伪造 ready。

## 7. 存储设计

P0：

```text
MySQL: asset_library primary DB, GORM
Redis: cache / distributed lock / active job set
OSS media bucket: upload source media
OSS result bucket: generated outputs and thumbnails
OSS temp bucket: transient provider transfer objects
```

表：

```text
assets
asset_lineage
media_jobs
collections
collection_members
smart_groups
asset_references
asset_acl
asset_audit_events
asset_search_index
event_outbox
schema_migrations
```

`assets` 还必须有 `sha256`，用于完整性校验、重复上传识别和后续去重版本功能。OSS 对象只保存 bytes，业务真相仍在 MySQL。

关键索引：

```text
assets(account_id, project_id, status, type, media_type, created_at)
assets(account_id, project_id, source, endpoint)
collection_members(collection_id, asset_id) unique
asset_acl(account_id, asset_id, subject_type, subject_id, permission) unique
asset_lineage(asset_id)
asset_lineage(parent_asset_id)
media_jobs(project_id, status, created_at)
asset_audit_events(asset_id, created_at)
asset_search_index(asset_id) unique
event_outbox(status, created_at)
```

MySQL/GORM P0 连接配置参考 aiproxy：

- 支持多库实例 map，assetd 至少需要 `asset_library`，后续可拆 usage/audit。
- GORM 开启 `PrepareStmt`、`TranslateError`，应用层显式事务拥有状态推进。
- 连接池设置上限、空闲连接、生命周期和 ping timeout。
- Redis lock 保护 sweeper、poller、远程结果转存等跨实例任务。

事务规则：

- 创建/更新资产 + audit + outbox 必须在一个事务里。
- 生成成功时：`media_jobs`、output `assets`、`asset_lineage`、audit、outbox 必须同事务提交。
- audit 写失败时，create/use/revoke/delete 失败。
- 搜索索引失败不能让资产消失，但必须记录 `asset.index_failed` 或 diagnostic。

## 8. HTTP API

所有 JSON API 返回统一 envelope，并带 `request_id`：

```json
{"code":0,"msg":"ok","request_id":"req_...","data":{}}
```

错误响应使用同一 envelope：`code` 为业务错误码，`msg` 为用户可读摘要，`request_id` 可用于日志和审计关联。列表接口统一分页参数：`limit`、`cursor`；列表响应为 `items`、`next_cursor`、`has_more`。

### 8.1 上传

```http
POST /api/assets/uploads
Content-Type: multipart/form-data
X-Hermes-Project-Id: proj_default
```

返回：

```json
{
  "asset_id": "asset_01j...",
  "name": "ref.png",
  "mime_type": "image/png",
  "size": 4200000,
  "status": "processing",
  "thumbnail_url": null,
  "preview_url": "/api/assets/asset_01j/preview"
}
```

流程：

1. 用 multipart 接收文件，streaming 读取并限制大小。
2. 校验 filename、MIME、后缀，同时计算 `sha256`。
3. 写入临时 object，完成后原子移动。
4. 插入 `assets(type=media_input,status=processing,source=upload)`。
5. 写 owner/project ACL。
6. 写 audit `asset.uploaded`。
7. 尝试生成 metadata/thumbnail；慢任务进入 worker。
8. 发布 `asset.processing` / `asset.ready` 事件。

### 8.2 资产读取

```http
GET /api/assets
GET /api/assets/{asset_id}
GET /api/assets/{asset_id}/lineage
GET /api/assets/{asset_id}/audit
GET /api/assets/{asset_id}/preview
GET /api/assets/{asset_id}/download
```

规则：

- list/detail/preview/download 都要求 `read`。
- download 不返回本地绝对路径。
- detail 返回 `asset`、`context`、`lineage`、`allowed_operations`、`recent_audit`。

### 8.3 Collection

```http
POST   /api/assets/collections
GET    /api/assets/collections?project_id=
PATCH  /api/assets/collections/{collection_id}
DELETE /api/assets/collections/{collection_id}
POST   /api/assets/collections/{collection_id}/members
DELETE /api/assets/collections/{collection_id}/members/{asset_id}
```

批量加成员返回部分成功：

```json
{
  "added": ["asset_1"],
  "failed": [{"asset_id": "asset_2", "code": "asset_access_denied"}]
}
```

### 8.4 Smart Group

```http
POST   /api/assets/smart-groups/preview
POST   /api/assets/smart-groups
GET    /api/assets/smart-groups?project_id=
GET    /api/assets/smart-groups/{group_id}/assets
PATCH  /api/assets/smart-groups/{group_id}
DELETE /api/assets/smart-groups/{group_id}
```

保存前必须 preview。`query_json` 错误返回字段级错误。

### 8.5 Reference

```http
POST /api/assets/references
GET  /api/assets/references?project_id=&type=&status=
GET  /api/assets/references/{reference_id}
POST /api/assets/references/{reference_id}/revoke
```

创建：

```json
{
  "source_asset_id": "asset_01j...",
  "type": "element",
  "kind": "reusable_element",
  "name": "Hero product angle",
  "metadata": {}
}
```

### 8.6 Mention

```http
GET /api/assets/mentions?q=lun&project_id=&types=asset,character,element,collection,smart_group&context=chat_prompt
```

返回：

```json
{
  "items": [
    {
      "id": "char_luna",
      "type": "character",
      "label": "Luna",
      "subtitle": "character · ready · 3 source assets",
      "thumbnail_url": "/api/assets/asset_src/preview",
      "status": "ready",
      "allowed_operations": ["read", "use"],
      "disabled_reason": null,
      "insert_token": "@Luna",
      "structured_ref": {
        "asset_ref_type": "character",
        "reference_id": "char_luna"
      }
    }
  ]
}
```

规则：

- 后端不信纯文本 `@Luna`。
- `revoked/deleted` 不返回。
- `processing/training/failed` 可返回 disabled row。
- `collection/smart_group` 需要前端确认或 picker 后才能展开。

### 8.7 Media Job

```http
POST /api/media-jobs
GET  /api/media-jobs/{job_id}
POST /api/media-jobs/{job_id}/cancel
POST /api/media-jobs/{job_id}/retry
```

创建：

```json
{
  "project_id": "proj_default",
  "media_type": "video",
  "mode": "image_to_video",
  "provider": "atlas",
  "endpoint": "atlas/kling-video",
  "model_route": "kling-v2",
  "prompt": "5 second rooftop product shot",
  "input_assets": [
    {"asset_id": "asset_ref", "role": "image_reference"}
  ],
  "params": {"duration": 5, "resolution": "720p"}
}
```

创建流程：

1. 校验所有 input asset 的 `use` 权限和 ready 状态。
2. 在后端把 `object_key` 转为 provider 可用输入。
3. 插入 `media_jobs(status=created)`。
4. 调用 Atlas adapter。
5. 保存 provider job id / response。
6. worker poll。
7. 输出落 object store。
8. 创建 output asset 和 lineage。
9. 发布 `media_job.updated`、`asset.ready`。

`finalize` 不是公开 API，只能由 worker 内部调用应用层方法完成“输出落 object store + 创建 output asset + lineage”。如果需要人工补偿接口，必须放在 `/internal/media-jobs/{job_id}/finalize`，只允许 `service_account`，并强制写 audit。P0 中 `cancel/retry` 如果未实现，返回 `not_supported`，不能假成功。

## 9. Chat 结构化提交

Chat 不再只提交文本：

```json
{
  "text": "用 @Luna 做一个天台夜景视频",
  "mentions": [
    {
      "span": [2, 7],
      "display": "@Luna",
      "entity_type": "character",
      "entity_id": "char_luna",
      "asset_ref_type": "character",
      "operation": "use"
    }
  ],
  "attachments": [
    {"asset_id": "asset_img_123", "role": "image_reference"}
  ]
}
```

Gateway/Go 服务处理：

1. 接收 structured payload。
2. 调用 `ValidateRefs(ctx, refs)`。
3. 任意 ref 不合法则整体失败。
4. 生成 provider-neutral `asset_refs`。
5. prompt text 只给模型理解，不作为权限凭证。

## 10. Worker 与事件

P0 worker 可以和 HTTP server 同进程，但必须有 lifecycle：

```go
type Worker struct {
    store *mysqlstore.Store
    redis *redisstore.Client
    atlas *atlas.Client
    oss   *ossstore.Store
    pub   events.Publisher
}

func (w *Worker) Run(ctx context.Context) error
func (w *Worker) Stop(ctx context.Context) error
```

P0 必须支持重启恢复，不等到 P2：

- `assetd` 启动后扫描 `status in (created, queued, running)` 的 `media_jobs`。
- 已有 `provider_job_id` 的 job 重新进入 poll；没有 `provider_job_id` 且超过提交 TTL 的 job 置为 `timeout`。
- sweeper 用 Redis 分布式锁单实例执行，每分钟扫描超时 job，把超过 `ASSETD_JOB_TIMEOUT` 的 running job 置为 `timeout` 并写 audit/outbox。
- 恢复和 sweeper 都必须幂等：只通过状态条件更新，避免重复 finalize。

事件类型：

```text
asset.upload.started
asset.processing
asset.ready
asset.failed
asset.revoked
asset.deleted
asset.indexed
collection.updated
smart_group.updated
reference.status
media_job.created
media_job.updated
media_job.failed
media_job.succeeded
```

事件先写 `event_outbox`，再由 publisher 推给 Dashboard/Gateway。即使 WebSocket 断开，状态仍以 DB 为准。

## 11. 可观测性

- HTTP middleware 生成或透传 `X-Request-ID`，响应 envelope 和日志都带 `request_id`。
- 使用 `log/slog` JSON 日志，字段至少包含 `request_id`、`job_id`、`asset_id`、`project_id`、`provider`、`duration_ms`、`error_code`。
- 提供 Prometheus metrics 端点，例如 `ASSETD_METRICS_ADDR=127.0.0.1:8788`。
- P0 指标：上传大小/失败数、media job 状态计数、provider poll 延迟、job 成功率、恢复 job 数、timeout sweeper 数、MySQL/Redis/OSS 错误和延迟。

## 12. 搜索与索引

搜索顺序：

1. project + ACL scope。
2. type/media_type/status/endpoint/source/collection/time 硬过滤。
3. title、filename、prompt、keywords FTS。
4. embedding 存在时才做 similarity。
5. relevance + recency 排序。

P0：

- MySQL FULLTEXT 或独立 keyword 表。
- extracted keywords 可以先用简单 tokenizer。
- embedding 字段预留。

无索引时：

- 资产仍在列表出现。
- mention 用 title/filename/reference name 搜索。
- Smart Group 返回 `semantic_pending`。

## 13. KubeDL 身份桥、权限与审计

P0 权限分两层：kubedl 是登录、account、member、RBAC 权威源；`assetd` 是资产资源权限源。aiproxy 只在“继续生成/调用模型”链路参与计费和 provider 调用，不能套到资产浏览、预览、下载、Collection 管理上。

### 13.1 KubeDL Console Identity Bridge

kubedl 已有 JWT/cookie 登录、`/current-user`、account/member、RBAC 和 serviceauth。Dashboard/kubedl 代理到 `assetd` 时注入内部 header：

```text
X-KubeDL-Account-ID
X-KubeDL-Account-UUID
X-KubeDL-User-ID
X-KubeDL-Roles
X-KubeDL-Is-System
X-KubeDL-Source
X-Request-ID
X-KubeDL-Auth-Timestamp
X-KubeDL-Auth-Signature
```

`internal/identitybridge` 用 `ASSETD_KUBEDL_BRIDGE_SECRET` 校验签名；签名覆盖 method、path、timestamp、account_id、user_id、request_id。timestamp 超窗口、签名缺失或 account/user 缺失一律 401，`assetd` 不能相信浏览器同名 header。

### 13.2 Asset Policy

权限动词：

```text
read
use
update
delete
revoke
```

审计事件：

```text
asset.uploaded
asset.created_from_job
asset.viewed
asset.used
asset.downloaded
asset.added_to_collection
asset.removed_from_collection
asset.revoked
asset.deleted
mention_resolved
media_job.created
media_job.failed
media_job.succeeded
reference.created
reference.status_changed
```

规则：

- 所有 asset、job、collection 都落 `account_id/account_uuid`，跨 account 默认不可见。
- list/detail/preview/download 检查 `read`。
- media job、reuse、mention use 检查 `use`。
- Chat `@` 搜索只返回可 `read` 的资产，提交 prompt 时后端重新校验 `use`。
- revoked/deleted/not_ready 在 ACL 后 fail closed。
- audit 不存 provider key、auth header、本地绝对路径；`assetd` 只接受带 kubedl 签名的内部请求。
- P0 固定默认 `workspace_id/project_id`，直到前端项目选择器完成。
- `asset_acl` 表可以先写 owner/project 记录，不做完整策略引擎。

## 14. 错误策略

| code | HTTP | 场景 |
|---|---:|---|
| `upload_mime_not_allowed` | 415 | 上传类型不允许 |
| `upload_too_large` | 413 | 文件过大 |
| `asset_not_found` | 404 | 不存在或跨项目 |
| `asset_access_denied` | 403 | 权限不足 |
| `asset_not_ready` | 409 | 资产或引用未 ready |
| `asset_revoked` | 410 | 已撤销 |
| `smart_group_query_invalid` | 400 | Smart Group 规则错误 |
| `collection_expand_requires_confirmation` | 409 | 需要用户确认展开 |
| `missing_credential` | 503 | provider key 缺失 |
| `provider_rejected_input` | 502 | provider 拒绝输入 |
| `job_timeout` | 504 | 生成超时 |
| `output_materialize_failed` | 502 | 输出落库失败 |
| `not_supported` | 501 | P0 暂不支持 |

Go 实现建议：

- domain 定义可匹配的 typed errors。
- app 层 wrap operation context。
- httpapi 层统一映射 HTTP code。
- 不解析 error string 做业务判断。
- 不 warning 后继续生成；输入 ref 错误必须 provider submit 前失败。

## 15. 配置

P0 环境变量或 config：

```text
ASSETD_ADDR=127.0.0.1:8787
ASSETD_HOME=~/.hermes/asset-library
ASSETD_MYSQL_ASSET_DSN=...
ASSETD_REDIS_ADDR=127.0.0.1:6379
ASSETD_OSS_ENDPOINT=...
ASSETD_OSS_INTERNAL_ENDPOINT=...
ASSETD_OSS_MEDIA_BUCKET=...
ASSETD_OSS_RESULT_BUCKET=...
ASSETD_OSS_TEMP_BUCKET=...
ASSETD_JOB_TIMEOUT=30m
ASSETD_METRICS_ADDR=127.0.0.1:8788
ATLAS_API_KEY=...
ATLAS_API_BASE=https://api.atlascloud.ai/v1
```

未来迁移：

```text
ASSETD_MYSQL_USAGE_DSN=...
ASSETD_EVENT_DRIVER=redis|mysql_outbox|nats
ASSETD_SEARCH_DRIVER=mysql_fulltext|vector
```

密钥只在 Go 服务后端读取，不返回前端，不写审计。

## 16. 兼容与迁移

| 兼容路径 | 原因 | 保留到 | 收敛条件 |
|---|---|---|---|
| `/api/chat/uploads` 返回 `path` | 旧 Chat 附件依赖 | 前端改用 `asset_id` | 旧接口代理 Go 上传并标记 deprecated。 |
| KubeDL 身份桥 | 复用 kubedl 登录、account、RBAC | 直连 introspect 完成 | P0 用签名 header，P1 可加 kubedl `/internal/auth/introspect`。 |
| OSS temp bucket | provider 输出中转需要 | result bucket 直写可靠后 | 保留 temp 生命周期清理和审计。 |
| MySQL 单库 | P0 简化 | usage/audit 写压力需要拆库 | 多库实例按 aiproxy 模式扩展。 |
| 同进程 worker | P0 少引入队列 | durable worker 完成 | P0 已有启动恢复和 timeout sweeper；P2 只替换执行载体。 |

## 17. 测试计划

| 层 | 测试 |
|---|---|
| domain | 状态转换、权限 gate、Smart Group query validation |
| identitybridge | 签名 header、过期 timestamp、缺失 account/user、伪造 header |
| store | MySQL migration、索引、事务 rollback、audit failure |
| redis | lock TTL、重复获取锁、active job set 对账 |
| object | OSS media/result/temp bucket 路由、远程结果转存、content hash |
| httpapi | upload/list/detail/collection/mention/media job |
| provider | fake Atlas success/failure/timeout/output URL filter |
| worker | poll、内部 finalize、重启恢复、timeout sweeper、retry/not_supported、shutdown |
| observability | request_id envelope、slog 字段、metrics endpoint |
| storage | MySQL/Redis/OSS 错误注入和延迟指标 |
| events | outbox 写入和发布 payload |
| e2e | upload -> `@` -> media job -> output asset -> collection -> revoke |

## 18. P0/P1/P2 路线

| 优先级 | 工作 | 完成标准 |
|---|---|---|
| P0 | Go module + `assetd` HTTP server | 本地可启动，health check 可用 |
| P0 | MySQL/GORM schema + repository | 重启后资产/job/collection 仍存在 |
| P0 | Redis lock/cache | sweeper/poller 不会多实例重复推进 |
| P0 | OSS media/result/temp buckets | 上传、临时中转、生成输出分 bucket 管理 |
| P0 | KubeDL 身份桥 | kubedl account/user/RBAC 通过签名 header 进入 assetd |
| P0 | Upload API | 返回 `asset_id`、preview URL、audit row |
| P0 | Asset read APIs | list/detail/lineage/audit 有 ACL |
| P0 | Collection CRUD | 支持批量 add/remove 和部分失败 |
| P0 | Mention API | Chat 可拿 structured ref |
| P0 | MediaJob + Atlas adapter | fake provider 成功创建 output asset |
| P0 | Job recovery + sweeper | 重启后 in-flight job 可恢复或超时 |
| P0 | API envelope + pagination | list 响应、错误响应都带 request_id |
| P0 | Observability | request_id、JSON logs、metrics 可用 |
| P0 | Outbox events | UI 能收到 ready/job status |
| P1 | Smart Group 深化 | preview/save/evaluate 一致 |
| P1 | Element/Character actions | ready 状态只来自真实完成 |
| P1 | FTS + keywords | 搜索可组合硬过滤 |
| P2 | 多库拆分/vector | 通过同一 contract tests |
| P2 | durable worker | job 跨进程重启恢复 |

## 19. 不做

- 不做公开 marketplace。
- 不把 prompt 文本当权限。
- 不让前端状态决定 asset/job 权威状态。
- 不在 P0 做完整云 TokenRouter。
- 不保留任何已删除的相关引用体系。

## 20. 已定决策和未决问题

已定：P0 通过 KubeDL 身份桥复用 kubedl 登录、account/member/RBAC，assetd 自己做资产级 read/use/update/delete/revoke；固定默认 workspace/project；事件由 Dashboard 转发复用现有连接；删除用 tombstone；Atlas 用 Go 原生 adapter，但必须把“prediction/status URL 不是输出”和 retryable poll error 做成 fake provider 测试。

未决：P1 是否新增 kubedl `/internal/auth/introspect` 替代签名 header；P2 durable worker 使用 DB poll、NATS、Redis 还是其他队列。
