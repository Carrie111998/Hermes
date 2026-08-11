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
- Working document：[flows/canonical-cli-turn.md](./flows/canonical-cli-turn.md)

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
- 逐 symbol 完成 Classic CLI 最小无工具回合：输入队列、lazy Agent、staged user dict、turn prologue、API projection、transport normalization、assistant append、final persistence 和 UI result adoption。
- 确认 Classic CLI 的 Agent loop 运行在独立 worker thread，thread-local approval/secret callbacks 在该线程重新绑定。
- 修正 final-response 不变量：canonical assistant row 先落库；file verifier footer、异常解释和 `transform_llm_output` 可以随后改变 delivery projection。
- 创建 `flows/canonical-cli-turn.md`，记录线程、state ownership、prompt-cache 和 persistence checkpoint。

## 尚未完成

- 尚未创建单工具调用时序和 CLI/Gateway/Desktop 构造参数对照。
- 尚未验证 parallel tool batch、interrupt closure 与 result persistence failure 的全部分支。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md` 和本文件。
2. 读取 `flows/canonical-cli-turn.md` 和 `architecture/data-flow.md`。
3. 从 `agent/conversation_loop.py` 的 `if assistant_message.tool_calls` 分支定位 assistant tool-call row 的 append 与 `_flush_messages_to_session_db` hard gate。
4. 沿 `agent/tool_executor.py`、`model_tools.py::handle_function_call` 和 `tools/registry.py` 追踪一个成功的单工具调用。
5. 记录 sequential/parallel 选择、approval/middleware/guard 顺序，以及异常/interrupt 如何补齐 tool result。
6. 创建单工具时序文档，更新 INV-004/005 的测试证据并提交独立研究单元。

## 工作区提示

当前分支应包含 `ea3bfe794`、`ab0e14d73` 和 `2c39face3`；继续前运行 `git status --short --branch`。
