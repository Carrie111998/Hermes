文档路径：docs/ultra-studio-zh/product-specs/components/08-asset-library-ui.md

# 08 资产库界面（Asset Library UI）

状态：仅规格阶段（spec-only）—— `web/src` 中尚无资产库页面、资产画廊、提及菜单或选择器；设计已在资产库后端设计文档中规定，仓库外有静态布局参考。  
日期：2026-06-11

来源：

- 文档：`docs/hermes-asset-library-backend-design.md` (§前端交互契约,
  §`@` mention 查询, §Picker 与 ask_user_question 复用, §实时事件,
  §前端必须配合的点), `docs/ultra-studio-product-specs/03-media-asset-contract.md`
  (§Asset Card UI, §Asset Types, §QA, §Acceptance),
  `01-product-surface.md` (§Right: Inspector / Live Panel),
  `06-delivery-plan.md` (P0 item 9, P1 items 6-7),
  `docs/hermes-soulid-element-asset-model.md` (§UI 要求)
- 演示参考（仅布局参考，仓库外）：
  `/Users/lifcc/Desktop/code/work/infra/her/asset-library-demo.html`
- 代码：无 —— `web/src/pages/` 不包含任何 asset/library/gallery 页面
  (本次会话已验证 listing)。聊天侧的 `ChatInspector.tsx` 在
  `03-inspector-live-panel.md` 中单独规定。

## 目的与范围

资产库界面（Asset Library UI）是项目类型化资产的浏览、搜索和复用界面：上传资产（`media_input`）、生成输出（`image_job` / `video_job` / `audio_job`）、可复用的元素（`element`）、角色（`character`）、灵魂 ID（`soul_id`）引用，以及普通合集（Collection）和智能分组（Smart Group）。它是"项目级资产服务，而不是普通图库"（`hermes-asset-library-backend-design.md`
§目标）—— 每张卡片操作都将结构化资产引用反馈到生成流程中。

范围：资产画廊与筛选、资产详情视图、普通合集和智能分组界面、聊天编辑器中的 `@` 提及菜单和选择器，以及实时更新。后端语义（实体、权限、来源链路、索引）由 `09-asset-service.md` 负责；每次选择的上下文面板为 `03-inspector-live-panel.md`。

## 实现状态

| 状态 | 条目 | 引用 |
|---|---|---|
| 已规定，未构建（Specified, not built） | 带硬过滤条件的资产画廊：类型（type）、媒体类型（media_type）、状态（status）、端点（endpoint）、来源（source）、合集（collection） | `hermes-asset-library-backend-design.md` §资产列表和详情（list API 参数） |
| 已规定，未构建（Specified, not built） | 资产详情视图：提示词、参数、种子、模型上下文、来源链路、权限、操作记录 | `hermes-asset-library-backend-design.md` §资产列表和详情（detail payload） |
| 已规定，未构建（Specified, not built） | 资产卡片操作：下载、检查、复用、转为元素、创建角色 | `03-media-asset-contract.md` §Asset Card UI |
| 已规定，未构建（Specified, not built） | 普通合集界面（Collection UI）：用户手动加入/移出成员 | `hermes-asset-library-backend-design.md` §Collection |
| 已规定，未构建（Specified, not built） | 智能分组构建器（Smart Group builder）：保存前预览命中结果 | `hermes-asset-library-backend-design.md` §Smart Group |
| 已规定，未构建（Specified, not built） | 聊天输入框 `@` 提及菜单：按类型分组展示结果 | `hermes-asset-library-backend-design.md` §`@` mention 查询 |
| 已规定，未构建（Specified, not built） | 共享选择器：复用到 `ask_user_question(entity)` 上下文 | `hermes-asset-library-backend-design.md` §Picker 与 ask_user_question 复用 |
| 已规定，未构建（Specified, not built） | 结构化消息提交：携带 mention payload 和附件，而不是纯文本 | `hermes-asset-library-backend-design.md` §前端交互契约 |
| 已规定，未构建（Specified, not built） | 实时事件订阅：`asset.ready`、`job.status` 等 | `hermes-asset-library-backend-design.md` §实时事件 |
| 已规定，未构建（Specified, not built） | 从结果卡片创建元素（Element）或角色（Character）的入口 | `06-delivery-plan.md` P1 item 7；`hermes-soulid-element-asset-model.md` §UI 要求 |
| 布局参考（Layout reference） | 静态资产画廊演示，无数据接入 | `asset-library-demo.html`（仓库外；不作为代码实现声明） |

## 用户入口

- 资产库页面（Asset library page）：在导航中的位置与 My office 相对仍是开放问题；
  `01-product-surface.md` IA 未列出专门的 Assets 入口 —— 见
  §Non-Goals "do not merge Marketplace, Memory, and Files into one generic
  Assets page"，这意味着资产是独立界面。
- 聊天编辑器中的 `@` 提及（`@asset`, `@character`, `@element`,
  `@collection`, `@group` 前缀）。
- 工作流中由 `ask_user_question(entity)` 打开的选择器。
- 检查器（Inspector）对选中资产的"复用（reuse）/ 转为元素（convert to element）/ 创建角色（create character）"操作（`03-inspector-live-panel.md`）。
- 上传完成：新上传同时出现在资产库和聊天侧面板（`hermes-asset-library-backend-design.md` §前端必须配合的点）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 按媒体类型展示缩略图网格 | 已规划（Planned） |
| 硬过滤条件：项目、类型、媒体类型、状态、端点、来源、合集、时间 | 已规划（Planned） |
| 关键词 + 语义搜索；索引未就绪时降级到全文搜索（FTS） | 已规划（Planned），见 `hermes-asset-library-backend-design.md` §搜索和索引降级规则 |
| 资产详情：展示生成上下文（endpoint、model_route、prompt、seed、params、request/run/session ids） | 已规划（Planned） |
| 来源面板：说明"这个资产从哪里来" | 已规划（Planned） |
| 操作记录视图 | 已规划（Planned） |
| 通过签名 URL 或真实 URL 下载 | 已规划（Planned） |
| 将资产作为结构化引用插回聊天输入框 | 已规划（Planned） |
| 将符合条件的资产保存为元素（Element）或创建角色（Character） | 已规划（Planned） |
| 普通合集：创建、重命名、添加/移除成员 | 已规划（Planned） |
| 智能分组：构建查询、预览命中数、保存；打开时实时计算 | 已规划（Planned） |
| `@` 提及菜单：按类型分组，展示状态副标题和缩略图 | 已规划（Planned） |
| 选择器上下文：`chat_prompt`、`asset_picker`、`character_picker`、`smart_group_builder`、`collection_member_add` | 已规划（Planned） |
| 已撤销资产从提及菜单隐藏；`not_ready` 可见但不可用 | 已规划（Planned） |
| 卡片实时状态更新：上传中 -> 处理中 -> 已就绪 | 已规划（Planned） |

## 状态机

UI 渲染由 Asset Service 拥有的资产生命周期：

```text
uploading -> processing -> ready -> archived
failed / revoked / deleted (terminal or gated)
```

各状态的 UI 渲染规则：

| 状态 | 卡片行为 |
|---|---|
| `uploading` / `processing` | 占位缩略图 + 进度；不能被选为 `use` |
| `ready` | 开放完整操作：复用、下载、符合条件时转元素/建角色 |
| `failed` | 显示错误标记；可检查，不可使用（`03-media-asset-contract.md` §Acceptance："Failed jobs remain inspectable"） |
| `revoked` | 从提及菜单/选择器结果中排除；只在资产库中以撤销标记展示 |
| `archived` | 默认视图隐藏；可通过过滤器访问 |

Reference (`element`/`character`/`soul_id`) 状态渲染
`queued -> training -> ready` / `failed` / `revoked`，无虚假 `ready`
（`hermes-asset-library-backend-design.md` §P0 切片 item 4）。

## API 与事件

UI 消费 Asset Service API（定义于 `09-asset-service.md`，
与设计文档原文一致）：

- `GET /api/assets?…`：带硬过滤的列表；`GET /api/assets/{id}`,
  `/lineage`（来源链路）、`/audit`（操作记录）。
- `GET /api/assets/mentions?q=&project_id=&types=&context=` —— 单一共享端点，用于提及菜单和所有选择器上下文。
- 普通合集和智能分组的 CRUD + `smart-groups/preview`.
- `POST /api/assets/references`：创建 element / character / soul_id 引用。

提交的聊天消息携带结构，而非仅文本：

```json
{
  "text": "用 @Luna 生成一个天台夜景视频",
  "mentions": [{ "span": [2,7], "entity_type": "character",
                 "entity_id": "char_luna", "asset_ref_type": "soul_id",
                 "operation": "use" }],
  "attachments": [{ "asset_id": "media_input_123", "role": "image_reference" }]
}
```

订阅事件：`asset.upload.started`, `asset.processing`,
`asset.ready`, `asset.failed`, `asset.revoked`, `asset.indexed`,
`collection.updated`, `smart_group.updated`, `reference.status`,
`job.status` (`hermes-asset-library-backend-design.md` §实时事件).

## 数据模型

UI 不拥有持久状态。客户端必须维护的状态：

- Composer mention tokens 作为结构化对象（id, type, span, ref type）——
  "Composer 内部维护 mention token，不只存文本"
  (`hermes-asset-library-backend-design.md` §前端必须配合的点).
- 资产画廊的过滤状态和游标分页。
- 由事件驱动的资产卡片缓存（id -> status/thumbnail），通过上述事件失效；禁止乐观伪造 `ready` 状态。
- 智能分组构建器草稿（`query_json` 镜像），保存前只存在于客户端。

所有权威数据来自 Asset Service；UI 不得在客户端派生权限。

## UI 行为

- 提及菜单按类型分组：资产、角色、元素、普通合集、智能分组；每行展示名称、类型、状态副标题
  （例如 "face identity · ready · 4 source images"）和缩略图。
- 选择 `@collection` / `@group` 时先打开预览（成员列表 / 命中数量），扩展前必须显式确认；不能静默插入大量资产。
- 同名实体必须在菜单中明确消歧。
- `not_ready` 条目显示为不可选择，并展示原因；如果仍然提交，后端返回 `asset_not_ready`，输入框内联显示错误。
- 资产详情里的"复用（reuse）"会向输入框插入结构化引用；元素 / 角色按钮调用 Asset Service，不写本地状态。
- 卡片绝不暴露内部文件系统路径
  (`03-media-asset-contract.md` §Asset Card UI).
- 上传进度必须可见；上传完成后，资产同时出现在资产库和聊天侧面板。
- 空资产库显示空态和上传入口，不展示样例资产。

## 权限与错误处理

列表 API 只返回具有 `read` 权限的资产；提交时再次校验 `use` 权限。UI 必须展示设计文档中的类型化错误：

| 错误 | UI 行为 |
|---|---|
| `asset_access_denied` | 阻断卡片操作，并显示权限说明。 |
| `asset_not_ready` | 在提及 chip 上显示输入框内联错误。 |
| `asset_revoked` | 提及 chip 变为无效；移除前阻止消息提交。 |
| `asset_not_found` | 移除过期卡片，并提示刷新。 |
| `upload_mime_not_allowed` | 上传前置校验失败，显示允许的类型。 |
| `smart_group_query_invalid` | 构建器显示字段级校验错误。 |
| `collection_expand_requires_confirmation` | 打开预览/确认对话框；这是正常路径，不是错误 toast。 |

失败关闭规则（fail-closed）：只要生成提交中包含任何无效引用，就整体阻断；不能提示警告后继续执行（`hermes-asset-library-backend-design.md`
§错误策略).

## 验收标准

- 提及流程端到端可用：输入 `@lun` 能列出 Luna 和状态副标题；选择后插入结构化引用；提交时携带 `mentions[]`（可在请求 payload 中验证）。
- `@collection` 未经确认步骤不能展开。
- 被撤销资产在一个事件周期内从提及结果中消失。
- 生成资产的详情页展示真实模型、作业、提示词和输入细节（`03-media-asset-contract.md` §Acceptance）。
- 下载交付真实二进制：通过存储 URL 或本地物化文件。
- 智能分组预览命中数与保存后第一次计算结果一致。
- 失败资产和 `not_ready` 资产如实展示；任何卡片都不得伪造 `ready`。

## 非目标

- 拥有资产状态、权限计算或来源链路计算；这些归 Asset Service 所有。
- 公共/共享图库或发布页面。
- 在资产库内实现图片编辑工具；编辑归生成工作流所有。
- 把 Marketplace、Memory、Files 合并进这个页面
  (`01-product-surface.md` §Non-Goals).
- 把纯文本 `@Luna` 解析当作权限机制；后端应按设计拒绝有歧义的纯文本解析。

## 开放问题

1. 导航位置：单独 Assets 入口，还是放在 My office 内的标签页？`01-product-surface.md` 的 IA 树没有明确列出。
2. 虚拟网格要求：预期资产库规模和缩略图加载策略尚未规定。
3. 多选和批量动作（例如把 N 个资产加入合集）是否进入 P1？设计文档尚未规定。
4. 当同一实体同时匹配资产和元素来源时，提及端点是否跨类型去重？
5. 视频卡片预览行为：悬停播放还是固定首帧？QA 合同只保证首帧（`03-media-asset-contract.md` §QA）。
6. 智能分组相似度阈值 UI 放在哪里（构建器 slider？），默认值是什么（设计文档示例使用 0.78）。
