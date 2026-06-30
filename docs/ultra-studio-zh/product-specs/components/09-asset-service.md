# 资产服务

状态：外部服务边界（external/spec-only in Hermes）—— 完整 Asset Service 仍未在 Hermes 内实现，也不应在 Hermes runtime 内实现。上传入库、资产 API、ACL、集合、搜索、mention、download、lineage 和 audit 都属于 Ultra Studio 产品基础服务。
日期：2026-06-30

来源：

- 文档：`docs/hermes-asset-library-backend-design.md`（全文：§架构形状、§核心实体、§API 面、§搜索和索引、§生成链路、§实时事件、§错误策略、§P0 切片）、`docs/ultra-studio-product-specs/03-media-asset-contract.md`（§Asset Types、§Asset Lifecycle、§来源链路（Lineage）、§QA、§Acceptance）、`docs/hermes-soulid-element-asset-model.md`（§资产类型、§最小数据模型、§工具映射、§安全要求）、`02-agent-runtime-contract.md`（§Error Contract）、`06-delivery-plan.md`（P0 第 9 项、P1 第 6-7 项）
- 代码：Hermes 仓库内没有 Asset Service 权威实现。上传入口仍仅作为聊天附件路径存在（`web/src/components/chat/ChatComposer.tsx`）。MediaJob 输出应由外部 Asset Service finalize，而不是由 Hermes 写本地资产表。

## 目的与范围

资产服务是类型化产品资产的记录系统：上传内容、生成输出、可复用的元素/角色/灵魂 ID、集合、智能组、权限、来源链路及操作记录。其设计规则为：生成调用消费结构化资产引用 —— "生成调用只消费结构化 asset refs，不能靠 prompt 里裸写 asset id"（`hermes-asset-library-backend-design.md` §目标）。

状态所有权（`§架构形状`）：资产服务拥有强一致的资产状态；对象存储仅保存二进制文件/缩略图；搜索索引器拥有可重建的投影；媒体作业服务必须在此回注册输出；TokenRouter 在生成前检查 `asset_ref` 权限。

范围：实体、API 界面、引用验证、索引管道、事件、权限/操作记录。浏览 UI 见 `08-asset-library-ui.md`；作业执行见 `10-media-job-service.md`；凭证策略见 `17-tokenrouter.md`。

## 实现状态

| 状态 | 项目 | 引用 |
|---|---|---|
| Hermes 未实现（Not in Hermes） | 资产权威状态、生成输出注册、lineage、ACL、audit 均不在 Hermes runtime 内实现 | 本文边界规则 |
| 已规定，未构建（Specified, not built） | 完整 `asset_lineage` / `generation_jobs` / `collections` / `smart_groups` / `asset_references` / `asset_acl` / `asset_audit_events` 实体 | `hermes-asset-library-backend-design.md` §核心实体 |
| 已规定，未构建（Specified, not built） | 两阶段上传（`uploads/init` 签名 URL -> `uploads/complete` -> 异步缩略图/元数据/嵌入） | §上传入库 |
| 已规定，未构建（Specified, not built） | 列表/详情/来源链路/操作记录读取 API | §资产列表和详情 |
| 已规定，未构建（Specified, not built） | 集合 CRUD（静态成员） | §Collection |
| 已规定，未构建（Specified, not built） | 智能组预览/保存/查询（动态，永不物化） | §Smart Group |
| 已规定，未构建（Specified, not built） | 为 element/character/soul_id 创建引用及状态，诚实反映 `queued/training/ready` | §Character / Element / Soul ID; `hermes-soulid-element-asset-model.md` |
| 已规定，未构建（Specified, not built） | Mention/选择器搜索端点，带权限过滤 | §`@` mention 查询 |
| 已规定，未构建（Specified, not built） | 生成链中的引用验证步骤 | §生成链路 |
| 已规定，未构建（Specified, not built） | 事件发射（`asset.*`、`reference.status`、`collection.updated`、…） | §实时事件 |
| 已规定，未构建（Specified, not built） | 双层搜索（硬过滤 + FTS/嵌入召回），支持索引待定的降级 | §搜索和索引 |
| 已规定，未构建（Specified, not built） | 类型化错误表，生成失败时关闭（fail-closed） | §错误策略 |
| 缺口（Gap） | 未选择存储后端（对象存储、数据库、向量索引） | 见待解决问题 |

## 用户入口点

该服务不对用户直接暴露；通过以下方式访问：

- 资产库 UI（画廊、详情、集合、mention 菜单）—— `08-asset-library-ui.md`。
- 聊天编辑器结构化提交（mentions + 附件）。
- 代理工具：资产工具组 `ultra_asset_upload / list / inspect / download / promote`（`04-skill-tool-prompt-contract.md` §Asset Tools；仅规格说明）及 `hermes-soulid-element-asset-model.md` §工具映射 中映射的引用工具。
- 媒体作业服务输出注册（外部 Media Job Service -> 外部 Asset Service finalize 路径）。
- 文件提升（`06-files-task-file-browser.md` 提升操作）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 将上传注册为 `media_input`，带 mime/大小验证 | 已规划（Planned）（P0 切片 1） |
| 将生成输出注册为 `image_job`/`video_job` 资产 | 已规划（Planned）（外部 Asset Service finalize；`audio_job` 未实现） |
| 资产生命周期管理（`uploading -> processing -> ready -> archived`） | 已规划（Planned） |
| 来源链路图（父级、源作业、提供商作业、模型、提示词哈希、种子、用户/会话/运行） | 已规划（Planned）（必须由外部 Asset Service 记录，Media Job Service 可提供 job/output refs） |
| 元素/角色/灵魂 ID 引用，带提供商训练状态 | 已规划（Planned）（P0 切片 4：允许模拟提供商，禁止伪造 `ready`） |
| 集合（手动）与智能组（动态查询） | 已规划（Planned）（P0 切片 2-3） |
| Mention/选择器搜索，带 `context=` 变体 | 已规划（Planned）（P0 切片 5） |
| 生成前引用验证（`media_generate` 预检） | 已规划（Planned）（P0 切片 6） |
| 向 UI 扇出事件 | 已规划（Planned）（P0 切片 7） |
| 权限执行（读/用/更新/删除/撤销） | 已规划（Planned） |
| 操作记录事件，包括 `mention_resolve` | 已规划（Planned） |
| 关键词提取 + 提示词/视觉嵌入 | 已规划（Planned）；降级至仅 FTS 是要求的行为，而非错误 |

## 状态机

资产生命周期（`03-media-asset-contract.md` §Asset Lifecycle，状态枚举见 §核心实体）：

```text
uploading -> processing -> ready -> archived
                 |-> failed
ready -> revoked
any -> deleted (explicit)
```

生成输出链式关联作业：

```text
job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
```

引用（`asset_references.status`）：

```text
queued -> training -> ready
queued -> failed
ready -> revoked
```

状态转换触发器：上传完成（系统）、处理管道（系统）、撤销（具有 `revoke` 权限的用户）、归档（用户）、训练进度（提供商回调/轮询）。已撤销资产必须通过索引管道从 mention/搜索可用集合中移除（`asset.revoked -> remove from usable set`）。

## API 与事件

来自 `hermes-asset-library-backend-design.md` 的 API 表面原文：

```http
POST /api/assets/uploads/init           # returns upload_id, asset_id, put_url
POST /api/assets/uploads/complete
GET  /api/assets?project_id=&media_type=&type=&status=&endpoint=&source=&collection_id=&q=&cursor=
GET  /api/assets/{asset_id}
GET  /api/assets/{asset_id}/lineage
GET  /api/assets/{asset_id}/audit
POST /api/assets/collections            (+ PATCH/DELETE, members add/remove)
POST /api/assets/smart-groups/preview   (+ CRUD, /assets evaluation)
POST /api/assets/references             # element | character | soul_id
GET  /api/assets/mentions?q=&project_id=&types=&context=
```

详情响应包含生成 `context` 块（endpoint、model_route、prompt、params、seed、request/run/session ids）、来源链路、权限、操作记录。

事件：`asset.upload.started`、`asset.processing`、`asset.ready`、`asset.failed`、`asset.revoked`、`asset.indexed`、`collection.updated`、`smart_group.updated`、`reference.status`、`job.status`。

代理工具映射（仅规格说明）：`ultra_asset_upload/list/inspect/download/promote` 为代理封装这些端点（`04-skill-tool-prompt-contract.md` §Asset Tools）。

## 数据模型

权威实体定义见 `hermes-asset-library-backend-design.md` §核心实体，此处不全量重复。摘要如下：

- `assets`：租户/工作区/项目作用域；`type` 涵盖 `media_input | image_job | video_job | audio_job | mesh_job | element | character | soul_id`；生成参数字段（prompt、endpoint、model_route、params_json、seed）；`object_key`/`thumbnail_key` 指向对象存储。
- `asset_lineage`：父级链接，含 `relation: input | output | derived_from | saved_as | character_source` 及排序。
- `generation_jobs`：提供商作业镜像，含 `request_json`、`output_asset_id`、`usage_event_id`、`error_code`（与 `10-media-job-service.md` 共享边界 —— 见待解决问题）。
- `asset_references`：元素/角色/灵魂 ID，含 provider_reference_id。
- `asset_acl`：主体（用户/项目/工作区/服务账户）× 权限（读/用/更新/删除/撤销）。
- `asset_audit_events`：执行者、动作（含 `mention_resolve`）、运行/请求 ID。
- `asset_search_index`：提示词文本、关键词、端点族、嵌入 —— 可重建，非权威。

## UI 行为

（对 UI 的服务端义务；完整 UI 规格见 `08-asset-library-ui.md`。）

- 列表 API 仅返回具有 `read` 权限的行；`allowed_operations` 按 mention 项预计算，UI 无需猜测。
- Mention 结果排除 `revoked`，包含 `not_ready` 作为不可用项。
- 详情负载一次性携带检查器所需全部内容（资产 + 上下文 + 来源链路 + 权限 + 操作记录）。
- 事件携带足够信息以无需重新获取即可更新卡片（`asset_id`、状态、`thumbnail_url`）。
- 上传初始化预先返回大小上限（`max_size`），UI 可预验证。

## 权限与错误处理

权限动作：针对主体作用域（用户/项目/工作区/服务账户）的 `read | use | update | delete | revoke`。Mention 解析写入操作记录事件（`mention_resolve`）。拒绝纯文本身份声明：后端必须拒绝歧义的纯文本解析，仅信任结构化 `entity_id`（§前端交互契约）。

类型化错误（§错误策略）：`asset_access_denied`、`asset_not_ready`、`asset_revoked`、`asset_not_found`、`upload_mime_not_allowed`、`smart_group_query_invalid`、`collection_expand_requires_confirmation`。

硬性规则：

- 生成链中任何引用错误均失败关闭（fail closed）—— 不得警告并继续（§错误策略，与 U-29 语义一致）。
- 索引不可用时降级搜索（语义召回"待定"），永不隐藏资产（§搜索和索引）。
- P0 中引用训练可使用模拟提供商，但不得伪造 `ready`（§P0 切片 4）。
- 不得在上下文负载中记录或返回提供商密钥（`hermes-soulid-element-asset-model.md` §安全要求）。

## 验收标准

- 上传初始化/完成产生 `assets` 行，状态从 `uploading` 经 `processing` 转换至 `ready`，并带有真实缩略图（`06-delivery-plan.md` P0；`03-media-asset-contract.md` §Acceptance："上传和生成媒体产生资产 ID"）。
- 最终化媒体作业的输出以完整来源链路形式作为资产出现（"所有生成结果具有来源链路"）。
- `GET /api/assets/{id}` 返回足以供检查器使用的生成上下文块。
- 已撤销资产从 mention 结果中消失，且 `use` 操作因 `asset_revoked` 失败。
- 智能组开放重新评估实时查询（无存储成员列表）。
- 租户/项目隔离：跨项目资产 ID 返回 `asset_not_found`。
- 生成中的每次 `use` 均产生可按运行 ID 追踪的操作记录事件。

## 非目标

- 执行媒体作业（媒体作业服务）或持有提供商凭证（TokenRouter）。
- 在服务数据库中存储二进制文件 —— 对象存储拥有字节。
- 公开分享/发布界面。
- 在 FTS + 硬过滤工作前构建向量/语义索引。
- 自动提升任务文件（仅显式提升）。

## 待解决问题

1. 存储选择：对象存储（S3 兼容？自托管本地磁盘？）、关系型数据库、FTS/向量引擎均未选定。
2. `generation_jobs` 表所有权：本文档将其置于此处，而 `10-media-job-service.md` 的 MediaJob 封装暗示作业服务拥有作业状态 —— 单表双写，还是事件镜像？
3. 自托管 Hermes 的多租户部署形态：MVP 中 tenant_id / workspace_id 是真实边界还是常量默认值？
4. 缩略图/嵌入管道运行时：进程内 worker 还是队列；`processing -> ready` 的 P0 可接受延迟是多少？
5. `ultra_asset_download` 是物化到任务文件根目录（链接至 `06-files-task-file-browser.md`）还是仅流式传输签名 URL？
6. 灵魂 ID 提供商抽象：P1 中哪个提供商支撑 `soul_id` 训练，且当该提供商禁用时 `ready` 引用如何处理？
