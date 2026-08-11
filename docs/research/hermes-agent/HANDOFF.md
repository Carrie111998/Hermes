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
- Working document：[flows/canonical-tool-turn.md](./flows/canonical-tool-turn.md)

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
- 完成 assistant tool-call branch、executor、middleware/approval、registry dispatch 和 tool result 回写的逐 symbol 追踪。
- 验证 intent 和 result 两道 hard persistence gate，以及失败时对 UI、后续工具和下一次 Provider request 的阻断。
- 验证 sequential/concurrent/segmented batch 的选择、安全 barrier、结果 emission order 和 interrupt closure。
- 创建 `flows/canonical-tool-turn.md`，将 SessionDB 定位为 tool side effect 前后的 recovery-oriented write-ahead record。
- 将 INV-004、INV-005 升级为 code/tests verified，并新增 result persistence 的 INV-015。
- 通过 `858bedea02` 和 `0fd0db1a8` 核对 result-first persistence 与 budget 后 steer 的历史意图。
- 记录 `OPEN-M2-001`：budget/steer 在 flush 后原地改写已标记 tool row，热运行与 cold resume 可能分叉；尚未运行 real DB 复现。

## 尚未完成

- 尚未创建 CLI/Gateway/Desktop 构造参数与交付语义对照。
- `OPEN-M2-001` 尚未通过可运行的 real SessionDB cold-resume test 复现。
- 当前工作区未发现项目 `.venv`/`venv` pytest executable，本单元只阅读了行为测试源码。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md` 和本文件。
2. 读取 `flows/canonical-cli-turn.md` 和 `architecture/data-flow.md`。
3. 从 Classic CLI 的 `_init_agent`/`chat`、Gateway turn executor/agent cache、`tui_gateway.server::_make_agent` 采集 Agent 构造与复用参数。
4. 对照 session id/history owner、platform metadata、toolsets、cwd/profile、stream/progress/approval callbacks。
5. 沿 Gateway delivery 与 Desktop JSON-RPC response adoption 追踪 `response_previewed` / `response_transformed`。
6. 创建 `flows/entry-surface-comparison.md` 并提交独立研究单元。
7. 若环境可运行 pytest，再为 `OPEN-M2-001` 建立 real SessionDB 最小复现；否则保留为明确开放问题。

## 工作区提示

当前分支应包含 `ea3bfe794`、`ab0e14d73`、`2c39face3` 和 `4dabf7827`；继续前运行 `git status --short --branch`。
