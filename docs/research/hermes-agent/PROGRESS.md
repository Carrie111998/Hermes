---
title: "Hermes Architecture Study Progress"
status: active
source_commit: dd0827710
updated_at: 2026-08-11
---

# 研究进度

## 当前里程碑

**M1 — 系统全景架构**

当前目标：逐边验证系统上下文图，并建立真实进程、传输和部署模型。

## 里程碑状态

| 里程碑 | 状态 | 置信度 | 说明 |
|---|---|---:|---|
| M0 研究基线与工作协议 | completed | high | 分支、恢复协议、计划、基线、模板和索引已建立 |
| M1 系统全景架构 | in progress | high | 已逐入口验证进程/传输模型；模块依赖图和顶层数据流图待完成 |
| M2 Canonical Turn | pending | — | — |
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

- 完成一级模块依赖图，区分 product surfaces、runtime services、Agent narrow waist 和 extension edges。
- 完成顶层数据流图，标出 live object、canonical SessionDB、Memory、Skills、配置和外部后端。

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
- 系统架构研究必须把 prompt-cache stability、role alternation 和 narrow-waist tool surface 作为跨模块约束。

## 待回答问题

1. Agent Core、Provider、Tool Runtime、Persistence 和 Extension 边缘的一级依赖方向是什么？
2. Prompt、message、tool result、memory 与 session metadata 的顶层数据流如何区分？
3. 哪些插件类别由通用 `PluginManager` 加载，哪些使用专用 loader？
4. Gateway routing state 与 canonical SessionDB 的准确写入/恢复边界是什么？

## 下一步

创建 `architecture/module-map.md` 和 `architecture/data-flow.md`，完成 M1 后进入 M2 Canonical Turn。
