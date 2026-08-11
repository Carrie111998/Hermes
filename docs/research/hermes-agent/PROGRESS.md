---
title: "Hermes Architecture Study Progress"
status: active
source_commit: dd0827710
updated_at: 2026-08-11
---

# 研究进度

## 当前里程碑

**M2 — Canonical Turn**

当前目标：沿 Classic CLI 追踪一次最小真实回合，定位每个状态突变、持久化点和错误边界。

## 里程碑状态

| 里程碑 | 状态 | 置信度 | 说明 |
|---|---|---:|---|
| M0 研究基线与工作协议 | completed | high | 分支、恢复协议、计划、基线、模板和索引已建立 |
| M1 系统全景架构 | completed | high | 系统上下文、进程模型、一级模块图与顶层数据流均已有源码证据 |
| M2 Canonical Turn | in progress | high | 已完成 Classic CLI 无工具回合与 canonical 工具回合；下一单元比较 CLI/Gateway/Desktop 构造与交付 |
| M3 Agent Loop | pending | — | — |
| M4 Prompt/Context/Provider | pending | — | — |
| M5 Tool Runtime | pending | — | — |
| M6 Memory/Session | pending | — | — |
| M7 Skills/Self-improvement | pending | — | — |
| M8 Execution/Delegation/Cron | pending | — | — |
| M9 Gateway/UI/Plugins | pending | — | — |
| M10 Security/Observability | pending | — | — |
| M11 E2E 综合验证 | pending | — | — |

状态枚举：`pending`、`in progress`、`blocked`、`needs revalidation`、`completed`。

## 当前研究单元

- 比较 Classic CLI、Gateway 和 Desktop/TUI gateway 如何构造或复用 `AIAgent`。
- 建立入口参数、session ownership、callback、approval 和 final delivery 对照。
- 验证 `response_previewed` / `response_transformed` 如何防止流式/最终响应重复投递。

## 已确认事实

- 当前研究基线为 commit `dd0827710`。
- 研究分支为 `docs/hermes-architecture-deep-dive`。
- Hermes 使用共享 Agent 核心服务多个入口，但 TUI、Dashboard 和 Desktop 的呈现/进程边界不同。
- “共享 Agent Core”表示共享代码内核，不是所有入口调用一个中央 Agent 服务。
- Classic CLI 与 Agent 同进程；standalone TUI 默认是 Node/Ink + Python stdio gateway child。
- Dashboard 主聊天通过 PTY 复用 Ink TUI；当前 profile 的 TUI attach 到 FastAPI 进程内 `/api/ws`，显式 profile chat 则启动独立 gateway child。
- Desktop 是独立 Electron/React surface，默认启动 headless `hermes serve`，不嵌入 TUI 或 Dashboard SPA。
- API Server 是 Gateway 进程内的平台 adapter；Cron 的内建 ticker 寄宿 Gateway 或 Desktop backend。
- `dashboard.turn_isolation` 默认关闭，compute-host 是可选隔离层而非固定路径。
- `AIAgent` 是共享 façade/live-state owner；初始化、turn prologue、主循环和 finalization 已拆到四个职责模块。
- Agent Core 的一级协作面是 Prompt/Context、Provider、Tool Runtime 与 Persistence；插件、Skills、MCP 和执行后端主要位于边缘。
- 同一回合存在展示视图、API 视图和持久化视图；`api_content` 保存模型实际看到的当前 user content，同时保持 clean transcript。
- Tool 调用遵循“先持久化 assistant intent，再执行 handler，再持久化匹配 result”的恢复顺序。
- Classic CLI 通过 UI → `process_loop` → agent worker 三层线程外壳运行同步 Agent loop；thread-local approval callbacks 在 worker 内重新绑定。
- CLI 在 worker 启动前暂存 exact user dict，并用 `conversation_history[:-1]` 调 Agent；turn prologue 通过 `_pending_cli_user_message` 复用同一 dict。
- turn-start user persistence 是首个 Provider request 前的 crash-resilience attempt，失败会记录并由后续 flush 重试，不是立即中止的 hard gate。
- 主循环每次 iteration 从 canonical messages 重建 provider projection，再经过 request/execution middleware 和 transport normalization。
- 正常无工具文本先 append canonical assistant row，再由 `finalize_turn` 清理并持久化。
- finalizer 可以在持久化之后追加安全 footer 或执行 `transform_llm_output`；最终展示文本与 durable assistant row 因此不保证逐字相同。
- assistant tool-call intent 是副作用前 hard gate：明确持久化失败时既不投影 interim assistant，也不执行 handler。
- tool result 是 completion/继续推理前 hard gate：明确持久化失败时停止后续 call/segment、completion UI 和下一次 Provider 请求。
- 多工具 batch 由 ordered segments 规划；safe 连续区段并行，interactive/unsafe/未知工具与冲突文件路径形成 barrier。
- concurrent worker 可以乱序完成，但 tool result 按模型 emission order append 和 flush。
- relay 参数改写先于 plugin/guardrail/checkpoint/dispatch；外层 executor 用 skip flags 保证通用 dispatcher 的 middleware/pre-hook 不重复触发。
- approval 分布在 plugin escalation、loop guardrail、ACP edit gate 和 handler-native policy，`tool.started` 不代表所有后续审批已通过。
- `make_tool_result_message()` 同时承担 Provider/DB 字段、untrusted output framing 和 effect disposition。
- `OPEN-M2-001`：aggregate budget 与 `/steer` 在 per-result flush 后原地改写已标记 tool row，可能造成热运行 Provider context 与 cold-resume SessionDB 内容差异；已核对代码和提交意图，尚待 real DB 复现。
- 系统架构研究必须把 prompt-cache stability、role alternation 和 narrow-waist tool surface 作为跨模块约束。

## 待回答问题

1. Gateway 与 Desktop 如何利用 `response_previewed`/`response_transformed` 避免重复投递？
2. 三个入口在 Agent cache/reuse、SessionDB ownership、approval callback 和 profile/cwd 绑定上有什么差异？
3. route/model 切换后，CLI history 与 Agent prompt/tool snapshot 如何重新建立？
4. `OPEN-M2-001` 能否由 real SessionDB cold-resume test 稳定复现？

## 下一步

创建 M2 的 CLI/Gateway/Desktop 入口对照；随后运行或补充 `OPEN-M2-001` 的最小冷恢复契约。
