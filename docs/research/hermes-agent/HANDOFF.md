---
title: "Latest Architecture Study Handoff"
status: active
source_commit: dd0827710
updated_at: 2026-08-11
---

# 最新会话交接

## 当前状态

- Branch：`docs/hermes-architecture-deep-dive`
- Baseline：`dd0827710`
- Milestone：M2 — Canonical Turn
- Working document：[architecture/data-flow.md](./architecture/data-flow.md)

## 本次已完成

- 创建专用研究分支。
- 建立研究目录和跨会话恢复协议。
- 将完整研究路线固化到 `PLAN.md`。
- 建立进度、基线、源码索引和模块模板。
- 建立第一版系统上下文图。
- 提交研究基础设施：`ea3bfe794 docs(study): initialize Hermes architecture deep dive`。
- 逐入口核对 Classic CLI、TUI、Dashboard、Desktop、Gateway/API、ACP、Batch 和 Cron 的 Agent 构造与进程边界。
- 创建进程/部署图并记录 live state、SessionDB、profile 和可选 compute-host 的 ownership。
- 创建一级模块依赖图，明确 `AIAgent` façade、四个核心协作面和扩展边缘。
- 创建顶层数据流图，区分展示/API/持久化视图以及 tool-call 小事务。
- 完成 M1 系统全景架构，进入 M2 Canonical Turn。

## 尚未完成

- 尚未逐 symbol 记录 Classic CLI 用户输入到 `AIAgent.run_conversation` 的调用链。
- 尚未验证最小无工具回合的准确 DB append/flush 顺序。
- 尚未创建单工具调用时序和 CLI/Gateway/Desktop 构造参数对照。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md` 和本文件。
2. 读取 `architecture/data-flow.md`；`system-context.md`、`process-model.md` 和 `module-map.md` 仅在遇到边界疑问时查阅。
3. 从 `cli.py::HermesCLI.run`/输入处理位置开始，定位 `self.agent.run_conversation` 的实际调用点和 history ownership。
4. 进入 `agent/turn_context.py::build_turn_context`，记录 session row、user row、prompt snapshot 和 `api_content` 的写入顺序。
5. 沿 `agent/conversation_loop.py` 的无工具路径走到 `agent/turn_finalizer.py::finalize_turn`。
6. 创建 `flows/canonical-cli-turn.md`，补 source/test evidence 后再研究单工具回合。

## 工作区提示

当前分支应包含 `ea3bfe794` 和 `ab0e14d73`；继续前运行 `git status --short --branch` 并确认 M1 完成单元也已提交。
