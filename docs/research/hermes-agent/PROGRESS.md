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
| M2 Canonical Turn | in progress | medium | 已有顶层数据流骨架，待逐 symbol 追踪 Classic CLI 回合 |
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

- 从 `hermes_cli.main::cmd_chat`、`cli.main` 和 `HermesCLI` 定位用户输入到 `AIAgent.run_conversation` 的调用链。
- 追踪 `build_turn_context → conversation_loop → finalize_turn` 的最小无工具回合。
- 再增加一次单工具调用，验证 assistant intent、handler side effect 和 tool result 的增量持久化顺序。

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
- 系统架构研究必须把 prompt-cache stability、role alternation 和 narrow-waist tool surface 作为跨模块约束。

## 待回答问题

1. Classic CLI 的输入方法、conversation history owner 和 `run_conversation` 调用点分别在哪里？
2. 一个无工具回合从 user row 到 final assistant row 的准确 append/flush 顺序是什么？
3. 一个工具回合在 parallel batch、interrupt 和 persistence failure 下如何保持 call/result 配对？
4. `final_response`、streamed preview 与 durable assistant row 如何去重并保持一致？

## 下一步

创建 M2 的 `flows/canonical-cli-turn.md`，先完成无工具回合，再叠加单工具回合。
