---
title: "Canonical Classic CLI Turn"
status: draft
source_commit: dd0827710
verified_at: 2026-08-11
confidence: high
---

# Classic CLI 最小回合

## 研究范围

本文逐 symbol 追踪一次最小的 Classic CLI 回合：用户从交互输入框提交纯文本，已有或新建的 `AIAgent` 对 Provider 发出一次请求，Provider 返回非空文本且没有 tool call，CLI 展示最终响应。

为了让主链清晰，先排除 image/voice、`@file` 展开、MoA、压缩、重试、fallback、steer、verification stop、插件输出变换和外部 memory failure。它们没有消失，而是在主链的明确扩展点分叉。下一份链路文档会在同一骨架上增加一次 tool call。

## 结论先行

Classic CLI 的一次回合跨越三个线程层次，但 Agent loop 本身仍是同步代码：

1. prompt-toolkit UI 接收输入并放入 `_pending_input`；
2. `process_loop` 后台线程取出输入并调用 `HermesCLI.chat()`；
3. `chat()` 再启动 agent worker thread，同步运行 `AIAgent.run_conversation()`，自身监测 interrupt queue。

这个路径维护四个不同但相关的状态引用：

| 状态 | Owner | 作用 |
|---|---|---|
| `HermesCLI.conversation_history` | CLI | 已完成回合的 caller history；提交时还短暂包含一个 staged user dict |
| `AIAgent._pending_cli_user_message` | Agent/CLI 握手 | 在 worker prologue 前让 close/signal 路径也能保存刚提交的输入 |
| `AIAgent._session_messages` | Agent | 当前 canonical live transcript |
| SessionDB message rows | `SessionDB` | 可恢复、可搜索的 durable transcript |

它们不是四份永久复制。CLI 有意复用同一个 staged user dict；回合结束后又用 `result["messages"]` 接管 Agent 返回的 canonical list。

## 端到端时序

```mermaid
sequenceDiagram
    actor U as User
    participant UI as prompt_toolkit UI
    participant PL as CLI process_loop thread
    participant C as HermesCLI.chat
    participant AW as Agent worker thread
    participant A as AIAgent façade
    participant TC as build_turn_context
    participant DB as SessionDB
    participant L as conversation_loop
    participant MW as LLM middleware
    participant T as Provider transport
    participant F as finalize_turn

    U->>UI: submit text
    UI->>PL: _pending_input.put(text)
    PL->>C: chat(text)
    C->>C: resolve route / lazy _init_agent
    C->>A: stage exact user dict
    Note over C,A: conversation_history append + _pending_cli_user_message
    C->>AW: start run_agent daemon thread
    AW->>A: run_conversation(user_message, history[:-1])
    A->>L: delegate canonical turn
    L->>TC: build_turn_context(...)
    TC->>A: restore/build cached system prompt
    TC->>TC: preflight + memory/plugin context
    TC->>A: reuse staged dict as current user row
    TC->>DB: crash-resilience persist attempt
    Note over TC,DB: failure is logged; later flush may retry
    TC-->>L: TurnContext
    L->>L: build API projection + cache plan + kwargs
    L->>MW: request/execution middleware
    MW->>T: interruptible streaming/non-streaming call
    T-->>MW: provider response
    MW-->>L: raw response
    L->>T: normalize_response
    T-->>L: normalized assistant text, no tool_calls
    L->>L: append canonical assistant row
    L->>F: finalize_turn(...)
    F->>F: repair/strip scaffolding + clean user override
    F->>DB: final persist
    F->>F: optional delivery footer / output transform
    F-->>A: result(messages, final_response, metadata)
    A-->>AW: result
    AW-->>C: result
    C->>C: conversation_history = result.messages
    C-->>PL: display/return result.final_response
    PL-->>UI: prompt becomes idle
```

## 调用链与状态突变

### 1. UI 提交到 `HermesCLI.chat`

`HermesCLI.run()` 创建 prompt-toolkit application 和 `process_loop()` 后台线程。idle 状态提交的内容进入 `_pending_input`；`process_loop()` 负责：

- 剥离终端控制残留、处理 file drop/paste；
- 在模型回合前拦截 slash command 和 `!command`；
- 将普通输入标为 active turn；
- 调用 `self.chat(user_input, ...)`；
- 在 `finally` 中清理 spinner/tool UI state，再让 prompt 回到 idle。

因此 slash command 并不天然进入 Agent history；只有 command handler 明确生成 `_pending_agent_seed` 时才落回普通 chat 路径。

### 2. Lazy Agent 与恢复 history

`HermesCLI.chat()` 先解析本轮 provider/model route。route signature 改变会清空 `self.agent`，随后由 `CLIAgentSetupMixin._init_agent()` 懒构造新 Agent。

resume 场景中，`_init_agent()` 从 `SessionDB.get_messages_as_conversation(..., repair_alternation=True)` 读取 canonical rows，过滤 `session_meta`，写入 `self.conversation_history`，再把同一个 `SessionDB`、`session_id`、platform、toolsets 和 callbacks 注入 `AIAgent`。

这说明 Classic CLI 的 history 恢复 source of truth 是 SessionDB，而不是终端 scrollback 或单独 JSON routing 文件。

### 3. 提交时 staging：为 terminal-close 窗口补洞

在启动 worker 前，`chat()` 创建：

```python
staged_user_message = {"role": "user", "content": message}
agent._pending_cli_user_message = staged_user_message
self.conversation_history.append(staged_user_message)
```

若 CLI history 此时与 `agent._session_messages` 是同一 list，先浅拷贝 list，避免 UI staging 直接污染 Agent 上一轮的 canonical snapshot。这个写入由 `_session_persist_lock` 包围，以便 `_persist_active_session_before_close()` 与 turn prologue 不会并发制造重复 user row。

worker 调用 Agent 时传入 `conversation_history[:-1]`，即排除刚 staged 的尾项；`build_turn_context()` 自己负责把当前 user message 放到本轮 `messages`。staged dict 则通过 `_pending_cli_user_message` 被识别并复用，保持 close-path marker 与正常 turn path 指向同一对象。

### 4. API-local 输入与 clean durable 输入

voice 指令、model switch note、skill reload note 等只应让模型看见，不应改写用户原话。CLI 因此把 decorated `agent_message` 作为 `user_message`，并在存在 decoration 时把原始 `message` 作为 `persist_user_message`。

`build_turn_context()` 先让当前 live user row 保持 API 所需内容，保存 clean override/index；完成 memory/provider/plugin 动态上下文合成后，再把确切 wire 文本写入 `api_content` sidecar。最终 `finalize_turn()` 将 live `content` 恢复为 clean user text后落库，而历史 replay 可继续从 `api_content` 重建当时的 Provider 输入。

### 5. Turn prologue

`AIAgent.run_conversation()` 是 façade，真正委派到 `agent.conversation_loop.run_conversation()`。后者首先调用 `build_turn_context()`，其关键顺序是：

```text
采用/复制 prior history，确定 current user row
→ 恢复或构建 _cached_system_prompt
→ 确保 DB session metadata 使用该 prompt snapshot
→ idle/preflight compression（如需要）
→ memory manager turn-start/prefetch + pre_llm_call context
→ 生成当前 user row 的 api_content
→ 尝试 crash-resilience persistence
→ 返回 TurnContext 给主循环
```

turn-start persist 是恢复增强，不是硬提交门：异常会被记录，后续 incremental/final flush 仍可重试。它发生在第一个 Provider request 前，所以即使模型调用期间 CLI 被关闭，user row 通常已经可恢复。

### 6. 每次 API 请求的 projection

主循环不直接把 canonical `messages` 交给 SDK。每轮 iteration 都从 live list 创建 outgoing copy，并执行：

- 移除 `api_content`、display metadata、row id 等内部字段；
- 当前/历史消息有 `api_content` 时将其投影为 wire `content`；
- replay provider 所需 reasoning fields，移除 trajectory-only 字段；
- 添加 cached system prompt 与允许的 ephemeral system text；
- 应用 context-engine selection、message sanitization 和 prompt-cache plan；
- 选择经过过滤的 `tools_for_api`，再由 transport 构造 `api_kwargs`。

请求随后经过两层可扩展边界：request middleware 可以变换 payload；execution middleware 包住实际调用。实际调用优先 interruptible streaming path，即使没有显示 consumer，也利用 stale-stream health checks；不支持 streaming 或特殊 provider 才走 relay 包装的 non-streaming call。

返回对象再经当前 transport 的 `normalize_response()` 转为统一 assistant content、reasoning、tool calls、finish reason 和 usage 形态。

### 7. 无工具文本终止

归一化响应没有 tool calls 时，主循环：

1. 取得 `assistant_message.content`；
2. 运行 empty/truncation/intermediate-ack/verification 等守卫；
3. 对正常文本剥离私有 think blocks；
4. 用 `_build_assistant_message()` 构造 canonical row；
5. `messages.append(final_msg)`；
6. 设置 `_turn_exit_reason = text_response(...)` 并退出循环。

在本文最小路径中，真正 durable write 发生在下一步 finalizer，而不是 append 当下。

### 8. Finalization 与交付投影

`finalize_turn()` 是 canonical turn 的收口点。它先：

- 清理 trajectory 和 task resources；
- 移除仅用于恢复/重试的 synthetic scaffolding；
- 在特殊恢复路径中补齐或填充最终 assistant row；
- 应用 clean user override；
- 可选执行 post-turn micro-compaction；
- 调用 `agent._persist_session(messages, conversation_history)`。

持久化之后，它还可能改变 `final_response`：

- 追加 file-mutation verifier footer；
- 用 turn-completion explainer 替代或补充异常短响应；
- 运行 `transform_llm_output` hook，首个非空字符串获胜；
- 把变换后的文本交给 `post_llm_call` 和 surface caller。

所以准确关系是：**durable transcript 必须包含 canonical core assistant response，但最终展示文本不保证与该 row 逐字相同。** 正常无 hook/无 footer 的最小路径二者相同；变换后的文本属于 delivery projection，结果中的 `response_transformed` 用于帮助 Gateway 避免把已 streaming 的旧文本误当最终文本。

### 9. CLI 接管结果

agent worker 完成后，`chat()` 将：

```python
self.conversation_history = result.get("messages", ...)
response = result.get("final_response")
```

随后根据 streaming/preview flags 决定是否再绘制完整 response。也就是说，CLI 后续回合从 canonical messages 继续，而 UI 展示采用可能经过 finalizer delivery transform 的 `final_response`。

## Persistence checkpoints

| 顺序 | 位置 | 内容 | 失败语义 |
|---:|---|---|---|
| 0 | CLI staging | 仅内存中的 exact user dict | close handler 会尝试将其并入 snapshot |
| 1 | turn prologue | session metadata + current user row，含 API fidelity sidecar | best-effort；记录后可由 later flush 重试 |
| 2 | normal no-tool loop | append final assistant row 到 live list | 尚非独立 durable commit |
| 3 | finalizer | clean canonical user/assistant transcript | final persistence；异常进入 `cleanup_errors` |
| 4 | finalizer persistence 之后 | delivery footer/output transform | 默认不回写 canonical assistant row |

工具路径另有更强的 checkpoint：assistant tool intent 在 side effect 前必须成功 flush；这个 hard gate 将在单工具回合文档验证。

## Prompt-cache 边界

- `_cached_system_prompt` 在 session 中复用；当前轮 memory/plugin context 不通过原地修改它注入。
- `api_content` 让历史回放复用当时 wire bytes，避免 clean transcript sanitation 让 cached prefix 在后续轮漂移。
- 每次请求可以重做 provider-specific cache decoration，但底层 system snapshot 不因此改变。
- route signature 改变可导致 CLI 重建 Agent；这属于明确的运行时切换边界，不能等同于同一 Agent 内静默替换 prompt/toolset。

## 并发与中断边界

- prompt_toolkit UI 不运行 Agent loop。
- `process_loop` 串行消费 idle input；Agent active 时的新输入走独立 interrupt queue。
- `chat()` 的 agent thread 内重新绑定 thread-local sudo/approval/secret callbacks，主线程绑定不会自动跨线程传播。
- `_session_persist_lock` 保护 staged input、Agent session messages 与 close snapshot 之间的交接。
- `AIAgent.run_conversation()` 内部仍是同步 loop；这里的线程是 surface 为保持输入/中断响应而提供的外壳。

## 源码证据

| 阶段 | 生产代码 |
|---|---|
| 输入消费与 chat dispatch | `cli.py::HermesCLI.run`, nested `process_loop` |
| CLI staging/worker/result adoption | `cli.py::HermesCLI.chat` |
| Lazy Agent/resume | `hermes_cli/cli_agent_setup_mixin.py::CLIAgentSetupMixin._init_agent` |
| close persistence | `cli.py::HermesCLI._persist_active_session_before_close` |
| façade | `run_agent.py::AIAgent.run_conversation` |
| turn prologue | `agent/turn_context.py::build_turn_context` |
| projection/request/normalization/no-tool branch | `agent/conversation_loop.py::run_conversation` |
| final persistence/delivery transform | `agent/turn_finalizer.py::finalize_turn` |

## 行为测试证据

- `tests/agent/test_turn_context.py`：staged CLI dict 复用、clean override 和 prompt-before-session 顺序。
- `tests/agent/test_api_content_sidecar.py`：clean transcript 与 wire `api_content` 的分离、final override。
- `tests/agent/test_turn_finalizer_final_response_persistence.py`：特殊 tool-tail 恢复时 canonical final assistant row 必须持久化。
- `tests/test_transform_llm_output_hook.py`：输出变换 hook 的注册、参数传递和错误隔离；first-non-empty wiring 仍以 finalizer 生产代码为证据。
- `tests/gateway/test_run_progress_topics.py`：streaming 后 output transform 的重新投递语义。
- `tests/test_lazy_session_regressions.py`：CLI lazy Agent/session history 相关回归路径。

## 尚未覆盖

- 一次成功 tool call 的 intent/result flush hard gate；
- parallel tool batch 中部分失败与配对闭合；
- Provider retry/fallback 后 projection 重建；
- compression 导致的 session lineage rotation；
- Gateway/Desktop 在 delivery ledger 与 streaming 上的差异。

这些内容分别进入 M2 的下一单元和 M3/M4 深入研究。
