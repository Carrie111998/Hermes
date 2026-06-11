# 任务 / 会话历史

状态：部分实现（partial）—— 会话存储、带消息搜索的会话浏览器以及会话上下文提示已实现；任务产品界面（带作业/输出的任务行、完整上下文恢复）仍为规格定义。  
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/05-memory-marketplace-files.md`
  (§Tasks, §Search, §Access Control), `02-agent-runtime-contract.md`
  (§Session Lifecycle, §Event Stream), `01-product-surface.md` (§Main Jobs:
  "Continue work"), `06-delivery-plan.md` (P0 gates, P2 gate on job
  survival)
- 代码（本次会话已验证）：`gateway/session.py` (`SessionStore`,
  `SessionEntry`, `SessionContext`, `SessionSource`, `build_session_key`,
  `build_session_context_prompt`, hashed ids via `_hash_sender_id` /
  `_hash_chat_id`), `web/src/pages/SessionsPage.tsx` (`SessionRow`,
  `MessageList`, `SessionsPagination`, search highlighting),
  `tools/session_search_tool.py`, `hermes_state.py`, `agent/insights.py`
  (`InsightsEngine._get_sessions`)

## 目的与范围

任务代表工作历史和运行中的作业
（`05-memory-marketplace-files.md` §Tasks）。产品承诺是
"继续工作"：点击任务可恢复对话记录、活动/已完成的作业、任务文件、选定的模型、活动技能配置文件以及相关记忆
（`01-product-surface.md` §Main Jobs; `05-memory-marketplace-files.md`
§Tasks）。

本规格涵盖任务列表界面、任务行约定、恢复语义，以及"会话"（运行时对话状态，由网关拥有）与"任务"（用户浏览的产品对象）之间的关系。媒体作业持久性本身由 `10-media-job-service.md` 拥有；恢复的任务所打开的聊天界面是 `02-creative-chat-ui.md`。

## 实现状态

| 状态 | 条目 | 引用 |
|---|---|---|
| 已实现（Implemented） | 按平台/来源键控的持久会话存储，包含会话条目和上下文 | `gateway/session.py` (`SessionStore`, `SessionEntry`, `build_session_key`) |
| 已实现（Implemented） | 会话记录中的隐私哈希标识符 | `gateway/session.py` (`_hash_sender_id`, `_hash_chat_id`) |
| 已实现（Implemented） | 用于恢复/多用户上下文的会话上下文提示组装 | `gateway/session.py` (`build_session_context_prompt`, `build_session_context`, `is_shared_multi_user_session`) |
| 已实现（Implemented） | 会话浏览器：列表、分页、打开对话记录、带命中高亮和自动滚动的消息搜索 | `web/src/pages/SessionsPage.tsx` (`SessionRow`, `MessageList`, `SnippetHighlight`, `SessionsPagination`) |
| 已实现（Implemented） | 在历史对话记录中渲染的工具调用 | `web/src/pages/SessionsPage.tsx` (`ToolCallBlock`) |
| 已实现（Implemented） | 面向智能体的历史会话搜索 | `tools/session_search_tool.py` |
| 已实现（Implemented） | 用于分析的会话聚合（计数、持续时间、模型/平台细分） | `agent/insights.py` (`InsightsEngine._get_sessions`, `_compute_overview`) |
| 已实现（Implemented） | 进程级状态持久化辅助工具 | `hermes_state.py` |
| 已规定，未构建（Specified, not built） | 与管理后台会话页面不同的 `Tasks` 导航入口 | `01-product-surface.md` §Left Nav Shell |
| 已规定，未构建（Specified, not built） | 任务行字段：标题、最后用户请求、状态、活动作业、输出计数、来源 | `05-memory-marketplace-files.md` §Tasks; `SessionsPage` 行显示会话元数据，而非作业/输出计数 |
| 已规定，未构建（Specified, not built） | 完整恢复：对话记录 + 作业 + 任务文件 + 模型 + 技能配置文件 + 记忆 | `05-memory-marketplace-files.md` §Tasks; `02-agent-runtime-contract.md` `session.resume` |
| 已规定，未构建（Specified, not built） | 携带活动媒体作业、选定资源、任务文件根目录、技能配置文件的会话状态 | `02-agent-runtime-contract.md` §Session Lifecycle |
| 已规定，未构建（Specified, not built） | "在媒体作业期间刷新浏览器不会丢失作业" | `02-agent-runtime-contract.md` §Acceptance; 依赖于 `10-media-job-service.md` |

## 用户入口点

- 左侧导航中的 `Tasks` 入口（计划中）；目前最接近的界面是仪表板会话页面（`web/src/pages/SessionsPage.tsx`）。
- 聊天中的"打开上一个…任务"措辞 — 智能体通过会话搜索找到先前工作（已实现：`tools/session_search_tool.py`），然后由路由器/用户恢复（恢复流程计划中）。
- 跨界面搜索返回任务结果（计划中，
  `05-memory-marketplace-files.md` §Search）。
- 从媒体作业或资源深链回其源任务
  （计划中；来源链路按 `03-media-asset-contract.md` §来源链路（Lineage） 携带 `user/session/run`）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 列出历史会话并分页 | 已实现（Implemented） (`SessionsPage`) |
| 跨会话搜索消息并高亮显示命中 | 已实现（Implemented） (`SessionsPage` search + `SnippetHighlight`) |
| 打开包含工具调用的历史对话记录 | 已实现（Implemented） (`MessageList`, `ToolCallBlock`) |
| 智能体端召回历史会话 | 已实现（Implemented） (`tools/session_search_tool.py`) |
| 带状态、活动作业、输出计数、日期、来源的任务行 | 已规划（Planned） |
| 任务行上的来源标签 `web / tui / cli / panel` | 已规划（Planned） (`SessionSource` 存在于 `gateway/session.py`；未作为规格的四个产品值展示) |
| 一键恢复到实时聊天会话 | 已规划（Planned） (`session.resume` contract) |
| 恢复选定的模型 + 活动技能配置文件 | 已规划（Planned）；会话状态字段尚未在 `SessionEntry` 中 |
| 将活动/完成的媒体作业恢复到任务视图 | 已规划（Planned）；依赖于持久的 MediaJob 记录 |
| 恢复任务文件和相关记忆 | 已规划（Planned） |
| 任务列表上的运行中作业指示器 | 已规划（Planned） |
| 重命名 / 归档 / 删除任务 | 已规划（Planned）；不在规格包中 — 参见待解决问题 |

## 状态机

任务状态（产品级，派生 — 今天未存储为单一枚举）：

```text
active      (live session; gateway holds runtime state)
  -> idle   (no live connection; transcript + state durable)
  -> resumed -> active
idle | active
  -> archived (explicit user action; read-only)
```

任务行上的派生显示状态结合了来自媒体作业服务的会话活跃度和作业状态：

| Display | Condition |
|---|---|
| Running | ≥1 个活动媒体作业，无论 websocket 活跃度如何 |
| Waiting | 待处理的批准或询问用户问题（`approval.requested` 未解决） |
| Idle | 无实时连接，无活动作业 |
| Failed | 最后一个作业/轮次以类型化错误结束 |

规则：刷新或断开连接绝不能将任务转换为终止状态；只有显式归档才会（`02-agent-runtime-contract.md`
§Acceptance）。

## API 与事件

已实现：

- 网关会话生命周期和存储 — `gateway/session.py`
  （`SessionStore` 创建/查找；通过 `build_session_key` 生成会话键）。
- 由 `SessionsPage` 消费的会话列表/读取 API（由仪表板 Web 服务器提供；页面已验证，路由形状此处未重新推导）。
- 面向智能体的会话搜索工具 — `tools/session_search_tool.py`。

计划中（按 `02-agent-runtime-contract.md` §Session Lifecycle）：

- `session.create`, `session.resume`（恢复消息、活动作业、
  选定资源、任务文件），`prompt.submit`, `slash.exec`。
- 任务列表 API：

```http
GET /api/tasks?project_id=&status=&source=&q=&cursor=
GET /api/tasks/{task_id}            # row + restore manifest
POST /api/tasks/{task_id}/archive
```

事件：任务行从现有网关事件流更新
（`media_job.created/updated`, `asset.ready`, `approval.requested` — 参见
`02-agent-runtime-contract.md` §Event Stream）；无单独的任务事件通道。

## 数据模型

已实现：网关存储中的会话条目和上下文
（`gateway/session.py`：带哈希发送者/聊天 ID 的 `SessionEntry`，
`SessionContext`），以及洞察数据库中的分析投影
（`agent/insights.py`）。

计划中的任务投影（会话 + 作业的视图，非第二真相源）：

```text
task_row
- task_id            (= session id)
- title              (generated; agent/title_generator.py exists)
- last_user_request
- status             (derived; see State Machine)
- active_job_ids[]   (from Media Job Service)
- output_count       (ready assets linked to this session)
- source: web | tui | cli | panel
- project_id, workspace_id
- created_at, last_activity_at

restore_manifest
- transcript ref
- active/complete job ids
- task_files root
- selected model
- active skill profile
- memory scope refs
```

恢复必须从所属组件读取每个元素（作业来自媒体作业服务，文件来自任务根目录，记忆来自记忆）— 清单是指针，而非副本。

## UI 行为

- 任务页面列出任务行：标题、状态芯片、最后请求片段、活动作业旋转器、输出计数、来源徽章、日期。默认排序：
  最后活动时间降序。
- 点击行打开任务视图：中间恢复的对话记录，任务文件浏览器标签页，以及显示最近作业/资源的检查器（按 `01-product-surface.md` §Required States）。
- 运行中任务在作业中途打开时显示来自恢复事件的实时作业状态，而非冻结快照。
- 搜索命中深链到对话记录位置（`SessionsPage` `MessageList` 中现有的自动滚动到命中行为是参考实现）。
- 已归档任务以只读方式渲染，并显示可见的归档横幅。
- 空状态为空白；无演示任务。

## 权限与错误处理

权限：任务的作用域与其会话相同（用户/工作区/项目）。
共享对话不意味着共享沙盒或凭据
（`05-memory-marketplace-files.md` §Access Control）；共享任务视图必须排除任务文件和凭据，除非明确共享。

错误约定：

| Error | Trigger |
|---|---|
| `session_not_found` | 过时的任务 ID 或跨项目访问。 |
| `resume_state_incomplete` | 恢复清单引用了缺失的片段（例如作业记录已消失）。必须列出恢复失败的内容；绝不能静默地将部分状态渲染为完整状态。 |
| `sandbox_unavailable` | 恢复需要无法附加的沙盒（类型化于 `02-agent-runtime-contract.md` §Error Contract）。 |
| `archive_failed` | 归档操作失败；任务保持先前状态。 |

仅允许在显式逐元素失败通知的情况下进行部分恢复
（例如"2 个任务文件缺失"）；在未验证所有元素的情况下声称完整恢复违反了包的无伪造规则。

## 验收标准

- 任务页面列出具有派生状态的实时会话；具有活动媒体作业的会话即使在浏览器刷新后仍显示为运行中
  （`02-agent-runtime-contract.md` §Acceptance）。
- 点击任务恢复对话记录并显示活动作业及其当前状态（"恢复的会话显示活动媒体作业及其当前状态"）。
- 恢复的任务显示会话最后使用的相同选定模型和技能配置文件（一旦这些字段进入会话状态）。
- 消息搜索找到来自先前会话的短语并深链到该位置。
- 来源徽章反映真实的来源界面。
- 归档是显式的，可逆状态被保留，且已归档任务保持可搜索。

## 非目标

- 项目管理功能（受让人、截止日期、看板）— 现有的
  `plugins/kanban` 是单独的插件，非此界面。
- 跨用户任务分配或共享编辑。
- 在任务投影中存储作业/资源状态（由媒体作业 /
  资源服务拥有）。
- 歪曲内容的合成任务标题（标题来自现有的标题生成路径，`agent/title_generator.py`）。

## 待解决问题

1. `task_id` 是否就是会话 ID，还是一个任务可以跨多个
   会话（例如跨界面恢复）？
2. 重命名/归档/删除语义不在规格包中 — 哪些动词在 P1 交付？
3. 非 Web 会话（TUI/CLI/网关平台）如何映射到四个产品来源值，鉴于 `gateway/session.py` `SessionSource` 携带更丰富的平台信息？
4. 恢复是重新附加先前的沙盒（`sandbox.attach`）还是使用 `restore_artifacts` 冷启动（`02-agent-runtime-contract.md` §Sandbox
   Lifecycle）？
5. 输出计数定义：仅就绪资源，还是所有已完成的作业输出包括失败但可检查的？
6. 空闲任务及其对话记录的保留/TTL；与已聚合会话统计的洞察数据库的关系。
