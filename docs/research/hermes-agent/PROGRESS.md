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
| M1 系统全景架构 | in progress | medium | 已有第一版系统上下文图，尚未逐边验证 |
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

- 建立跨会话文档协议。
- 固定源码研究基线。
- 建立第一版系统上下文图。
- 为 M1 准备进程和部署信息清单。

## 已确认事实

- 当前研究基线为 commit `dd0827710`。
- 研究分支为 `docs/hermes-architecture-deep-dive`。
- Hermes 使用共享 Agent 核心服务多个入口，但 TUI、Dashboard 和 Desktop 的呈现/进程边界不同。
- 系统架构研究必须把 prompt-cache stability、role alternation 和 narrow-waist tool surface 作为跨模块约束。

## 待回答问题

1. 所有产品入口创建或复用 `AIAgent` 的准确入口分别是什么？
2. TUI、Dashboard 和 Desktop 的进程/传输边界应如何画在同一张部署图中？
3. Gateway、Cron、ACP 和 API Server 对 SessionDB 的 ownership 有何差异？
4. 哪些插件类别由通用 `PluginManager` 加载，哪些使用专用 loader？

## 下一步

完成 [architecture/system-context.md](./architecture/system-context.md) 的源码逐边验证，然后创建 `architecture/process-model.md`。
