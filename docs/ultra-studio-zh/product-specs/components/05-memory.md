# 05 记忆系统（Memory）

状态：部分实现（partial）—— 代理端内存存储、提供者插件层和提示注入已实现；Memory 产品界面（UI 中可见、可编辑、带来源标注的条目）仍为规格说明。  
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/05-memory-marketplace-files.md` (§Memory, §Search, §Access Control, §Acceptance), `01-product-surface.md` (§Left Nav Shell), `02-agent-runtime-contract.md` (§Session Lifecycle), `06-delivery-plan.md` (P1 item 9), `docs/hermes-references-knowledge-model.md`
- 代码（本次会话已验证）：`tools/memory_tool.py` (`MemoryStore`, `memory_tool`, `get_memory_dir`), `agent/memory_manager.py` (`MemoryManager`, `build_memory_context_block`, `sanitize_context`, `StreamingContextScrubber`), `agent/memory_provider.py` (`MemoryProvider` ABC), `plugins/memory/` (`byterover`, `hindsight`, `holographic`, `honcho`, `mem0`, `openviking`, `retaindb`, `supermemory`)

## 目的与范围

Memory 存储应影响未来工作的持久性事实：用户偏好、品牌规则、项目事实、可复用的提示决策、模型偏好、被拒绝的风格以及安全/策略说明（`05-memory-marketplace-files.md` §Memory）。管理规则是"Memory 必须可见且可编辑。隐藏的内存会产生信任问题。"

本规格涵盖内存数据层、提供者插件层、提示注入以及计划中的 Memory 页面。它不包括会话记录（由 `07-tasks-session-history.md` 负责）或 RAM 监控（`gateway/memory_monitor.py` 是进程 RSS 日志记录，与此组件无关）。

## 实现状态

| 状态 | 条目 | 引用 |
|---|---|---|
| 已实现（Implemented） | 基于文件的内存存储，包含按目标划分的文件、字符预算（默认 memory 2200 / user 1375）、文件锁定以及针对外部编辑的漂移检测 | `tools/memory_tool.py` (`MemoryStore.__init__`, `_file_lock`, `_drift_error`, `load_from_disk`) |
| 已实现（Implemented） | 面向代理的内存工具：添加 / 替换 / 删除条目 | `tools/memory_tool.py` (`MemoryStore.add`, `.replace`, `.remove`, `memory_tool`) |
| 已实现（Implemented） | 内存注入到系统提示中 | `tools/memory_tool.py` (`format_for_system_prompt`), `agent/memory_manager.py` (`build_memory_context_block`) |
| 已实现（Implemented） | 注入前的上下文清理（提示注入清理） | `agent/memory_manager.py` (`sanitize_context`, `StreamingContextScrubber`), `tools/memory_tool.py` (`_scan_memory_content`) |
| 已实现（Implemented） | 基于 `MemoryProvider` ABC 的可插拔外部内存提供者 | `agent/memory_provider.py`; `plugins/memory/mem0/__init__.py` (Mem0 Platform API, circuit breaker), plus `byterover`, `hindsight`, `holographic`, `honcho`, `openviking`, `retaindb`, `supermemory` |
| 已规定，未构建（Specified, not built） | Memory 导航条目和 Memory 页面：列表、查看、删除/撤销 | `01-product-surface.md` §Left Nav Shell；`web/src/pages/` 中不存在 memory 页面（本次会话已验证列表） |
| 已规定，未构建（Specified, not built） | 每个条目的来源归属：显示来源会话或用户动作 | `05-memory-marketplace-files.md` §Memory；`MemoryStore` 条目为纯字符串，无来源字段 |
| 已规定，未构建（Specified, not built） | 区分用户手写记忆和代理推断记忆 | `05-memory-marketplace-files.md` §Memory |
| 已规定，未构建（Specified, not built） | 工作区/项目作用域：Memory 按用户/工作区/项目划分范围 | `05-memory-marketplace-files.md` §Access Control；当前存储是按 Hermes 主目录，而非按项目 |
| 已规定，未构建（Specified, not built） | 统一搜索中的 Memory，带类型化结果卡片 | `05-memory-marketplace-files.md` §Search |
| 已规定，未构建（Specified, not built） | P1 门槛：Memory 可以影响后续请求，且用户可以检查它 | `06-delivery-plan.md` P1 |

## 用户入口

- 左侧导航中的 `Memory` 条目，用于打开 Memory 页面（计划中）。
- 代理在对话期间通过内存工具发起的写入（已实现：`tools/memory_tool.py` `memory_tool`）。
- 隐式读取路径：每次代理轮次都可以在其系统提示中接收内存块（已实现：`format_for_system_prompt`, `build_memory_context_block`）。
- 类型为 `memory` 的搜索结果（计划中）。
- 任务恢复：重新打开任务应将"相关内存"带回上下文（`05-memory-marketplace-files.md` §Tasks；计划中）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 从代理轮次添加 / 替换 / 删除内存条目 | 已实现（Implemented，`MemoryStore.add/replace/remove`） |
| 按目标设置显式限制的字符预算存储 | 已实现（Implemented，`MemoryStore._char_limit`） |
| 并发写入安全和外部编辑漂移检测 | 已实现（Implemented，`_file_lock`, `_drift_error`） |
| 提示使用前对内存内容做注入清理 | 已实现（Implemented，`sanitize_context`, `_scan_memory_content`） |
| 外部内存后端（Mem0、Supermemory 等）带故障断路器 | 已实现（Implemented，`plugins/memory/*`; circuit breaker in `plugins/memory/mem0/__init__.py`） |
| Memory 页面：列出当前工作区/项目的条目 | 已规划（Planned） |
| 查看条目：内容、类别、来源会话、创建时间 | 已规划（Planned） |
| 从 UI 删除 / 撤销 | 已规划（Planned；当前 delete 只存在于代理工具 `remove`） |
| 用户手写与代理推断的标记区分 | 已规划（Planned） |
| 类别分类法：偏好、品牌规则、项目事实等 | 已规划（Planned；当前存储只有 target，没有 category） |
| 按用户/工作区/项目划分作用域 | 已规划（Planned） |
| 绝不存储提供者密钥 | 已规划为验证规则（Planned；当前已有清理，但未规定写入时按密钥模式拒绝） |
| 统一搜索中的内存条目 | 已规划（Planned） |

## 状态机

当前 Memory 条目还不是完整状态对象；已存条目只有"存在 / 不存在"（`MemoryStore` 按 target 保存字符串列表）。计划中的产品模型需要补充生命周期：

```text
proposed (inferred by agent)
  -> active            (auto, or user confirms)
active
  -> revoked           (user delete/revoke from Memory page)
active
  -> superseded        (replace writes a new active entry)
```

- `proposed -> active` 的策略仍是开放问题：自动生效，还是用户确认后生效。
- `revoked` 条目必须立刻停止影响提示词，并保留操作记录：谁撤销、何时撤销。
- `superseded` 保留历史，方便追查"代理为什么曾经这样判断"。

在该模型出现前，当前已实现行为是：`add` 后立即生效，`remove` 后硬删除。

## API 与事件

已实现（代理工具表面，不是 HTTP API）：

- `memory_tool(action=add|replace|remove, target, content, …)` — `tools/memory_tool.py`（`memory_tool`，分发到 `MemoryStore`）。
- 外部后端的 `MemoryProvider` ABC — `agent/memory_provider.py`；提供者通过环境变量或 `$HERMES_HOME` 配置，例如 `MEM0_API_KEY`，详见 `plugins/memory/mem0/__init__.py` 文档字符串。

已规划（Memory 页面使用的 HTTP API；当前无代码）：

```http
GET    /api/memory?scope=workspace|project|user&category=&q=&cursor=
GET    /api/memory/{entry_id}
DELETE /api/memory/{entry_id}        # revoke
```

已规划事件，命名遵循 `02-agent-runtime-contract.md`：

- `memory.entry.created`（携带 `source: user | inferred` 和 session id）
- `memory.entry.revoked`

## 数据模型

已实现：memory 目录下按 target 文件保存的纯文本条目列表（`tools/memory_tool.py` `get_memory_dir`, `_path_for`），带字符预算和用于提示词注入的渲染块格式（`_render_block`）。

产品界面计划实体：

```text
memory_entries
- id
- scope: user | workspace | project
- scope_id
- category: preference | brand_rule | project_fact | prompt_decision
            | model_preference | rejected_style | policy_note
- content
- source: user_authored | inferred
- source_session_id
- created_by, created_at
- status: active | revoked | superseded
```

迁移说明：现有文件存储条目可映射为 `scope=user, source=inferred, category=null`，迁移时不得静默丢弃。

## UI 行为

- Memory 页面按类别分组展示条目，并限定在当前 workspace/project；同时提供作用域切换器。
- 每行展示：内容、类别 chip、来源 badge（user/inferred）、来源会话链接、创建日期、撤销按钮。
- 撤销前需要确认，并在下一个代理回合生效。
- 空态显示"暂无记忆"，不得预置假示例。
- 记忆搜索结果卡必须看起来像 memory，不像 file
  (`05-memory-marketplace-files.md` §Search).
- 页面永远不渲染提供者密钥；如果清理逻辑标记某条内容有风险，页面显示警告状态，而不是原文。

## 权限与错误处理

权限（`05-memory-marketplace-files.md` §Access Control）：memory 按 user/workspace/project 划分作用域；最小动词为 read、update、delete、revoke。共享对话不得泄漏其他用户的 memory 作用域。

错误合约：

| 错误 | 触发条件 | 当前状态 |
|---|---|---|
| `memory_drift_detected` | 加载后、写入前，存储文件被 Hermes 之外的过程修改 | 已实现为结构化错误（`tools/memory_tool.py` `_drift_error`） |
| `memory_budget_exceeded` | 新增内容超过目标字符限制 | 已实现（限制在 `MemoryStore` 中；add 返回失败响应） |
| `memory_entry_not_found` | 撤销/检查未知条目 id | 随 HTTP API 规划中 |
| `memory_scope_denied` | 跨工作区读取或撤销 | 已规划 |
| Provider outage（提供者不可用） | 外部提供者（例如 Mem0）无法访问 | 已实现断路器暂停（`plugins/memory/mem0/__init__.py`）；UI 展示仍在规划中 |

提供者失败必须可见降级（Memory 标记为不可用），不能静默生成一个忽略已知事实的回合。

## 验收标准

- 左侧导航暴露 Memory；页面列出存储中的真实条目（没有条目时显示空态，不展示样例数据）。
- 用户可以撤销条目，后续代理回合可证明不再使用该条目（通过 prompt block diff 验证）。
- 新的推断条目展示来源会话归属。
- 用户手写条目和代理推断条目在视觉上可区分。
- 包含提供者密钥模式的 Memory 写入被拒绝并显示错误（永不入库）。
- 满足 `06-delivery-plan.md` 的 P1 门槛：Memory 能影响后续请求，并且可以被检查。

## 非目标

- 不把基于 embedding 的语义 memory 检索列为 P0/P1 要求；外部提供者可以支持，但产品界面只要求列表、检查、撤销。
- 不把 Memory 当作聊天记录归档；转录和历史记录归 Tasks 所有。
- 不做跨租户或跨工作区的 Memory 共享。
- 不存储任何提供者凭证或 token。
- 不在缺少推断来源轨迹的情况下，自动从每个回合抽取 Memory。

## 开放问题

1. 推断条目是自动激活，还是必须用户确认后激活（`proposed -> active` 策略）？
2. 外部提供者（Mem0 等）和本地文件存储不一致时，哪个是权威来源？
3. 项目级作用域如何建 key：使用会话状态里的 project id（`02-agent-runtime-contract.md` §Session Lifecycle），还是按目录推导？
4. 类别分类法如何强制：自由标签，还是 `05-memory-marketplace-files.md` 中固定七类？
5. 保留策略：已撤销/已取代条目是否过期？是否需要导出？
6. Memory 页面是否允许原地编辑（`update` 动词），还是只能撤销后重建？这要结合文件存储的漂移检测来决定。
