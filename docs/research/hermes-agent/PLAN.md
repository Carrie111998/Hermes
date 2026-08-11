---
title: "Hermes Agent 完整架构研究计划"
status: active
source_commit: dd0827710
verified_at: 2026-08-11
confidence: high
---

# Hermes Agent 完整架构研究计划

## 1. 目标与完成标准

本研究要回答三个层次的问题：

1. **系统如何工作**：从任一入口进入的请求，如何经过 Agent、Provider、Tools、Persistence 和后台任务完成一次生命周期。
2. **为什么这样设计**：提示词缓存、严格角色交替、窄腰工具面、持久化顺序、隔离后台分叉等约束如何塑造实现。
3. **如何安全扩展和演进**：Provider、Plugin、Memory、Context Engine、Gateway Platform 与 Skill 的扩展边界在哪里。

完成时应具备：

- 一套相互一致的系统、进程、组件、数据和时序图；
- 每个主要模块的职责、状态、接口、不变量、失败模式和测试地图；
- 至少六条经过源码与测试验证的端到端运行链路；
- 一份跨模块安全边界和权限矩阵；
- 一份当前架构风险、复杂度热点与可能演进方向总结。

## 2. 统一研究方法

每个模块统一回答：

1. 它解决什么问题？
2. 它的公开入口和内部入口在哪里？
3. 输入、输出和持久状态是什么？
4. 它依赖谁，谁依赖它？
5. 正常路径如何运行？
6. 错误、重试、中断、恢复和并发路径如何运行？
7. 哪些不变量不可破坏？
8. 哪些扩展点是稳定接口，哪些只是内部实现？

证据优先级：

```text
可运行的 E2E/集成测试
    ↓
生产源码与行为契约测试
    ↓
开发者架构文档
    ↓
README/用户文档
    ↓
Git 历史和 PR 解释设计意图
    ↓
研究推断
```

文档与代码冲突时，记录差异并优先相信当前代码和可运行测试；涉及“为什么”的问题再使用 `git log -p -S` 或 `git blame` 追溯意图。

## 3. 里程碑

### M0 — 研究基线与工作协议

目标：固定研究对象并建立跨会话恢复机制。

任务：

- 记录 branch、commit、版本、工作区状态和源码规模；
- 建立研究目录、文档模板和证据等级；
- 建立源码、官方文档和测试的初始映射；
- 明确 rebase 后的重验规则。

产物：`README.md`、`BASELINE.md`、`SOURCE-MAP.md`、模板和首份 journal。

完成条件：新会话只依赖仓库内容即可恢复任务。

### M1 — 系统全景架构

目标：理解有哪些参与者、进程、存储和外部系统。

任务：

- 识别 CLI、TUI、Desktop、Web、Gateway、API、ACP、Batch 和 Cron 入口；
- 区分 Python Agent、Node TUI、Electron、FastAPI、平台适配器、MCP 和执行后端进程；
- 标出 SQLite、配置、Memory、Skills、Session routing 和插件状态的位置；
- 区分产品界面、Agent 核心、运行时服务和扩展边缘。

产物：

- 系统上下文图；
- 进程/部署图；
- 一级模块依赖图；
- 顶层数据流图。

完成条件：能够解释任一用户入口最终如何汇入共享 Agent 核心。

### M2 — Canonical Turn：一次请求的完整链路

目标：先看懂一条最小真实链路，再进入模块细节。

研究顺序：

```text
CLI input
→ AIAgent initialization
→ turn context
→ system prompt
→ runtime provider
→ provider request
→ tool call
→ tool dispatch/result
→ final response
→ SessionDB persistence
→ post-turn background work
```

随后比较 Gateway 和 Desktop 入口的差异。

产物：

- 纯文本回合时序图；
- 单次工具调用时序图；
- CLI/Gateway/Desktop 入口对照表；
- 从入口到最终持久化的源码导航。

完成条件：链路上的每个状态突变和持久化点都有源码证据。

### M3 — Agent Loop 与回合可靠性

范围：

- `agent_init`、`turn_context`、`conversation_loop`、`turn_finalizer`；
- iteration budget、grace call、checkpoint；
- tool loop、最终摘要、incomplete/empty recovery；
- retry、fallback、credential rotation；
- stop、steer、redirect 和 interrupt；
- crash-resilient incremental persistence；
- 消息角色交替和 tool-call/result 配对。

核心问题：

- 一次用户回合在什么条件下算成功、失败、部分完成或中断？
- 为什么有副作用的工具运行前必须先写 SessionDB？
- 已展示但未持久化的响应如何恢复？
- 不同 API 模式如何归一化成统一内部消息？

产物：Agent Loop 状态机、四类异常时序、恢复矩阵和已验证不变量。

### M4 — Prompt、Context、Cache 与 Provider

范围：

- stable/context/volatile prompt tiers；
- SOUL、AGENTS、skills index、memory snapshot；
- `api_content` sidecar 和 API-only injections；
- prompt cache plan 和 provider cache-control；
- token estimation、preflight/post-response compression；
- ContextEngine ABC 与默认 compressor；
- ProviderProfile、runtime resolution、transport adapters；
- credential pools 和 fallback chain。

核心问题：

- 哪些字节必须在会话中保持不变？
- 哪些动态信息不能进入 system prompt？
- 压缩如何创建 session lineage，而不破坏当前入口持有的 session？
- Provider 特异性在哪一层被吸收？

产物：Prompt 分层图、缓存边界图、压缩状态机、Provider 决策树和 API 模式对照。

### M5 — Tool Runtime 与执行后端

范围：

- registry、auto-discovery、schema/handler binding；
- toolsets、composite resolution、platform presets；
- `check_fn` 服务门控与缓存；
- dispatch、参数修复、错误包装和 hook；
- 文件、Terminal、Process、Browser、Web、MCP 代表工具；
- local、Docker、SSH、Modal、Daytona、Singularity、Vercel 等后端。

核心问题：

- 模型实际收到哪些工具，为什么？
- 新能力应进入 core tool、service-gated tool、plugin、skill 还是 MCP？
- 远程环境如何保持文件、终端和执行语义一致？

产物：工具注册/调用时序、Toolset 解析图、工具安全级别矩阵和环境对照。

### M6 — Memory、Session 与长期召回

范围：

- `MEMORY.md`、`USER.md`、字符上限和 frozen snapshot；
- memory add/replace/remove、漂移检测、审批和扫描；
- MemoryProvider ABC、MemoryManager 和外部 Provider；
- SessionDB schema、WAL、消息/推理/tool state；
- FTS5、trigram/CJK 搜索、bookends 和 lineage；
- gateway routing index 与 canonical database 的边界。

核心问题：

- 当前上下文、精炼记忆、完整会话档案分别解决什么问题？
- Memory 写入为何立刻落盘但不立即进入当前 prompt？
- 外部 Memory Provider 如何按用户、聊天和 profile 隔离？

产物：Memory 分层图、读写时序、SessionDB ER 图、召回路径和 Provider 生命周期图。

### M7 — Skills、自我改进与 Curator

范围：

- skill discovery、progressive disclosure、slash command；
- bundled、user、hub、external、agent-created 来源；
- `skill_manage`、support files、同步和 Hub；
- usage/provenance、write approval、guard；
- Background Review 的隔离 Agent fork；
- Curator 的 active/stale/archived、consolidation、backup/rollback。

核心问题：

- 什么时候应该写 Memory，什么时候应该写 Skill？
- 自动学习何时触发，为什么不是“每个任务后必定写 Skill”？
- 谁能修改哪类 Skill？
- 未解决失败和短期环境错误如何避免固化？
- Skill 数量和重复度如何治理？

产物：Skill 状态机、自我改进闭环、来源/权限矩阵、Background Review 与 Curator 时序图。

### M8 — Programmatic Execution、Delegation、Kanban 与 Cron

范围：

- `execute_code` 的 UDS/file RPC、stub、白名单和资源限制；
- delegation 的 context isolation、tool inheritance、role/depth/concurrency；
- child progress、heartbeat、interrupt 和 result projection；
- Kanban task/worker/dispatcher、claim、retry 和 circuit；
- Cron 的 agent job、skill attachment、delivery 和 durable state。

核心问题：

- 机械工具流水线与独立推理任务如何分工？
- 子 Agent 如何继承能力而不扩大权限？
- 哪些后台工作能够跨 session/process 生存，哪些不能？

产物：Execute Code RPC 图、Delegation 树、Kanban 状态机和 Cron 投递时序。

### M9 — Gateway、UI Surfaces 与 Plugin System

范围：

- MessageEvent、BasePlatformAdapter、authorization/pairing；
- session key、agent cache、message queue、delivery ledger；
- slash commands、hooks、media/markdown delivery；
- TUI JSON-RPC、Dashboard PTY、Desktop `hermes serve`；
- PluginManager discovery、enablement、precedence 和 specialized loaders。

核心问题：

- Gateway 如何把多平台差异压缩到统一事件模型？
- TUI、Dashboard 和 Desktop 哪些部分共享，哪些刻意独立？
- arbitrary plugin code 在哪里获得信任和能力？

产物：Gateway 时序、Platform Adapter 图、多端边界图和 Plugin Discovery 图。

### M10 — Security、Observability 与 Research Runtime

范围：

- command approvals、allowlists、credential/env scrubbing；
- webhook safe toolset、project-plugin trust、MCP trust；
- memory/skill/tool-output prompt injection；
- profile、gateway user/chat 和 remote environment isolation；
- logs、lifecycle hooks、usage/billing、trajectories、batch runner；
- observability plugins 和 relay integration。

产物：Threat Model、信任边界图、权限矩阵、故障恢复矩阵和观测链路。

### M11 — 端到端综合验证与最终手册

至少完整验证六个场景：

1. CLI 文件修改任务；
2. Gateway 消息调用 Browser/Terminal；
3. 长会话触发 Context Compression；
4. 成功复杂任务触发 Background Review；
5. 主 Agent 并行委派多个子 Agent；
6. Cron 加载 Skill 并投递到消息平台。

每个场景记录：参与进程、调用链、状态变化、持久化点、缓存边界、权限边界、失败恢复和相关测试。

最终产物：总架构图 2.0、设计决策、不变量、风险热点、技术债和后续演进建议。

## 4. 模块研究顺序

推荐顺序不是按目录，而是按依赖关系：

```text
全景
→ Canonical Turn
→ Agent Loop
→ Prompt/Provider
→ Tool Runtime
→ Memory/Session
→ Skills/Learning
→ Execution/Delegation/Cron
→ Gateway/UI/Plugins
→ Security/Observability
→ E2E 综合验证
```

Memory 和 Skills 在模型看来也是工具，但它们承载长期学习语义，因此在理解通用 Tool Runtime 后单独研究。

## 5. 研究单元与节奏

| 阶段 | 建议研究单元 |
|---|---:|
| M0–M1 基线与全景 | 2 |
| M2 Canonical Turn | 2 |
| M3 Agent Loop | 4 |
| M4 Prompt/Context/Provider | 3 |
| M5 Tool Runtime | 3 |
| M6 Memory/Session | 3 |
| M7 Skills/Self-improvement | 3 |
| M8 Execution/Delegation/Cron | 3 |
| M9 Gateway/UI/Plugins | 4 |
| M10–M11 安全与综合 | 3 |

总计约 30 个研究单元。每个单元应只解决 1–3 个明确问题，并产生可提交的文档增量。

## 6. 验证策略

- 优先运行目标模块的行为测试，而不是一开始运行完整测试套件。
- 对配置传播、存储、权限和远端边界至少执行一个真实 import/E2E 路径。
- 图中的每条关键边标注对应代码入口。
- 对恢复逻辑同时记录触发条件、状态突变和持久化后果。
- 对设计意图不明确的限制，使用 Git 历史确认其是否刻意存在。
- 每次 rebase 后根据变更路径决定哪些模块需要重新验证。

## 7. 分支与提交策略

- 本分支只提交研究文档、图和研究辅助索引。
- 发现的产品 Bug、测试缺口和重构建议记录在模块文档中，实施时另开分支。
- 提交保持单一主题，例如：
  - `docs(study): establish Hermes system context`
  - `docs(study): trace canonical CLI tool turn`
  - `docs(study): document prompt cache invariants`
- 每个里程碑完成后同步一次 `main`，更新 `BASELINE.md` 并执行受影响模块重验。

