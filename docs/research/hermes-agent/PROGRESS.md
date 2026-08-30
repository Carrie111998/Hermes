---
title: "Hermes Architecture Study Progress"
status: active
source_commit: 26350357d7
previous_commit: dd0827710
updated_at: 2026-08-30
---

# 研究进度

## 当前里程碑

**M3 — Agent Loop 与回合可靠性**（前置：基线重验）

2026-08-30 已将研究基线从 `dd0827710` 推进到 `26350357d7`（上游 5,922 个 commit）。M1/M2 结论仍锚定旧 commit，全部转为 `needs revalidation`。

当前目标：先按新基线复核 M1/M2 的入口与回合结论，再把 canonical text/tool turn 提升为完整状态机，系统覆盖 retry、fallback、empty recovery、budget、interrupt 和 crash matrix。

## 里程碑状态

| 里程碑 | 状态 | 置信度 | 说明 |
|---|---|---:|---|
| M0 研究基线与工作协议 | completed | high | 分支、恢复协议、计划、基线、模板和索引已建立 |
| M1 系统全景架构 | needs revalidation | — | 结论在 `dd0827710` 成立；新基线下 `agent/` `hermes_cli/` `gateway/` `apps/desktop/` 均大幅变更，待复核 |
| M2 Canonical Turn | needs revalidation | — | 结论在 `dd0827710` 成立；`cli.py` +4,346 行、`tools/` 637 commits，符号与行号引用待复核 |
| M3 Agent Loop | blocked | — | 等待 M1/M2 按 `26350357d7` 复核；`agent/conversation_loop.py` 自旧基线起被 90 个 commit 修改 |
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

**先做（基线重验）：**

- 按 `26350357d7` 复核 M1 的进程/入口边界：`hermes_cli/`（1,118 commits）与 `apps/desktop/`（1,708 commits）是重灾区。
- 按 `26350357d7` 复核 M2 的双持久化门与 batch segmentation：`cli.py`、`tools/`、`agent/` 均已实质改写。
- 逐条复核 `INVARIANTS.md`，区分仍成立、已变更和已失效。

**再做（M3 本体）：**

- 枚举 `conversation_loop` 的状态、transition 和所有 terminal `turn_exit_reason`。
- 将 retry/fallback、tool continuation、compression、interrupt 和 grace-call 画成状态机。
- 建立成功/失败/partial/interrupted/completed 返回字段的真值矩阵。

## 已确认事实

> ⚠️ 除前两条外，以下全部结论是在旧基线 `dd0827710` 上逐符号验证的，**尚未按 `26350357d7` 复核**。引用其中的符号名、调用顺序或行号前，先在新代码中确认。

- 当前研究基线为 commit `26350357d7`（2026-08-30 同步自上游 `main`）。
- 上一基线为 `dd0827710`；以下第 3 条起的结论均在该 commit 上验证。
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
- “共享 Agent Core”是共享 library kernel，不是单一中央 Agent 服务；CLI、Gateway、`tui_gateway` 在各自 Python 进程内持有实例。
- CLI 以 active route signature 复用一个当前 Agent；Gateway 以 session key/config signature/message-count guard 管理 LRU cache；TUI/Desktop backend 以 runtime sid session table 持有 per-session Agent。
- Gateway 每轮从 durable transcript 重建安全 replay，同时在同一 cached Agent live history 更长时保留它，兼顾跨进程 coherence 与同进程写失败下的连续性。
- Cached Agent 的 callback、ContextVar、reasoning/service-tier/request override 必须每轮重绑，不能成为跨会话静态状态。
- `response_previewed` 表示 final candidate 已作为 interim 发布，不等于任意 stream/progress 已送达；`response_transformed` 表示旧 stream 不包含最终展示变换。
- Messaging Gateway 只有确认 delivered payload 与 completed final 匹配时才设 `already_sent`；stale 或 transformed final 优先 edit，失败回退普通 send。
- `tui_gateway` 始终发送 authoritative `message.complete`；Ink 与 Desktop 使用不同但明确测试的 interim segmentation policy 合并 client state。
- 系统架构研究必须把 prompt-cache stability、role alternation 和 narrow-waist tool surface 作为跨模块约束。

## 待回答问题

1. `conversation_loop` 的所有 exit reason 如何映射到 completed/failed/partial/interrupted？
2. retry、credential rotation、fallback model 与 one-turn grace call 的优先级是什么？
3. empty response、dropped tool call、verification nudge 与 max-iteration recovery 如何保持角色交替？
4. `OPEN-M2-001` 能否由 real SessionDB cold-resume test 稳定复现？该问题在 `26350357d7` 上是否依然存在？
5. 旧基线的哪些 INVARIANTS 在新基线下已失效？

## 下一步

1. 按 `26350357d7` 复核 M1/M2 结论，逐份文档推进 `source_commit` 并移除 `revalidation_target`。
2. 复核完成后再创建 M3 Agent Loop 状态机和 exit-reason/return-field 矩阵。
3. 将 `OPEN-M2-001` 在新基线上重新取证，并纳入 crash recovery matrix。
