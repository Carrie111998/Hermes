文档路径：docs/ultra-studio-zh/product-specs/components/17-tokenrouter.md

# 17 TokenRouter 凭证与额度路由

状态：中文详细版（由英文核对版与中文阅读页整理）  
来源：`docs/ultra-studio-product-specs/components/17-tokenrouter.md` 与 `docs/ultra-studio-product-specs/components-zh/17-tokenrouter.html`  
日期：2026-06-11

## 目的与范围

控制 provider 凭证、租户策略、额度、并发和计费。沙箱或浏览器不拿真实上游 key，只拿短期 Hermes token。

本页不是英文原文的逐词镜像，而是面向实施的中文详细版：保留原文的组件边界、实现状态、状态机、API/事件、数据模型、权限错误和验收要求，方便后续直接拆任务。

## 实现状态

- credential-flow 文档设计完整，但代码里没有 tokenrouter/token_router 实现。
- 现状是 provider key 通过服务端环境变量静态解析。
- 邻近代码：plugins/video_gen/atlas/client.py、agent/credential_pool.py、agent/secret_sources/*。

## 主要缺口

- 没有 JWT claim、policy evaluation、vault exchange、decision logging。
- 没有租户/工作区/预算/并发 enforcement substrate。
- MediaJob envelope 里的 tokenrouter_decision_id 还是设计字段。

## 用户入口

- 如果组件是用户可见能力，应从左侧导航、会话中心、检查器或搜索结果进入。
- 如果组件是基础设施能力，应通过上层 UI 的状态、错误、进度或操作记录 id 间接暴露。
- 所有入口必须展示真实状态；没有数据时显示空态，不展示伪数据。

## 功能列表

P0 必做：

- 单人 self-host MVP 可先不建 TokenRouter，但文档和 UI 要标注为未实现。
- 服务端继续从 .env 读 Atlas key，禁止发送到浏览器或 sandbox。
- 为未来预留 decision_id 字段，不假装已有额度控制。

P1 / P2 后续：

- Vault/OpenBao、Redis quota、OPA policy、Postgres 操作记录、mTLS service identity。
- 按 tenant/workspace/project/session 计量和限流。

## 状态机

通用状态约束：

```text
idle / empty
  -> loading / resolving
  -> ready
  -> active / running
  -> complete
  -> failed
  -> retrying / recovered
```

- `idle / empty`：没有数据时必须明确展示空态。
- `loading / resolving`：正在加载目录、会话、资产或策略时给出可见状态。
- `ready`：组件具备执行条件，但尚未开始动作。
- `active / running`：正在运行时必须产生事件或进度，不能让 UI 卡死。
- `complete`：完成后要能被历史、资产或操作记录链路找回。
- `failed`：失败必须是类型化错误，并能说明用户可采取的下一步。

## API 与事件

本组件应遵循统一的 Gateway / Session / Tool 事件风格：

| 类型 | 要求 |
|---|---|
| 查询 API | 返回真实后端状态，不能把空数组伪装成成功能力。 |
| 操作 API | 使用显式动作，如 create、resume、install、enable、upload、download、retry。 |
| 事件 | 对长任务输出 `started`、`progress`、`complete`、`failed` 等状态。 |
| 错误 | 错误码必须稳定，错误信息面向用户可理解，内部细节进日志。 |

## 数据模型

最小数据模型应包含：

| 字段 | 用途 |
|---|---|
| `id` | 可追踪、可恢复、可检查的稳定标识。 |
| `workspace_id` / `project_id` | 权限、资产、记忆和任务的隔离边界。 |
| `status` | UI 状态和事件状态的真实来源。 |
| `source` / `provenance` | 标明对象来自上传、生成、市场、技能、记忆或外部引用。 |
| `created_at` / `updated_at` | 支持历史排序和排错。 |

如果组件引用资产、角色、媒体任务、Skill 或模型，必须保存引用 id，而不是复制整份对象。

## UI 行为

- 页面和面板优先服务创作流程，不显示与视频/图片创作无关的管理噪声。
- 操作按钮只在有权限、有数据、有可执行后端时启用。
- 加载、空态、失败、成功四类状态要视觉上可区分。
- 需要用户决策的地方使用结构化问题或确认卡，不靠模型自由文本猜测。
- 移动端不能让侧栏、检查器或底部输入框相互遮挡。

## 权限与错误处理

- 权限不足、未安装、未启用、缺少 provider、缺少资产、配额不足必须是类型化错误。
- 用户可见错误不能暴露 provider key、内部 prompt、vault 路径或跨租户对象 id。
- 对生成、下载、删除、外部发布等高风险动作，需要审批或至少明确确认。
- 禁止静默降级：如果真实能力不存在，就显示阻塞原因，不要展示假成功。

## 验收标准

- 没有 TokenRouter 时不要展示额度/策略已生效。
- Atlas key 不出现在前端 bundle、日志或 job payload。
- 云模式上线前必须 fail-closed。

## 非目标

- 不做仅用于展示的假数据 demo。
- 不把组件职责混入其他组件，例如 Marketplace 不拥有资产，TokenRouter 不做媒体预处理。
- 不把内部管理入口暴露成创作者默认界面。
- 不在 P0 中引入超出垂直切片需要的云端重构。

## 开放问题

| 问题 | 处理方式 |
|---|---|
| 哪些字段必须 P0 持久化？ | 以真实恢复、操作记录和错误排查为准。 |
| 是否需要多租户隔离？ | 本地单用户可先弱化，但 cloud 设计必须保留 `tenant/workspace/project` 边界。 |
| 组件是否需要独立 API？ | 优先复用 Gateway / Session / Tool / Asset Service，不为 UI 重复建状态。 |

## 与英文源文档的结构对应

| 英文章节 | 中文章节 |
|---|---|
| Purpose & Scope | 目的与范围 |
| Implementation Status | 实现状态 |
| User Entry Points | 用户入口 |
| Feature List | 功能列表 |
| State Machine | 状态机 |
| APIs & Events | API 与事件 |
| Data Model | 数据模型 |
| UI Behavior | UI 行为 |
| Permissions & Error Handling | 权限与错误处理 |
| Acceptance Criteria | 验收标准 |
| Non-Goals | 非目标 |
| Open Questions | 开放问题 |
